# tools/graph_store/topology_ops.py
from __future__ import annotations

import ipaddress
import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_TS = lambda: datetime.now(timezone.utc).isoformat()

class TopologyOpsMixin:
    async def upsert_l1_link(
        self,
        local_device: str,
        local_port: str,
        remote_device: str,
        remote_port: str,
        source: str = "spec",
    ):
        local_port = self._normalize_port(local_port, local_device)
        remote_port = self._normalize_port(remote_port, remote_device)
        """
        Persiste un cavo fisico L1 tra due Port.

        source:
          "spec"     → dichiarato nella spec YAML (fonte di verità)
          "cdp"      → scoperto via CDP
          "lldp"     → scoperto via LLDP
          "inferred" → inferito da subnet matching (fallback)

        La spec è authoritative: se esiste già un link "spec", un link
        "inferred" sulle stesse porte non lo sovrascrive.
        """
        ts = _TS()
        base = """
        MERGE (d1:Device {name: $ld})
        MERGE (d2:Device {name: $rd})
        MERGE (p1:Port {name: $lp, device: $ld})
        ON CREATE SET p1.oper_status = 'up', p1.admin_status = 'up', p1.last_seen = $ts
        MERGE (d1)-[:HAS_PORT]->(p1)
        MERGE (p2:Port {name: $rp, device: $rd})
        ON CREATE SET p2.oper_status = 'up', p2.admin_status = 'up', p2.last_seen = $ts
        MERGE (d2)-[:HAS_PORT]->(p2)
        """
        params = dict(ld=local_device, lp=local_port, rd=remote_device, rp=remote_port,
                      source=source, ts=ts)

        async def _body():
            async with self._driver.session() as session:
                # Non sovrascrivere link "spec" con "inferred"
                check_query = base + """
                WITH p1, p2
                OPTIONAL MATCH (p1)-[existing:CABLED_TO]->(p2)
                RETURN existing.source AS existing_source
                """
                result = await session.run(check_query, **params)
                rec = await result.single()
                existing_source = rec["existing_source"] if rec else None

                if source == "inferred" and existing_source in ("spec", "cdp", "lldp"):
                    return  # Non degradare la fonte

                await session.run(
                    base + """
                    WITH p1, p2
                    MERGE (p1)-[r:CABLED_TO]->(p2)
                    SET r.source = $source, r.updated = $ts
                    """, **params,
                )
                await session.run(
                    base + """
                    WITH p1, p2
                    MERGE (p2)-[r:CABLED_TO]->(p1)
                    SET r.source = $source, r.updated = $ts
                    """, **params,
                )

        await self._execute_with_retry(_body)

    async def store_l1_neighbors(
        self,
        device: str,
        neighbors: list[dict],
        source: str = "cdp",
    ):
        """
        Persiste neighbor CDP/LLDP come link L1.
        Ogni dict: {local_iface, remote_device, remote_iface}
        """
        for nb in neighbors:
            await self.upsert_l1_link(
                local_device=device,
                local_port=nb["local_iface"],
                remote_device=nb["remote_device"],
                remote_port=nb["remote_iface"],
                source=source,
            )
        if neighbors:
            logger.info("[Neo4j Store] %d link L1 (%s) salvati per %s.",
                        len(neighbors), source, device)

    async def clear_l1_links(self, device: str, preserve_spec: bool = True):
        """
        Rimuove i link CABLED_TO di un device.
        Se preserve_spec=True (default), mantiene i link source='spec'.
        """
        if preserve_spec:
            await self._run(
                """
                MATCH (d:Device {name: $device})-[:HAS_PORT]->(p:Port)
                MATCH (p)-[r:CABLED_TO]-()
                WHERE r.source <> 'spec'
                DELETE r
                """,
                device=device,
            )
        else:
            await self._run(
                """
                MATCH (d:Device {name: $device})-[:HAS_PORT]->(p:Port)
                MATCH (p)-[r:CABLED_TO]-()
                DELETE r
                """,
                device=device,
            )

    async def upsert_l2_link(
        self,
        local_device: str,
        local_iface: str,
        remote_device: str,
        remote_iface: str,
        source: str = "spec",
    ):
        local_iface = self._normalize_port(local_iface, local_device)
        remote_iface = self._normalize_port(remote_iface, remote_device)
        """
        Compatibilità con observe_node che chiama upsert_l2_link.
        Crea sia il nodo Port (L1) che la relazione CABLED_TO.
        """
        await self.upsert_l1_link(
            local_device=local_device,
            local_port=local_iface,
            remote_device=remote_device,
            remote_port=remote_iface,
            source=source,
        )
        # Mantieni anche PHYSICALLY_CONNECTED_TO per compatibilità
        # con le query esistenti in neo4j_queries.py e troubleshoot.py
        ts = _TS()
        base = """
        MERGE (d1:Device {name: $ld})
        MERGE (d2:Device {name: $rd})
        MERGE (i1:Interface {name: $lp, device: $ld})
        ON CREATE SET i1.ip_address = 'unassigned', i1.status = 'up'
        MERGE (d1)-[:HAS_INTERFACE]->(i1)
        MERGE (i2:Interface {name: $rp, device: $rd})
        ON CREATE SET i2.ip_address = 'unassigned', i2.status = 'up'
        MERGE (d2)-[:HAS_INTERFACE]->(i2)
        """
        params = dict(ld=local_device, lp=local_iface, rd=remote_device, rp=remote_iface,
                      source=source, ts=ts)

        async def _body():
            async with self._driver.session() as session:
                # Only overwrite if not degrading an authoritative source
                check = await session.run(
                    base + """
                    WITH i1, i2
                    OPTIONAL MATCH (i1)-[ex:PHYSICALLY_CONNECTED_TO]->(i2)
                    RETURN ex.source AS existing_source
                    """, **params,
                )
                rec = await check.single()
                existing = rec["existing_source"] if rec else None
                if source == "inferred" and existing in ("spec", "cdp", "lldp"):
                    return
                await session.run(
                    base + """
                    WITH i1, i2
                    MERGE (i1)-[r:PHYSICALLY_CONNECTED_TO]->(i2)
                    SET r.source = $source, r.updated = $ts
                    """, **params,
                )
                await session.run(
                    base + """
                    WITH i1, i2
                    MERGE (i2)-[r:PHYSICALLY_CONNECTED_TO]->(i1)
                    SET r.source = $source, r.updated = $ts
                    """, **params,
                )
        await self._execute_with_retry(_body)

    async def store_l2_neighbors(
        self,
        device: str,
        neighbors: list[dict],
        source: str = "cdp",
    ):
        """
        Persiste CDP/LLDP neighbor come link L1 (CABLED_TO) + L2 (PHYSICALLY_CONNECTED_TO).
        Usa UNWIND per batch insert — riduce N round-trip a 2.
        Non sovrascrive link source='spec' con link CDP/LLDP.
        """
        if not neighbors:
            return
        ts = _TS()
        batch = [
            {
                "ld": device,
                "lp": self._normalize_port(nb["local_iface"], device),
                "rd": nb["remote_device"],
                "rp": self._normalize_port(nb["remote_iface"], nb["remote_device"]),
            }
            for nb in neighbors
        ]
        unwind_base = """
        UNWIND $batch AS row
        MERGE (d1:Device {name: row.ld})
        MERGE (d2:Device {name: row.rd})
        MERGE (p1:Port {name: row.lp, device: row.ld})
        ON CREATE SET p1.oper_status = 'up', p1.admin_status = 'up', p1.last_seen = $ts
        MERGE (d1)-[:HAS_PORT]->(p1)
        MERGE (p2:Port {name: row.rp, device: row.rd})
        ON CREATE SET p2.oper_status = 'up', p2.admin_status = 'up', p2.last_seen = $ts
        MERGE (d2)-[:HAS_PORT]->(p2)
        MERGE (i1:Interface {name: row.lp, device: row.ld})
        ON CREATE SET i1.ip_address = 'unassigned', i1.status = 'up'
        MERGE (d1)-[:HAS_INTERFACE]->(i1)
        MERGE (i2:Interface {name: row.rp, device: row.rd})
        ON CREATE SET i2.ip_address = 'unassigned', i2.status = 'up'
        MERGE (d2)-[:HAS_INTERFACE]->(i2)
        """
        async def _body():
            async with self._driver.session() as session:
                # CABLED_TO L1 — do not overwrite spec
                await session.run(unwind_base + """
                    WITH p1, p2
                    OPTIONAL MATCH (p1)-[ex:CABLED_TO]->(p2)
                    WITH p1, p2, ex WHERE ex IS NULL OR NOT ex.source IN ['spec']
                    MERGE (p1)-[r:CABLED_TO]->(p2)
                    SET r.source = $source, r.updated = $ts
                """, batch=batch, source=source, ts=ts)
                await session.run(unwind_base + """
                    WITH p1, p2
                    OPTIONAL MATCH (p2)-[ex:CABLED_TO]->(p1)
                    WITH p1, p2, ex WHERE ex IS NULL OR NOT ex.source IN ['spec']
                    MERGE (p2)-[r:CABLED_TO]->(p1)
                    SET r.source = $source, r.updated = $ts
                """, batch=batch, source=source, ts=ts)
                # PHYSICALLY_CONNECTED_TO L2 compat — do not overwrite spec
                await session.run(unwind_base + """
                    WITH i1, i2
                    OPTIONAL MATCH (i1)-[ex:PHYSICALLY_CONNECTED_TO]->(i2)
                    WITH i1, i2, ex WHERE ex IS NULL OR NOT ex.source IN ['spec']
                    MERGE (i1)-[r:PHYSICALLY_CONNECTED_TO]->(i2)
                    SET r.source = $source, r.updated = $ts
                """, batch=batch, source=source, ts=ts)
                await session.run(unwind_base + """
                    WITH i1, i2
                    OPTIONAL MATCH (i2)-[ex:PHYSICALLY_CONNECTED_TO]->(i1)
                    WITH i1, i2, ex WHERE ex IS NULL OR NOT ex.source IN ['spec']
                    MERGE (i2)-[r:PHYSICALLY_CONNECTED_TO]->(i1)
                    SET r.source = $source, r.updated = $ts
                """, batch=batch, source=source, ts=ts)
        await self._execute_with_retry(_body)
        logger.info("[Neo4j Store] %d link %s salvati per %s.", len(neighbors), source, device)

    async def store_static_l2_links(self, device: str, links: list[dict]) -> None:
        """
        Persiste link fisici dichiarati staticamente in devices.yaml.
        Formato:
          l2_links:
            - local_iface: eth0
              remote_device: SW1
              remote_iface: Ethernet0/1

        Usato per device senza CDP (VPCS, host Linux) dove i link
        non possono essere scoperti automaticamente.
        Questi link hanno source='static' — non sovrascrivono link 'spec'.
        """
        if not links:
            return
        for link in links:
            local_iface  = link.get("local_iface")
            remote_dev   = link.get("remote_device")
            remote_iface = link.get("remote_iface")
            if not (local_iface and remote_dev and remote_iface):
                logger.warning("[Neo4j Store] store_static_l2_links: link incompleto ignorato: %s", link)
                continue
            await self.upsert_l1_link(
                local_device=device,
                local_port=local_iface,
                remote_device=remote_dev,
                remote_port=remote_iface,
                source="static",
            )
            # PHYSICALLY_CONNECTED_TO per compatibilità
            await self.upsert_l2_link(
                local_device=device,
                local_iface=local_iface,
                remote_device=remote_dev,
                remote_iface=remote_iface,
                source="static",
            )
        logger.info("[Neo4j Store] %d link statici salvati per %s.", len(links), device)

    async def clear_l2_links(self, device: str):
        """Compatibilità: rimuove link fisici di un device."""
        await self._run(
            """
            MATCH (d:Device {name: $device})-[:HAS_INTERFACE]->(i:Interface)
            MATCH (i)-[r:PHYSICALLY_CONNECTED_TO]-()
            DELETE r
            """,
            device=device,
        )
        await self.clear_l1_links(device)

    async def compute_l2_topology(self):
        """
        Calcola i link L2 logici.

        Strategia (in ordine di priorità):
          1. Link da spec YAML — già seedati in observe_node via upsert_l2_link(source='spec').
             NON vengono mai sovrascritti da inferenza.
          2. Link CDP/LLDP — fonte live più affidabile.
          3. EtherChannel — Port-Channel logici dai link fisici.
          4. Inferenza black-box — solo per interfacce senza link spec/CDP/LLDP.
        """
        ts = _TS()

        # ── Step 1: EtherChannel — crea link logici Po↔Po da link fisici dei membri ──
        await self._run("""
            MATCH (phys1:Port)-[:MEMBER_OF_LAG]->(po1:Port)
            MATCH (phys2:Port)-[:MEMBER_OF_LAG]->(po2:Port)
            WHERE po1.device <> po2.device
              AND phys1.device = po1.device
              AND phys2.device = po2.device
            MATCH (phys1)-[:CABLED_TO {source: 'cdp'}]-(phys2)
            WITH po1, po2, $ts AS ts
            MERGE (po1)-[r:CABLED_TO]->(po2)
            SET r.source = 'etherchannel', r.updated = ts
        """, ts=ts)
        await self._run("""
            MATCH (phys1:Port)-[:MEMBER_OF_LAG]->(po1:Port)
            MATCH (phys2:Port)-[:MEMBER_OF_LAG]->(po2:Port)
            WHERE po1.device <> po2.device
              AND phys1.device = po1.device
              AND phys2.device = po2.device
            MATCH (phys1)-[:CABLED_TO {source: 'cdp'}]-(phys2)
            WITH po2, po1, $ts AS ts
            MERGE (po2)-[r:CABLED_TO]->(po1)
            SET r.source = 'etherchannel', r.updated = ts
        """, ts=ts)

        # ── Step 2: rimuovi link diretti tra porte fisiche in LAG ──────────────
        await self._run("""
            MATCH (phys1:Port)-[:MEMBER_OF_LAG]->(po1:Port)
            MATCH (phys2:Port)-[:MEMBER_OF_LAG]->(po2:Port)
            WHERE po1.device <> po2.device
            MATCH (phys1)-[r:CABLED_TO]-(phys2)
            DELETE r
        """)

        # ── Step 3: inferenza black-box SOLO per coppie senza link spec/CDP/LLDP ──
        # NON crea link inferred se esiste già spec/cdp/lldp sulle stesse interfacce.
        _blackbox_match = """
            MATCH (d1:Device)-[:HAS_INTERFACE]->(i1:Interface)
            WHERE i1.ip_address <> 'unassigned'
              AND d1.vendor <> 'vpcs'
              AND NOT i1.name CONTAINS '.'
            MATCH (d2:Device)-[:HAS_INTERFACE]->(i2:Interface)
            WHERE i2.ip_address <> 'unassigned'
              AND d2.vendor <> 'vpcs'
              AND NOT i2.name CONTAINS '.'
              AND d1.name <> d2.name
              AND elementId(i1) < elementId(i2)
            WITH i1, i2,
                 split(i1.ip_address, '/')[0] AS ip1,
                 toInteger(split(i1.ip_address, '/')[1]) AS prefix1,
                 split(i2.ip_address, '/')[0] AS ip2,
                 toInteger(split(i2.ip_address, '/')[1]) AS prefix2
            WHERE prefix1 = prefix2
              AND prefix1 >= 24
              AND split(ip1, '.')[0] = split(ip2, '.')[0]
              AND split(ip1, '.')[1] = split(ip2, '.')[1]
              AND split(ip1, '.')[2] = split(ip2, '.')[2]
            WITH i1, i2,
                 EXISTS {
                     MATCH (i1)-[x:PHYSICALLY_CONNECTED_TO]-(i2)
                     WHERE x.source IN ['spec','cdp','lldp','etherchannel']
                 } AS has_authoritative_link
            WHERE NOT has_authoritative_link
        """
        await self._run(
            _blackbox_match + """
            WITH i1, i2, $ts AS ts
            MERGE (i1)-[r:PHYSICALLY_CONNECTED_TO]->(i2)
            SET r.source = 'inferred', r.updated = ts
            """, ts=ts,
        )
        await self._run(
            _blackbox_match + """
            WITH i2, i1, $ts AS ts
            MERGE (i2)-[r:PHYSICALLY_CONNECTED_TO]->(i1)
            SET r.source = 'inferred', r.updated = ts
            """, ts=ts,
        )
        logger.info("[Neo4j Store] Topologia L2 calcolata.")

    async def compute_topology_links(self):
        """
        Ricalcola i link L3 CONNECTED_TO basandosi su subnet matching reale.
        Non tocca i link L1/L2 (CABLED_TO, PHYSICALLY_CONNECTED_TO).

        Implementazione: subnet matching in Python (necessario per IPv4Network)
        con batch write finale via UNWIND — riduce N^2 round-trip a 1.
        """
        await self._run("MATCH ()-[r:CONNECTED_TO]->() DELETE r")

        # Carica interfacce con IP in una sola query
        query = (
            "MATCH (i:Interface) "
            "WHERE i.ip_address IS NOT NULL AND i.ip_address <> 'unassigned' "
            "  AND i.status <> 'down' "
            "RETURN i.device AS device, i.ip_address AS ip, elementId(i) AS id"
        )
        interfaces = []
        async with self._driver.session() as session:
            result = await session.run(query)
            async for rec in result:
                try:
                    iface = ipaddress.IPv4Interface(rec["ip"])
                    interfaces.append({"device": rec["device"], "ip": rec["ip"],
                                       "id": rec["id"], "net": str(iface.network)})
                except ValueError:
                    continue

        # Raggruppa per subnet — O(N) invece di O(N^2)
        subnet_map: dict[str, list[dict]] = {}
        for iface in interfaces:
            subnet_map.setdefault(iface["net"], []).append(iface)

        # Costruisci coppie da connettere
        pairs = []
        for subnet, members in subnet_map.items():
            for i, m1 in enumerate(members):
                for m2 in members[i + 1:]:
                    if m1["device"] != m2["device"]:
                        pairs.append({"id1": m1["id"], "id2": m2["id"]})

        if pairs:
            # Batch write con UNWIND — 1 round-trip invece di N
            await self._run(
                """
                UNWIND $pairs AS pair
                MATCH (i1:Interface) WHERE elementId(i1) = pair.id1
                MATCH (i2:Interface) WHERE elementId(i2) = pair.id2
                MERGE (i1)-[:CONNECTED_TO]->(i2)
                MERGE (i2)-[:CONNECTED_TO]->(i1)
                """,
                pairs=pairs,
            )

        logger.info("[Neo4j Store] Topologia L3 ricalcolata (%d adiacenze).", len(pairs))

    async def clear_inactive_interfaces(self):
        """Rimuove interfacce senza IP e senza ruolo strutturale."""
        await self._run(
            """
            MATCH (i:Interface)
            WHERE (i.ip_address = 'unassigned' OR i.status = 'down')
              AND NOT (i)-[:MEMBER_OF]->()
              AND NOT ()-[:MEMBER_OF]->(i)
              AND NOT (i)-[:PHYSICALLY_CONNECTED_TO]-()
            DETACH DELETE i
            """
        )

    async def get_topology_summary(self) -> str:
        """
        Vista L3 operativa compatta per il nodo PLAN/TROUBLESHOOT.
        Non include running-config. Include IP, rotte, DHCP pool,
        subnet adiacenti e link fisici noti.
        """
        query = """
        MATCH (d:Device) WHERE d.status = 'REACHABLE'
        OPTIONAL MATCH (d)-[:HAS_INTERFACE]->(i:Interface)
        OPTIONAL MATCH (d)-[:HAS_ROUTE]->(r:StaticRoute)
        OPTIONAL MATCH (d)-[:HAS_DHCP_POOL]->(p:DhcpPool)
        RETURN d.name    AS device_name,
               d.vendor  AS vendor,
               d.mgmt_ip AS mgmt,
               collect(distinct {
                   name:    i.name,
                   ip:      i.ip_address,
                   status:  i.status,
                   vlan_id: i.vlan_id
               }) AS ifaces,
               collect(distinct {
                   network:  r.network,
                   next_hop: r.next_hop
               }) AS routes,
               collect(distinct {
                   name:    p.name,
                   network: p.network,
                   gateway: p.default_router
               }) AS pools
        ORDER BY d.name
        """

        lines = ["=== TOPOLOGY — L3 VIEW ==="]

        async with self._driver.session() as session:
            result = await session.run(query)
            async for rec in result:
                dev = rec["device_name"]
                lines.append(f"\nDevice: {dev} | vendor={rec['vendor']} mgmt={rec['mgmt']}")

                for iface in sorted(rec["ifaces"], key=lambda x: x.get("name") or ""):
                    if iface.get("name") and iface.get("ip") and iface["ip"] != "unassigned":
                        vlan_tag = f" [Vlan{iface['vlan_id']}]" if iface.get("vlan_id") else ""
                        lines.append(
                            f"  iface {iface['name']}: {iface['ip']}"
                            f" ({iface.get('status','?')}){vlan_tag}"
                        )
                for route in rec["routes"]:
                    if route.get("network"):
                        lines.append(f"  route {route['network']} via {route['next_hop']}")
                for pool in rec.get("pools", []):
                    if pool.get("name"):
                        lines.append(
                            f"  dhcp {pool['name']}: {pool['network']} gw={pool['gateway']}"
                        )

            # Subnet adjacencies
            adj_result = await session.run(
                """
                MATCH (i1:Interface)-[:CONNECTED_TO]->(i2:Interface)
                WHERE elementId(i1) < elementId(i2)
                RETURN i1.device AS d1, i1.name AS n1, i1.ip_address AS ip1,
                       i2.device AS d2, i2.name AS n2, i2.ip_address AS ip2
                """
            )
            subnets: dict[str, list[str]] = {}
            async for rec in adj_result:
                if not rec["ip1"] or "/" not in rec["ip1"]:
                    continue
                try:
                    net = str(ipaddress.IPv4Interface(rec["ip1"]).network)
                except ValueError:
                    continue
                entry = (
                    f"  {rec['d1']}:{rec['n1']} ({rec['ip1'].split('/')[0]})"
                    f" <-> {rec['d2']}:{rec['n2']} ({rec['ip2'].split('/')[0]})"
                )
                subnets.setdefault(net, []).append(entry)

            if subnets:
                lines.append("\nSubnet Adjacencies:")
                for net, entries in sorted(subnets.items()):
                    lines.append(f"  {net}:")
                    for e in entries:
                        lines.append(e)

        if len(lines) == 1:
            lines.append("  No L3 data.")
        return "\n".join(lines)

    async def get_l1_topology_summary(self) -> str:
        """
        Vista L1 fisica: cavi, stato porte, velocità/duplex.
        Mostra coerenza spec vs live (porta up/down, link presenti/assenti).
        """
        query = """
        MATCH (d:Device)
        OPTIONAL MATCH (d)-[:HAS_PORT]->(p:Port)
        OPTIONAL MATCH (p)-[cable:CABLED_TO]->(remote_p:Port)
              WHERE elementId(p) < elementId(remote_p)
        RETURN d.name   AS device,
               d.vendor AS vendor,
               d.status AS dev_status,
               collect(distinct {
                   name:         p.name,
                   oper_status:  p.oper_status,
                   admin_status: p.admin_status,
                   speed:        p.speed,
                   duplex:       p.duplex
               }) AS ports,
               collect(distinct {
                   local_port:    p.name,
                   remote_device: remote_p.device,
                   remote_port:   remote_p.name,
                   source:        cable.source
               }) AS cables
        ORDER BY d.name
        """

        lines = ["=== PHYSICAL TOPOLOGY — L1 VIEW ==="]

        async with self._driver.session() as session:
            result = await session.run(query)
            async for rec in result:
                dev = rec["device"]
                lines.append(
                    f"\nDevice: {dev} | vendor={rec['vendor']} [{rec.get('dev_status','?')}]"
                )

                ports = [p for p in rec.get("ports", []) if p.get("name")]
                if ports:
                    lines.append("  Ports:")
                    for p in sorted(ports, key=lambda x: x["name"]):
                        oper = p.get("oper_status", "?")
                        admin = p.get("admin_status", "up")
                        speed = p.get("speed") or ""
                        duplex = p.get("duplex") or ""
                        detail = " | ".join(filter(None, [speed, duplex]))
                        admin_tag = " [SHUTDOWN]" if admin == "down" else ""
                        lines.append(
                            f"    {p['name']}: {oper.upper()}{admin_tag}"
                            + (f" ({detail})" if detail else "")
                        )

                cables = [c for c in rec.get("cables", []) if c.get("local_port")]
                if cables:
                    lines.append("  Cables:")
                    for c in sorted(cables, key=lambda x: x["local_port"]):
                        src_tag = f"[{c['source']}]" if c.get("source") else ""
                        lines.append(
                            f"    {c['local_port']} <-> "
                            f"{c['remote_device']}:{c['remote_port']} {src_tag}"
                        )

        if len(lines) == 1:
            lines.append("  No L1 data.")
        return "\n".join(lines)

    async def get_l2_topology_summary(self) -> str:
        """
        Vista L2: VLAN, trunk, access, LAG, STP, endpoint.
        """
        query = """
        MATCH (d:Device)
        OPTIONAL MATCH (d)-[:HAS_PORT]->(p:Port)
        OPTIONAL MATCH (p)-[:CARRIES_VLAN]->(cv:Vlan)
        OPTIONAL MATCH (p)-[:NATIVE_VLAN]->(nv:Vlan)
        OPTIONAL MATCH (d)-[:HAS_PORT]->(phys:Port)-[:MEMBER_OF_LAG]->(po:Port)
        OPTIONAL MATCH (d)-[:CONFIGURED_VLAN]->(dv:Vlan)
        OPTIONAL MATCH (d)-[:HAS_PORT]->(sp:Port)-[:CONNECTS_ENDPOINT]->(ep:Endpoint)
        RETURN
            d.name   AS device,
            d.vendor AS vendor,
            d.status AS dev_status,
            collect(distinct {
                name:          p.name,
                mode:          p.switchport_mode,
                stp:           p.stp_state,
                carry_vlan:    cv.vlan_id,
                native_vlan:   nv.vlan_id
            }) AS port_vlans,
            collect(distinct {
                po_name: po.name,
                member:  phys.name
            }) AS lags,
            collect(distinct {
                vlan_id: dv.vlan_id,
                name:    dv.name
            }) AS device_vlans,
            collect(distinct {
                ep_ip:   ep.ip,
                ep_mac:  ep.mac,
                ep_port: sp.name,
                ep_vlan: ep.vlan
            }) AS endpoints
        ORDER BY d.name
        """

        lines = ["=== SWITCHING TOPOLOGY — L2 VIEW ==="]

        async with self._driver.session() as session:
            result = await session.run(query)
            async for rec in result:
                dev = rec["device"]
                lines.append(f"\nDevice: {dev} | vendor={rec['vendor']} [{rec.get('dev_status','?')}]")

                # VLAN configurate sul device
                dv = [v for v in rec.get("device_vlans", []) if v.get("vlan_id")]
                if dv:
                    v_strs = [
                        f"Vlan{v['vlan_id']}({v['name'] or '?'})"
                        for v in sorted(dv, key=lambda x: x["vlan_id"])
                    ]
                    lines.append(f"  VLANs: {', '.join(v_strs)}")

                # Porte e VLAN
                port_data: dict[str, dict] = {}
                for pv in rec.get("port_vlans", []):
                    pname = pv.get("name")
                    if not pname:
                        continue
                    if pname not in port_data:
                        port_data[pname] = {
                            "mode": pv.get("mode"),
                            "stp": pv.get("stp"),
                            "carry_vlans": [],
                            "native_vlan": None,
                        }
                    if pv.get("carry_vlan"):
                        port_data[pname]["carry_vlans"].append(pv["carry_vlan"])
                    if pv.get("native_vlan"):
                        port_data[pname]["native_vlan"] = pv["native_vlan"]

                if port_data:
                    lines.append("  Switchports:")
                    for pname, pd in sorted(port_data.items()):
                        mode = pd["mode"] or "?"
                        parts = [f"mode={mode}"]
                        if pd["carry_vlans"]:
                            parts.append(f"vlans={sorted(pd['carry_vlans'])}")
                        if pd["native_vlan"]:
                            parts.append(f"native={pd['native_vlan']}")
                        if pd["stp"]:
                            try:
                                stp_d = json.loads(pd["stp"])
                                stp_str = ",".join(
                                    f"V{vid}:{st}"
                                    for vid, st in sorted(stp_d.items(), key=lambda x: x[0])
                                )
                                parts.append(f"stp=[{stp_str}]")
                            except Exception:
                                pass
                        lines.append(f"    {pname}: {' | '.join(parts)}")

                # LAG
                lags: dict[str, list[str]] = {}
                for lag in rec.get("lags", []):
                    if lag.get("po_name") and lag.get("member"):
                        lags.setdefault(lag["po_name"], []).append(lag["member"])
                if lags:
                    lines.append("  LAG:")
                    for po, members in sorted(lags.items()):
                        lines.append(f"    {po} <- {', '.join(sorted(members))}")

                # Endpoint
                eps = [e for e in rec.get("endpoints", []) if e.get("ep_ip")]
                if eps:
                    lines.append("  Endpoints:")
                    for ep in sorted(eps, key=lambda x: x["ep_ip"]):
                        lines.append(
                            f"    {ep['ep_ip']} (mac={ep['ep_mac']}) "
                            f"port={ep['ep_port']} vlan={ep['ep_vlan']}"
                        )

        if len(lines) == 1:
            lines.append("  No L2 data.")
        return "\n".join(lines)

    async def get_full_topology_summary(self) -> str:
        """
        Combina L1 + L2 + L3 in un unico snapshot per il troubleshooter.
        Compatto ma completo: ogni layer su sezione separata.
        """
        l1 = await self.get_l1_topology_summary()
        l2 = await self.get_l2_topology_summary()
        l3 = await self.get_topology_summary()
        return f"{l1}\n\n{l2}\n\n{l3}"
