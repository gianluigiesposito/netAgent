# generate/diff/engine.py
"""
Motore di diff idempotente — Logica pura basata su CiscoConfParse, zero I/O (SRP).

Ogni funzione confronta lo stato desiderato con lo stato attuale
e produce un delta chirurgico contenente solo le differenze.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Optional, Callable
from ciscoconfparse import CiscoConfParse

from generate.models.deltas import (
    BaseConfig,
    BaseConfigDelta,
    InterfaceDelta,
    RouteDelta,
    SubinterfaceDelta,
    SwitchportDelta,
    VlanDelta,
    VPCSDelta,
    EtherChannelDelta,
)
from tools.parser import normalize_interface_name

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Utility interne e Helper CiscoConfParse
# ─────────────────────────────────────────────────────────────────────────────

def _parse_cidr_from_stored(stored: str) -> tuple[str, int]:
    """Separa 'ip/cidr' in (ip, cidr_int). Ritorna cidr=-1 se non presente."""
    if "/" in stored:
        ip_part, cidr_part = stored.split("/", 1)
        return ip_part.strip(), int(cidr_part.strip())
    return stored.strip(), -1


def _ips_equivalent(desired_ip: str, desired_cidr: int, stored: str) -> bool:
    """
    Confronta IP+CIDR desiderato con il valore salvato nel DB.
    Usa ipaddress per normalizzazione: evita falsi positivi.
    Supports comma-separated stored IPs.
    """
    if not stored or stored == "unassigned":
        return False

    for item in stored.split(","):
        item = item.strip()
        if not item:
            continue
        stored_ip, stored_cidr = _parse_cidr_from_stored(item)
        try:
            ip_match = (
                ipaddress.ip_address(desired_ip) == ipaddress.ip_address(stored_ip)
            )
            if stored_cidr == -1:
                if ip_match:
                    return True
            elif ip_match and desired_cidr == stored_cidr:
                return True
        except ValueError:
            pass
    return False


def _mask_to_cidr(mask: str) -> int:
    """Converte una maschera dotted-decimal in prefix length CIDR."""
    try:
        return ipaddress.IPv4Network(f"0.0.0.0/{mask}").prefixlen
    except ValueError:
        return 24


def _parse_vlan_list(value: str) -> list[int]:
    """Parsa liste IOS tipo 10,20,30 o 10-12."""
    vlans: list[int] = []
    for part in (value or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start_s, end_s = part.split("-", 1)
            if start_s.strip().isdigit() and end_s.strip().isdigit():
                start, end = int(start_s), int(end_s)
                if start <= end:
                    vlans.extend(range(start, end + 1))
            continue
        if part.isdigit():
            vlans.append(int(part))
    return sorted(set(vlans))


def _find_interface_obj(iface: str, parse: CiscoConfParse):
    """Trova l'oggetto di configurazione per un'interfaccia, supportando nomi normalizzati."""
    norm_iface = normalize_interface_name(iface)
    for obj in parse.find_objects(r"^interface\s+"):
        parts = obj.text.split()
        if len(parts) >= 2:
            if normalize_interface_name(parts[1]) == norm_iface:
                return obj
    return None


def _is_interface_shutdown(iface: str, parse: CiscoConfParse) -> bool:
    """Rileva se un'interfaccia ha il comando shutdown applicato nel running-config."""
    obj = _find_interface_obj(iface, parse)
    if not obj:
        return False
    for child in obj.children:
        if re.match(r'^shutdown\b', child.text.strip(), re.IGNORECASE):
            return True
    return False


def _running_config_has(parse: CiscoConfParse, command: str) -> bool:
    """Verifica se un comando globale è presente nel running-config."""
    cmd_stripped = command.strip()
    return bool(parse.find_lines(rf"^{re.escape(cmd_stripped)}$"))


def _rsa_key_present(parse: CiscoConfParse) -> bool:
    """Rileva se una chiave RSA per SSH è già stata generata."""
    return bool(
        parse.find_lines(r"ip ssh version") or
        parse.find_lines(r"rsa keys")
    )


