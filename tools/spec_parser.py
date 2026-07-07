from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass, field
from typing import Optional

from core.state import IntentModel, RouterIntent

logger = logging.getLogger(__name__)


@dataclass
class NetworkSpec:
    name: str
    cidr: str
    gateway: Optional[str] = None
    router: Optional[str] = None
    router_interface: Optional[str] = None
    dhcp: bool = False
    dhcp_pool: Optional[str] = None
    dns: str = "8.8.8.8"
    lease: int = 1
    excluded: Optional[str] = None
    # VLAN: se la rete è servita su una VLAN specifica
    vlan_id: Optional[int] = None
    vlan_name: Optional[str] = None

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.IPv4Network(self.cidr, strict=False)


@dataclass
class DeviceSpec:
    name: str
    profile: str = ""
    interfaces: list[tuple[str, str]] = field(default_factory=list)
    static_routes: list[str] = field(default_factory=list)
    connect_to_networks: list[str] = field(default_factory=list)
    extra_lines: list[str] = field(default_factory=list)
    network_name: Optional[str] = None
    ip_address: Optional[str] = None
    netmask: Optional[str] = None
    gateway: Optional[str] = None
    config_base: dict[str, str] = field(default_factory=dict)
    # VLAN: configurazione porte switch
    vlan_definitions: list[tuple[int, str]] = field(default_factory=list)  # [(vlan_id, name), ...]
    access_ports: list[tuple[str, int]] = field(default_factory=list)       # [(iface, vlan_id), ...]
    trunk_ports: list[tuple[str, list[int], int]] = field(default_factory=list)  # [(iface, [vlans], native), ...]
    # VLAN: subinterface per inter-VLAN routing su cisco_ios
    subinterfaces: list[tuple[str, int, int, str]] = field(default_factory=list)  # [(parent, sub_id, vlan_id, ip/cidr), ...]
    etherchannels: list[tuple[str, list[str], str]] = field(default_factory=list)  # [(pc_name, [members], mode), ...]
    extra_params: str = ""

    @property
    def vendor(self) -> str:
        prof = self.profile.lower()
        if prof in ("cisco_ios", "cisco_switch", "frrouting", "vpcs"):
            return prof
        if "pc" in self.name.lower() or prof == "vpcs":
            return "vpcs"
        if "cisco" in self.name.lower() or "cisco" in prof:
            return "cisco_ios"
        return "frrouting"

    @property
    def needs_dhcp_client(self) -> bool:
        return self.profile == "vpcs" and (
            not self.ip_address or self.ip_address.upper() == "DHCP"
        )


_BOOL_TRUE = {"true", "yes", "si", "sì", "1", "on", "enabled"}


def _strip_comment(line: str) -> str:
    return line.split("#", 1)[0].strip()


def _parse_bool(value: str) -> bool:
    return value.strip().lower() in _BOOL_TRUE


