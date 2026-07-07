# nodes/observe.py
"""
Nodo OBSERVE v4.

Rispetto alla v3:
  - Dopo compute_l2_topology(), esegue run_l2_discovery() che:
      1. Raccoglie le ARP table dai router (IP → MAC)
      2. Raccoglie le MAC table dagli switch (MAC → porta)
      3. Fa il merge e persiste nodi Endpoint in Neo4j
    Il risultato è la topologia L2 completa con host localizzati
    sulla porta fisica dello switch, identificati per IP e MAC.
"""

from __future__ import annotations

import re
import yaml
import logging
import asyncio
from pathlib import Path

from core.state import AgentState
from tools.device_snapshot import snapshot_device
from tools.graph_store import AsyncNetworkGraphStore
from tools.l2_discovery import run_l2_discovery
from tools.parser import load_inventory
from tools.dhcp_relay import extract_dhcp_relay_params

logger = logging.getLogger(__name__)


# Linee di extra_params che contengono IP di rete/gateway (NON dell'host).
# Vengono escluse da _build_ip_to_device per evitare di mappare
# "192.168.10.0" o "10.0.0.1" al nome del router sbagliato.
_EXCLUDE_PARAM_RE = re.compile(
    r'(?i)^\s*(ip\s+route|ip\s+dhcp|network|gateway|dns|default-router|next.hop)',
)


def _build_ip_to_device(intent, inventory: dict) -> dict[str, str]:
    """
    Costruisce la mappa {ip: device_name} da tutte le fonti disponibili.
    Supporta sia il vecchio IntentModel sia il nuovo NetworkIntentSchema.
    """
    ip_map: dict[str, str] = {}

    if intent:
        if getattr(intent, "router_plans", None):
            for rp in intent.router_plans:
                for line in rp.extra_params.splitlines():
                    if _EXCLUDE_PARAM_RE.match(line):
                        continue
                    for m in re.finditer(r"(\d{1,3}(?:\.\d{1,3}){3})", line):
                        ip = m.group(1)
                        last_octet = int(ip.split(".")[-1])
                        if last_octet in (0, 255):
                            continue
                        ip_map[ip] = rp.router_name
        elif getattr(intent, "devices", None):
            for dev in intent.devices:
                for iface in dev.interfaces:
                    if iface.ip and iface.ip.lower() != "dhcp":
                        # iface.ip is ip/cidr, extract ip
                        ip = iface.ip.split("/")[0]
                        ip_map[ip] = dev.name

    # Da devices.yaml — campo opzionale 'known_ip'
    for name, cfg in inventory.items():
        known = cfg.get("known_ip")
        if known:
            ip_map[known] = name

    return ip_map


