# nodes/verify.py
"""
Nodo VERIFY v4 — Eventual Consistency, Pessimistic Verification & L2/L3 Control Plane Assurance.
"""

from __future__ import annotations

import logging
import asyncio
import ipaddress
import re
import yaml
from pathlib import Path

from core.state import AgentState, RouterCommands, DeviceIntent, NetworkIntentSchema
from tools.device_snapshot import snapshot_device
from tools.connection import get_connection
from tools.ping_parser import parse_ping_result
from tools.graph_store import AsyncNetworkGraphStore
from tools.template_engine import parser as output_parser
from tools.parser import load_inventory, normalize_interface_name
from tools.metrics import metrics

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Estrazione IP live dal Knowledge Graph
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_live_ips_from_graph(store: AsyncNetworkGraphStore) -> dict[str, str]:
    """Restituisce {pc_name: ip/cidr} per tutti i PC terminali nel grafo."""
    live_ips = {}
    query = (
        "MATCH (d:Device)-[:HAS_INTERFACE]->(i:Interface) "
        "WHERE (d.vendor = 'vpcs' OR toLower(d.name) CONTAINS 'pc') "
        "AND i.ip_address IS NOT NULL AND i.ip_address <> 'unassigned' "
        "RETURN d.name AS pc_name, i.ip_address AS ip"
    )
    async with store._driver.session() as session:
        res = await session.run(query)
        async for rec in res:
            if rec["ip"] and rec["ip"] != "unassigned":
                live_ips[rec["pc_name"]] = rec["ip"].split("/")[0].strip()
    return live_ips


async def _fetch_network_device_ips(store: AsyncNetworkGraphStore) -> dict[str, list[str]]:
    """Restituisce {device_name: [ip_address, ...]} per tutti i router e switch nel grafo."""
    dev_ips = {}
    if not hasattr(store, "_driver") or store._driver is None or type(store._driver).__name__ in ("MagicMock", "AsyncMock"):
        logger.debug("[VERIFY] store._driver è un mock o assente, salto _fetch_network_device_ips")
        return dev_ips

    query = (
        "MATCH (d:Device)-[:HAS_INTERFACE]->(i:Interface) "
        "WHERE d.vendor IN ['cisco_ios', 'cisco_switch', 'frrouting'] "
        "AND i.ip_address <> 'unassigned' AND i.ip_address IS NOT NULL "
        "RETURN d.name AS dev_name, i.ip_address AS ip"
    )
    try:
        async with store._driver.session() as session:
            res = await session.run(query)
            async for rec in res:
                dev_name = rec["dev_name"]
                ip = rec["ip"].split("/")[0].strip()
                dev_ips.setdefault(dev_name, []).append(ip)
    except Exception as e:
        logger.warning("[VERIFY] Errore in _fetch_network_device_ips (possibile mock): %s", e)
    return dev_ips


# ─────────────────────────────────────────────────────────────────────────────
# Auto-Remediation (STP Bypass)
# ─────────────────────────────────────────────────────────────────────────────

async def _force_dhcp_renew(hosts: set[str], inventory: dict) -> None:
    """Auto-remediation: forza un rinnovo DHCP sui nodi bloccati con sfasamento temporale."""
    async def _renew(idx: int, h: str):
        cfg = inventory.get(h)
        if not cfg:
            return
        delay = idx * 3.0
        if delay > 0:
            await asyncio.sleep(delay)
        try:
            async with get_connection(cfg) as conn:
                await conn.send_command("ip dhcp", timeout=2.0)
                logger.info("[VERIFY] Comando DHCP forzato su %s", h)
        except Exception as e:
            logger.debug("[VERIFY] Impossibile forzare DHCP su %s: %s", h, e)

    hosts_list = list(hosts)
    await asyncio.gather(*[_renew(i, h) for i, h in enumerate(hosts_list)])


# ─────────────────────────────────────────────────────────────────────────────
# Post-execute sync: aggiorna Neo4j con Active Polling per DHCP
# ─────────────────────────────────────────────────────────────────────────────

