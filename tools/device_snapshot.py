# tools/device_snapshot.py
"""
Snapshot di stato di un singolo dispositivo di rete.

v3.0 — Novità rispetto alla v2.1:

  • Separazione configurazione pesante:
      Il running-config viene persisto in DeviceConfig (via store_running_config)
      separatamente dal nodo Device. Le chiamate di topologia non lo toccano.

  • Vista L2 — Discovery fisica CDP/LLDP:
      _collect_l2_neighbors() tenta prima CDP poi LLDP.
      I neighbor trovati vengono salvati via store.store_l2_neighbors().
      Dopo tutti gli snapshot, compute_l2_topology() aggiunge i link
      inferiti per le interfacce rimaste orfane (reti black box).

  • Fallback automatico:
      Se CDP e LLDP non sono disponibili (vendor non supportato o comando
      assente), il dispositivo viene processato normalmente: i link L2
      verranno inferiti da compute_l2_topology() in base alle subnet.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from dataclasses import dataclass, field

from tools.connection import get_connection
from tools.template_engine import parser as output_parser
from tools.graph_store import AsyncNetworkGraphStore
from tools.parser import resolve_vendor

logger = logging.getLogger(__name__)


@dataclass
class DeviceSnapshot:
    router_name:        str
    vendor:             str
    interfaces:         dict[str, str]          # iface_name -> ip/cidr o "unassigned"
    static_routes:      dict[str, str]          # "network/cidr" -> next_hop
    running_config:     str
    operational_status: str                     = ""
    reachable:          bool                    = True
    etherchannels:      dict[str, list[str]] | None = None
    l2_neighbors:       list[dict]              = field(default_factory=list)
    l2_source:          str                     = "none"   # "cdp", "lldp", "none"
    vlans:              dict[int, str]          = field(default_factory=dict)
    interface_l2:       dict[str, dict]         = field(default_factory=dict)
    interface_statuses: dict[str, str]         = field(default_factory=dict)


# =============================================================================
# Entry point pubblico
# =============================================================================

async def snapshot_device(
    router_name: str,
    cfg: dict,
    store: AsyncNetworkGraphStore,
) -> DeviceSnapshot | None:
    vendor = resolve_vendor(cfg, router_name)
    host   = cfg.get("host", "127.0.0.1")
    port   = cfg.get("port")

    if not port:
        logger.error("[%s] Porta mancante in devices.yaml.", router_name)
        return None

    try:
        async with get_connection(cfg) as conn:
            snapshot = await _collect_state(router_name, vendor, conn)

    except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
        logger.warning(
            "[%s] Non raggiungibile (%s: %s). Marco come STALE.",
            router_name, type(e).__name__, e,
        )
        await store.upsert_device(
            name=router_name, vendor=vendor,
            mgmt_ip=f"{host}:{port}", status="UNREACHABLE", confidence="STALE",
        )
        await store.delete_operational_status(router_name)
        return None
    except Exception as e:
        logger.error("[%s] Errore inatteso snapshot: %s", router_name, e, exc_info=True)
        return None

    # ── Sync Neo4j — Device ──────────────────────────────────────────────────
    synthetic_status = build_synthetic_status(snapshot)
    await store.upsert_device(
        name=router_name, vendor=vendor,
        mgmt_ip=f"{host}:{port}", status="REACHABLE", confidence="FRESH",
        operational_status=synthetic_status,
    )

    if snapshot.operational_status:
        await store.store_operational_status(router_name, snapshot.operational_status)

    # ── Sync Neo4j — VLANs (globali) ─────────────────────────────────────────
    if snapshot.vlans:
        await store.clear_vlans(router_name)
        for vid, vname in snapshot.vlans.items():
            await store.upsert_vlan(router_name, vid, vname)

    # ── Sync Neo4j — L1: Port fisici con stato operativo ─────────────────────
    # Popola i nodi Port L1 con oper_status reale (up/down).
    # Questo layer era assente: interface_statuses veniva usato solo per
    # set-are lo status sulle Interface L3, ma non creava nodi Port distinti.
    for iface_name, oper_status in snapshot.interface_statuses.items():
        await store.upsert_port(
            device=router_name,
            port_name=iface_name,
            oper_status=oper_status,
            admin_status="down" if oper_status == "administratively down" else "up",
        )

    # ── Sync Neo4j — L3: Interfacce con IP ───────────────────────────────────
    for iface_name, ip in snapshot.interfaces.items():
        l2_props = snapshot.interface_l2.get(iface_name, {})
        vid = l2_props.get("vlan_id")
        status = snapshot.interface_statuses.get(iface_name, "up")
        await store.upsert_interface(router_name, iface_name, ip, status, vlan_id=vid)

    # ── Sync Neo4j — Management IP switch (fix bug "switch isolato") ─────────
    # Il bug: per cisco_switch, show ip interface brief spesso non mostra
    # le SVI (Vlan99, Vlan1 ecc.) perché non hanno routing abilitato.
    # Risultato: snapshot.interfaces è vuoto → lo switch non ha IP nel grafo
    # → CONNECTED_TO non viene creato → switch isolato in L3.
    # Fix: estrae TUTTE le SVI con IP dal running-config, non solo Vlan1.
    if vendor == "cisco_switch" and snapshot.running_config:
        svi_ips = _extract_all_svi_ips(snapshot.running_config)
        for svi_name, svi_ip in svi_ips.items():
            if svi_name not in snapshot.interfaces:
                logger.info(
                    "[%s] SVI %s estratta dal running-config: %s",
                    router_name, svi_name, svi_ip,
                )
                await store.upsert_interface(router_name, svi_name, svi_ip, "up")
                snapshot.interfaces[svi_name] = svi_ip

    # ── Sync Neo4j — L2: Proprietà switchport ────────────────────────────────
    for iface_name, l2_props in snapshot.interface_l2.items():
        if iface_name not in snapshot.interfaces:
            status = snapshot.interface_statuses.get(iface_name, "up")
            await store.upsert_interface(
                router_name, iface_name, "unassigned", status,
                vlan_id=l2_props.get("vlan_id"),
            )

        await store.upsert_l2_properties_and_vlans(
            device=router_name,
            iface=iface_name,
            mode=l2_props.get("mode"),
            access_vlan=l2_props.get("access_vlan"),
            trunk_vlans=l2_props.get("trunk_vlans"),
            native_vlan=l2_props.get("native_vlan"),
            stp_state=l2_props.get("stp_state"),
            vlan_id=l2_props.get("vlan_id"),
        )

    # ── Sync Neo4j — EtherChannel ─────────────────────────────────────────────
    if snapshot.etherchannels:
        for po_name, members in snapshot.etherchannels.items():
            await store.upsert_etherchannel_members(router_name, po_name, members)

    # ── Sync Neo4j — DHCP Pools ───────────────────────────────────────────────
    if vendor in ("frrouting", "cisco_ios"):
        await store.clear_dhcp_pools(router_name)
        if snapshot.running_config:
            from tools.dhcp_config import DhcpStateInspector
            try:
                pools = DhcpStateInspector().parse_running_config(snapshot.running_config)
                for pool_name, info in pools.items():
                    if info.get("network") and info.get("netmask"):
                        try:
                            cidr = ipaddress.IPv4Network(
                                f"{info['network']}/{info['netmask']}", strict=False
                            )
                            net_cidr = str(cidr)
                        except ValueError:
                            net_cidr = f"{info['network']}/{info['netmask']}"
                        await store.upsert_dhcp_pool(
                            device=router_name,
                            pool_name=pool_name.upper(),
                            network=net_cidr,
                            default_router=info.get("router") or "",
                            dns_server=info.get("dns") or "8.8.8.8",
                        )
            except Exception as e:
                logger.error("[%s] Errore salvataggio pool DHCP: %s", router_name, e)

    # ── Sync Neo4j — Rotte statiche ───────────────────────────────────────────
    if vendor in ("frrouting", "cisco_ios", "cisco_switch"):
        await store.clear_static_routes(router_name)
        for network, next_hop in snapshot.static_routes.items():
            await store.upsert_static_route(router_name, network, next_hop)

    # ── Sync Neo4j — Config satellite (mai nelle query topologiche) ───────────
    if vendor != "vpcs" and snapshot.running_config:
        await store.store_running_config(router_name, snapshot.running_config)

    # ── Sync Neo4j — Link L1/L2 (CDP/LLDP + statici da devices.yaml) ─────────
    # NON cancellare i link "spec" seedati da observe_node prima dello snapshot:
    # cancella solo i link CDP/LLDP/inferred del device corrente.
    static_links = cfg.get("l2_links", [])
    has_live_l2  = bool(snapshot.l2_neighbors or static_links)

    if has_live_l2:
        # clear_l2_links cancella solo PHYSICALLY_CONNECTED_TO e CABLED_TO
        # con source != "spec" per non perdere i link dell'intent YAML.
        await _clear_non_spec_links(store, router_name)

    if snapshot.l2_neighbors:
        await store.store_l2_neighbors(
            device=router_name,
            neighbors=snapshot.l2_neighbors,
            source=snapshot.l2_source,
        )

    # Link statici da devices.yaml per device senza CDP (es. VPCS):
    #   PC1:
    #     vendor: vpcs
    #     l2_links:
    #       - local_iface: eth0
    #         remote_device: SW1
    #         remote_iface: Ethernet0/1
    if static_links:
        await store.store_static_l2_links(router_name, static_links)

    # ── Sync Neo4j — Cleanup Stale Interfaces & Ports ─────────────────────────
    # Rimuove interfacce e porte fisiche che non sono state rilevate in questo snapshot.
    # Questo evita di mantenere nodi residui da configurazioni o laboratori precedenti.
    active_ifaces = set(snapshot.interfaces.keys()) | set(snapshot.interface_l2.keys()) | set(snapshot.interface_statuses.keys())
    active_normalized = {store._normalize_port(name, router_name) for name in active_ifaces if name}
    if active_normalized:
        await store._run(
            """
            MATCH (d:Device {name: $device})-[:HAS_INTERFACE]->(i:Interface)
            WHERE NOT i.name IN $active_names
            DETACH DELETE i
            """,
            device=router_name,
            active_names=list(active_normalized),
        )
        await store._run(
            """
            MATCH (d:Device {name: $device})-[:HAS_PORT]->(p:Port)
            WHERE NOT p.name IN $active_names
            DETACH DELETE p
            """,
            device=router_name,
            active_names=list(active_normalized),
        )

    return snapshot


# =============================================================================
# Raccolta stato dal dispositivo
# =============================================================================

async def _collect_state(router_name: str, vendor: str, conn) -> DeviceSnapshot:
    def _is_cli_error(output: str) -> bool:
        lo = (output or "").lower()
        explicit_errors = (
            "% invalid input",
            "% unknown command",
            "% incomplete command",
            "% ambiguous command",
            "% command rejected",
            "% bad ip address",
            "% bad mask",
            "% too many parameters",
            "% mode not valid",
            "command unknown",
            "error:",
            "malformed command",
        )
        return any(m in lo for m in explicit_errors)

    # 1. Disabilita paginazione
    if vendor in ("cisco_ios", "cisco_switch", "frrouting"):
        await conn.send_command("terminal length 0")

    # 2. Interfacce via TextFSM (con fallback regex automatico)
    cmd_iface  = _iface_command(vendor)
    raw_iface  = await conn.send_command(cmd_iface)
    parsed     = await asyncio.to_thread(output_parser.parse_interfaces, raw_iface, vendor)
    interfaces = {i["name"]: i["ip"] for i in parsed}
    interface_statuses = {i["name"]: i["status"] for i in parsed}

    # 3. Running config
    running_config = ""
    if vendor != "vpcs":
        running_config = await conn.send_command("show running-config", timeout=15.0)

    if running_config and vendor in ("cisco_ios", "cisco_switch"):
        interfaces = _enrich_interfaces_with_cidr(running_config, interfaces)

    # 3.5. Operational status — comandi separati per vendor.
    # cisco_ios usa comandi router: show ip arp, show ip route.
    # cisco_switch usa comandi L2: trunk, etherchannel, STP, MAC table.
    # I comandi switch su un router IOS producono "% Invalid input" —
    # li filtriamo a monte scegliendo il set corretto per vendor.
    operational_status = ""

    _ROUTER_OP_CMDS = [
        "show ip arp",
        "show ip route",
    ]
    _SWITCH_OP_CMDS = [
        "show vlan brief",
        "show interfaces trunk",
        "show etherchannel summary",
        "show spanning-tree",
        "show ip dhcp binding",
        "show mac address-table",
    ]

    if vendor == "cisco_switch":
        op_cmds = _SWITCH_OP_CMDS
    elif vendor == "cisco_ios":
        op_cmds = _ROUTER_OP_CMDS
    else:
        op_cmds = []

    if op_cmds:
        op_outputs = []
        for op_cmd in op_cmds:
            try:
                out = await conn.send_command(op_cmd)
                if out and not _is_cli_error(out):
                    op_outputs.append(f"--- {op_cmd} ---\n{out}")
                else:
                    logger.debug("[%s] Comando non supportato o vuoto: %s", router_name, op_cmd)
            except Exception as e:
                logger.debug("[%s] Failed to run %s: %s", router_name, op_cmd, e)
        operational_status = "\n\n".join(op_outputs)

    # Extraction of VLANs and L2/STP properties
    vlans: dict[int, str] = {}
    interface_l2: dict[str, dict] = {}
    stp_data: dict[str, dict[str, str]] = {}

    if vendor == "cisco_switch":
        try:
            raw_vlan = await conn.send_command("show vlan brief")
            for line in raw_vlan.splitlines():
                m = re.match(r'^\s*(\d+)\s+(\S+)\s+(active|act/uns|suspend)', line, re.IGNORECASE)
                if m:
                    vlans[int(m.group(1))] = m.group(2)
        except Exception as e:
            logger.debug("[%s] Failed to run show vlan brief: %s", router_name, e)

        try:
            raw_stp = await conn.send_command("show spanning-tree")
            stp_data = _parse_spanning_tree(raw_stp)
        except Exception as e:
            logger.debug("[%s] Failed to run show spanning-tree: %s", router_name, e)

    if running_config:
        # Fallback/additional vlan parsing from running-config
        for vid, vname in re.findall(r"(?m)^vlan\s+(\d+)\s*\n\s*name\s+(\S+)", running_config):
            vlans[int(vid)] = vname
        interface_l2 = _parse_switchports(running_config, vendor, stp_data)

    # 4. Rotte statiche
    static_routes: dict[str, str] = {}

    if vendor == "frrouting":
        for net, nh in re.findall(
            r"(?m)^\s*ip route\s+([\d\.]+/\d+)\s+([\d\.]+)",
            running_config,
        ):
            if net in static_routes:
                existing = [x.strip() for x in static_routes[net].split(",")]
                if nh not in existing:
                    static_routes[net] = static_routes[net] + "," + nh
            else:
                static_routes[net] = nh

    elif vendor in ("cisco_ios", "cisco_switch"):
        for net, mask, nh in re.findall(
            r"(?m)^\s*ip route\s+([\d\.]+)\s+([\d\.]+)\s+([\d\.]+)",
            running_config,
        ):
            try:
                prefix = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
                key = f"{net}/{prefix}"
                if key in static_routes:
                    existing = [x.strip() for x in static_routes[key].split(",")]
                    if nh not in existing:
                        static_routes[key] = static_routes[key] + "," + nh
                else:
                    static_routes[key] = nh
            except ValueError:
                logger.warning(
                    "[%s] Rotta non parsabile: %s %s %s", router_name, net, mask, nh
                )

        if vendor == "cisco_switch":
            # Per gli switch L2/L3, se configurato, catturiamo 'ip default-gateway' come rotta di default 0.0.0.0/0
            gw_match = re.search(r"(?m)^\s*ip default-gateway\s+([\d\.]+)", running_config)
            if gw_match:
                static_routes["0.0.0.0/0"] = gw_match.group(1)

    # 5. Arricchimento CIDR per Cisco IOS
    if vendor == "cisco_ios" and running_config:
        for iface_name in list(interfaces.keys()):
            ip_val = interfaces[iface_name]
            if ip_val == "unassigned" or "/" in ip_val:
                continue
            m = re.search(
                rf"ip address\s+{re.escape(ip_val)}\s+([\d\.]+)", running_config
            )
            if m:
                mask_val = m.group(1)
                try:
                    cidr = ipaddress.IPv4Network(f"0.0.0.0/{mask_val}").prefixlen
                    interfaces[iface_name] = f"{ip_val}/{cidr}"
                except ValueError:
                    pass

    # 6. EtherChannel
    etherchannels: dict[str, list[str]] = {}
    if vendor in ("cisco_ios", "cisco_switch") and running_config:
        for iface_raw, block in re.findall(
            r"(?ms)^interface\s+(\S+)\s*\n(.*?)(?=^interface\s+\S+|^end\s*$|\Z)",
            running_config,
        ):
            cg_m = re.search(r"channel-group\s+(\d+)", block, re.IGNORECASE)
            if cg_m:
                po_id = cg_m.group(1)
                from tools.parser import normalize_interface_name
                po_name   = normalize_interface_name(f"Port-channel{po_id}")
                norm_iface = normalize_interface_name(iface_raw)
                etherchannels.setdefault(po_name, []).append(norm_iface)

    # 7. Discovery L2 — CDP poi LLDP (fallback)
    l2_neighbors: list[dict] = []
    l2_source = "none"

    if vendor in ("cisco_ios", "cisco_switch"):
        l2_neighbors, l2_source = await _collect_l2_neighbors_cisco(
            router_name, conn
        )
    elif vendor == "frrouting":
        l2_neighbors, l2_source = await _collect_l2_neighbors_frr(
            router_name, conn
        )

    return DeviceSnapshot(
        router_name=router_name,
        vendor=vendor,
        interfaces=interfaces,
        static_routes=static_routes,
        running_config=running_config,
        operational_status=operational_status,
        etherchannels=etherchannels,
        l2_neighbors=l2_neighbors,
        l2_source=l2_source,
        vlans=vlans,
        interface_l2=interface_l2,
        interface_statuses=interface_statuses,
    )


# =============================================================================
# Discovery L2 — Cisco (CDP → LLDP)
# =============================================================================

async def _collect_l2_neighbors_cisco(
    router_name: str, conn
) -> tuple[list[dict], str]:
    """
    Tenta CDP (detail poi plain), poi LLDP, per i device Cisco IOS e Cisco Switch.
    Restituisce (neighbors, source) dove source è "cdp", "lldp" o "none".
    """
    # ── Tentativo CDP detail ─────────────────────────────────────────────────
    try:
        raw = await conn.send_command("show cdp neighbors detail")
        neighbors = _parse_cdp_detail(raw)
        if neighbors:
            logger.info("[%s] CDP detail: trovati %d neighbor.", router_name, len(neighbors))
            return neighbors, "cdp"
    except Exception as e:
        logger.debug("[%s] CDP detail non disponibile: %s", router_name, e)

    # ── Fallback CDP plain ───────────────────────────────────────────────────
    try:
        raw = await conn.send_command("show cdp neighbors")
        neighbors = _parse_cdp_detail(raw)   # il parser gestisce entrambi i formati
        if neighbors:
            logger.info("[%s] CDP plain: trovati %d neighbor.", router_name, len(neighbors))
            return neighbors, "cdp"
    except Exception as e:
        logger.debug("[%s] CDP plain non disponibile: %s", router_name, e)

    # ── Tentativo LLDP ───────────────────────────────────────────────────────
    try:
        raw = await conn.send_command("show lldp neighbors detail")
        neighbors = _parse_lldp_detail(raw)
        if neighbors:
            logger.info("[%s] LLDP: trovati %d neighbor.", router_name, len(neighbors))
            return neighbors, "lldp"
    except Exception as e:
        logger.debug("[%s] LLDP non disponibile: %s", router_name, e)

    logger.debug("[%s] Nessun neighbor L2 CDP/LLDP trovato.", router_name)
    return [], "none"


async def _collect_l2_neighbors_frr(
    router_name: str, conn
) -> tuple[list[dict], str]:
    """
    FRRouting non supporta CDP. Tenta LLDP tramite lldpctl se disponibile
    (comune in ambienti Linux con lldpd in esecuzione).
    """
    try:
        raw = await conn.send_command("show lldp neighbor detail")
        neighbors = _parse_lldp_detail(raw)
        if neighbors:
            logger.info(
                "[%s] LLDP (FRR): trovati %d neighbor.", router_name, len(neighbors)
            )
            return neighbors, "lldp"
    except Exception as e:
        logger.debug("[%s] LLDP FRR non disponibile: %s", router_name, e)

    return [], "none"


# =============================================================================
# Parser CDP / LLDP
# =============================================================================

def _strip_domain(device_id: str) -> str:
    """
    Rimuove il suffisso FQDN dal Device ID CDP/LLDP.
    Es: 'SW2.lab.local' → 'SW2', 'R2.lab.local' → 'R2'.
    Se il nome non ha punti lo restituisce invariato.
    """
    return device_id.split(".")[0]


def is_valid_interface_name(name: str) -> bool:
    if not name:
        return False
    return bool(re.match(r'^(Ethernet|FastEthernet|GigabitEthernet|Serial|Vlan|Port-channel|Po|Loopback|Lo|e|fa|gi|se|po|eth|ens|eno|enp)\d+([\w/.-]*)$', name, re.IGNORECASE))


def _parse_cdp_detail(raw: str) -> list[dict]:
    """
    Parsa l'output di 'show cdp neighbors detail'.

    Gestisce due formati:
      1. Blocchi separati da trattini (show cdp neighbors detail)
      2. Tabella a colonne fisse (show cdp neighbors) — fallback automatico

    Normalizza sempre il Device ID rimuovendo il dominio FQDN
    (es. 'SW2.lab.local' → 'SW2') per garantire il match con i nomi
    in devices.yaml.
    """
    neighbors: list[dict] = []

    # ── Formato detail (blocchi separati da trattini) ────────────────────────
    blocks = re.split(r"-{10,}", raw)
    for block in blocks:
        device_m = re.search(r"Device ID:\s*(\S+)",                        block)
        local_m  = re.search(r"Interface:\s*([\w/\.]+)",                   block)
        remote_m = re.search(r"Port ID\s*\(outgoing port\):\s*([\w/\.]+)", block)

        if device_m and local_m and remote_m:
            local_iface = local_m.group(1).strip()
            remote_iface = remote_m.group(1).strip()
            if is_valid_interface_name(local_iface) and is_valid_interface_name(remote_iface):
                neighbors.append({
                    "local_iface":   local_iface,
                    "remote_device": _strip_domain(device_m.group(1).strip()),
                    "remote_iface":  remote_iface,
                })

    if neighbors:
        return neighbors

    # ── Fallback: formato tabellare (show cdp neighbors) ─────────────────────
    # Esempio riga:
    # SW2.lab.local    Eth 0/1    154    S I    Linux Uni    Eth 0/0
    # Il formato ha: DeviceID, LocalIface (su 2 token), Holdtime,
    # Capability, Platform (2 token), PortID (2 token)
    for line in raw.splitlines():
        stripped = line.strip()
        # Salta header, righe vuote e le righe esplicative di capability (es. "D - Remote")
        if not stripped or stripped.lower().startswith("capability") or stripped.lower().startswith("device id") or " - " in stripped:
            continue
        tokens = stripped.split()
        if len(tokens) < 6:
            continue
        # Il Device ID è sempre il primo token
        remote_device = _strip_domain(tokens[0])
        # Local interface: es. "Eth 0/1" → due token
        local_iface  = _normalize_cdp_iface(tokens[1] + tokens[2])
        # Port ID: ultimi due token della riga
        remote_iface = _normalize_cdp_iface(tokens[-2] + tokens[-1])
        if is_valid_interface_name(local_iface) and is_valid_interface_name(remote_iface):
            neighbors.append({
                "local_iface":   local_iface,
                "remote_device": remote_device,
                "remote_iface":  remote_iface,
            })

    return neighbors


def _normalize_cdp_iface(raw: str) -> str:
    """
    Normalizza le abbreviazioni di interfaccia CDP nel formato esteso.
    Es: 'Eth0/1' → 'Ethernet0/1', 'Gig0/0' → 'GigabitEthernet0/0'.
    Usato dal parser tabellare dove Cisco abbrevia i nomi.
    """
    prefixes = {
        "Eth": "Ethernet",
        "Gig": "GigabitEthernet",
        "Fas": "FastEthernet",
        "Ten": "TenGigabitEthernet",
        "Ser": "Serial",
        "Tun": "Tunnel",
        "Loo": "Loopback",
    }
    for short, full in prefixes.items():
        if raw.startswith(short):
            return full + raw[len(short):]
    return raw


def _parse_lldp_detail(raw: str) -> list[dict]:
    """
    Parsa l'output di 'show lldp neighbors detail'.

    Estrae:
      - System Name     → remote_device
      - Local Intf      → local_iface
      - Port ID         → remote_iface

    Esempio:
      ------------------------------------------------
      Local Intf: Gi0/0
      Chassis id: aabb.cc00.0200
      Port id: Gi0/1
      Port Description: GigabitEthernet0/1
      System Name: SW1
    """
    neighbors: list[dict] = []
    blocks = re.split(r"-{10,}", raw)

    for block in blocks:
        local_m   = re.search(r"Local\s+Intf(?:erface)?:\s*([\w/\.]+)",    block, re.IGNORECASE)
        remote_m  = re.search(r"Port\s+[Ii][Dd]:\s*([\w/\.]+)",            block, re.IGNORECASE)
        system_m  = re.search(r"System\s+Name:\s*(\S+)",                   block, re.IGNORECASE)

        if local_m and remote_m and system_m:
            local_iface = local_m.group(1).strip()
            remote_iface = remote_m.group(1).strip()
            if is_valid_interface_name(local_iface) and is_valid_interface_name(remote_iface):
                neighbors.append({
                    "local_iface":   local_iface,
                    "remote_device": system_m.group(1).strip(),
                    "remote_iface":  remote_iface,
                })

    return neighbors


# =============================================================================
# Utility
# =============================================================================

def _iface_command(vendor: str) -> str:
    if vendor == "vpcs":
        return "show ip"
    if vendor in ("cisco_ios", "cisco_switch"):
        return "show ip interface brief"
    return "show interface brief"



def _extract_switch_mgmt_ip(running_config: str) -> str | None:
    """
    Compatibilità: estrae il primo IP di management da uno switch.
    """
    svis = _extract_all_svi_ips(running_config)
    # Sort by numeric VLAN ID, preferring non-Vlan1 (management SVI)
    def _vlan_sort(item):
        m = re.match(r"Vlan(\d+)", item[0])
        vid = int(m.group(1)) if m else 9999
        return (vid == 1, vid)
    for name, ip in sorted(svis.items(), key=_vlan_sort):
        return ip
    return None


def _extract_all_svi_ips(running_config: str) -> dict[str, str]:
    """
    Estrae TUTTE le SVI con IP dal running-config dello switch.
    Fix del bug "switch isolato": show ip interface brief non mostra
    le SVI degli switch L2 puri, quindi le estraiamo dal running-config.
    Es: {"Vlan99": "192.168.99.2/28", "Vlan1": "192.168.1.1/24"}
    """
    result: dict[str, str] = {}
    pattern = re.compile(
        r"(?ms)^interface\s+Vlan(\d+)\s*\n(.*?)(?=^interface\s+\S+|^end\s*$|\Z)"
    )
    for vlan_id, block in pattern.findall(running_config):
        if "no ip address" in block or "shutdown" in block:
            continue
        ip_m = re.search(r"ip address\s+([\d\.]+)\s+([\d\.]+)", block)
        if ip_m:
            ip   = ip_m.group(1)
            mask = ip_m.group(2)
            try:
                cidr = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
                result[f"Vlan{vlan_id}"] = f"{ip}/{cidr}"
            except ValueError:
                pass
    return result


def _enrich_interfaces_with_cidr(running_config: str, interfaces: dict[str, str]) -> dict[str, str]:
    """
    Scansiona il running-config per trovare la subnet mask reale di ciascuna interfaccia
    e aggiorna il dizionario degli IP aggiungendo il suffisso /CIDR.
    """
    pattern = re.compile(
        r"(?ms)^interface\s+([a-zA-Z0-9\/\.\-]+)\s*\n(.*?)(?=^interface\s+\S+|^router\s+\S+|^ip route|^end\s*$|\Z)"
    )
    for iface_raw, block in pattern.findall(running_config):
        from tools.parser import normalize_interface_name
        iface_name = normalize_interface_name(iface_raw)
        if iface_name in interfaces and interfaces[iface_name] != "unassigned":
            ip_m = re.search(r"ip address\s+([\d\.]+)\s+([\d\.]+)", block)
            if ip_m:
                ip   = ip_m.group(1)
                mask = ip_m.group(2)
                try:
                    cidr = ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
                    interfaces[iface_name] = f"{ip}/{cidr}"
                except ValueError:
                    pass
    return interfaces



async def _clear_non_spec_links(
    store,
    device: str,
) -> None:
    """
    Cancella link CDP/LLDP/inferred senza toccare i link "spec".
    I link spec rappresentano il cablaggio fisico dichiarato nella
    specifica YAML e sono la fonte di verita autoritativa.
    """
    await store._run(
        """
        MATCH (d:Device {name: $device})-[:HAS_INTERFACE]->(i:Interface)
        MATCH (i)-[r:PHYSICALLY_CONNECTED_TO]-()
        WHERE r.source IN ['cdp', 'lldp', 'inferred', 'none']
        DELETE r
        """,
        device=device,
    )
    await store._run(
        """
        MATCH (d:Device {name: $device})-[:HAS_PORT]->(p:Port)
        MATCH (p)-[r:CABLED_TO]-()
        WHERE r.source IN ['cdp', 'lldp', 'inferred', 'none']
        DELETE r
        """,
        device=device,
    )

def _parse_spanning_tree(stp_output: str) -> dict[str, dict[str, str]]:
    """
    Parsa l'output di 'show spanning-tree' e restituisce una mappa:
    {interface_name: {vlan_id: stp_status}}
    """
    iface_stp = {}
    if not stp_output:
        return iface_stp
    
    # Split by VLAN blocks
    parts = re.split(r'(?i)VLAN\s*0*(\d+)', stp_output)
    if len(parts) < 3:
        return iface_stp
        
    from tools.parser import normalize_interface_name
    
    for idx in range(1, len(parts), 2):
        vlan_id_str = parts[idx]
        block_text = parts[idx + 1]
        try:
            vlan_id = int(vlan_id_str)
        except ValueError:
            continue
            
        for line in block_text.splitlines():
            line = line.strip()
            # Match interface status lines
            m = re.match(r'^([a-zA-Z0-9/\-]+)\s+(\S+)\s+([A-Z]{3})\s+', line)
            if m:
                iface_raw = m.group(1)
                status = m.group(3)
                iface_name = normalize_interface_name(iface_raw)
                
                if iface_name not in iface_stp:
                    iface_stp[iface_name] = {}
                iface_stp[iface_name][str(vlan_id)] = status
                
    return iface_stp


def _parse_switchports(running_config: str, vendor: str, stp_data: dict) -> dict[str, dict]:
    """
    Parsa running_config per estrarre switchport mode, VLAN, ecc.
    """
    interface_l2 = {}
    if not running_config:
        return interface_l2
        
    from tools.parser import normalize_interface_name
    
    def parse_vlan_range(vlan_str: str) -> list[int]:
        vlans = []
        cleaned = re.sub(r'(?i)(add|remove|except|all|none)\s+', '', vlan_str).strip()
        for part in cleaned.split(','):
            part = part.strip()
            if not part:
                continue
            if '-' in part:
                try:
                    start, end = part.split('-')
                    vlans.extend(range(int(start), int(end) + 1))
                except ValueError:
                    pass
            else:
                try:
                    vlans.append(int(part))
                except ValueError:
                    pass
        return sorted(list(set(vlans)))

    for iface_raw, block in re.findall(
        r"(?ms)^interface\s+(\S+)\s*\n(.*?)(?=^interface\s+\S+|^end\s*$|\Z)",
        running_config,
    ):
        iface_name = normalize_interface_name(iface_raw)
        if iface_name.lower().startswith("null"):
            continue
            
        vlan_id = None
        if iface_name.startswith("Vlan"):
            vlan_m = re.match(r"^Vlan(\d+)$", iface_name)
            if vlan_m:
                vlan_id = int(vlan_m.group(1))
        else:
            encap_m = re.search(r"encapsulation dot1Q\s+(\d+)", block, re.IGNORECASE)
            if encap_m:
                vlan_id = int(encap_m.group(1))
                
        is_switchport = "switchport" in block
        has_ip = "ip address" in block
        
        mode = None
        if vendor == "cisco_switch":
            if "switchport mode trunk" in block:
                mode = "trunk"
            elif "switchport mode access" in block:
                mode = "access"
            elif is_switchport:
                mode = "access"
        else:
            if has_ip:
                mode = "routed"
                
        access_vlan = None
        if mode == "access":
            acc_m = re.search(r"switchport access vlan\s+(\d+)", block)
            if acc_m:
                access_vlan = int(acc_m.group(1))
            else:
                access_vlan = 1
                
        native_vlan = 1
        if mode == "trunk":
            nat_m = re.search(r"switchport trunk native vlan\s+(\d+)", block)
            if nat_m:
                native_vlan = int(nat_m.group(1))
                
        trunk_vlans = None
        if mode == "trunk":
            allowed_m = re.search(r"switchport trunk allowed vlan\s+(\S+)", block)
            if allowed_m:
                trunk_vlans = parse_vlan_range(allowed_m.group(1))
            else:
                trunk_vlans = []
                
        cg_m = re.search(r"channel-group\s+(\d+)", block, re.IGNORECASE)
        channel_group = int(cg_m.group(1)) if cg_m else None
        
        stp_state = stp_data.get(iface_name, {})
        
        interface_l2[iface_name] = {
            "mode": mode,
            "access_vlan": access_vlan,
            "trunk_vlans": trunk_vlans,
            "native_vlan": native_vlan,
            "channel_group": channel_group,
            "stp_state": stp_state,
            "vlan_id": vlan_id,
        }
        
    return interface_l2


DIAGNOSTIC_CONNECTION_SEMAPHORE = asyncio.Semaphore(5)

async def live_snapshot_for_diagnostics(device_name: str, cfg: dict) -> dict:
    """
    Raccoglie show running-config e show ip interface brief in tempo reale per la diagnostica.

    Ritorna un dict con:
      running_config     : str  (vuoto se non raggiungibile)
      interfaces         : str  (output grezzo)
      operational_status : str  (output comandi operativi, vuoto se non cisco o non supportato)
      error              : str  (messaggio errore, vuoto se OK)
    """
    result = {"running_config": "", "interfaces": "", "operational_status": "", "error": ""}
    vendor = (cfg.get("vendor") or "").lower()

    async with DIAGNOSTIC_CONNECTION_SEMAPHORE:
        try:
            async with get_connection(cfg) as conn:
                await conn.send_command("terminal length 0")

                if vendor in ("cisco_ios", "cisco_switch", "frrouting"):
                    result["running_config"] = await conn.send_command("show running-config")

                if vendor in ("cisco_ios", "cisco_switch"):
                    result["interfaces"] = await conn.send_command("show ip interface brief")
                    
                    # Raccogli dati operativi aggiuntivi
                    op_cmds = [
                        "show interfaces trunk",
                        "show etherchannel summary",
                        "show spanning-tree",
                        "show ip dhcp binding",
                    ]
                    op_outputs = []
                    for op_cmd in op_cmds:
                        try:
                            out = await conn.send_command(op_cmd)
                            # Se l'output contiene errori sintattici noti Cisco, lo scartiamo
                            if "% invalid input" in out.lower() or "% unknown command" in out.lower() or "% incomplete command" in out.lower() or "% ambiguous command" in out.lower():
                                logger.debug("[%s] Comando non supportato: %s", device_name, op_cmd)
                                continue
                            op_outputs.append(f"--- {op_cmd} ---\n{out}")
                        except Exception as ex:
                            logger.debug("[%s] Fallito comando operativo %s: %s", device_name, op_cmd, ex)
                    result["operational_status"] = "\n\n".join(op_outputs)
                elif vendor == "frrouting":
                    result["interfaces"] = await conn.send_command("show interface brief")
                elif vendor == "vpcs":
                    result["interfaces"] = await conn.send_command("show ip")

        except (ConnectionRefusedError, OSError, asyncio.TimeoutError) as e:
            result["error"] = f"{type(e).__name__}: {e}"
            logger.warning("[TROUBLESHOOT] %s non raggiungibile: %s", device_name, e)
        except Exception as e:
            result["error"] = f"Unexpected: {e}"
            logger.error("[TROUBLESHOOT] Errore snapshot %s: %s", device_name, e, exc_info=True)

    return result


def truncate_operational_status(op_status: str, max_lines_per_section: int = 10) -> str:
    """
    Tronca ogni sezione dell'operational_status a un numero massimo di righe
    per evitare il bloat del database Neo4j.
    """
    if not isinstance(op_status, str) or not op_status:
        return ""
    
    sections = []
    # Divide l'output in base alle intestazioni dei comandi, es: --- show ip route ---
    parts = re.split(r"(^--- show .*? ---)", op_status, flags=re.MULTILINE)
    
    i = 0
    while i < len(parts):
        part = parts[i]
        if not part.strip():
            i += 1
            continue
            
        if re.match(r"^--- show .*? ---$", part.strip()):
            header = part.strip()
            content = ""
            if i + 1 < len(parts):
                content = parts[i + 1]
                i += 1
                
            lines = content.strip().splitlines()
            if len(lines) > max_lines_per_section:
                truncated_lines = lines[:max_lines_per_section]
                truncated_lines.append(f"... (truncated, {len(lines) - max_lines_per_section} lines omitted)")
                content_str = "\n".join(truncated_lines)
            else:
                content_str = "\n".join(lines)
                
            sections.append(f"{header}\n{content_str}")
        else:
            sections.append(part.strip())
        i += 1
        
    return "\n\n".join(sections)


def build_synthetic_status(snapshot: DeviceSnapshot) -> str:
    """
    Costruisce un riepilogo sintetico e strutturato dello stato del dispositivo
    da salvare sul nodo Device in Neo4j, evitando bloat.
    """
    if not snapshot:
        return ""
        
    lines = [f"=== Synthetic Status for {snapshot.router_name} ==="]
    lines.append(f"Vendor profile: {snapshot.vendor}")
    
    # 1. Interfaces
    if getattr(snapshot, "interfaces", None):
        lines.append("\n[Interfaces L3]")
        for name, ip in sorted(snapshot.interfaces.items()):
            status = snapshot.interface_statuses.get(name, "up")
            l2 = snapshot.interface_l2.get(name, {})
            vlan_id = l2.get("vlan_id")
            encap_str = f" (encapsulation dot1Q {vlan_id})" if vlan_id and "." in name else ""
            lines.append(f"  - {name}: {ip} ({status}){encap_str}")
            
    # 2. VLANs
    if getattr(snapshot, "vlans", None):
        lines.append("\n[VLAN Database]")
        for vid, name in sorted(snapshot.vlans.items()):
            lines.append(f"  - {vid}: {name}")
            
    # 3. Ports L2/STP (Switchports)
    if getattr(snapshot, "interface_l2", None):
        l2_lines = []
        for name, props in sorted(snapshot.interface_l2.items()):
            mode = props.get("mode")
            if mode in ("access", "trunk"):
                detail = f"  - {name}: mode={mode}"
                if mode == "access":
                    detail += f", access_vlan={props.get('access_vlan') or 1}"
                else:
                    trunk_vlans = props.get("trunk_vlans") or []
                    detail += f", trunk_vlans={trunk_vlans}, native_vlan={props.get('native_vlan') or 1}"
                
                cg = props.get("channel_group")
                if cg:
                    detail += f", channel_group={cg}"
                    
                stp = props.get("stp_state")
                if stp:
                    stp_str = ", ".join(f"VLAN {v}:{s}" for v, s in sorted(stp.items()))
                    detail += f", STP=[{stp_str}]"
                l2_lines.append(detail)
        if l2_lines:
            lines.append("\n[Switchports L2/STP]")
            lines.extend(l2_lines)
            
    # 4. EtherChannels
    if getattr(snapshot, "etherchannels", None):
        lines.append("\n[EtherChannels]")
        for po, members in sorted(snapshot.etherchannels.items()):
            lines.append(f"  - {po}: members={members}")
            
    # 5. Static Routes
    if getattr(snapshot, "static_routes", None):
        lines.append("\n[Static Routes]")
        for net, nh in sorted(snapshot.static_routes.items()):
            lines.append(f"  - {net} via {nh}")
            
    # 6. DHCP Pools (parsed from running-config)
    if getattr(snapshot, "running_config", None) and snapshot.vendor in ("frrouting", "cisco_ios"):
        from tools.dhcp_config import DhcpStateInspector
        try:
            pools = DhcpStateInspector().parse_running_config(snapshot.running_config)
            if pools:
                lines.append("\n[DHCP Pools]")
                for pool_name, info in sorted(pools.items()):
                    net = info.get("network")
                    mask = info.get("netmask")
                    gw = info.get("router")
                    dns = info.get("dns")
                    lines.append(f"  - {pool_name}: network={net}/{mask}, gateway={gw}, dns={dns}")
        except Exception:
            pass
            
    # 7. DHCP Relay (helper-address)
    if getattr(snapshot, "running_config", None):
        helpers = re.findall(r"(?ms)^interface\s+(\S+)\s*\n(.*?)(?=^interface\s+\S+|^end\s*$|\Z)", snapshot.running_config)
        helper_lines = []
        for iface_raw, block in helpers:
            has_helpers = re.findall(r"ip helper-address\s+([\d\.]+)", block)
            if has_helpers:
                from tools.parser import normalize_interface_name
                iface_name = normalize_interface_name(iface_raw)
                helper_lines.append(f"  - {iface_name}: helpers={has_helpers}")
        if helper_lines:
            lines.append("\n[DHCP Relay]")
            lines.extend(helper_lines)
            
    # 8. Security & Management
    sec_info = []
    rc = getattr(snapshot, "running_config", "") or ""
    if rc:
        has_ssh2 = "ip ssh version 2" in rc
        has_pw_enc = "service password-encryption" in rc
        sec_info.append(f"  - SSH version 2: {'Configured' if has_ssh2 else 'Not configured'}")
        sec_info.append(f"  - Service Password Encryption: {'Enabled' if has_pw_enc else 'Disabled'}")
        
        # Check cleartext passwords
        cleartext_pws = re.findall(r"(?m)^\s*(?:username\s+\S+\s+password\s+\S+|enable\s+password\s+\S+)", rc)
        if cleartext_pws:
            sec_info.append("  - Cleartext Passwords: WARNING (Found unencrypted passwords in config)")
        else:
            sec_info.append("  - Cleartext Passwords: OK (No unencrypted passwords found)")
            
    if sec_info:
        lines.append("\n[Security & Management]")
        lines.extend(sec_info)
        
    return "\n".join(lines)