async def observe_node(state: AgentState) -> dict:
    logger.info(">>> OBSERVE <<<")

    intent            = state.get("intent")
    specification_raw = state.get("specification_raw", "")
    raw_input         = state.get("raw_input", "")

    # Determina i dispositivi da osservare
    target_routers: list[str] = []

    if intent:
        if getattr(intent, "router_plans", None):
            target_routers = [rp.router_name for rp in intent.router_plans]
        elif getattr(intent, "devices", None):
            target_routers = [d.name for d in intent.devices]

    if not target_routers:
        text = specification_raw or raw_input
        if text:
            # Prova a parsarla come YAML per estrarre i nomi dei device
            try:
                import yaml
                data = yaml.safe_load(text)
                if isinstance(data, dict) and "devices" in data:
                    target_routers = [d["name"] for d in data["devices"] if "name" in d]
            except Exception:
                pass
            if not target_routers:
                target_routers = re.findall(r"DEVICE:\s*([\w-]+)", text, re.IGNORECASE)

    if not target_routers:
        logger.error("[OBSERVE] Impossibile determinare la lista dei dispositivi target.")
        return {"reachability": {}}

    logger.info("[OBSERVE] Scansione per: %s", target_routers)

    inventory = load_inventory()
    async with AsyncNetworkGraphStore() as store:
        # Seeding dei link fisici dall'Intent (Ground Truth)
        intent_links = []
        if intent and hasattr(intent, "links") and intent.links:
            for link in intent.links:
                if hasattr(link, "endpoints") and len(link.endpoints) == 2:
                    intent_links.append(link.endpoints)
        else:
            spec_text = state.get("specification_raw", "")
            if spec_text:
                try:
                    import yaml
                    data = yaml.safe_load(spec_text)
                    if isinstance(data, dict) and "links" in data:
                        for link in data["links"]:
                            if isinstance(link, dict) and "endpoints" in link:
                                endpoints = link["endpoints"]
                                if isinstance(endpoints, list) and len(endpoints) == 2:
                                    intent_links.append(endpoints)
                except Exception:
                    pass

        if intent_links:
            logger.info("[OBSERVE] Seeding %d physical links from intent/spec", len(intent_links))
            from tools.parser import normalize_interface_name
            for ep_pair in intent_links:
                try:
                    dev1, iface1 = ep_pair[0].split(":", 1)
                    dev2, iface2 = ep_pair[1].split(":", 1)
                    dev1 = dev1.strip()
                    dev2 = dev2.strip()
                    iface1 = normalize_interface_name(iface1.strip())
                    iface2 = normalize_interface_name(iface2.strip())
                    await store.upsert_l2_link(
                        local_device=dev1,
                        local_iface=iface1,
                        remote_device=dev2,
                        remote_iface=iface2,
                        source="intent",
                    )
                except Exception as e:
                    logger.warning("[OBSERVE] Failed to seed link %s: %s", ep_pair, e)

        tasks = [
            snapshot_device(name, inventory.get(name, {}), store)
            for name in target_routers
        ]
        snapshots = await asyncio.gather(*tasks, return_exceptions=True)

        # L3: link basati su subnet matching
        await store.compute_topology_links()

        # L2: Step 1 — EtherChannel + inferenza black box
        await store.compute_l2_topology()

        # L2: Step 2 — Discovery ARP+MAC per localizzare gli host sulle porte.
        # Usa solo i device del task corrente (target_routers) come filtro:
        # router e switch effettivamente scansiti in questo OBSERVE, non
        # l'intero inventory che potrebbe contenere device non coinvolti.
        ip_to_device = _build_ip_to_device(intent, inventory)
        target_inventory = {
            name: cfg
            for name, cfg in inventory.items()
            if name in target_routers
        }
        try:
            await run_l2_discovery(
                inventory=target_inventory,
                ip_to_device=ip_to_device,
                store=store,
            )
        except Exception as e:
            logger.warning("[OBSERVE] L2 Discovery fallita (non bloccante): %s", e)

    reachability: dict[str, str] = {}
    for name, result in zip(target_routers, snapshots):
        if isinstance(result, Exception):
            logger.error("[OBSERVE] Eccezione inattesa per %s: %s", name, result)
            reachability[name] = "UNREACHABLE"
        elif result is None:
            reachability[name] = "UNREACHABLE"
        else:
            reachability[name] = "REACHABLE"

    reachable_count = list(reachability.values()).count("REACHABLE")
    log_msg = (
        f"OBSERVE: Scansione conclusa. "
        f"Raggiungibili: {reachable_count}/{len(target_routers)} nodi."
    )
    logger.info(log_msg)

    return {
        "reachability":  reachability,
        "execution_log": [log_msg],
    }


def _find_relay_devices(plan) -> list[str]:
    """
    Estrae i nomi dei device che hanno DHCP_RELAY nella loro spec,
    incluso il server DHCP di destinazione.
    """
    if not plan:
        return []

    devices_to_snapshot: set[str] = set()

    if getattr(plan, "devices", None):
        for dev in plan.devices:
            relay_subnets, server = extract_dhcp_relay_params(
                dev.extra_params,
                getattr(dev, "dhcp_relay_server", None),
                getattr(dev, "dhcp_relay_subnets", None)
            )
            if relay_subnets and server:
                devices_to_snapshot.add(dev.name)
                devices_to_snapshot.add(server)
    elif getattr(plan, "router_plans", None):
        for rp in plan.router_plans:
            relay_subnets, server = extract_dhcp_relay_params(
                rp.extra_params,
                getattr(rp, "dhcp_relay_server", None),
                getattr(rp, "dhcp_relay_subnets", None)
            )
            if relay_subnets and server:
                devices_to_snapshot.add(rp.router_name)
                devices_to_snapshot.add(server)

    return list(devices_to_snapshot)


async def observe_relay_node(state: AgentState) -> dict:
    logger.info(">>> OBSERVE_RELAY <<<")

    plan          = state.get("plan")
    relay_devices = _find_relay_devices(plan)

    if not relay_devices:
        logger.info("[OBSERVE_RELAY] Nessun device relay trovato. Skip.")
        return {}

    logger.info("[OBSERVE_RELAY] Snapshot relay devices: %s", relay_devices)

    inventory = load_inventory()
    async with AsyncNetworkGraphStore() as store:
        tasks = [
            snapshot_device(name, inventory.get(name, {}), store)
            for name in relay_devices
            if name in inventory
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Aggiorna entrambi i layer dopo che le subinterface sono apparse
        await store.compute_topology_links()
        await store.compute_l2_topology()

    log_msg = f"OBSERVE_RELAY: snapshot aggiornato per {relay_devices}."
    logger.info(log_msg)
    return {"execution_log": [log_msg]}