async def _post_execute_graph_sync(
    infra: list[str],
    dhcp_hosts: list[str],
    static_hosts: list[str],
    inventory: dict,
    store: AsyncNetworkGraphStore,
    max_retries: int = 5,
    delay: int = 4,
) -> None:
    """
    Sincronizzazione Ibrida:
    1. Sync immediato per infrastruttura core L2/L3 e PC Statici.
    2. Polling intelligente SOLO per i client DHCP.
    """
    immediate_sync = infra + static_hosts
    if immediate_sync:
        logger.info(
            "[VERIFY][Sync] Sincronizzazione immediata infrastruttura e PC statici: %s",
            immediate_sync,
        )
        tasks = [
            snapshot_device(name, inventory.get(name, {}), store)
            for name in immediate_sync if name in inventory
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

    if not dhcp_hosts:
        return

    logger.info(
        "[VERIFY][Sync] Avvio polling convergenza DHCP per i client: %s", dhcp_hosts
    )
    pending_hosts = set(dhcp_hosts)

    for attempt in range(1, max_retries + 1):
        logger.info(
            "[VERIFY][Sync] Polling DHCP (Tentativo %d/%d) - Attesa %ds...",
            attempt, max_retries, delay,
        )
        await asyncio.sleep(delay)

        tasks = [
            snapshot_device(name, inventory.get(name, {}), store)
            for name in pending_hosts if name in inventory
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

        await store.compute_topology_links()
        live_ips = await _fetch_live_ips_from_graph(store)
        logger.info("[VERIFY][Sync][Debug] IP live correnti nel grafo: %s", live_ips)

        still_pending = [h for h in pending_hosts if h not in live_ips]
        if still_pending:
            logger.info("[VERIFY][Sync][Debug] Client DHCP ancora pendenti (senza IP): %s", still_pending)

        if not still_pending:
            logger.info("[VERIFY][Sync] Convergenza DHCP raggiunta per tutti i client!")
            return

        if attempt == 3:
            logger.warning(
                "[VERIFY][Sync] I client %s tardano (possibile blocco STP). "
                "Forzo rinnovo DHCP...", still_pending,
            )
            await _force_dhcp_renew(set(still_pending), inventory)

        pending_hosts = set(still_pending)

    logger.error(
        "[VERIFY][Sync] TIMEOUT: I client %s non hanno ottenuto un IP dal DHCP.",
        list(pending_hosts),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Ping task
# ─────────────────────────────────────────────────────────────────────────────

async def _run_ping(
    src_name: str,
    src_cfg: dict,
    target_ip: str,
    lock: asyncio.Lock,
    attempts: int = 2,
    retry_delay: float = 5.0,
) -> tuple[str, str, bool]:
    """Esegue un ping con retry breve prima di dichiarare loss.

    Il primo ping dopo un deploy puo fallire per ARP/STP/DHCP appena convergenti.
    Non deve quindi essere il singolo campione che manda subito il workflow in
    troubleshooting cognitivo.
    """
    clean_target = target_ip.split("/")[0].strip()
    attempts = max(1, attempts)
    last_result = None
    last_output = ""
    last_error = None

    async with lock:
        for attempt in range(1, attempts + 1):
            try:
                async with get_connection(src_cfg) as conn:
                    cmd = f"ping {clean_target} -c 3"
                    logger.info(
                        "[CROSS-VERIFY][Debug] %s: invio comando '%s' (tentativo %d/%d)",
                        src_name, cmd, attempt, attempts,
                    )
                    output = await conn.send_command(cmd)
                    last_output = output
                    logger.info("[CROSS-VERIFY][Debug] Output ricevuto da %s:\n%s", src_name, output)
                result = parse_ping_result(output, vendor="vpcs")
                last_result = result
                if result.success:
                    logger.info(
                        "[CROSS-VERIFY] 🟢 %s -> %s OK (%d/%d, tentativo %d/%d)",
                        src_name, clean_target, result.packets_received, result.packets_sent,
                        attempt, attempts,
                    )
                    return src_name, clean_target, True
                logger.warning(
                    "[CROSS-VERIFY] %s -> %s non riuscito al tentativo %d/%d (loss %.0f%%)",
                    src_name, clean_target, attempt, attempts, result.loss_pct,
                )
            except Exception as e:
                last_error = e
                logger.warning(
                    "[CROSS-VERIFY] Eccezione %s -> %s al tentativo %d/%d: %s",
                    src_name, clean_target, attempt, attempts, e,
                )

            if attempt < attempts:
                await asyncio.sleep(retry_delay)

        if last_result is not None:
            logger.error(
                "[CROSS-VERIFY] 🔴 %s -> %s LOST dopo %d tentativi (%.0f%%)",
                src_name, clean_target, attempts, last_result.loss_pct,
            )
            return src_name, clean_target, False

        if last_error is not None:
            logger.error("[CROSS-VERIFY] Eccezione finale %s -> %s: %s", src_name, clean_target, last_error)
        else:
            logger.error("[CROSS-VERIFY] Nessun risultato ping parsabile da %s -> %s:\n%s", src_name, clean_target, last_output)
        return src_name, clean_target, False


# ─────────────────────────────────────────────────────────────────────────────
# Matrice di validazione
# ─────────────────────────────────────────────────────────────────────────────

def _build_validation_matrix(
    live_map: dict[str, str],
    dev_ips: dict[str, list[str]] = None,
) -> list[tuple[str, str]]:
    """Genera test cross-subnet e intra-subnet, inclusi i test verso router e switch."""
    subnet_groups: dict[ipaddress.IPv4Network, list[tuple[str, str]]] = {}

    for pc_name, ip_raw in live_map.items():
        if "/" not in ip_raw:
            ip_raw = f"{ip_raw}/24"
        try:
            net = ipaddress.IPv4Network(ip_raw, strict=False)
            subnet_groups.setdefault(net, []).append((pc_name, ip_raw))
        except ValueError:
            logger.warning("[VERIFY] IP non valido per %s: %s", pc_name, ip_raw)

    representatives: list[tuple[str, str, ipaddress.IPv4Network]] = []
    for net, members in subnet_groups.items():
        members_sorted = sorted(members, key=lambda x: x[0])
        chosen_pc, chosen_ip = members_sorted[0]
        representatives.append((chosen_pc, chosen_ip, net))
        logger.info(
            "[VERIFY] Subnet %s → rappresentante: %s (%s)", net, chosen_pc, chosen_ip
        )
        if len(members_sorted) >= 2:
            second_pc, second_ip = members_sorted[1]
            logger.info(
                "[VERIFY] Test intra-LAN aggiunto: %s -> %s", chosen_pc, second_ip
            )

    matrix: list[tuple[str, str]] = []
    for i, (src, _, _src_net) in enumerate(representatives):
        for j, (dst, dst_ip, _dst_net) in enumerate(representatives):
            if i != j:
                matrix.append((src, dst_ip))

    for net, members in subnet_groups.items():
        if len(members) >= 2:
            members_sorted = sorted(members, key=lambda x: x[0])
            matrix.append((members_sorted[0][0], members_sorted[1][1]))

    # Aggiunge pings verso router/switch da un PC esterno alla loro subnet (se dev_ips è valorizzato)
    if dev_ips and live_map:
        for dev_name, ips in dev_ips.items():
            for ip in ips:
                try:
                    ip_addr = ipaddress.IPv4Address(ip)
                    # Trova un PC sorgente che NON sia nella stessa subnet di questo IP del device
                    for pc_name, pc_ip in live_map.items():
                        pc_net = ipaddress.IPv4Network(f"{pc_ip}/24", strict=False)
                        if ip_addr not in pc_net:
                            matrix.append((pc_name, ip))
                            logger.info(
                                "[VERIFY] Aggiunto test connettività infrastruttura: %s -> %s (%s)",
                                pc_name, dev_name, ip
                            )
                            break
                except Exception as e:
                    logger.warning("[VERIFY] Impossibile analizzare IP %s per %s: %s", ip, dev_name, e)

    return matrix


# ─────────────────────────────────────────────────────────────────────────────
# Estrazione device falliti per TROUBLESHOOT
# ─────────────────────────────────────────────────────────────────────────────

def _extract_failed_devices(
    ping_results: list,
    missing_dhcp: list[str],
    missing_static: list[str],
    inventory: dict,
    live_map: dict[str, str],
    dev_ips: dict[str, list[str]] = None,
) -> list[str]:
    """Produce la lista deduplicata dei device da passare a TROUBLESHOOT."""
    failed: set[str] = set()

    failed.update(missing_dhcp)
    failed.update(missing_static)

    # Costruiamo una mappa IP -> Device Name estesa (include sia PC che router/switch)
    ip_to_device = {ip: name for name, ip in live_map.items()}
    if dev_ips:
        for dev_name, ips in dev_ips.items():
            for ip in ips:
                ip_to_device[ip] = dev_name

    for res in ping_results:
        if isinstance(res, Exception):
            continue
        src, dst_ip, ok = res
        if not ok:
            failed.add(src)
            dst_clean = dst_ip.split("/")[0].strip()
            if dst_clean in ip_to_device:
                failed.add(ip_to_device[dst_clean])

    return [d for d in sorted(failed) if d in inventory]


# ─────────────────────────────────────────────────────────────────────────────
# Control Plane Assurance
# ─────────────────────────────────────────────────────────────────────────────

async def _verify_control_plane(
    device_name: str,
    device_intent: DeviceIntent,
    inventory: dict,
) -> list[str]:
    """Verifica lo stato reale dei protocolli L2/L3 sul dispositivo (Control Plane)."""
    errors = []
    cfg = inventory.get(device_name)
    if not cfg:
        return [f"Device {device_name} non trovato nell'inventario."]

    vendor = (cfg.get("vendor") or "").lower()
    if vendor not in ("cisco_ios", "cisco_switch"):
        return errors

    commands = []
    has_vlans = bool(device_intent.vlans)
    has_dhcp = bool(device_intent.dhcp_pools)
    
    etherchannels = {}
    for iface in device_intent.interfaces:
        if iface.channel_group is not None:
            etherchannels.setdefault(iface.channel_group, []).append(iface.name)

    if has_vlans and vendor == "cisco_switch":
        commands.append(("show vlan brief", "cisco_show_vlan_brief.textfsm"))
    if has_dhcp:
        commands.append(("show ip dhcp pool", "cisco_show_ip_dhcp_pool.textfsm"))
    if etherchannels:
        commands.append(("show etherchannel summary", "cisco_show_etherchannel_summary.textfsm"))

    if not commands:
        logger.info(f"[{device_name}][Debug] Nessun comando Control Plane configurato da verificare.")
        return errors

    try:
        async with get_connection(cfg) as conn:
            for cmd, template in commands:
                logger.info(f"[{device_name}] Control Plane Verification: running '{cmd}'")
                output = await conn.send_command(cmd)
                logger.info(f"[{device_name}][Debug] Raw CLI Output:\n{output}")
                parsed = output_parser.parse_with_template(output, template)
                logger.info(f"[{device_name}][Debug] Parsed structure (TextFSM): {parsed}")

                if template == "cisco_show_vlan_brief.textfsm":
                    vlan_map = {int(r["VLAN_ID"]): r for r in parsed if r.get("VLAN_ID")}
                    logger.info(f"[{device_name}][Debug] Mappa VLAN rilevata: {list(vlan_map.keys())}")
                    

                    
                    for v_id, v_name in device_intent.vlans.items():
                        v_id_int = int(v_id)
                        logger.info(f"[{device_name}][Debug] Verifica presenza VLAN {v_id_int} ({v_name})...")
                        if v_id_int not in vlan_map:
                            errors.append(f"VLAN {v_id} ({v_name}) non trovata sul dispositivo.")
                            continue
                        record = vlan_map[v_id_int]
                        status = record.get("STATUS", "")
                        logger.info(f"[{device_name}][Debug] VLAN {v_id_int} stato reale: '{status}'")
                        if status.lower() != "active":
                            errors.append(f"VLAN {v_id} trovata ma in stato non attivo: {status}")
                        
                        # Parsing e normalizzazione dei port associati alla VLAN nel record reale
                        ports_str = record.get("PORTS", "")
                        raw_ports = [p.strip() for p in ports_str.split(",") if p.strip()]
                        real_ports_normalized = {normalize_interface_name(p) for p in raw_ports}
                        logger.info(f"[{device_name}][Debug] VLAN {v_id_int} porte reali normalizzate: {real_ports_normalized}")
                        
                        for iface in device_intent.interfaces:
                            if iface.mode == "access" and (iface.access_vlan == v_id_int or iface.vlan_id == v_id_int):
                                iface_norm = normalize_interface_name(iface.name)
                                logger.info(f"[{device_name}][Debug] Verifica porta access {iface.name} (normalizzata: {iface_norm}) su VLAN {v_id_int}")
                                if iface_norm not in real_ports_normalized:
                                    errors.append(f"Porta {iface.name} non assegnata alla VLAN {v_id} (PORTS reali: {ports_str})")

                elif template == "cisco_show_ip_dhcp_pool.textfsm":
                    active_pools = {r["POOL_NAME"] for r in parsed if r.get("POOL_NAME")}
                    if not active_pools and any(marker in output for marker in ("% Invalid input", "% unknown command", "% Incomplete command", "% Command incomplete")):
                        logger.warning(f"[{device_name}] 'show ip dhcp pool' command not supported or failed. Trying fallback 'show running-config | include ip dhcp pool'...")
                        try:
                            fb_output = await conn.send_command("show running-config | include ip dhcp pool")
                            logger.info(f"[{device_name}][Debug] Fallback Output:\n{fb_output}")
                            fb_pools = re.findall(r'ip dhcp pool\s+(\S+)', fb_output)
                            active_pools = {name.strip() for name in fb_pools}
                        except Exception as fb_err:
                            logger.error(f"[{device_name}] Fallback check failed: {fb_err}")
                    logger.info(f"[{device_name}][Debug] DHCP Pools attive nel Control Plane: {active_pools}")
                    for pool in device_intent.dhcp_pools:
                        logger.info(f"[{device_name}][Debug] Verifica presenza pool DHCP '{pool.name}'...")
                        if pool.name not in active_pools:
                            errors.append(f"DHCP Pool '{pool.name}' non attivo sul server DHCP.")

                elif template == "cisco_show_etherchannel_summary.textfsm":
                    chan_map = {int(r["GROUP"]): r for r in parsed if r.get("GROUP")}
                    logger.info(f"[{device_name}][Debug] Gruppi EtherChannel rilevati: {list(chan_map.keys())}")
                    

                    
                    for grp_id, members in etherchannels.items():
                        logger.info(f"[{device_name}][Debug] Verifica EtherChannel Gruppo {grp_id} con membri {members}")
                        if grp_id not in chan_map:
                            errors.append(f"EtherChannel Group {grp_id} non trovato sul dispositivo.")
                            continue
                        record = chan_map[grp_id]
                        ports_str = record.get("PORTS", "")
                        logger.info(f"[{device_name}][Debug] EtherChannel Gruppo {grp_id} PORTS reali: '{ports_str}'")
                        
                        ports_list = ports_str.split()
                        parsed_ports = {}
                        for p_item in ports_list:
                            pm = re.match(r'^([a-zA-Z0-9/]+)\((.)\)$', p_item.strip())
                            if pm:
                                p_name_norm = normalize_interface_name(pm.group(1))
                                p_status = pm.group(2)
                                parsed_ports[p_name_norm] = p_status
                        
                        logger.info(f"[{device_name}][Debug] EtherChannel Gruppo {grp_id} porte reali decodificate: {parsed_ports}")
                        
                        for m in members:
                            m_norm = normalize_interface_name(m)
                            logger.info(f"[{device_name}][Debug] Verifica membro {m} (normalizzato: {m_norm})")
                            if m_norm not in parsed_ports:
                                errors.append(f"Membro EtherChannel {m} non trovato nel gruppo {grp_id} (PORTS reali: {ports_str})")
                            else:
                                status_flag = parsed_ports[m_norm]
                                logger.info(f"[{device_name}][Debug] Membro {m} stato reale flag: '{status_flag}'")
                                if status_flag != "P":
                                    errors.append(f"Membro EtherChannel {m} non è in stato bundled (P) ma in stato '{status_flag}'")

    except Exception as e:
        errors.append(f"Errore connessione/interrogazione durante la verifica: {e}")

    return errors


# ─────────────────────────────────────────────────────────────────────────────
# Rollback esecutore
# ─────────────────────────────────────────────────────────────────────────────

async def _rollback_device(device_name: str, commands_obj: RouterCommands, inventory: dict) -> bool:
    cfg = inventory.get(device_name)
    if not cfg:
        logger.error(f"[ROLLBACK] Device {device_name} non trovato nell'inventario.")
        return False
    pairs = getattr(commands_obj, "pairs", [])
    if not pairs:
        logger.info(f"[ROLLBACK] Nessun comando da sottoporre a rollback per {device_name}.")
        return True

    vendor = (cfg.get("vendor") or "").lower()
    logger.warning(f"[ROLLBACK] Esecuzione rollback su {device_name}...")

    rb_blocks = []
    for pair in reversed(pairs):
        block = pair.rollback
        if block and block.strip():
            rb_blocks.append(block.strip())

    if not rb_blocks:
        return True

    try:
        async with get_connection(cfg) as conn:
            for block in rb_blocks:
                lines = [l for l in block.splitlines() if l.strip()]
                for line in lines:
                    await conn.send_command(line)
                logger.warning(f"[{device_name}] ROLLBACK BLOCK:\n{block}")

            if vendor != "vpcs":
                await conn.save_config()
            return True
    except Exception as e:
        logger.error(f"[ROLLBACK] Fallito rollback su {device_name}: {e}")
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Verifica e Correzione IP statico VPCS (Auto-Remediation)
# ─────────────────────────────────────────────────────────────────────────────

def _parse_vpcs_show_ip(raw_output: str) -> tuple[str | None, str | None, str | None]:
    """Parsifica l'output del comando 'show ip' di VPCS e ritorna (ip, mask, gateway)."""
    ip = None
    mask = None
    gateway = None
    
    for line in raw_output.splitlines():
        line = line.strip()
        if not line:
            continue
        if "IP/MASK" in line:
            m = re.search(r'IP/MASK\s*:\s*([\d\.]+)/(\d+)', line, re.IGNORECASE)
            if m:
                ip = m.group(1)
                cidr = int(m.group(2))
                try:
                    mask = str(ipaddress.IPv4Network(f"0.0.0.0/{cidr}").netmask)
                except Exception:
                    mask = "255.255.255.0"
        if "GATEWAY" in line:
            m = re.search(r'GATEWAY\s*:\s*([\d\.]+)', line, re.IGNORECASE)
            if m:
                gateway = m.group(1)
                if gateway == "0.0.0.0":
                    gateway = ""
    return ip, mask, gateway


def _is_ip_equal(ip1: str | None, ip2: str | None) -> bool:
    if not ip1 or not ip2:
        return (ip1 or "").strip() == (ip2 or "").strip()
    try:
        return ipaddress.IPv4Address(ip1.strip()) == ipaddress.IPv4Address(ip2.strip())
    except Exception:
        return ip1.strip() == ip2.strip()


async def _heal_static_vpcs_if_needed(
    static_configs: dict[str, dict],
    live_map: dict[str, str],
    missing_static: list[str],
    inventory: dict,
    store: AsyncNetworkGraphStore,
) -> bool:
    """
    Verifica se gli host VPCS con IP statico hanno configurazioni errate.
    Se errate, si collega, le corregge, le salva e aggiorna il grafo Neo4j.
    Ritorna True se è stata effettuata almeno una correzione.
    """
    any_corrected = False
    
    hosts_to_check = set(missing_static)
    for host, desired in static_configs.items():
        if host in live_map:
            desired_ip = desired["ip"]
            live_ip = live_map[host]
            if not _is_ip_equal(desired_ip, live_ip):
                hosts_to_check.add(host)
                
    if not hosts_to_check:
        return False
        
    for host in hosts_to_check:
        desired = static_configs.get(host)
        cfg = inventory.get(host)
        if not desired or not cfg:
            continue
            
        desired_ip = desired["ip"]
        desired_mask = desired["mask"]
        desired_gw = desired["gateway"]
        
        logger.info("[VERIFY][Static-Heal] Verifico configurazione reale su %s...", host)
        need_snapshot = False
        try:
            async with get_connection(cfg) as conn:
                output = await conn.send_command("show ip")
                logger.info("[VERIFY][Static-Heal] Output show ip di %s:\n%s", host, output)
                
                real_ip, real_mask, real_gw = _parse_vpcs_show_ip(output)
                logger.info(
                    "[VERIFY][Static-Heal] %s - Desiderato: IP=%s Mask=%s GW=%s | Rilevato: IP=%s Mask=%s GW=%s",
                    host, desired_ip, desired_mask, desired_gw, real_ip, real_mask, real_gw
                )
                
                is_correct = (
                    _is_ip_equal(desired_ip, real_ip) and
                    _is_ip_equal(desired_mask, real_mask) and
                    _is_ip_equal(desired_gw, real_gw)
                )
                
                if not is_correct:
                    logger.warning("[VERIFY][Static-Heal] Configurazione errata su %s! Applico correzione...", host)
                    cmd = f"ip {desired_ip} {desired_mask} {desired_gw}".strip()
                    logger.warning("  [%s] Eseguo: %s", host, cmd)
                    await conn.send_command(cmd)
                    await asyncio.sleep(0.5)
                    await conn.send_command("save")
                    await asyncio.sleep(0.5)
                    
                    logger.info("[VERIFY][Static-Heal] Configurazione aggiornata e salvata su %s.", host)
                    any_corrected = True
                    need_snapshot = True
                else:
                    logger.info("[VERIFY][Static-Heal] Configurazione su %s è già corretta.", host)
            
            # Aggiorna lo snapshot del dispositivo fuori dal lock context manager
            if need_snapshot:
                await snapshot_device(host, cfg, store)
        except Exception as e:
            logger.error("[VERIFY][Static-Heal] Errore durante verifica/correzione su %s: %s", host, e)
            
    return any_corrected


# ─────────────────────────────────────────────────────────────────────────────
# Nodo principale
# ─────────────────────────────────────────────────────────────────────────────

async def verify_node(state: AgentState) -> dict:
    logger.info(">>> VERIFY <<<")

    attempt = state.get("troubleshoot_attempt", 0)
    if attempt > 0:
        logger.info("[VERIFY] Rilevato tentativo di troubleshooting #%d. Attesa 15 secondi...", attempt)
        await asyncio.sleep(15.0)

    inventory       = load_inventory()
    router_commands = state.get("router_commands", {})
    async with AsyncNetworkGraphStore() as store:
        spec_raw        = state.get("specification_raw", "")
        intent          = state.get("intent")

        dhcp_pcs:   list[str] = []
        static_pcs: list[str] = []
        static_vpcs_desired_config: dict[str, dict] = {}
        network_device_ips: dict[str, list[str]] = {}

        # Parsing robusto sia per il formato YAML che per il formato legacy testuale
        yaml_parsed = False
        try:
            if spec_raw.strip().startswith("{") or "devices:" in spec_raw:
                parsed_data = yaml.safe_load(spec_raw)
                if isinstance(parsed_data, dict) and "devices" in parsed_data:
                    for dev in parsed_data.get("devices", []):
                        name = dev.get("name")
                        profile = dev.get("profile", "")
                        if not name:
                            continue
                        if profile == "vpcs" or "pc" in name.lower():
                            is_dhcp = False
                            static_ip = None
                            static_mask = "255.255.255.0"
                            for iface in dev.get("interfaces", []):
                                ip_val = iface.get("ip")
                                if ip_val:
                                    if str(ip_val).lower() == "dhcp":
                                        is_dhcp = True
                                        break
                                    else:
                                        if "/" in str(ip_val):
                                            static_ip, cidr_str = str(ip_val).split("/", 1)
                                            try:
                                                static_mask = str(ipaddress.IPv4Network(f"0.0.0.0/{cidr_str}").netmask)
                                            except Exception:
                                                pass
                                        else:
                                            static_ip = str(ip_val)
                            if is_dhcp:
                                dhcp_pcs.append(name)
                            else:
                                static_pcs.append(name)
                                gateway = ""
                                routes = dev.get("static_routes", [])
                                if routes:
                                    gateway = routes[0].get("next_hop", "")
                                if static_ip:
                                    static_vpcs_desired_config[name] = {
                                        "ip": static_ip,
                                        "mask": static_mask,
                                        "gateway": gateway
                                    }
                    yaml_parsed = True
                    logger.info("[VERIFY][Debug] Specifica parsata con successo come YAML strutturato.")
        except Exception as e:
            logger.debug("[VERIFY][Debug] Parsing YAML non riuscito (provo fallback legacy): %s", e)

        if not yaml_parsed:
            logger.info("[VERIFY][Debug] Specifica in formato legacy o non YAML strutturato. Eseguo parsing testuale...")
            for block in re.split(r'---\s*DEVICE:', spec_raw)[1:]:
                lines = block.strip().splitlines()
                if not lines:
                    continue
                name = lines[0].replace("-", "").strip()
                if "pc" in name.lower() or "vpcs" in block.lower():
                    if re.search(r'IP_ADDRESS:\s*DHCP', block, re.IGNORECASE):
                        dhcp_pcs.append(name)
                    else:
                        static_pcs.append(name)
                        ip_match = re.search(r'IP_ADDRESS:\s*([\d\.]+)', block, re.IGNORECASE)
                        mask_match = re.search(r'NETMASK:\s*([\d\.]+)', block, re.IGNORECASE)
                        gw_match = re.search(r'GATEWAY:\s*([\d\.]+)', block, re.IGNORECASE)
                        
                        static_ip = ip_match.group(1) if ip_match else None
                        static_mask = mask_match.group(1) if mask_match else "255.255.255.0"
                        gateway = gw_match.group(1) if gw_match else ""
                        
                        if static_ip:
                            static_vpcs_desired_config[name] = {
                                "ip": static_ip,
                                "mask": static_mask,
                                "gateway": gateway
                            }

        touched        = list(router_commands.keys())
        infra          = [d for d in touched if d not in dhcp_pcs and d not in static_pcs]
        touched_dhcp   = [d for d in touched if d in dhcp_pcs]
        touched_static = [d for d in touched if d in static_pcs]

        logger.info("[VERIFY][Debug] Classificazione dispositivi:")
        logger.info("  - DHCP PCs rilevati nella spec: %s", dhcp_pcs)
        logger.info("  - Static PCs rilevati nella spec: %s", static_pcs)
        logger.info("  - Dispositivi toccati dalla config: %s", touched)
        logger.info("    * Infrastruttura (Core): %s", infra)
        logger.info("    * Host DHCP toccati: %s", touched_dhcp)
        logger.info("    * Host Statici toccati: %s", touched_static)

        await _post_execute_graph_sync(
            infra, touched_dhcp, touched_static, inventory, store
        )
        await store.clear_inactive_interfaces()
        await store.compute_topology_links()

        live_map = await _fetch_live_ips_from_graph(store)
        
        # Auto-remediation static VPCS config healing
        missing_static = [pc for pc in touched_static if pc not in live_map]
        corrected = await _heal_static_vpcs_if_needed(
            static_vpcs_desired_config, live_map, missing_static, inventory, store
        )
        if corrected:
            logger.info("[VERIFY] Auto-correzione applicata a VPCS statici. Ricalcolo topologia...")
            await store.compute_topology_links()
            live_map = await _fetch_live_ips_from_graph(store)
        network_device_ips = await _fetch_network_device_ips(store)


    logger.info("[VERIFY] IP live nel grafo: %s", live_map)


    # 1. PESSIMISTIC VERIFICATION (Data Plane)
    missing_dhcp   = [pc for pc in touched_dhcp   if pc not in live_map]
    missing_static = [pc for pc in touched_static if pc not in live_map]

    degraded_msg = None
    if missing_dhcp or missing_static:
        errs = []
        if missing_dhcp:   errs.append(f"DHCP fallito per {missing_dhcp}")
        if missing_static: errs.append(f"IP statico assente per {missing_static}")
        degraded_msg = "VERIFY WARNING: " + " | ".join(errs) + " — Nodi ignorati nei ping."
        logger.error(degraded_msg)

    matrix = _build_validation_matrix(live_map, dev_ips=network_device_ips)

    results = []
    matrix_success = True
    log_lines: list[str] = []

    if matrix:
        session_locks = {src: asyncio.Lock() for src, _ in matrix}
        ping_tasks = []
        for src, dst_ip in matrix:
            src_cfg = inventory.get(src)
            if not src_cfg:
                continue
            ping_tasks.append(_run_ping(src, src_cfg, dst_ip, session_locks[src]))

        results = await asyncio.gather(*ping_tasks, return_exceptions=True)

        for res in results:
            if isinstance(res, Exception):
                log_lines.append(f"VERIFY EXCEPTION: {res}")
                matrix_success = False
            else:
                src, target_ip, ok = res
                if ok:
                    log_lines.append(f"VERIFY SUCCESS: 🟢 {src} -> {target_ip} OK")
                else:
                    log_lines.append(f"VERIFY FAILED: 🔴 {src} -> {target_ip} LOSS")
                    matrix_success = False

    # 2. Control Plane Assurance (VLAN, DHCP pool, Etherchannel)
    assurance_errors = {}
    plan_for_assurance = state.get("plan")
    if plan_for_assurance and hasattr(plan_for_assurance, "devices"):
        for device_intent in plan_for_assurance.devices:
            # Esegui controlli solo sui dispositivi infrastruttura attivi e raggiungibili
            dev_name = device_intent.name
            reachability_status = state.get("reachability", {}).get(dev_name)
            if reachability_status == "REACHABLE" and dev_name not in dhcp_pcs and dev_name not in static_pcs:
                errs = await _verify_control_plane(dev_name, device_intent, inventory)
                if errs:
                    assurance_errors[dev_name] = errs
                    for e in errs:
                        log_lines.append(f"ASSURANCE FAILED [{dev_name}]: {e}")

    # 3. Estrazione failed_devices per TROUBLESHOOT / Rollback
    failed_devices = _extract_failed_devices(
        ping_results=list(results),
        missing_dhcp=missing_dhcp,
        missing_static=missing_static,
        inventory=inventory,
        live_map=live_map,
        dev_ips=network_device_ips,
    )
    # Aggiungi i device che hanno fallito il controllo di assurance
    for dev in assurance_errors.keys():
        if dev not in failed_devices:
            failed_devices.append(dev)

    is_fully_successful = matrix_success and not degraded_msg and not assurance_errors
    protocol_label = getattr(intent, "protocol", "Static") if intent else "Static"

    if is_fully_successful:
        logger.info("[VERIFY] Validazione PASSED.")
        ping_total = len([r for r in results if not isinstance(r, Exception)])
        ping_passed = sum(1 for r in results if not isinstance(r, Exception) and r[2])
        is_first_try = (state.get("troubleshoot_attempt", 0) == 0)
        metrics.record_verification(
            ping_total=ping_total,
            ping_passed=ping_passed,
            control_plane_errors=0,
            is_first_try=is_first_try,
            is_success=True,
        )
        return {
            "final_status":   "SUCCESS",
            "failed_devices": [],
            "execution_log":  [
                f"VERIFY: {protocol_label} Cross-Validation PASSED"
            ] + log_lines,
        }
    else:
        logger.error("[VERIFY] Validazione FAILED (Ping loss, nodi isolati o anomalie di assurance).")
        
        # Metriche di verifica
        ping_total = len([r for r in results if not isinstance(r, Exception)])
        ping_passed = sum(1 for r in results if not isinstance(r, Exception) and r[2])
        is_first_try = (state.get("troubleshoot_attempt", 0) == 0)
        metrics.record_verification(
            ping_total=ping_total,
            ping_passed=ping_passed,
            control_plane_errors=len(assurance_errors),
            is_first_try=is_first_try,
            is_success=False,
        )

        attempt = state.get("troubleshoot_attempt", 0)
        from nodes.troubleshoot import MAX_ATTEMPTS
        
        # Eseguiamo il rollback solo alla fine del ciclo di troubleshooting
        if attempt >= MAX_ATTEMPTS:
            # Esegui rollback condizionale
            rollback_scope = "all"
            if intent and hasattr(intent, "rollback_scope"):
                rollback_scope = intent.rollback_scope

            executed_commands = state.get("executed_commands") or {}
            log_lines.append(f"[ROLLBACK] Avvio rollback condizionale (scope={rollback_scope})...")
            
            if rollback_scope == "device-only":
                for dev in failed_devices:
                    if dev in executed_commands:
                        ok = await _rollback_device(dev, executed_commands[dev], inventory)
                        log_lines.append(f"[ROLLBACK] Rollback {dev}: {'SUCCESS' if ok else 'FAILED'}")
            else: # "all"
                for dev in reversed(list(executed_commands.keys())):
                    ok = await _rollback_device(dev, executed_commands[dev], inventory)
                    log_lines.append(f"[ROLLBACK] Rollback {dev}: {'SUCCESS' if ok else 'FAILED'}")
        else:
            log_lines.append(f"[ROLLBACK] Tentativo #{attempt} fallito. Rollback rimandato per consentire il troubleshooting della configurazione attiva.")

        return {
            "final_status":   "FAILED",
            "failed_devices": failed_devices,
            "execution_log":  [
                f"VERIFY: {protocol_label} Cross-Validation DEGRADED"
            ] + log_lines,
        }
