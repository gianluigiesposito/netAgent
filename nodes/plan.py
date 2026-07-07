# nodes/plan.py
import logging
from core.state import AgentState, NetworkIntentSchema
from tools.graph_store import AsyncNetworkGraphStore  # Verifica se il tuo import è tools.graph_store o tools.network_graph_store
from tools.spec_parser import parse_spec_to_intent
from llm.async_client import llm_client

logger = logging.getLogger(__name__)


def _coerce_legacy_to_schema(legacy_plan) -> NetworkIntentSchema:
    from core.state import DeviceIntent, InterfaceIntent, StaticRouteIntent, DhcpPoolIntent, LinkIntent
    from tools.parser import load_inventory, resolve_vendor
    import re
    import ipaddress

    inventory = load_inventory()
    devices = []

    for rp in legacy_plan.router_plans:
        name = rp.router_name
        extra = rp.extra_params or ""
        
        # Load profile from inventory or heuristic
        dev_cfg = inventory.get(name, {})
        profile = resolve_vendor(dev_cfg, name)
        # normalize profile to Literal values
        if profile == "cisco_ios" or profile == "cisco":
            profile = "cisco_ios"
        elif profile == "cisco_switch":
            profile = "cisco_switch"
        elif profile == "vpcs":
            profile = "vpcs"
        else:
            profile = "frrouting"

        device_intent = DeviceIntent(
            name=name,
            profile=profile,
            interfaces=[],
            static_routes=[],
            dhcp_pools=[],
            vlans={},
            extra_params=extra
        )

        # Temp storage for parsing dhcp pools
        dhcp_pools_dict = {}
        lines = extra.splitlines()
        
        iface_map = {}
        for iface_name in rp.interfaces:
            iface_map[iface_name] = InterfaceIntent(name=iface_name)

        def get_or_create_iface(iname: str) -> InterfaceIntent:
            if iname not in iface_map:
                iface_map[iname] = InterfaceIntent(name=iname)
            return iface_map[iname]

        for line in lines:
            line = line.strip()
            if not line:
                continue

            if line.startswith("PROFILE:"):
                prof_val = line.split(":", 1)[1].strip().lower()
                if prof_val in ("cisco_ios", "cisco_switch", "frrouting", "vpcs"):
                    device_intent.profile = prof_val
                continue

            match = re.match(r"^Configure\s+(\S+)\s+with\s+(\S+)$", line, re.IGNORECASE)
            if match:
                iface_name, ip_val = match.groups()
                iface = get_or_create_iface(iface_name)
                iface.ip = ip_val
                continue

            # DHCP Pool details
            found_dhcp_field = False
            for prefix, field in [
                ("DHCP_POOL_NAME:", "name"),
                ("DHCP_NETWORK:", "network"),
                ("DHCP_ROUTER:", "gateway"),
                ("DHCP_DNS:", "dns"),
                ("DHCP_LEASE:", "lease"),
                ("DHCP_EXCLUDED:", "excluded")
            ]:
                if line.startswith(prefix):
                    val = line.split(prefix, 1)[1].strip()
                    if prefix == "DHCP_POOL_NAME:":
                        active_pool_name = val
                        dhcp_pools_dict[active_pool_name] = {"name": active_pool_name}
                    elif 'active_pool_name' in locals() and active_pool_name in dhcp_pools_dict:
                        if field == "lease":
                            try:
                                dhcp_pools_dict[active_pool_name]["lease"] = int(val)
                            except ValueError:
                                dhcp_pools_dict[active_pool_name]["lease"] = 1
                        elif field == "excluded":
                            ex_parts = val.split()
                            if len(ex_parts) >= 1:
                                dhcp_pools_dict[active_pool_name]["excluded_start"] = ex_parts[0]
                            if len(ex_parts) >= 2:
                                dhcp_pools_dict[active_pool_name]["excluded_end"] = ex_parts[1]
                        else:
                            dhcp_pools_dict[active_pool_name][field] = val
                    found_dhcp_field = True
                    break
            
            if found_dhcp_field:
                continue

            if line.startswith("VLAN_DEF:"):
                parts = line.split("VLAN_DEF:", 1)[1].strip().split(None, 1)
                if len(parts) >= 1:
                    vid = int(parts[0])
                    vname = parts[1] if len(parts) > 1 else f"VLAN_{vid}"
                    device_intent.vlans[vid] = vname
                continue

            if line.startswith("ACCESS_PORT:"):
                parts = line.split("ACCESS_PORT:", 1)[1].strip().split()
                if len(parts) >= 2:
                    iface_name = parts[0]
                    vid = int(parts[1])
                    iface = get_or_create_iface(iface_name)
                    iface.mode = "access"
                    iface.access_vlan = vid
                continue

            if line.startswith("TRUNK_PORT:"):
                match_trunk = re.match(r"^TRUNK_PORT:\s+(\S+)\s+VLANS\s+(\S+)(?:\s+NATIVE\s+(\d+))?$", line, re.IGNORECASE)
                if match_trunk:
                    iface_name, vlans_str, native_str = match_trunk.groups()
                    iface = get_or_create_iface(iface_name)
                    iface.mode = "trunk"
                    iface.trunk_vlans = [int(v) for v in vlans_str.split(",") if v.isdigit()]
                    if native_str:
                        iface.native_vlan = int(native_str)
                continue

            if line.startswith("SUBINTERFACE:"):
                match_sub = re.match(r"^SUBINTERFACE:\s+(\S+)\s+VLAN\s+(\d+)\s+IP\s+(\S+)$", line, re.IGNORECASE)
                if match_sub:
                    sub_name, vlan_id, ip_val = match_sub.groups()
                    iface = get_or_create_iface(sub_name)
                    iface.vlan_id = int(vlan_id)
                    iface.ip = ip_val
                continue

            if line.startswith("ETHERCHANNEL:"):
                match_ec = re.match(r"^ETHERCHANNEL:\s+(\S+)\s+members\s+(\S+)\s+mode\s+(\S+)$", line, re.IGNORECASE)
                if match_ec:
                    pc_name, members_str, ec_mode = match_ec.groups()
                    pc_iface = get_or_create_iface(pc_name)
                    try:
                        pc_num = int(re.search(r'\d+', pc_name).group())
                    except Exception:
                        pc_num = 1
                    pc_iface.channel_group = pc_num
                    
                    for m_name in members_str.split(","):
                        m_iface = get_or_create_iface(m_name)
                        m_iface.channel_group = pc_num
                        if ec_mode in ("active", "passive", "on"):
                            m_iface.channel_mode = ec_mode
                continue

            if line.startswith("DHCP_RELAY:"):
                val = line.split("DHCP_RELAY:", 1)[1].strip()
                device_intent.dhcp_relay_subnets = [s.strip() for s in val.split(",") if s.strip()]
                continue
            if line.startswith("DHCP_SERVER:"):
                val = line.split("DHCP_SERVER:", 1)[1].strip()
                device_intent.dhcp_relay_server = val
                continue

            if line.startswith("ip route "):
                route_cmd = line.split("ip route ", 1)[1].strip()
                r_parts = route_cmd.split()
                if len(r_parts) == 2:
                    net, nh = r_parts
                    try:
                        ipaddress.IPv4Network(net, strict=False)
                        device_intent.static_routes.append(StaticRouteIntent(network=net, next_hop=nh))
                    except Exception:
                        pass
                elif len(r_parts) == 3:
                    net_ip, mask, nh = r_parts
                    try:
                        net = str(ipaddress.IPv4Interface(f"{net_ip}/{mask}").network)
                        device_intent.static_routes.append(StaticRouteIntent(network=net, next_hop=nh))
                    except Exception:
                        pass
                continue

        for pool_data in dhcp_pools_dict.values():
            if "network" in pool_data and "gateway" in pool_data:
                pname = pool_data.get("name", "POOL")
                device_intent.dhcp_pools.append(
                    DhcpPoolIntent(
                        name=pname,
                        network=pool_data["network"],
                        gateway=pool_data["gateway"],
                        dns=pool_data.get("dns", "8.8.8.8"),
                        lease=pool_data.get("lease", 1),
                        excluded_start=pool_data.get("excluded_start"),
                        excluded_end=pool_data.get("excluded_end")
                    )
                )

        device_intent.interfaces = list(iface_map.values())
        devices.append(device_intent)

    return NetworkIntentSchema(devices=devices, links=[])


