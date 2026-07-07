# tools/l2_discovery.py
"""
Discovery L2 completa tramite ARP (router) e MAC table (switch).

Flusso:
  1. Router  → show arp          → dict {ip: (mac, iface)}
  2. Switch  → show mac address-table → dict {mac: porta_access}
  3. Merge   → IP + MAC + porta switch + device_name noti → nodo Endpoint

Il risultato viene persistito in Neo4j come nodi Endpoint collegati
all'interfaccia dello switch su cui sono stati rilevati:

  (Switch)-[:HAS_INTERFACE]->(Interface)-[:CONNECTS_ENDPOINT]->(Endpoint)

Il nodo Endpoint ha le proprietà:
  - ip        : indirizzo IP (da ARP del router)
  - mac       : indirizzo MAC (da ARP + MAC table)
  - switch    : nome dello switch dove è stato visto
  - port      : porta dello switch (es. Ethernet0/1)
  - device    : nome del device NetAgent se noto (da ip_to_device)
  - last_seen : timestamp ultimo aggiornamento

La funzione run_l2_discovery() è chiamata da observe_node dopo
compute_l2_topology() e riceve:
  - conn_map     : {device_name: cfg_dict} — inventory completo
  - ip_to_device : {ip_str: device_name}   — mappa IP→nome noto
  - store        : AsyncNetworkGraphStore
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from tools.connection import get_connection
from tools.graph_store import AsyncNetworkGraphStore
from tools.parser import resolve_vendor

logger = logging.getLogger(__name__)


# =============================================================================
# Entry point pubblico
# =============================================================================

async def run_l2_discovery(
    inventory: dict,
    ip_to_device: dict[str, str],
    store: AsyncNetworkGraphStore,
) -> None:
    """
    Esegue la discovery L2 completa su tutta la rete.

    Args:
        inventory:     dict completo da devices.yaml  {name: cfg}
        ip_to_device:  {ip_str: device_name} — IP noti (es. dai RouterIntent)
        store:         istanza già aperta di AsyncNetworkGraphStore
    """
    # Raccogli ARP da tutti i router (cisco_ios e frrouting)
    router_names = [
        name for name, cfg in inventory.items()
        if resolve_vendor(cfg, name) in ("cisco_ios", "frrouting")
    ]
    switch_names = [
        name for name, cfg in inventory.items()
        if resolve_vendor(cfg, name) == "cisco_switch"
    ]

    # Esegui ARP e MAC table in parallelo
    arp_tasks = [
        _collect_arp(name, inventory[name])
        for name in router_names
    ]
    mac_tasks = [
        _collect_mac_table(name, inventory[name])
        for name in switch_names
    ]

    arp_results  = await asyncio.gather(*arp_tasks,  return_exceptions=True)
    mac_results  = await asyncio.gather(*mac_tasks,  return_exceptions=True)

    # Unisci tutti gli ARP in un'unica mappa ip → (mac, router_iface, router_name)
    global_arp: dict[str, dict] = {}
    for name, result in zip(router_names, arp_results):
        if isinstance(result, Exception):
            logger.warning("[L2-Discovery] ARP fallito per %s: %s", name, result)
            continue
        for ip, entry in result.items():
            global_arp[ip] = {**entry, "router": name}

    # Unisci tutte le MAC table in un'unica mappa mac → (switch, port, vlan).
    # In caso di MAC visto su più switch (flooding o trunk mal configurato),
    # si usa lo switch con il numero di porte più basso (più "vicino" all'edge)
    # e si logga il conflitto per debug.
    global_mac: dict[str, dict] = {}
    for name, result in zip(switch_names, mac_results):
        if isinstance(result, Exception):
            logger.warning("[L2-Discovery] MAC table fallita per %s: %s", name, result)
            continue
        for mac, entry in result.items():
            if mac in global_mac:
                existing = global_mac[mac]
                logger.debug(
                    "[L2-Discovery] MAC collision: %s visto su %s:%s e %s:%s — "
                    "tengo il primo (potrebbe essere flooding/trunk)",
                    mac,
                    existing["switch"], existing["port"],
                    name, entry["port"],
                )
                # Mantieni il primo trovato — più stabile del last-write-wins
                continue
            global_mac[mac] = {**entry, "switch": name}

    logger.info(
        "[L2-Discovery] ARP entries: %d | MAC entries: %d",
        len(global_arp), len(global_mac),
    )

    # Merge e persistenza
    await _merge_and_store(global_arp, global_mac, ip_to_device, store)


# =============================================================================
# Raccolta ARP dai router
# =============================================================================

async def _collect_arp(router_name: str, cfg: dict) -> dict[str, dict]:
    """
    Ritorna {ip: {mac, iface}} dal router.
    Supporta Cisco IOS e FRRouting.
    """
    vendor = resolve_vendor(cfg, router_name)
    try:
        async with get_connection(cfg) as conn:
            await conn.send_command("terminal length 0")
            if vendor == "cisco_ios":
                raw = await conn.send_command("show arp")
                return _parse_cisco_arp(raw)
            elif vendor == "frrouting":
                raw = await conn.send_command("show arp")
                return _parse_frr_arp(raw)
            else:
                logger.debug(
                    "[L2-Discovery] Vendor %s non supportato per ARP, skip %s.",
                    vendor, router_name,
                )
                return {}
    except Exception as e:
        logger.warning("[L2-Discovery] Connessione ARP fallita per %s: %s", router_name, e)
        return {}


def _parse_cisco_arp(raw: str) -> dict[str, dict]:
    """
    Parsa output di 'show arp' Cisco IOS.

    Esempio:
      Protocol  Address    Age  Hardware Addr   Type   Interface
      Internet  192.168.10.2  0  aabb.cc00.0500  ARPA   Ethernet0/1.10
    """
    result: dict[str, dict] = {}
    for line in raw.splitlines():
        # Cerca righe con IP, MAC e interfaccia
        m = re.match(
            r'\s*Internet\s+([\d\.]+)\s+\S+\s+([0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4})\s+\S+\s+(\S+)',
            line, re.IGNORECASE
        )
        if m:
            ip, mac, iface = m.group(1), _normalize_mac(m.group(2)), m.group(3)
            result[ip] = {"mac": mac, "iface": iface}
    logger.debug("[L2-Discovery] ARP Cisco: %d entries", len(result))
    return result


def _parse_frr_arp(raw: str) -> dict[str, dict]:
    """
    Parsa output di 'show arp' FRRouting.

    Esempio:
      Address         HWtype  HWaddress           Flags Iface
      192.168.10.2    ether   aa:bb:cc:00:05:00   C     eth0.10
    """
    result: dict[str, dict] = {}
    for line in raw.splitlines():
        m = re.match(
            r'\s*([\d\.]+)\s+\S+\s+([0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2}:[0-9a-f]{2})\s+\S+\s+(\S+)',
            line, re.IGNORECASE
        )
        if m:
            ip  = m.group(1)
            mac = _normalize_mac(m.group(2))
            iface = m.group(3)
            result[ip] = {"mac": mac, "iface": iface}
    logger.debug("[L2-Discovery] ARP FRR: %d entries", len(result))
    return result


# =============================================================================
# Raccolta MAC table dagli switch
# =============================================================================

async def _collect_mac_table(switch_name: str, cfg: dict) -> dict[str, dict]:
    """
    Ritorna {mac: {port, vlan}} dallo switch.
    """
    try:
        async with get_connection(cfg) as conn:
            await conn.send_command("terminal length 0")
            raw = await conn.send_command("show mac address-table")
            return _parse_mac_table(raw)
    except Exception as e:
        logger.warning("[L2-Discovery] MAC table fallita per %s: %s", switch_name, e)
        return {}


# Porte uplink da escludere dalla MAC table (non sono endpoint access)
_UPLINK_PORT_RE = re.compile(
    r'(?i)^(Port-channel|Po\d|GigabitEthernet|FastEthernet|TenGigabitEthernet)',
    re.IGNORECASE,
)


def _parse_mac_table(raw: str) -> dict[str, dict]:
    """
    Parsa output di 'show mac address-table' Cisco IOS/Switch.

    Mantiene solo le porte access (Ethernet) escludendo uplink
    (Port-channel, GigabitEthernet, ecc.) — la logica precedente
    aveva un OR invertito che includeva porte per caso.

    Esempio:
          Vlan    Mac Address       Type        Ports
          ----    -----------       --------    -----
            10    aabb.cc00.0500    DYNAMIC     Et0/1    ← incluso
            20    aabb.cc00.0600    DYNAMIC     Po1      ← escluso (uplink)
           All    0100.0ccc.cccc    STATIC      CPU      ← escluso (header)
    """
    result: dict[str, dict] = {}
    for line in raw.splitlines():
        # Esclude header, righe speciali e CPU
        if any(kw in line for kw in ("CPU", "Router", "Switch", "---", "Vlan", "All ")):
            continue
        m = re.match(
            r'\s*(\d+)\s+([0-9a-f]{4}\.[0-9a-f]{4}\.[0-9a-f]{4})\s+\S+\s+(\S+)',
            line, re.IGNORECASE,
        )
        if not m:
            continue
        vlan = int(m.group(1))
        mac  = _normalize_mac(m.group(2))
        port = _expand_interface(m.group(3))
        # Esclude esplicitamente le porte uplink — teniamo solo access
        if _UPLINK_PORT_RE.match(port):
            continue
        result[mac] = {"port": port, "vlan": vlan}
    logger.debug("[L2-Discovery] MAC table: %d entries", len(result))
    return result


# =============================================================================
# Merge e persistenza Neo4j
# =============================================================================

# MAC visti su porte uplink CDP dello switch (trunk verso router).
# Questi MAC appartengono alle subinterface del router, non a host reali.
# Vengono costruiti durante il merge confrontando la porta MAC con le porte
# che hanno PHYSICALLY_CONNECTED_TO nel grafo.
async def _get_uplink_ports(store: AsyncNetworkGraphStore) -> set[tuple[str, str]]:
    """
    Ritorna l'insieme di (switch, porta) che sono uplink CDP/LLDP/etherchannel.
    Un MAC visto su queste porte è un gateway, non un host.
    """
    query = """
    MATCH (d:Device)-[:HAS_INTERFACE]->(i:Interface)-[:PHYSICALLY_CONNECTED_TO]->(:Interface)
    RETURN d.name AS switch, i.name AS port
    """
    uplinks: set[tuple[str, str]] = set()
    async with store._driver.session() as session:
        result = await session.run(query)
        async for rec in result:
            if rec["switch"] and rec["port"]:
                uplinks.add((rec["switch"], rec["port"]))
    return uplinks


async def _get_device_ips(store: AsyncNetworkGraphStore) -> set[str]:
    """
    Ritorna tutti gli IP assegnati a interfacce di Device nel grafo.
    Questi IP appartengono a router/switch — non sono endpoint host.
    Usato per filtrare le entry ARP che corrispondono a subinterface
    o interfacce del router stesso (es. 192.168.10.1 = gateway R1).
    """
    query = """
    MATCH (:Device)-[:HAS_INTERFACE]->(i:Interface)
    WHERE i.ip_address <> 'unassigned' AND i.ip_address IS NOT NULL
    RETURN split(i.ip_address, '/')[0] AS ip
    """
    device_ips: set[str] = set()
    async with store._driver.session() as session:
        result = await session.run(query)
        async for rec in result:
            if rec["ip"]:
                device_ips.add(rec["ip"])
    return device_ips


async def _merge_and_store(
    global_arp:    dict[str, dict],
    global_mac:    dict[str, dict],
    ip_to_device:  dict[str, str],
    store:         AsyncNetworkGraphStore,
) -> None:
    """
    Unisce ARP + MAC e persiste nodi Endpoint in Neo4j.

    Logica di merge:
      1. Recupera le porte uplink dal grafo (porte con PHYSICALLY_CONNECTED_TO)
      2. Per ogni IP nell'ARP table, cerca il MAC corrispondente
      3. Se il MAC è visto su una porta uplink → è un gateway, non un host → skip
      4. Crea/aggiorna il nodo Endpoint con IP, MAC, switch, porta, device_name
      5. Collega l'Endpoint all'interfaccia dello switch
    """
    ts = datetime.now(timezone.utc).isoformat()
    merged = 0
    skipped = 0

    # Recupera le porte uplink e gli IP dei device per filtrare i gateway
    uplink_ports = await _get_uplink_ports(store)
    device_ips   = await _get_device_ips(store)
    logger.debug("[L2-Discovery] Porte uplink: %s", uplink_ports)
    logger.debug("[L2-Discovery] IP device da escludere: %s", device_ips)

    for ip, arp_entry in global_arp.items():
        mac = arp_entry.get("mac")
        if not mac:
            continue

        mac_entry   = global_mac.get(mac)
        device_name = ip_to_device.get(ip, "")

        switch = mac_entry["switch"] if mac_entry else ""
        port   = mac_entry["port"]   if mac_entry else ""
        vlan   = mac_entry["vlan"]   if mac_entry else 0

        # Salta IP che appartengono a interfacce di router/switch
        # (es. 192.168.10.1 = subinterface R1, non un PC)
        if ip in device_ips:
            logger.debug(
                "[L2-Discovery] Skip device IP: %s (è un'interfaccia di rete)", ip
            )
            skipped += 1
            continue

        # Salta i MAC visti su porte uplink — trunk verso router, non access host
        if switch and port and (switch, port) in uplink_ports:
            logger.debug(
                "[L2-Discovery] Skip gateway: ip=%s mac=%s su porta uplink %s:%s",
                ip, mac, switch, port,
            )
            skipped += 1
            continue

        await store.upsert_endpoint(
            ip=ip,
            mac=mac,
            switch=switch,
            port=port,
            vlan=vlan,
            device_name=device_name,
            ts=ts,
        )
        merged += 1
        logger.debug(
            "[L2-Discovery] Endpoint: ip=%s mac=%s switch=%s port=%s device=%s",
            ip, mac, switch, port, device_name,
        )

    logger.info(
        "[L2-Discovery] %d endpoint salvati, %d gateway filtrati.",
        merged, skipped,
    )


# =============================================================================
# Utility
# =============================================================================

def _normalize_mac(raw: str) -> str:
    """
    Normalizza MAC in formato aa:bb:cc:dd:ee:ff minuscolo.
    Accetta sia aabb.ccdd.eeff (Cisco) che aa:bb:cc:dd:ee:ff (Linux).
    """
    clean = re.sub(r'[^0-9a-fA-F]', '', raw).lower()
    if len(clean) != 12:
        return raw.lower()
    return ':'.join(clean[i:i+2] for i in range(0, 12, 2))


def _expand_interface(abbrev: str) -> str:
    """
    Espande abbreviazioni di interfaccia Cisco nella forma completa.
    Es: 'Et0/1' → 'Ethernet0/1', 'Gi0/1' → 'GigabitEthernet0/1'
    """
    prefixes = {
        "Et":  "Ethernet",
        "Gi":  "GigabitEthernet",
        "Fa":  "FastEthernet",
        "Te":  "TenGigabitEthernet",
        "Po":  "Port-channel",
    }
    for short, full in prefixes.items():
        if abbrev.startswith(short) and not abbrev.startswith(full):
            return full + abbrev[len(short):]
    return abbrev



