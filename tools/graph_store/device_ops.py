# tools/graph_store/device_ops.py
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

_TS = lambda: datetime.now(timezone.utc).isoformat()

class DeviceOpsMixin:
    async def upsert_device(
        self,
        name: str,
        vendor: str,
        mgmt_ip: str,
        status: str = "REACHABLE",
        confidence: str = "FRESH",
        operational_status: str = "",
    ):
        await self._run(
            """
            MERGE (d:Device {name: $name})
            SET d.vendor             = $vendor,
                d.mgmt_ip            = $mgmt_ip,
                d.status             = $status,
                d.confidence         = $confidence,
                d.operational_status = $operational_status,
                d.last_seen          = $ts
            """,
            name=name, vendor=vendor, mgmt_ip=mgmt_ip,
            status=status, confidence=confidence,
            operational_status=operational_status,
            ts=_TS(),
        )

    async def upsert_port(
        self,
        device: str,
        port_name: str,
        oper_status: str = "up",
        admin_status: str = "up",
        speed: Optional[str] = None,
        duplex: Optional[str] = None,
        media: Optional[str] = None,
    ):
        """
        Crea/aggiorna un nodo Port L1 e lo collega al Device.

        Port vs Interface:
          - Port   = entità fisica: ha stato operativo, velocità, duplex.
          - Interface = entità L3: ha IP, partecipa al routing.
          Stessa porta fisica può avere sia un nodo Port (L1) che un nodo
          Interface (L3) quando ha un IP assegnato.
        """
        port_name = self._normalize_port(port_name, device)
        await self._run(
            """
            MERGE (d:Device {name: $device})
            MERGE (p:Port {name: $port_name, device: $device})
            SET p.oper_status  = $oper_status,
                p.admin_status = $admin_status,
                p.speed        = $speed,
                p.duplex       = $duplex,
                p.media        = $media,
                p.last_seen    = $ts
            MERGE (d)-[:HAS_PORT]->(p)
            """,
            device=device, port_name=port_name,
            oper_status=oper_status, admin_status=admin_status,
            speed=speed, duplex=duplex, media=media,
            ts=_TS(),
        )

    async def upsert_vlan_global(self, vlan_id: int, name: str = "", switch: str = ""):
        """
        Crea/aggiorna un nodo Vlan globale (non legato a un singolo switch).
        Se `switch` è fornito, crea anche la relazione Device-[:CONFIGURED_VLAN]->Vlan.

        I nodi Vlan globali permettono query cross-device:
          MATCH (v:Vlan {vlan_id: 10})<-[:CARRIES_VLAN]-(p:Port)<-[:HAS_PORT]-(d:Device)
        """
        await self._run(
            """
            MERGE (v:Vlan {vlan_id: $vlan_id})
            SET v.name = CASE WHEN $name <> '' THEN $name ELSE v.name END,
                v.updated = $ts
            """,
            vlan_id=vlan_id, name=name, ts=_TS(),
        )
        if switch:
            await self._run(
                """
                MATCH (d:Device {name: $switch})
                MATCH (v:Vlan {vlan_id: $vlan_id})
                MERGE (d)-[:CONFIGURED_VLAN]->(v)
                """,
                switch=switch, vlan_id=vlan_id,
            )

    async def upsert_vlan(self, switch_name: str, vlan_id: int, vlan_name: str):
        """Compatibilità: upsert VLAN globale con owner switch."""
        await self.upsert_vlan_global(vlan_id, vlan_name, switch_name)

    async def clear_vlans(self, switch_name: str):
        """Rimuove le relazioni CONFIGURED_VLAN di uno switch (non i nodi Vlan globali)."""
        await self._run(
            """
            MATCH (d:Device {name: $switch})-[r:CONFIGURED_VLAN]->(:Vlan)
            DELETE r
            """,
            switch=switch_name,
        )

    async def upsert_port_l2(
        self,
        device: str,
        port_name: str,
        mode: Optional[str] = None,
        access_vlan: Optional[int] = None,
        trunk_vlans: Optional[list[int]] = None,
        native_vlan: Optional[int] = None,
        stp_state: Optional[dict] = None,
        channel_group: Optional[int] = None,
    ):
        port_name = self._normalize_port(port_name, device)
        """
        Aggiorna le proprietà L2 di un nodo Port e crea le relazioni VLAN.

        Logica relazioni:
          access:  (Port)-[:CARRIES_VLAN {tagging:'untagged'}]->(Vlan)
          trunk:   (Port)-[:CARRIES_VLAN {tagging:'tagged'}]->(Vlan)   per ogni VLAN
                   (Port)-[:NATIVE_VLAN]->(Vlan)                        per native
          LAG:     (Port)-[:MEMBER_OF_LAG]->(Port[Port-channel])
        """
        stp_str = json.dumps(stp_state) if stp_state else None

        # Upsert Port con proprietà L2
        await self._run(
            """
            MERGE (d:Device {name: $device})
            MERGE (p:Port {name: $port_name, device: $device})
            ON CREATE SET p.oper_status = 'up', p.admin_status = 'up', p.last_seen = $ts
            SET p.switchport_mode = $mode,
                p.stp_state       = $stp_state
            MERGE (d)-[:HAS_PORT]->(p)
            """,
            device=device, port_name=port_name,
            mode=mode, stp_state=stp_str, ts=_TS(),
        )

        # Pulizia relazioni VLAN stale
        await self._run(
            """
            MATCH (p:Port {name: $port_name, device: $device})
            MATCH (p)-[r:CARRIES_VLAN|NATIVE_VLAN]->()
            DELETE r
            """,
            device=device, port_name=port_name,
        )

        if mode == "access" and access_vlan:
            await self.upsert_vlan_global(access_vlan, switch=device)
            await self._run(
                """
                MATCH (p:Port {name: $port_name, device: $device})
                MATCH (v:Vlan {vlan_id: $vlan_id})
                MERGE (p)-[:CARRIES_VLAN {tagging: 'untagged'}]->(v)
                """,
                device=device, port_name=port_name, vlan_id=access_vlan,
            )

        elif mode == "trunk":
            trunk_list = trunk_vlans or []
            if trunk_list:
                # Batch: upsert tutti i nodi Vlan e tutte le relazioni in 2 query UNWIND
                await self._run(
                    """
                    UNWIND $vids AS vid
                    MERGE (v:Vlan {vlan_id: vid})
                    WITH v, vid
                    MATCH (d:Device {name: $device})
                    MERGE (d)-[:CONFIGURED_VLAN]->(v)
                    """,
                    vids=trunk_list, device=device,
                )
                await self._run(
                    """
                    UNWIND $vids AS vid
                    MATCH (p:Port {name: $port_name, device: $device})
                    MATCH (v:Vlan {vlan_id: vid})
                    MERGE (p)-[:CARRIES_VLAN {tagging: 'tagged'}]->(v)
                    """,
                    vids=trunk_list, device=device, port_name=port_name,
                )
            if native_vlan:
                await self.upsert_vlan_global(native_vlan, switch=device)
                await self._run(
                    """
                    MATCH (p:Port {name: $port_name, device: $device})
                    MATCH (v:Vlan {vlan_id: $vlan_id})
                    MERGE (p)-[:NATIVE_VLAN]->(v)
                    """,
                    device=device, port_name=port_name, vlan_id=native_vlan,
                )

        # LAG membership
        if channel_group:
            po_name = f"Port-channel{channel_group}"
            await self._run(
                """
                MERGE (d:Device {name: $device})
                MERGE (po:Port {name: $po_name, device: $device})
                ON CREATE SET po.oper_status = 'up', po.admin_status = 'up', po.last_seen = $ts
                MERGE (d)-[:HAS_PORT]->(po)
                MERGE (p:Port {name: $port_name, device: $device})
                MERGE (p)-[:MEMBER_OF_LAG]->(po)
                """,
                device=device, po_name=po_name, port_name=port_name, ts=_TS(),
            )

    async def upsert_l2_properties_and_vlans(
        self,
        device: str,
        iface: str,
        mode: Optional[str] = None,
        access_vlan: Optional[int] = None,
        trunk_vlans: Optional[list[int]] = None,
        native_vlan: Optional[int] = None,
        stp_state: Optional[dict] = None,
        vlan_id: Optional[int] = None,
    ):
        iface = self._normalize_port(iface, device)
        """
        Bridge verso upsert_port_l2 + Interface L3 TERMINATES_VLAN.
        Mantiene compatibilità con device_snapshot v3.
        """
        await self.upsert_port_l2(
            device=device,
            port_name=iface,
            mode=mode,
            access_vlan=access_vlan,
            trunk_vlans=trunk_vlans,
            native_vlan=native_vlan,
            stp_state=stp_state,
        )

        # TERMINATES_VLAN su Interface L3 (per sub-interfacce ROAS e SVI)
        if vlan_id:
            await self.upsert_vlan_global(vlan_id)
            await self._run(
                """
                MERGE (i:Interface {name: $iface, device: $device})
                ON CREATE SET i.ip_address = 'unassigned', i.status = 'up'
                WITH i
                MATCH (v:Vlan {vlan_id: $vlan_id})
                MERGE (i)-[:TERMINATES_VLAN]->(v)
                """,
                device=device, iface=iface, vlan_id=vlan_id,
            )

    async def upsert_etherchannel_members(
        self, device: str, po_name: str, members: list[str]
    ):
        po_name = self._normalize_port(po_name, device)
        members = [self._normalize_port(phys, device) for phys in members]
        """Compatibilità: crea MEMBER_OF_LAG tra porte fisiche e Port-Channel."""
        for phys in members:
            await self._run(
                """
                MERGE (d:Device {name: $device})
                MERGE (po:Port {name: $po_name, device: $device})
                ON CREATE SET po.oper_status = 'up', po.admin_status = 'up', po.last_seen = $ts
                MERGE (d)-[:HAS_PORT]->(po)
                MERGE (phys:Port {name: $phys, device: $device})
                ON CREATE SET phys.oper_status = 'up', phys.admin_status = 'up', phys.last_seen = $ts
                MERGE (d)-[:HAS_PORT]->(phys)
                MERGE (phys)-[:MEMBER_OF_LAG]->(po)
                """,
                device=device, po_name=po_name, phys=phys, ts=_TS(),
            )

    async def upsert_interface(
        self, device: str, iface: str, ip: str, status: str, vlan_id: int | None = None
    ):
        iface = self._normalize_port(iface, device)
        await self._run(
            """
            MATCH (d:Device {name: $device})
            MERGE (i:Interface {name: $iface, device: $device})
            SET i.ip_address = $ip,
                i.status     = $status,
                i.vlan_id    = $vlan_id
            MERGE (d)-[:HAS_INTERFACE]->(i)
            """,
            device=device, iface=iface, ip=ip, status=status, vlan_id=vlan_id,
        )

    async def upsert_static_route(self, device: str, network: str, next_hop: str):
        await self._run(
            """
            MATCH (d:Device {name: $device})
            MERGE (r:StaticRoute {network: $network, device: $device})
            SET r.next_hop = $next_hop, r.updated = $ts
            MERGE (d)-[:HAS_ROUTE]->(r)
            """,
            device=device, network=network, next_hop=next_hop, ts=_TS(),
        )

    async def clear_static_routes(self, device: str):
        await self._run(
            """
            MATCH (:Device {name: $device})-[:HAS_ROUTE]->(r:StaticRoute {device: $device})
            DETACH DELETE r
            """,
            device=device,
        )

    async def upsert_dhcp_pool(
        self,
        device: str,
        pool_name: str,
        network: str,
        default_router: str,
        dns_server: str = "8.8.8.8",
    ):
        await self._run(
            """
            MATCH (d:Device {name: $device})
            MERGE (p:DhcpPool {name: $pool_name, device: $device})
            SET p.network         = $network,
                p.default_router  = $default_router,
                p.dns_server      = $dns_server,
                p.updated         = $ts
            MERGE (d)-[:HAS_DHCP_POOL]->(p)
            """,
            device=device, pool_name=pool_name, network=network,
            default_router=default_router, dns_server=dns_server, ts=_TS(),
        )

    async def clear_dhcp_pools(self, device: str):
        await self._run(
            """
            MATCH (:Device {name: $device})-[:HAS_DHCP_POOL]->(p:DhcpPool {device: $device})
            DETACH DELETE p
            """,
            device=device,
        )

    async def store_running_config(self, device: str, running_config: str):
        import os
        store_config = os.getenv("NETAGENT_STORE_CONFIG_NODES", "true").lower() == "true"
        if not store_config:
            await self.delete_running_config(device)
            return
        await self._run(
            """
            MERGE (d:Device {name: $device})
            MERGE (c:DeviceConfig {device: $device})
            SET c.running_config = $rc,
                c.config_updated = $ts
            MERGE (d)-[:HAS_CONFIG]->(c)
            """,
            device=device, rc=running_config, ts=_TS(),
        )

    async def get_running_config(self, device: str) -> str:
        query = (
            "MATCH (d:Device {name:$device})-[:HAS_CONFIG]->(c:DeviceConfig) "
            "RETURN c.running_config AS rc"
        )
        async with self._driver.session() as session:
            result = await session.run(query, device=device)
            record = await result.single()
            if record and record["rc"]:
                return record["rc"]
        return ""

    async def delete_running_config(self, device: str):
        await self._run(
            "MATCH (:Device {name:$device})-[:HAS_CONFIG]->(c:DeviceConfig {device:$device}) "
            "DETACH DELETE c",
            device=device,
        )

    async def store_operational_status(self, device: str, op_status: str):
        await self._run(
            """
            MERGE (d:Device {name: $device})
            MERGE (s:DeviceStatus {device: $device})
            SET s.operational_status = $op_status,
                s.updated = $ts
            MERGE (d)-[:HAS_STATUS]->(s)
            """,
            device=device, op_status=op_status, ts=_TS(),
        )

    async def delete_operational_status(self, device: str):
        await self._run(
            "MATCH (:Device {name:$device})-[:HAS_STATUS]->(s:DeviceStatus {device:$device}) "
            "DETACH DELETE s",
            device=device,
        )

    async def upsert_endpoint(
        self,
        ip: str,
        mac: str,
        switch: str,
        port: str,
        vlan: int,
        device_name: str,
        ts: str,
    ):
        port = self._normalize_port(port, switch)
        async with self._driver.session() as session:
            if switch and port:
                await session.run(
                    """
                    MERGE (d:Device {name: $switch})
                    MERGE (p:Port {name: $port, device: $switch})
                    ON CREATE SET p.oper_status = 'up', p.admin_status = 'up', p.last_seen = $ts
                    MERGE (d)-[:HAS_PORT]->(p)
                    MERGE (ep:Endpoint {ip: $ip})
                    SET ep.mac         = $mac,
                        ep.device_name = $device_name,
                        ep.vlan        = $vlan,
                        ep.switch      = $switch,
                        ep.port        = $port,
                        ep.last_seen   = $ts
                    MERGE (p)-[:CONNECTS_ENDPOINT]->(ep)
                    """,
                    ip=ip, mac=mac, device_name=device_name,
                    vlan=vlan, switch=switch, port=port, ts=ts,
                )
                # Anche su Interface per compatibilità
                await session.run(
                    """
                    MERGE (d:Device {name: $switch})
                    MERGE (i:Interface {name: $port, device: $switch})
                    ON CREATE SET i.ip_address = 'unassigned', i.status = 'up'
                    MERGE (d)-[:HAS_INTERFACE]->(i)
                    MERGE (ep:Endpoint {ip: $ip})
                    MERGE (i)-[:CONNECTS_ENDPOINT]->(ep)
                    """,
                    ip=ip, port=port, switch=switch, ts=ts,
                )
            else:
                await session.run(
                    """
                    MERGE (ep:Endpoint {ip: $ip})
                    SET ep.mac         = $mac,
                        ep.device_name = $device_name,
                        ep.vlan        = $vlan,
                        ep.switch      = $switch,
                        ep.port        = $port,
                        ep.last_seen   = $ts
                    """,
                    ip=ip, mac=mac, device_name=device_name,
                    vlan=vlan, switch=switch, port=port, ts=ts,
                )

    async def clear_endpoints(self):
        await self._run("MATCH (ep:Endpoint) DETACH DELETE ep")

    async def get_endpoints(self) -> list[dict]:
        query = """
        MATCH (ep:Endpoint)
        OPTIONAL MATCH (p:Port)-[:CONNECTS_ENDPOINT]->(ep)
        RETURN ep.ip          AS ip,
               ep.mac         AS mac,
               ep.device_name AS device_name,
               ep.vlan        AS vlan,
               ep.switch      AS switch,
               ep.port        AS port,
               ep.last_seen   AS last_seen,
               p.name         AS switch_port
        ORDER BY ep.ip
        """
        endpoints = []
        async with self._driver.session() as session:
            result = await session.run(query)
            async for rec in result:
                endpoints.append(dict(rec))
        return endpoints
