# database/neo4j_queries.py
"""
Repository di accesso dati Neo4j per il motore GENERATE (SRP: solo I/O database).

v2.0 — Adattato alla separazione Device / DeviceConfig:
  - get_device_state() legge il running-config da (Device)-[:HAS_CONFIG]->(DeviceConfig)
    invece che dalla proprietà d.running_config sul nodo Device.
  - Tutto il resto rimane invariato: nessuna logica di business,
    solo accesso dati parametrizzato.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class GenerateRepository:
    """
    Repository per il nodo GENERATE.

    Richiede un driver Neo4j async già inizializzato.
    Non gestisce il ciclo di vita del driver: è responsabilità
    del chiamante chiudere la connessione.
    """

    def __init__(self, driver) -> None:
        self._driver = driver

    async def get_device_state(
        self,
        router_name: str,
    ) -> tuple[Optional[str], dict[str, str], dict[str, str], str]:
        """
        Carica lo stato corrente completo di un dispositivo.

        Il running-config viene letto dal nodo DeviceConfig separato
        (relazione HAS_CONFIG), mantenendo il nodo Device leggero.

        Returns:
            Tupla di 4 elementi:
              - vendor_type:        tipo vendor (es. "cisco_ios", "frrouting") o None
              - current_interfaces: dict {iface_name: ip_cidr_string}
              - current_routes:     dict {network_cidr: next_hop}
              - running_config_raw: running-config grezzo come stringa
        """
        vendor_type: Optional[str] = None
        current_interfaces: dict[str, str] = {}
        current_routes: dict[str, str] = {}
        running_config_raw: str = ""

        async with self._driver.session() as session:

            # ── Vendor type ───────────────────────────────────────────────────
            result = await session.run(
                "MATCH (d:Device {name:$n}) RETURN d.vendor AS v",
                n=router_name,
            )
            record = await result.single()
            if record:
                vendor_type = record["v"]

            # ── Interfacce ────────────────────────────────────────────────────
            result = await session.run(
                "MATCH (d:Device {name:$n})-[:HAS_INTERFACE]->(i:Interface) "
                "RETURN i.name AS iface, i.ip_address AS ip",
                n=router_name,
            )
            async for rec in result:
                iface_name = rec["iface"]
                ip_value   = rec["ip"] or "unassigned"
                current_interfaces[iface_name] = ip_value

            # ── Rotte statiche ────────────────────────────────────────────────
            result = await session.run(
                "MATCH (d:Device {name:$n})-[:HAS_ROUTE]->(r:StaticRoute) "
                "RETURN r.network AS network, r.next_hop AS next_hop",
                n=router_name,
            )
            async for rec in result:
                current_routes[rec["network"]] = rec["next_hop"]

            # ── Running-config (da DeviceConfig) ─────────────────────────────
            # La config è in un nodo satellite per mantenere Device leggero.
            # OPTIONAL MATCH garantisce che il metodo funzioni anche se il
            # nodo DeviceConfig non esiste ancora (primo run).
            result = await session.run(
                "MATCH (d:Device {name:$n}) "
                "OPTIONAL MATCH (d)-[:HAS_CONFIG]->(c:DeviceConfig) "
                "RETURN c.running_config AS rc",
                n=router_name,
            )
            record = await result.single()
            if record and record["rc"]:
                running_config_raw = record["rc"]

        return vendor_type, current_interfaces, current_routes, running_config_raw

    async def resolve_next_hop(
        self,
        router_name: str,
        target_network_cidr: str,
    ) -> Optional[str]:
        """
        Risolve il next-hop per raggiungere una rete target dal router specificato.

        Strategia:
          Cerca nel grafo Neo4j un dispositivo adiacente al router corrente
          che abbia un'interfaccia nella stessa subnet della rete target.

        Args:
            router_name:          Nome del router sorgente.
            target_network_cidr:  Rete target in formato CIDR (es. '192.168.20.0/24').

        Returns:
            IP del next-hop (solo la parte host, senza CIDR) oppure None
            se il percorso non è risolvibile.
        """
        try:
            target_net = ipaddress.IPv4Network(target_network_cidr, strict=False)
        except ValueError:
            logger.error(
                "[%s] Target CIDR non valido: %s", router_name, target_network_cidr
            )
            return None

        # Query per recuperare l'IP di connessione logica verso src (next_hop_raw) e
        # tutti gli IP delle interfacce del vicino (neighbor_ip_raw)
        query = """
            MATCH (src:Device {name: $router_name})-[:HAS_INTERFACE]->(src_iface:Interface)
            MATCH (src_iface)-[:CONNECTED_TO]-(dst_iface:Interface)<-[:HAS_INTERFACE]-(dst:Device)
            WHERE src_iface.ip_address IS NOT NULL AND src_iface.ip_address <> 'unassigned'
              AND dst_iface.ip_address IS NOT NULL AND dst_iface.ip_address <> 'unassigned'
            MATCH (dst)-[:HAS_INTERFACE]->(neighbor_iface:Interface)
            WHERE neighbor_iface.ip_address IS NOT NULL AND neighbor_iface.ip_address <> 'unassigned'
            RETURN dst_iface.ip_address AS next_hop_raw, neighbor_iface.ip_address AS neighbor_ip_raw
        """

        try:
            async with self._driver.session() as session:
                result = await session.run(query, router_name=router_name)
                async for record in result:
                    next_hop_raw = record["next_hop_raw"]
                    neighbor_ip_raw = record["neighbor_ip_raw"]
                    if not next_hop_raw or not neighbor_ip_raw:
                        continue
                    
                    # Estrai l'IP per il controllo di appartenenza
                    neighbor_ip = neighbor_ip_raw.split("/")[0].strip()
                    try:
                        neighbor_ip_obj = ipaddress.IPv4Address(neighbor_ip)
                        if neighbor_ip_obj in target_net:
                            # Trovato! Ritorna l'IP di connessione logica adiacente a src (senza CIDR)
                            return next_hop_raw.split("/")[0].strip()
                    except ValueError:
                        continue
        except Exception as exc:
            logger.error(
                "[%s] Errore GraphRAG next-hop per %s: %s",
                router_name,
                target_network_cidr,
                exc,
            )

        return None


class TroubleshootRepository:
    """
    Repository per il nodo TROUBLESHOOT (SRP: solo I/O database).

    Richiede un driver Neo4j async già inizializzato.
    """

    def __init__(self, driver) -> None:
        self._driver = driver

    async def collect_transit_devices(self, failed_devices: list[str]) -> list[str]:
        """
        Trova i dispositivi di transito lungo il cammino L1/L2/L3 tra i dispositivi falliti.
        Usa query Cypher shortestPath per estrarre il sotto-grafo esatto.
        In caso di rete partizionata o singolo fallimento, esegue un fallback 1-hop protetto.
        """
        if not failed_devices:
            return []

        devices_found: set[str] = set(failed_devices)

        # 1. Eseguiamo il pathfinding se abbiamo almeno 2 dispositivi falliti
        if len(failed_devices) >= 2:
            path_query = """
                MATCH (d1:Device) WHERE d1.name IN $failed_devices
                MATCH (d2:Device) WHERE d2.name IN $failed_devices AND d1.name <> d2.name
                MATCH p = shortestPath((d1)-[:HAS_INTERFACE|CONNECTED_TO|PHYSICALLY_CONNECTED_TO|HAS_PORT|CABLED_TO|CARRIES_VLAN|MEMBER_OF_LAG*..15]-(d2))
                UNWIND nodes(p) AS n
                MATCH (dev:Device) WHERE dev = n OR (dev)-[:HAS_INTERFACE|HAS_PORT]->(n)
                RETURN DISTINCT dev.name AS device
            """
            try:
                async with self._driver.session() as session:
                    result = await session.run(path_query, failed_devices=failed_devices)
                    async for rec in result:
                        dev = rec["device"]
                        if dev:
                            devices_found.add(dev)
            except Exception as e:
                logger.error("[TROUBLESHOOT] Errore query pathfinding: %s", e)

        # 2. Se non abbiamo trovato transit devices oltre a quelli falliti (o se avevamo solo 1 failed device),
        # eseguiamo il fallback 1-hop per raccogliere i vicini fisici/logici diretti (es. switch di accesso e gateway).
        if len(devices_found) <= len(failed_devices):
            fallback_query = """
                MATCH (d:Device) WHERE d.name IN $failed_devices
                MATCH (d)-[:HAS_PORT|HAS_INTERFACE]->(n)-[:CABLED_TO|CONNECTED_TO|PHYSICALLY_CONNECTED_TO]-(neighbor_node)
                MATCH (neighbor_dev:Device)-[:HAS_PORT|HAS_INTERFACE]->(neighbor_node)
                RETURN DISTINCT neighbor_dev.name AS device
            """
            try:
                async with self._driver.session() as session:
                    result = await session.run(fallback_query, failed_devices=failed_devices)
                    async for rec in result:
                        dev = rec["device"]
                        if dev:
                            devices_found.add(dev)
            except Exception as e:
                logger.error("[TROUBLESHOOT] Errore query fallback 1-hop: %s", e)

        all_devices = sorted(list(devices_found))
        logger.info("[TROUBLESHOOT] Scope diagnostico calcolato via Cypher: %s", all_devices)
        return all_devices

    async def collect_graph_topology(self, scope: list[str]) -> str:
        """
        Vista multi-layer L1+L2+L3 dal grafo Neo4j focalizzata sullo scope.
        Fornisce all'LLM informazioni strutturate che lo snapshot live non include:
        stato fisico dei cavi (L1), configurazione VLAN/trunk (L2), adiacenze IP (L3).
        """
        scope_list = list(set(scope))
        lines = [f"=== GRAPH TOPOLOGY (scope: {', '.join(sorted(scope_list))}) ==="]

        try:
            async with self._driver.session() as session:
                # L1: porte fisiche, stato operativo, cavi
                r1 = await session.run("""
                    MATCH (d:Device) WHERE d.name IN $scope
                    OPTIONAL MATCH (d)-[:HAS_PORT]->(p:Port)
                    OPTIONAL MATCH (p)-[c:CABLED_TO]->(rp:Port)
                    WHERE elementId(p) < elementId(rp)
                    RETURN d.name AS dev, d.vendor AS vendor, d.status AS dstatus,
                           collect(distinct {n:p.name, o:p.oper_status, a:p.admin_status}) AS ports,
                           collect(distinct {lp:p.name, rd:rp.device, rp:rp.name, src:c.source}) AS cables
                    ORDER BY d.name
                """, scope=scope_list)
                lines.append("\n--- L1 PHYSICAL ---")
                async for rec in r1:
                    lines.append(f"[{rec['dev']}] {rec['vendor']} [{rec['dstatus'] or '?'}]")
                    down = [p['n'] for p in rec.get('ports', []) if p.get('o') == 'down' and p.get('n')]
                    if down:
                        lines.append(f"  DOWN: {', '.join(down)}")
                    for c in sorted([c for c in rec.get('cables', []) if c.get('lp')], key=lambda x: x['lp']):
                        lines.append(f"  {c['lp']} <-> {c['rd']}:{c['rp']} [{c.get('src','?')}]")

                # L2: VLAN configurate e switchport
                r2 = await session.run("""
                    MATCH (d:Device) WHERE d.name IN $scope
                    OPTIONAL MATCH (d)-[:HAS_PORT]->(p:Port)
                    OPTIONAL MATCH (p)-[:CARRIES_VLAN]->(cv:Vlan)
                    OPTIONAL MATCH (p)-[:NATIVE_VLAN]->(nv:Vlan)
                    OPTIONAL MATCH (phys:Port)-[:MEMBER_OF_LAG]->(po:Port) WHERE phys.device = d.name
                    OPTIONAL MATCH (d)-[:CONFIGURED_VLAN]->(dv:Vlan)
                    RETURN d.name AS dev,
                           collect(distinct {p:p.name, mode:p.switchport_mode, cv:cv.vlan_id, nv:nv.vlan_id}) AS pvlans,
                           collect(distinct {po:po.name, m:phys.name}) AS lags,
                           collect(distinct dv.vlan_id) AS dvlans
                    ORDER BY d.name
                """, scope=scope_list)
                lines.append("\n--- L2 SWITCHING ---")
                async for rec in r2:
                    dvlans = sorted([v for v in rec.get('dvlans', []) if v])
                    if dvlans:
                        lines.append(f"[{rec['dev']}] VLANs: {dvlans}")
                    pmap = {}
                    for pv in rec.get('pvlans', []):
                        pn = pv.get('p')
                        if not pn:
                            continue
                        pmap.setdefault(pn, {'mode': pv.get('mode'), 'vlans': [], 'native': None})
                        if pv.get('cv'):
                            pmap[pn]['vlans'].append(pv['cv'])
                        if pv.get('nv'):
                            pmap[pn]['native'] = pv['nv']
                    for pn, pd in sorted(pmap.items()):
                        mode = pd['mode'] or '?'
                        vstr = f" vlans={sorted(pd['vlans'])}" if pd['vlans'] else ''
                        nstr = f" native={pd['native']}" if pd['native'] else ''
                        lines.append(f"  {pn}: {mode}{vstr}{nstr}")
                    lmap = {}
                    for lg in rec.get('lags', []):
                        if lg.get('po') and lg.get('m'):
                            lmap.setdefault(lg['po'], []).append(lg['m'])
                    for po, mbs in sorted(lmap.items()):
                        lines.append(f"  LAG {po} <- {', '.join(sorted(mbs))}")

                # L3: interfacce IP, rotte, DHCP
                r3 = await session.run("""
                    MATCH (d:Device) WHERE d.name IN $scope
                    OPTIONAL MATCH (d)-[:HAS_INTERFACE]->(i:Interface)
                    WHERE i.ip_address <> 'unassigned' AND i.ip_address IS NOT NULL
                    OPTIONAL MATCH (d)-[:HAS_ROUTE]->(r:StaticRoute)
                    OPTIONAL MATCH (d)-[:HAS_DHCP_POOL]->(p:DhcpPool)
                    RETURN d.name AS dev,
                           collect(distinct {n:i.name, ip:i.ip_address, s:i.status}) AS ifaces,
                           collect(distinct {net:r.network, nh:r.next_hop}) AS routes,
                           collect(distinct {name:p.name, net:p.network, gw:p.default_router}) AS pools
                    ORDER BY d.name
                """, scope=scope_list)
                lines.append("\n--- L3 ROUTING ---")
                async for rec in r3:
                    lines.append(f"[{rec['dev']}]")
                    for i in sorted(rec.get('ifaces', []), key=lambda x: x.get('n') or ''):
                        if i.get('ip'):
                            lines.append(f"  {i['n']}: {i['ip']} ({i.get('s','?')})")
                    for rt in rec.get('routes', []):
                        if rt.get('net'):
                            lines.append(f"  route {rt['net']} via {rt['nh']}")
                    for pl in rec.get('pools', []):
                        if pl.get('name'):
                            lines.append(f"  dhcp {pl['name']}: {pl['net']} gw={pl['gw']}")

        except Exception as e:
            logger.error("[TROUBLESHOOT] Graph topology query error: %s", e)
            lines.append(f"  (errore: {e})")

        return "\n".join(lines)