async def plan_node(state: AgentState) -> dict:
    """
    Nodo PLAN (Macro-Strategia Semantica):
    Interroga lo snapshot del Knowledge Graph (GraphRAG), integra le adiacenze desiderate
    e i vincoli fisici di raggiungibilità, e inoltra il contesto normalizzato al LLM.
    """
    logger.info(">>> PLAN <<<")

    user_task = state.get("user_task", "")
    specification_raw = state.get("specification_raw", "")
    reachability = state.get("reachability", {})

    # Se l'intento strutturato è già stato generato/caricato in PARSE_INPUT, usiamolo direttamente
    intent = state.get("intent")
    from core.state import NetworkIntentSchema
    if intent and isinstance(intent, NetworkIntentSchema):
        msg = f"PLAN: Specifica strutturata già presente (devices: {[d.name for d in intent.devices]}). Skip LLM Planning."
        logger.info(msg)
        return {"plan": intent, "execution_log": [msg]}

    if specification_raw:
        # Fallback per specifiche testuali non strutturate
        deterministic_plan = parse_spec_to_intent(specification_raw)
        if deterministic_plan:
            coerced = _coerce_legacy_to_schema(deterministic_plan)
            msg = (
                f"PLAN: protocol={deterministic_plan.protocol}, "
                f"devices={[d.name for d in coerced.devices]}, "
                "source=deterministic_spec_parser"
            )
            logger.info(msg)
            return {"plan": coerced, "execution_log": [msg]}
    
    # Isoliando i nodi offline
    unreachable = [k for k, v in reachability.items() if v == "UNREACHABLE"]

    # Estrazione dello snapshot topologico da Neo4j
    from tools.graph_store import AsyncNetworkGraphStore
    async with AsyncNetworkGraphStore() as store:
        topology = await store.get_topology_summary()

    # FIX DEL MISMATCH DI FIRMA: Mappiamo i parametri esattamente secondo il contratto di async_client.py
    plan = await llm_client.generate_plan(
        intent_text=user_task,
        topology_dump=topology,
        unreachable_routers=unreachable,      # Nome parametro allineato ad async_client.py
        specification_raw=specification_raw,  # Passiamo la specifica per azzerare le allucinazioni IP
    )

    msg = (
        f"PLAN: devices={[d.name for d in plan.devices]}, "
        f"skipped={unreachable}"
    )
    logger.info(msg)

    return {"plan": plan, "execution_log": [msg]}