def _has_command_in_block(parse: CiscoConfParse, block_header_re: str, command: str) -> bool:
    """Verifica se un blocco (es. line con 0) contiene un sotto-comando specifico."""
    parents = parse.find_objects(rf"^{block_header_re}")
    for parent in parents:
        for child in parent.children:
            if re.search(rf"^\s*{re.escape(command.strip())}\b", child.text.strip(), re.IGNORECASE):
                return True
    return False


def _parse_vlan_database(parse: CiscoConfParse) -> dict[int, str]:
    """Estrae il database VLAN locale dal running-config Cisco."""
    vlans = {}
    vlan_objs = parse.find_objects(r"^vlan\s+(\d+)")
    for obj in vlan_objs:
        m = re.match(r"^vlan\s+(\d+)", obj.text, re.IGNORECASE)
        if m:
            vlan_id = int(m.group(1))
            vlan_name = ""
            for child in obj.children:
                name_m = re.match(r"^\s*name\s+(\S+)", child.text, re.IGNORECASE)
                if name_m:
                    vlan_name = name_m.group(1).upper()
                    break
            vlans[vlan_id] = vlan_name
    return vlans


def _parse_switchport_config(iface: str, parse: CiscoConfParse) -> dict:
    """Estrae la configurazione switchport di un'interfaccia dal running-config."""
    result = {"mode": "", "access_vlan": 0, "trunk_vlans": [], "native_vlan": 1, "portfast": False, "is_shutdown": False}
    obj = _find_interface_obj(iface, parse)
    if not obj:
        return result

    for child in obj.children:
        s = child.text.strip()
        if re.match(r'^switchport\s+mode\s+access', s, re.IGNORECASE):
            result["mode"] = "access"
        elif re.match(r'^switchport\s+mode\s+trunk', s, re.IGNORECASE):
            result["mode"] = "trunk"
        elif re.match(r'^shutdown\b', s, re.IGNORECASE):
            result["is_shutdown"] = True

        av = re.match(r'^switchport\s+access\s+vlan\s+(\d+)', s, re.IGNORECASE)
        if av:
            result["access_vlan"] = int(av.group(1))

        tv = re.match(r'^switchport\s+trunk\s+allowed\s+vlan(?:\s+add)?\s+([\d,-]+)', s, re.IGNORECASE)
        if tv:
            result["trunk_vlans"].extend(_parse_vlan_list(tv.group(1)))

        nv = re.match(r'^switchport\s+trunk\s+native\s+vlan\s+(\d+)', s, re.IGNORECASE)
        if nv:
            result["native_vlan"] = int(nv.group(1))

        if re.search(r'spanning-tree\s+portfast', s, re.IGNORECASE):
            result["portfast"] = True

    return result


def _parse_subinterface_state(
    iface_name: str,
    parse: CiscoConfParse,
) -> tuple[str, int, Optional[int]]:
    """Estrae IP/CIDR e VLAN ID di una subinterface dal running-config."""
    obj = _find_interface_obj(iface_name, parse)
    if not obj:
        return "unassigned", 0, None

    encap_vlan = None
    ip = "unassigned"
    cidr = 0
    for child in obj.children:
        s = child.text.strip()
        encap_m = re.match(r'^encapsulation\s+dot1q\s+(\d+)', s, re.IGNORECASE)
        if encap_m:
            encap_vlan = int(encap_m.group(1))
        ip_m = re.match(r'^ip\s+address\s+([\d.]+)\s+([\d.]+)', s, re.IGNORECASE)
        if ip_m:
            ip   = ip_m.group(1)
            cidr = _mask_to_cidr(ip_m.group(2))
    return ip, cidr, encap_vlan


def _parse_channel_group(iface: str, parse: CiscoConfParse) -> tuple[Optional[int], Optional[str]]:
    """Estrae (channel_group_id, mode) per una specifica interfaccia fisica."""
    obj = _find_interface_obj(iface, parse)
    if not obj:
        return None, None

    for child in obj.children:
        s = child.text.strip()
        m = re.match(r'^channel-group\s+(\d+)\s+mode\s+(\S+)', s, re.IGNORECASE)
        if m:
            return int(m.group(1)), m.group(2).lower()
    return None, None