def _parse_key_values(raw: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in raw.splitlines():
        clean = _strip_comment(line)
        if not clean or ":" not in clean:
            continue
        key, value = clean.split(":", 1)
        values[key.strip().upper()] = value.strip()
    return values


def _extract_network_name(value: str) -> Optional[str]:
    match = re.search(r'\(([^)]+)\)', value)
    return match.group(1).strip() if match else None


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _parse_vlan_ids(value: str) -> list[int]:
    """Parsa una lista di VLAN ID separata da virgole: '10,20,30' → [10, 20, 30]."""
    result = []
    for part in value.split(","):
        part = part.strip()
        if part.isdigit():
            result.append(int(part))
    return result


def _parse_etherchannel_value(value: str) -> Optional[tuple[str, list[str], str]]:
    """
    Parsa un intent EtherChannel.

    Formati supportati:
      Port-channel1 members Ethernet0/1,Ethernet0/2 mode active
      Po1 members Ethernet0/1, Ethernet0/2
    """
    ec_m = re.match(
        r'(\S+)\s+members\s+(.+?)(?:\s+mode\s+(\S+))?\s*$',
        value,
        re.IGNORECASE,
    )
    if not ec_m:
        return None
    pc_name = ec_m.group(1)
    members_raw = ec_m.group(2).strip()
    mode = (ec_m.group(3) or "active").lower()
    members = [
        item.strip()
        for item in re.split(r'\s*,\s*', members_raw)
        if item.strip()
    ]
    return pc_name, members, mode


def _parse_networks(spec_raw: str) -> dict[str, NetworkSpec]:
    networks: dict[str, NetworkSpec] = {}
    pattern = re.compile(
        r'---\s*NETWORK:\s*([\w.-]+)\s*---(?P<body>.*?)(?=---\s*(?:NETWORK|DEVICE):|\Z)',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(spec_raw):
        name = match.group(1).strip()
        values = _parse_key_values(match.group("body"))
        cidr = values.get("CIDR") or values.get("NETWORK") or values.get("SUBNET")
        if not cidr:
            logger.warning("[SPEC] Rete '%s' ignorata: CIDR mancante.", name)
            continue
        gateway = values.get("GATEWAY") or values.get("DEFAULT_GATEWAY")

        # VLAN opzionale per la rete
        vlan_id = None
        vlan_name = None
        if "VLAN_ID" in values:
            try:
                vlan_id = int(values["VLAN_ID"])
            except ValueError:
                logger.warning("[SPEC] VLAN_ID non valido per rete '%s': %s", name, values["VLAN_ID"])
        vlan_name = values.get("VLAN_NAME") or (name.upper() if vlan_id else None)

        networks[name] = NetworkSpec(
            name=name,
            cidr=str(ipaddress.IPv4Network(cidr, strict=False)),
            gateway=gateway,
            router=values.get("ROUTER"),
            router_interface=values.get("ROUTER_INTERFACE") or values.get("INTERFACE"),
            dhcp=_parse_bool(values.get("DHCP", "false")),
            dhcp_pool=values.get("DHCP_POOL") or values.get("DHCP_POOL_NAME"),
            dns=values.get("DNS") or values.get("DHCP_DNS") or "8.8.8.8",
            lease=int(values.get("LEASE") or values.get("DHCP_LEASE") or 1),
            excluded=values.get("EXCLUDED") or values.get("DHCP_EXCLUDED"),
            vlan_id=vlan_id,
            vlan_name=vlan_name,
        )
    return networks


def _parse_devices(spec_raw: str) -> dict[str, DeviceSpec]:
    devices: dict[str, DeviceSpec] = {}
    pattern = re.compile(
        r'---\s*DEVICE:\s*([\w-]+)\s*---(?P<body>.*?)(?=---\s*(?:DEVICE|NETWORK):|\Z)',
        re.IGNORECASE | re.DOTALL,
    )
    for match in pattern.finditer(spec_raw):
        name = match.group(1).strip()
        body = match.group("body")
        device = DeviceSpec(name=name)
        pending_iface: Optional[str] = None
        last_key = None

        for raw_line in body.splitlines():
            if raw_line.startswith((" ", "\t")) and last_key:
                clean_line = _strip_comment(raw_line)
                if clean_line:
                    if last_key == "EXTRA_PARAMS":
                        if device.extra_lines:
                            device.extra_lines[-1] = device.extra_lines[-1] + "\n" + clean_line
                        else:
                            device.extra_lines.append(clean_line)
                continue

            line = _strip_comment(raw_line)
            if not line or ":" not in line:
                continue
            key, value = line.split(":", 1)
            key_u = key.strip().upper()
            value = value.strip()
            last_key = key_u

            if key_u == "PROFILE":
                device.profile = value.lower()
            elif key_u == "INTERFACE":
                parts = [part.strip() for part in value.split("|")]
                pending_iface = parts[0]
                ip_part = next((p for p in parts[1:] if p.upper().startswith("IP_ADDRESS:")), None)
                if ip_part:
                    ip_value = ip_part.split(":", 1)[1].strip()
                    device.interfaces.append((pending_iface, ip_value))
                    pending_iface = None
            elif key_u == "IP_ADDRESS":
                device.ip_address = value
                if pending_iface:
                    device.interfaces.append((pending_iface, value))
                    pending_iface = None
            elif key_u == "NETMASK":
                device.netmask = value
            elif key_u == "GATEWAY":
                device.gateway = value
            elif key_u in {"NETWORK", "RETE"}:
                device.network_name = value
            elif key_u == "STATIC_ROUTE":
                device.static_routes.append(value)
            elif key_u == "CONNECT_TO_NETWORKS":
                device.connect_to_networks.extend(_split_csv(value))
            elif key_u == "EXTRA_PARAMS":
                device.extra_lines.append(value)

            # ── VLAN definitions (switch/router) ─────────────────────────────
            elif key_u == "VLAN":
                # Formato: VLAN: 10 NOME_VLAN   oppure   VLAN: 10
                vlan_parts = value.split(None, 1)
                if vlan_parts and vlan_parts[0].isdigit():
                    vid = int(vlan_parts[0])
                    vname = vlan_parts[1].strip() if len(vlan_parts) > 1 else f"VLAN{vid}"
                    device.vlan_definitions.append((vid, vname))

            elif key_u == "VLANS":
                # Formato: VLANS: 10 OFFICE, 20 SERVERS, 30 GUESTS
                for entry in value.split(","):
                    entry = entry.strip()
                    entry_parts = entry.split(None, 1)
                    if entry_parts and entry_parts[0].isdigit():
                        vid = int(entry_parts[0])
                        vname = entry_parts[1].strip() if len(entry_parts) > 1 else f"VLAN{vid}"
                        device.vlan_definitions.append((vid, vname))

            # ── Switchport access ─────────────────────────────────────────────
            elif key_u == "ACCESS_PORT":
                # Formato: ACCESS_PORT: Ethernet0/1 VLAN 10
                ap_m = re.match(r'^(\S+)\s+(?:VLAN\s+)?(\d+)\s*$', value.strip(), re.IGNORECASE)
                if ap_m:
                    device.access_ports.append((ap_m.group(1), int(ap_m.group(2))))
                else:
                    logger.warning("[SPEC] ACCESS_PORT malformato per %s: '%s'", name, value)

            elif key_u == "ACCESS_PORTS":
                # Formato legacy (solo lista di porte senza VLAN): già gestito da config_base
                device.config_base[key_u] = value

            # ── Switchport trunk ─────────────────────────────────────────────
            elif key_u == "TRUNK_PORT":
                # Formato: TRUNK_PORT: Ethernet0/0 VLANS 10,20,30 NATIVE 1
                tp_m = re.match(r'^(\S+)\s+VLANS?\s+([\d,]+)(?:\s+NATIVE\s+(\d+))?\s*$', value.strip(), re.IGNORECASE)
                if tp_m:
                    tp_iface = tp_m.group(1)
                    tp_vlans = _parse_vlan_ids(tp_m.group(2))
                    tp_native = int(tp_m.group(3)) if tp_m.group(3) else 1
                    device.trunk_ports.append((tp_iface, tp_vlans, tp_native))
                else:
                    logger.warning("[SPEC] TRUNK_PORT malformato per %s: '%s'", name, value)

            # ── Subinterface dot1Q (inter-VLAN routing su cisco_ios) ─────────
            elif key_u == "SUBINTERFACE":
                # Formato: SUBINTERFACE: Ethernet0/0.10 VLAN 10 IP 192.168.10.1/24
                si_m = re.match(
                    r'^(\S+)\.(\d+)\s+VLAN\s+(\d+)\s+IP\s+([\d./]+)\s*$',
                    value.strip(), re.IGNORECASE,
                )
                if si_m:
                    device.subinterfaces.append((
                        si_m.group(1),       # parent_iface
                        int(si_m.group(2)),  # sub_id
                        int(si_m.group(3)),  # vlan_id
                        si_m.group(4),       # ip/cidr
                    ))
                else:
                    logger.warning("[SPEC] SUBINTERFACE malformato per %s: '%s'", name, value)

            elif key_u == "ETHERCHANNEL":
                # Formato: ETHERCHANNEL: Port-channel1 members Ethernet0/1, Ethernet0/2 mode active
                parsed_ec = _parse_etherchannel_value(value)
                if parsed_ec:
                    pc_name, members, mode = parsed_ec
                    device.etherchannels.append((pc_name, members, mode))
                else:
                    logger.warning("[SPEC] ETHERCHANNEL malformato per %s: '%s'", name, value)

            elif key_u in {"CONFIGURAZIONEBASE", "CONFIGURAZIONE_BASE", "BASE_CONFIG"}:
                device.config_base["CONFIGURAZIONE_BASE"] = value
            elif key_u.startswith("BASE_") or key_u in {
                "HOSTNAME", "BANNER", "ENABLE_SECRET", "ENABLE_PASSWORD",
                "USERNAME", "PASSWORD", "DOMAIN_NAME", "DOMINIO",
                "SSH_USERNAME", "SSH_PASSWORD", "SSH_TIMEOUT", "SSH_RETRIES",
                "LOGIN_BLOCK_FOR", "LOGIN_ATTEMPTS", "LOGIN_WITHIN",
                "PASSWORD_MIN_LENGTH", "CONSOLE_TIMEOUT", "VTY_TIMEOUT", "VTY_LINES",
                "SERVICE_PASSWORD_ENCRYPTION", "NO_CDP_RUN",
                "UPLINK_PORTS", "TRUSTED_PORTS", "DHCP_SNOOPING_VLANS",
                "ARP_INSPECTION_VLANS", "PORT_SECURITY", "PORT_SECURITY_MAX",
                "PORT_SECURITY_VIOLATION", "PORTFAST", "BPDUGUARD",
                "DHCP_RATE_LIMIT",
            }:
                device.config_base[key_u] = value
            elif key_u.startswith("DHCP_"):
                device.extra_lines.append(f"{key_u}: {value}")

        devices[name] = device
    return devices


def _host_extra(device: DeviceSpec, networks: dict[str, NetworkSpec]) -> str:
    if device.ip_address and device.ip_address.lower().startswith("dhcp"):
        return "ip dhcp"

    network_name = device.network_name or _extract_network_name(device.ip_address or "")
    network = networks.get(network_name or "")
    if network and (not device.ip_address or "(" in device.ip_address):
        return "ip dhcp" if network.dhcp else ""

    if device.ip_address and device.netmask and device.gateway:
        return f"ip {device.ip_address} {device.netmask} {device.gateway}"

    if device.ip_address and "/" in device.ip_address and device.gateway:
        ip_iface = ipaddress.IPv4Interface(device.ip_address)
        return f"ip {ip_iface.ip} {ip_iface.network.netmask} {device.gateway}"

    return " ".join(device.extra_lines)


def _device_l3_interfaces(devices: dict[str, DeviceSpec], networks: dict[str, NetworkSpec]) -> dict[str, list[ipaddress.IPv4Interface]]:
    by_device: dict[str, list[ipaddress.IPv4Interface]] = {}
    for device in devices.values():
        if device.profile == "vpcs" or device.name.lower().startswith("pc") or device.profile == "cisco_switch":
            continue
        entries: list[ipaddress.IPv4Interface] = []
        for _, ip_value in device.interfaces:
            if ip_value and "/" in ip_value and not ip_value.lower().startswith("dhcp"):
                entries.append(ipaddress.IPv4Interface(ip_value))
        # Subinterface dot1Q contano come interfacce L3
        for _, _, _, ip_cidr in device.subinterfaces:
            if ip_cidr and "/" in ip_cidr:
                entries.append(ipaddress.IPv4Interface(ip_cidr))
        for net in networks.values():
            if net.router == device.name:
                gateway = net.gateway or str(next(net.network.hosts()))
                entries.append(ipaddress.IPv4Interface(f"{gateway}/{net.network.prefixlen}"))
        by_device[device.name] = entries
    return by_device


def _desired_static_routes(
    source: DeviceSpec,
    devices: dict[str, DeviceSpec],
    networks: dict[str, NetworkSpec],
) -> list[str]:
    if not source.connect_to_networks:
        return []

    l3 = _device_l3_interfaces(devices, networks)
    source_ifaces = l3.get(source.name, [])
    adjacency: dict[str, set[str]] = {name: set() for name in l3}

    for a, a_ifaces in l3.items():
        for b, b_ifaces in l3.items():
            if a >= b:
                continue
            if any(ai.network == bi.network for ai in a_ifaces for bi in b_ifaces):
                adjacency[a].add(b)
                adjacency[b].add(a)

    routes: list[str] = []
    for target_raw in source.connect_to_networks:
        target_cidr = networks[target_raw].cidr if target_raw in networks else target_raw
        target_net = ipaddress.IPv4Network(target_cidr, strict=False)
        if any(iface.network == target_net for iface in source_ifaces):
            continue

        target_devices = [
            name for name, ifaces in l3.items()
            if name != source.name and any(iface.network == target_net for iface in ifaces)
        ]
        if not target_devices:
            logger.warning("[SPEC] Nessun device possiede la rete target %s richiesta da %s.", target_net, source.name)
            continue

        queue: list[list[str]] = [[source.name]]
        visited = {source.name}
        path: Optional[list[str]] = None
        while queue and not path:
            current_path = queue.pop(0)
            current = current_path[-1]
            if current in target_devices:
                path = current_path
                break
            for neighbor in sorted(adjacency.get(current, [])):
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append(current_path + [neighbor])

        if not path or len(path) < 2:
            logger.warning("[SPEC] Impossibile calcolare rotta deterministica %s -> %s.", source.name, target_net)
            continue

        next_device = path[1]
        next_hop = None
        for src_iface in l3[source.name]:
            for neighbor_iface in l3[next_device]:
                if src_iface.network == neighbor_iface.network:
                    next_hop = str(neighbor_iface.ip)
                    break
            if next_hop:
                break
        if next_hop:
            routes.append(f"ip route {target_net.network_address}/{target_net.prefixlen} {next_hop}")

    return routes


def _router_extra(device: DeviceSpec, devices: dict[str, DeviceSpec], networks: dict[str, NetworkSpec]) -> tuple[list[str], str]:
    interfaces: list[str] = []
    lines: list[str] = [f"PROFILE: {device.profile}"]

    for iface, ip_value in device.interfaces:
        interfaces.append(iface)
        if ip_value and not ip_value.lower().startswith("dhcp"):
            lines.append(f"Configure {iface} with {ip_value}")

    for net in networks.values():
        if net.router != device.name:
            continue
        if net.router_interface:
            if net.router_interface not in interfaces:
                interfaces.append(net.router_interface)
            
            # FIX: Se c'è una VLAN, l'IP va sulla subinterface, NON sull'interfaccia fisica genitore!
            if not net.vlan_id:
                gateway = net.gateway or str(next(net.network.hosts()))
                lines.append(f"Configure {net.router_interface} with {gateway}/{net.network.prefixlen}")
        if net.dhcp:
            gateway = net.gateway or str(next(net.network.hosts()))
            pool_name = net.dhcp_pool or f"{net.name.upper()}_POOL"
            lines.extend([
                f"DHCP_POOL_NAME: {pool_name}",
                f"DHCP_NETWORK: {net.network}",
                f"DHCP_ROUTER: {gateway}",
                f"DHCP_DNS: {net.dns}",
                f"DHCP_LEASE: {net.lease}",
            ])
            if net.excluded:
                lines.append(f"DHCP_EXCLUDED: {net.excluded}")

    lines.extend(_desired_static_routes(device, devices, networks))
    lines.extend(device.static_routes)
    lines.extend(device.extra_lines)

    # Deduce gateway for the switch/router if not explicitly set but present in the network specs
    gw = device.gateway
    if not gw:
        for iface, ip_val in device.interfaces:
            if not ip_val or ip_val.lower().startswith("dhcp"):
                continue
            try:
                iface_ip = ipaddress.IPv4Interface(ip_val).ip
                for net in networks.values():
                    net_obj = ipaddress.IPv4Network(net.cidr, strict=False)
                    if iface_ip in net_obj and net.gateway:
                        gw = net.gateway
                        break
                if gw:
                    break
            except ValueError:
                pass

    if gw:
        lines.append(f"DEFAULT_GATEWAY: {gw}")

    # ── VLAN definitions (switch e router) ───────────────────────────────────
    for vlan_id, vlan_name in device.vlan_definitions:
        lines.append(f"VLAN_DEF: {vlan_id} {vlan_name}")

    # Determina l'assegnazione automatica della configurazione switchport trunk/access
    # del Port-channel logico ai relativi membri fisici dell'EtherChannel per prevenire LACP suspended (SD)
    pc_to_members = {pc.lower(): members for pc, members, _ in device.etherchannels}

    # ── Switchport access ────────────────────────────────────────────────────
    propagated_access = []
    for iface, vlan_id in device.access_ports:
        if iface.lower() in pc_to_members:
            for member in pc_to_members[iface.lower()]:
                if not any(ap[0].lower() == member.lower() for ap in device.access_ports) and \
                   not any(pa[0].lower() == member.lower() for pa in propagated_access):
                    propagated_access.append((member, vlan_id))
    device.access_ports.extend(propagated_access)

    for iface, vlan_id in device.access_ports:
        lines.append(f"ACCESS_PORT: {iface} {vlan_id}")
        if iface not in interfaces:
            interfaces.append(iface)

    # ── Switchport trunk ─────────────────────────────────────────────────────
    propagated_trunk = []
    for iface, trunk_vlans, native_vlan in device.trunk_ports:
        if iface.lower() in pc_to_members:
            for member in pc_to_members[iface.lower()]:
                if not any(tp[0].lower() == member.lower() for tp in device.trunk_ports) and \
                   not any(pt[0].lower() == member.lower() for pt in propagated_trunk):
                    propagated_trunk.append((member, trunk_vlans, native_vlan))
    device.trunk_ports.extend(propagated_trunk)

    for iface, trunk_vlans, native_vlan in device.trunk_ports:
        vlans_str = ",".join(str(v) for v in trunk_vlans)
        lines.append(f"TRUNK_PORT: {iface} VLANS {vlans_str} NATIVE {native_vlan}")
        if iface not in interfaces:
            interfaces.append(iface)

    # ── Subinterface dot1Q (inter-VLAN routing) ───────────────────────────────
    for parent, sub_id, vlan_id, ip_cidr in device.subinterfaces:
        lines.append(f"SUBINTERFACE: {parent}.{sub_id} VLAN {vlan_id} IP {ip_cidr}")
        parent_full = parent
        if parent_full not in interfaces:
            interfaces.append(parent_full)

    # ── EtherChannel ─────────────────────────────────────────────────────────
    for pc_name, members, mode in device.etherchannels:
        members_str = ",".join(members)
        lines.append(f"ETHERCHANNEL: {pc_name} members {members_str} mode {mode}")
        if pc_name not in interfaces:
            interfaces.append(pc_name)
        for m in members:
            if m not in interfaces:
                interfaces.append(m)

    # ── VLAN dedotte dal network registry ────────────────────────────────────
    # Se una rete referenzia questo device come router e ha un VLAN_ID,
    # generiamo automaticamente la SUBINTERFACE corrispondente (se non
    # già dichiarata esplicitamente) usando il ROUTER_INTERFACE come parent.
    if device.profile in ("cisco_ios",):
        existing_sub_parents = {(p, sid) for p, sid, _, _ in device.subinterfaces}
        for net in networks.values():
            if net.router != device.name or not net.vlan_id or not net.router_interface:
                continue
            gateway = net.gateway or str(next(net.network.hosts()))
            ip_cidr_str = f"{gateway}/{net.network.prefixlen}"
            # Usa vlan_id come sub_id di default (convenzione comune)
            sub_id = net.vlan_id
            parent_iface = net.router_interface
            if (parent_iface, sub_id) not in existing_sub_parents:
                lines.append(
                    f"SUBINTERFACE: {parent_iface}.{sub_id} VLAN {net.vlan_id} IP {ip_cidr_str}"
                )
                if parent_iface not in interfaces:
                    interfaces.append(parent_iface)

    if _parse_bool(device.config_base.get("CONFIGURAZIONE_BASE", "false")):
        for key, value in device.config_base.items():
            lines.append(f"{key}: {value}")

    return interfaces, "\n".join(lines)


def parse_spec_to_intent(spec_raw: str) -> Optional[IntentModel]:
    """Parse the text IaC format into the same IntentModel produced by the LLM planner."""
    import warnings
    warnings.warn(
        "parse_spec_to_intent is deprecated and will be removed in a future version. "
        "Please use the standard YAML Pydantic input parser (NetworkIntentSchema).",
        DeprecationWarning,
        stacklevel=2
    )
    logger.warning(
        "DEPRECATION WARNING: Utilizing legacy text specification parser. "
        "Convert specification to YAML Pydantic format."
    )
    devices = _parse_devices(spec_raw)
    if not devices:
        return None

    networks = _parse_networks(spec_raw)
    plans: list[RouterIntent] = []

    for device in devices.values():
        if device.profile == "vpcs" or device.name.lower().startswith("pc"):
            interfaces = [iface for iface, _ in device.interfaces] or ["eth0"]
            extra = _host_extra(device, networks)
        else:
            interfaces, extra = _router_extra(device, devices, networks)
            interfaces = interfaces or [iface for iface, _ in device.interfaces]

        plans.append(RouterIntent(
            router_name=device.name,
            interfaces=interfaces,
            extra_params=extra,
        ))

    return IntentModel(protocol="Declarative", router_plans=plans)


class SpecFileParser:
    pass


def parse_spec_file(spec_raw: str) -> tuple[list[DeviceSpec], IntentModel]:
    devices = _parse_devices(spec_raw)
    networks = _parse_networks(spec_raw)
    
    # Calculate interfaces and extra_params for each device first without modifying in place
    resolved_data = {}
    for name, device in devices.items():
        if device.profile == "vpcs" or device.name.lower().startswith("pc"):
            ifaces = [iface for iface, _ in device.interfaces] or ["eth0"]
            extra = _host_extra(device, networks)
        else:
            ifaces, extra = _router_extra(device, devices, networks)
            ifaces = ifaces or [iface for iface, _ in device.interfaces]
        resolved_data[name] = (ifaces, extra)

    # Assign dynamically to DeviceSpec for test compatibility after all calculations are done
    for name, device in devices.items():
        ifaces, extra = resolved_data[name]
        device.interfaces = ifaces
        device.extra_params = extra

    intent = parse_spec_to_intent(spec_raw)
    if intent is None:
        intent = IntentModel(protocol="Static", router_plans=[])
        
    # Check if there is any DHCP client in device specs to set protocol to DHCP
    if intent:
        has_dhcp = any(getattr(d, "needs_dhcp_client", False) for d in devices.values())
        if has_dhcp:
            intent.protocol = "DHCP"
        else:
            intent.protocol = "Static"

    return list(devices.values()), intent


def get_vendor_for_device(name: str, specs: list[DeviceSpec]) -> str:
    for s in specs:
        if s.name == name:
            if s.profile:
                return s.profile
    name_l = name.lower()
    if "pc" in name_l:
        return "vpcs"
    if "cisco" in name_l:
        return "cisco_ios"
    return "frrouting"