def _get_current_channel_members(pc_id: int, parse: CiscoConfParse) -> list[str]:
    """Trova tutte le interfacce fisiche che hanno channel-group pc_id nel running-config."""
    members = []
    for obj in parse.find_objects(r"^interface\s+"):
        parts = obj.text.split()
        if len(parts) < 2:
            continue
        iface_name = parts[1]
        if "port-channel" in iface_name.lower():
            continue
        for child in obj.children:
            s = child.text.strip()
            m = re.match(r'^channel-group\s+(\d+)\b', s, re.IGNORECASE)
            if m and int(m.group(1)) == pc_id:
                members.append(iface_name)
    return members


# ─────────────────────────────────────────────────────────────────────────────
# Diff interfacce
# ─────────────────────────────────────────────────────────────────────────────

def diff_interface(
    iface: str,
    desired_ip: str,
    desired_cidr: int,
    current_interfaces: dict[str, str],
    running_config_raw: Optional[str] = None,
) -> InterfaceDelta:
    """
    Produce il delta per una singola interfaccia.
    """
    norm_iface = normalize_interface_name(iface)
    norm_current = {normalize_interface_name(k): v for k, v in current_interfaces.items()}
    current_value = norm_current.get(norm_iface, "unassigned")

    is_shutdown = False
    if running_config_raw:
        parse = CiscoConfParse((running_config_raw or "").splitlines(), factory=False)
        is_shutdown = _is_interface_shutdown(iface, parse)

    if _ips_equivalent(desired_ip, desired_cidr, current_value) and not is_shutdown:
        return InterfaceDelta(
            iface=iface,
            desired_ip=desired_ip,
            desired_cidr=desired_cidr,
            current_ip=current_value,
            action_needed="CORRECT",
        )

    if not current_value or current_value == "unassigned":
        return InterfaceDelta(
            iface=iface,
            desired_ip=desired_ip,
            desired_cidr=desired_cidr,
            current_ip=current_value,
            action_needed="EMPTY",
        )

    stale_ip, stale_cidr = _parse_cidr_from_stored(current_value)
    return InterfaceDelta(
        iface=iface,
        desired_ip=desired_ip,
        desired_cidr=desired_cidr,
        current_ip=current_value,
        action_needed="WRONG",
        stale_ip_to_remove=stale_ip,
        stale_cidr_to_remove=stale_cidr if stale_cidr != -1 else desired_cidr,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Diff rotte statiche
# ─────────────────────────────────────────────────────────────────────────────

def diff_route(
    network: str,
    cidr: int,
    desired_next_hop: str,
    current_routes: dict[str, str],
) -> RouteDelta:
    """
    Produce il delta per una singola rotta statica.
    """
    key = f"{network}/{cidr}"
    current_next_hop = current_routes.get(key)

    if current_next_hop is None:
        return RouteDelta(
            network=network,
            cidr=cidr,
            next_hop=desired_next_hop,
            action_needed="MISSING",
        )

    try:
        hops = [h.strip() for h in current_next_hop.split(",")]
        same_next_hop = any(
            ipaddress.ip_address(h) == ipaddress.ip_address(desired_next_hop)
            for h in hops
        )
    except ValueError:
        same_next_hop = desired_next_hop in [h.strip() for h in current_next_hop.split(",")]

    if same_next_hop:
        return RouteDelta(
            network=network,
            cidr=cidr,
            next_hop=desired_next_hop,
            action_needed="CORRECT",
        )

    return RouteDelta(
        network=network,
        cidr=cidr,
        next_hop=desired_next_hop,
        action_needed="MISSING",
        stale_next_hop_to_remove=current_next_hop,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Diff VPCS
# ─────────────────────────────────────────────────────────────────────────────

def diff_vpcs(
    extra_params: str,
    current_interfaces: dict[str, str],
    parse_vpcs_intent: Callable[[str], Optional[tuple[str, str, str]]],
) -> VPCSDelta:
    """
    Produce il delta per un host VPCS.
    """
    current_ip = "unassigned"
    for k in ("eth0", "Ethernet0"):
        if k in current_interfaces:
            current_ip = current_interfaces[k]
            break

    if "dhcp" in extra_params.lower():
        has_valid_ip = (
            current_ip != "unassigned"
            and not current_ip.startswith("0.0.0.0")
        )
        return VPCSDelta(
            action_needed="CORRECT" if has_valid_ip else "NEED_DHCP",
            current_ip=current_ip,
        )

    parsed = parse_vpcs_intent(extra_params)
    if parsed is not None:
        ip, mask, gw = parsed
        cidr = _mask_to_cidr(mask)
        if _ips_equivalent(ip, cidr, current_ip):
            return VPCSDelta(action_needed="CORRECT", current_ip=current_ip)
        return VPCSDelta(
            action_needed="STATIC_REQUIRED",
            desired_ip=ip,
            desired_mask=mask,
            desired_gw=gw,
            current_ip=current_ip,
        )

    return VPCSDelta(action_needed="CORRECT", current_ip=current_ip)


# ─────────────────────────────────────────────────────────────────────────────
# Diff base config Cisco
# ─────────────────────────────────────────────────────────────────────────────

def diff_base_config(
    desired: BaseConfig,
    running_config_raw: str,
    is_switch: bool = False,
) -> BaseConfigDelta:
    """
    Confronto chirurgico della base config desiderata vs running-config.
    """
    parse = CiscoConfParse((running_config_raw or "").splitlines(), factory=False)
    checks: list[str] = []

    if is_switch and desired.vtp_mode:
        checks.append(f"vtp mode {desired.vtp_mode}")

    if desired.hostname:
        checks.append(f"hostname {desired.hostname}")
    if desired.banner:
        checks.append(f"banner motd #{desired.banner}#")
    if desired.enable_secret:
        checks.append("enable secret")
    if (
        desired.login_block_for is not None
        and desired.login_attempts is not None
        and desired.login_within is not None
    ):
        checks.append(
            f"login block-for {desired.login_block_for} "
            f"attempts {desired.login_attempts} "
            f"within {desired.login_within}"
        )
    if desired.password_min_length is not None:
        checks.append(
            f"security passwords min-length {desired.password_min_length}"
        )
    if desired.username:
        checks.append(f"username {desired.username}")
    if desired.domain_name:
        checks.append(f"ip domain-name {desired.domain_name}")

    checks.append("ip ssh version 2")
    checks.append(f"ip ssh time-out {desired.ssh_timeout}")
    checks.append(f"ip ssh authentication-retries {desired.ssh_retries}")

    if desired.service_password_encryption:
        checks.append("service password-encryption")
    if desired.no_cdp_run:
        checks.append("no cdp run")
    if is_switch and desired.default_gateway:
        checks.append(f"ip default-gateway {desired.default_gateway}")
        checks.append("no ip routing")

    missing: list[str] = []
    raw = running_config_raw or ""

    for cmd in checks:
        present = False

        if cmd == "enable secret":
            present = bool(parse.find_lines(r"^enable secret\b"))
        elif cmd.startswith("username "):
            present = bool(parse.find_lines(rf"^username\s+{re.escape(desired.username)}\b"))
        elif cmd.startswith("vtp mode "):
            present = bool(parse.find_lines(rf"^vtp\s+mode\s+{re.escape(desired.vtp_mode)}\b"))
        elif cmd.startswith("banner motd "):
            if not desired.banner:
                present = True
            else:
                clean_banner = desired.banner.replace("\r\n", "\n").replace("\r", "\n").strip()
                clean_raw = raw.replace("\r\n", "\n").replace("\r", "\n")
                present = clean_banner in clean_raw
        elif cmd.startswith("ip ssh authentication-retries "):
            present = _running_config_has(parse, cmd)
            if not present and desired.ssh_retries == 3:
                no_retries_at_all = not bool(parse.find_lines(r"^ip ssh authentication-retries"))
                if no_retries_at_all:
                    present = True
        else:
            present = _running_config_has(parse, cmd)

        if not present:
            missing.append(cmd)

    console_regex = r"line con(?:sole)? 0"

    if not _has_command_in_block(parse, console_regex, "login local"):
        missing.append("line console 0: login local")

    console_timeout = desired.console_timeout or (10, 0)
    cmd_ct = f"exec-timeout {console_timeout[0]} {console_timeout[1]}"
    present_ct = _has_command_in_block(parse, console_regex, cmd_ct)

    if not present_ct and cmd_ct == "exec-timeout 10 0":
        has_any_exec_timeout = _has_command_in_block(parse, console_regex, "exec-timeout")
        if not has_any_exec_timeout:
            present_ct = True

    if not present_ct:
        missing.append(f"line console 0: {cmd_ct}")

    vty_header = r"line vty\s+0\b"

    if not _has_command_in_block(parse, vty_header, "transport input ssh"):
        missing.append("line vty: transport input ssh")

    if not _has_command_in_block(parse, vty_header, "login local"):
        missing.append("line vty: login local")

    vty_timeout = desired.vty_timeout or (10, 0)
    cmd_vt = f"exec-timeout {vty_timeout[0]} {vty_timeout[1]}"
    present_vt = _has_command_in_block(parse, vty_header, cmd_vt)

    if not present_vt and cmd_vt == "exec-timeout 10 0":
        has_any_vty_timeout = _has_command_in_block(parse, vty_header, "exec-timeout")
        if not has_any_vty_timeout:
            present_vt = True

    if not present_vt:
        missing.append(f"line vty: {cmd_vt}")

    return BaseConfigDelta(
        desired=desired,
        action_needed="MISSING" if missing else "CORRECT",
        missing_commands=missing,
        needs_crypto_key=not _rsa_key_present(parse),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Diff VLAN (switch Cisco)
# ─────────────────────────────────────────────────────────────────────────────

def diff_vlan(
    vlan_id: int,
    desired_name: str,
    running_config_raw: str,
) -> VlanDelta:
    """
    Produce il delta per una VLAN nel database locale dello switch.
    """
    parse = CiscoConfParse((running_config_raw or "").splitlines(), factory=False)
    existing = _parse_vlan_database(parse)

    if vlan_id not in existing:
        return VlanDelta(
            vlan_id=vlan_id,
            desired_name=desired_name,
            action_needed="MISSING",
        )

    current_name = existing[vlan_id]
    if current_name.upper() == desired_name.upper():
        return VlanDelta(
            vlan_id=vlan_id,
            desired_name=desired_name,
            action_needed="CORRECT",
            current_name=current_name,
        )

    return VlanDelta(
        vlan_id=vlan_id,
        desired_name=desired_name,
        action_needed="WRONG_NAME",
        current_name=current_name,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Diff switchport (porte access/trunk su switch Cisco)
# ─────────────────────────────────────────────────────────────────────────────

def diff_switchport(
    iface: str,
    desired_mode: str,
    running_config_raw: str,
    desired_access_vlan: int = 0,
    desired_trunk_vlans: Optional[list[int]] = None,
    desired_native_vlan: int = 1,
    port_security: bool = False,
    port_security_max: int = 1,
    port_security_violation: str = "restrict",
    portfast: bool = False,
    bpduguard: bool = False,
) -> SwitchportDelta:
    """
    Produce il delta per le porte access/trunk.
    """
    parse = CiscoConfParse((running_config_raw or "").splitlines(), factory=False)
    norm_iface = normalize_interface_name(iface)
    desired_trunk_vlans = desired_trunk_vlans or []
    current = _parse_switchport_config(norm_iface, parse)

    if desired_mode == "access":
        already_correct = (
            current["mode"] == "access"
            and current["access_vlan"] == desired_access_vlan
            and (not portfast or current["portfast"])
            and not current["is_shutdown"]
        )
        return SwitchportDelta(
            iface=iface,
            desired_mode="access",
            action_needed="CORRECT" if already_correct else (
                "WRONG" if current["mode"] else "MISSING"
            ),
            desired_access_vlan=desired_access_vlan,
            current_mode=current["mode"],
            current_trunk_vlans=sorted(set(current["trunk_vlans"])),
            current_native_vlan=current["native_vlan"],
            port_security=port_security,
            port_security_max=port_security_max,
            port_security_violation=port_security_violation,
            portfast=portfast,
            bpduguard=bpduguard,
        )

    current_trunk_set = set(current["trunk_vlans"])
    desired_trunk_set = set(desired_trunk_vlans)
    extra_trunk_vlans = sorted(current_trunk_set - desired_trunk_set)
    missing_trunk_vlans = sorted(desired_trunk_set - current_trunk_set)
    already_correct = (
        current["mode"] == "trunk"
        and current_trunk_set == desired_trunk_set
        and current["native_vlan"] == desired_native_vlan
        and not current["is_shutdown"]
    )
    return SwitchportDelta(
        iface=iface,
        desired_mode="trunk",
        action_needed="CORRECT" if already_correct else (
            "WRONG" if current["mode"] else "MISSING"
        ),
        desired_trunk_vlans=desired_trunk_vlans,
        desired_native_vlan=desired_native_vlan,
        current_mode=current["mode"],
        current_trunk_vlans=sorted(current_trunk_set),
        current_native_vlan=current["native_vlan"],
        extra_trunk_vlans=extra_trunk_vlans,
        missing_trunk_vlans=missing_trunk_vlans,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Diff subinterface dot1Q (router-on-a-stick, inter-VLAN routing)
# ─────────────────────────────────────────────────────────────────────────────

def diff_subinterface(
    parent_iface: str,
    sub_id: int,
    vlan_id: int,
    desired_ip: str,
    desired_cidr: int,
    running_config_raw: str,
) -> SubinterfaceDelta:
    """
    Produce il delta per una subinterface dot1Q su router Cisco.
    """
    parse = CiscoConfParse((running_config_raw or "").splitlines(), factory=False)
    iface_name = f"{parent_iface}.{sub_id}"
    current_ip, current_cidr, current_vlan_id = _parse_subinterface_state(
        iface_name, parse
    )

    if (
        current_vlan_id == vlan_id
        and _ips_equivalent(desired_ip, desired_cidr, f"{current_ip}/{current_cidr}")
    ):
        return SubinterfaceDelta(
            parent_iface=parent_iface,
            sub_id=sub_id,
            vlan_id=vlan_id,
            desired_ip=desired_ip,
            desired_cidr=desired_cidr,
            action_needed="CORRECT",
            current_ip=f"{current_ip}/{current_cidr}",
            current_vlan_id=current_vlan_id,
        )

    if current_ip == "unassigned":
        return SubinterfaceDelta(
            parent_iface=parent_iface,
            sub_id=sub_id,
            vlan_id=vlan_id,
            desired_ip=desired_ip,
            desired_cidr=desired_cidr,
            action_needed="EMPTY",
            current_vlan_id=current_vlan_id,
        )

    return SubinterfaceDelta(
        parent_iface=parent_iface,
        sub_id=sub_id,
        vlan_id=vlan_id,
        desired_ip=desired_ip,
        desired_cidr=desired_cidr,
        action_needed="WRONG",
        current_ip=f"{current_ip}/{current_cidr}",
        current_vlan_id=current_vlan_id,
        stale_ip_to_remove=current_ip,
        stale_cidr_to_remove=current_cidr if current_cidr else desired_cidr,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Diff EtherChannel
# ─────────────────────────────────────────────────────────────────────────────

def diff_etherchannel(
    pc_name: str,
    desired_members: list[str],
    desired_mode: str,
    running_config_raw: str,
) -> EtherChannelDelta:
    """
    Produce il delta per la configurazione di un EtherChannel (Port-channel).
    """
    parse = CiscoConfParse((running_config_raw or "").splitlines(), factory=False)
    norm_pc = normalize_interface_name(pc_name)
    pc_id_m = re.search(r'\d+', norm_pc)
    pc_id = int(pc_id_m.group(0)) if pc_id_m else 1

    missing_members = []
    wrong_members = []

    # Verifica l'assegnazione e la modalità per ciascun membro desiderato
    for member in desired_members:
        norm_member = normalize_interface_name(member)
        curr_group, curr_mode = _parse_channel_group(norm_member, parse)

        if curr_group is None:
            missing_members.append(member)
        elif curr_group != pc_id or curr_mode != desired_mode:
            wrong_members.append((member, curr_group, curr_mode))

    # Verifica la presenza di membri obsoleti (stale) nel running-config
    current_members = _get_current_channel_members(pc_id, parse)
    stale_members = []
    desired_norm_set = {normalize_interface_name(m) for m in desired_members}
    for m in current_members:
        if normalize_interface_name(m) not in desired_norm_set:
            stale_members.append(m)

    dirty_members = []
    for member in desired_members:
        norm_member = normalize_interface_name(member)
        obj = _find_interface_obj(norm_member, parse)
        if obj:
            for child in obj.children:
                s = child.text.strip().lower()
                if any(s.startswith(cmd) for cmd in (
                    "switchport mode",
                    "switchport trunk allowed",
                    "switchport trunk native",
                    "switchport access",
                    "switchport trunk encapsulation"
                )):
                    dirty_members.append(member)
                    break

    action_needed = "CORRECT"
    if missing_members or wrong_members or stale_members or dirty_members:
        action_needed = "WRONG" if (wrong_members or dirty_members) else "MISSING"

    needs_pc_interface = _find_interface_obj(norm_pc, parse) is None

    return EtherChannelDelta(
        pc_name=pc_name,
        desired_members=desired_members,
        desired_mode=desired_mode,
        action_needed=action_needed,
        missing_members=missing_members,
        wrong_members=wrong_members,
        stale_members=stale_members,
        needs_pc_interface=needs_pc_interface,
        dirty_members=dirty_members,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Riconciliazione Sweep (Actual minus Desired)
# ─────────────────────────────────────────────────────────────────────────────

def get_management_interfaces(running_config_raw: str, mgmt_ip: str) -> set[str]:
    """
    Rileva le interfacce (inclusi i parent fisici) associate all'IP di management.
    Restituisce un set con i nomi normalizzati di queste interfacce.
    """
    protected = set()
    if not mgmt_ip or mgmt_ip in ("127.0.0.1", "localhost"):
        return protected

    parse = CiscoConfParse((running_config_raw or "").splitlines(), factory=False)
    for obj in parse.find_objects(r"^interface\s+"):
        parts = obj.text.split()
        if len(parts) < 2:
            continue
        iface_name = parts[1]
        has_mgmt_ip = False
        for child in obj.children:
            m = re.match(r'^\s*ip\s+address\s+([\d.]+)\b', child.text, re.IGNORECASE)
            if m and m.group(1) == mgmt_ip:
                has_mgmt_ip = True
                break
        if has_mgmt_ip:
            norm_name = normalize_interface_name(iface_name)
            protected.add(norm_name)
            if "." in norm_name:
                parent = norm_name.split(".")[0]
                protected.add(parent)
    return protected


def _get_active_referenced_vlans(parse: CiscoConfParse) -> set[int]:
    """Raccoglie tutti i VLAN ID associati a porte fisiche o trunk nel running config."""
    referenced = set()
    for obj in parse.find_objects(r"^interface\s+"):
        parts = obj.text.split()
        if len(parts) < 2:
            continue
        iface_name = parts[1]
        if iface_name.lower().startswith("vlan"):
            continue
        cfg = _parse_switchport_config(iface_name, parse)
        if cfg["mode"] == "access" and cfg["access_vlan"] > 0:
            referenced.add(cfg["access_vlan"])
        elif cfg["mode"] == "trunk":
            if cfg["native_vlan"] > 0:
                referenced.add(cfg["native_vlan"])
            if cfg["trunk_vlans"]:
                referenced.update(cfg["trunk_vlans"])
    return referenced


def diff_routes_sweep(current_routes: dict[str, str], desired_routes: list) -> list[RouteDelta]:
    """
    Trova le rotte statiche extra presenti sul dispositivo ma non nello YAML.
    """
    desired_routes_set = set()
    for r in desired_routes:
        if "/" in r.network:
            net, pfx = r.network.split("/", 1)
            pfx = int(pfx)
        else:
            net = r.network
            pfx = 24
        try:
            net_ip = str(ipaddress.IPv4Network(f"{net}/{pfx}", strict=False).network_address)
        except ValueError:
            net_ip = net
        desired_routes_set.add((net_ip, pfx, r.next_hop.strip()))

    stale_routes = []
    for key, next_hops in current_routes.items():
        if not key or not next_hops:
            continue
        if "/" in key:
            curr_net, curr_pfx = key.split("/", 1)
            curr_pfx = int(curr_pfx)
        else:
            curr_net = key
            curr_pfx = 24
        try:
            curr_net_ip = str(ipaddress.IPv4Network(f"{curr_net}/{curr_pfx}", strict=False).network_address)
        except ValueError:
            curr_net_ip = curr_net

        for nh in next_hops.split(","):
            nh = nh.strip()
            if not nh:
                continue
            if (curr_net_ip, curr_pfx, nh) not in desired_routes_set:
                stale_routes.append(
                    RouteDelta(
                        network=curr_net_ip,
                        cidr=curr_pfx,
                        next_hop=nh,
                        action_needed="REMOVE"
                    )
                )
    return stale_routes


def diff_vlans_sweep(running_config: str, desired_vlans: dict[str, str]) -> list[tuple[int, str]]:
    """
    Trova le VLAN extra presenti nello switch ma non nello YAML.
    Esclude VLAN di default e VLAN associate a porte attive.
    """
    parse = CiscoConfParse((running_config or "").splitlines(), factory=False)
    existing_vlans = _parse_vlan_database(parse)
    desired_ids = {int(vid) for vid in desired_vlans.keys()}
    referenced_ids = _get_active_referenced_vlans(parse)

    stale_vlans = []
    for vid, name in existing_vlans.items():
        if vid in (1, 1002, 1003, 1004, 1005):
            continue
        if vid not in desired_ids and vid not in referenced_ids:
            stale_vlans.append((vid, name))
    return sorted(stale_vlans, key=lambda x: x[0])


def _parse_dhcp_pools(parse: CiscoConfParse) -> dict[str, str]:
    """
    Estrae i pool DHCP attivi e il loro blocco di configurazione grezzo normalizzato.
    """
    pools = {}
    for obj in parse.find_objects(r"^ip\s+dhcp\s+pool\s+"):
        parts = obj.text.split()
        if len(parts) < 4:
            continue
        pool_name = parts[-1]
        lines = [f"ip dhcp pool {pool_name}"]
        for child in obj.children:
            lines.append(child.text.strip())
        pools[pool_name] = "\n".join(lines)
    return pools


def diff_dhcp_pools_sweep(running_config: str, desired_pools: list) -> list[tuple[str, str]]:
    """
    Trova i pool DHCP extra presenti nel dispositivo ma non nello YAML.
    """
    parse = CiscoConfParse((running_config or "").splitlines(), factory=False)
    existing_pools = _parse_dhcp_pools(parse)
    desired_names = {p.name for p in desired_pools}

    stale_pools = []
    for name, raw_cfg in existing_pools.items():
        if name not in desired_names:
            stale_pools.append((name, raw_cfg))
    return stale_pools


def _parse_subinterfaces(parse: CiscoConfParse) -> dict[str, str]:
    """
    Estrae le sottointerfacce attive e il loro blocco di configurazione grezzo normalizzato.
    """
    subinterfaces = {}
    for obj in parse.find_objects(r"^interface\s+(\S+\.\d+)"):
        parts = obj.text.split()
        if len(parts) < 2:
            continue
        iface_name = parts[1]
        lines = [f"interface {iface_name}"]
        for child in obj.children:
            lines.append(child.text.strip())
        subinterfaces[iface_name] = "\n".join(lines)
    return subinterfaces


def diff_subinterfaces_sweep(running_config: str, desired_interfaces: list, mgmt_ip: str) -> list[tuple[str, str]]:
    """
    Trova le sottointerfacce extra presenti nel dispositivo ma non nello YAML.
    """
    parse = CiscoConfParse((running_config or "").splitlines(), factory=False)
    existing_subifs = _parse_subinterfaces(parse)
    desired_names = {normalize_interface_name(i.name) for i in desired_interfaces}
    protected = get_management_interfaces(running_config, mgmt_ip)

    stale_subifs = []
    for name, raw_cfg in existing_subifs.items():
        norm_name = normalize_interface_name(name)
        if norm_name in protected:
            continue
        if norm_name not in desired_names:
            stale_subifs.append((name, raw_cfg))
    return stale_subifs

