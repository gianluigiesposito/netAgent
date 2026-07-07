# nodes/generate.py
"""
Nodo GENERATE v3 — Orchestratore.

Questo file è deliberatamente snello: la sua unica responsabilità è
coordinare i moduli specializzati nella sequenza corretta.

Struttura del refactoring SOLID:
  generate/models/deltas.py   → dataclass (SRP: solo dati)
  generate/parsers/           → parsing intent per vendor (OCP: aggiungere vendor = aggiungere file)
  generate/diff/engine.py     → logica diff pura (SRP: solo matematica del confronto)
  database/neo4j_queries.py   → accesso Neo4j (SRP: solo I/O database)
  nodes/generate.py           → orchestratore (SRP: solo coordinamento)

Flusso per ogni device:
  1. GenerateRepository.get_device_state()  → stato attuale da Neo4j
  2. VendorParserRegistry.get(vendor)       → parser corretto per il vendor
  3. DiffEngine functions                   → delta desired vs actual
  4. _compile_delta()                       → RouterCommands via Jinja2
  5. LLM fallback se Jinja2 non copre
"""

from __future__ import annotations

import ipaddress
import logging
import re
from typing import Optional

from core.state import RouterCommands, CommandPair
from config.settings import DEFAULTS
from database.neo4j_queries import GenerateRepository
from generate.diff.engine import (
    diff_interface,
    diff_route,
    diff_base_config,
    diff_vpcs,
    diff_vlan,
    diff_switchport,
    diff_subinterface,
    diff_etherchannel,
    _mask_to_cidr,
    _ips_equivalent,
)
from generate.models.deltas import DeviceDelta, HelperAddressDelta, EtherChannelDelta
from llm.async_client import llm_client
from tools.dhcp_config import diff_dhcp
from tools.dhcp_relay import compute_helper_addresses, extract_dhcp_relay_params
from tools.graph_store import AsyncNetworkGraphStore
from tools.template_engine import renderer

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Compilazione delta → RouterCommands via Jinja2
# ─────────────────────────────────────────────────────────────────────────────

def group_interfaces_for_range(interfaces: list[str]) -> str:
    """Group a list of interface names into a Cisco range string representation."""
    if not interfaces:
        return ""
    
    groups = {}
    for iface in sorted(interfaces):
        m = re.match(r'^([a-zA-Z\-_]+\d+(?:/\d+)*?/)?(\d+)$', iface)
        if m:
            prefix, num = m.groups()
            prefix = prefix or ""
            groups.setdefault(prefix, []).append(int(num))
        else:
            groups.setdefault(iface, []).append(None)

    range_parts = []
    for prefix, nums in sorted(groups.items()):
        if None in nums:
            range_parts.append(prefix)
            continue
        
        ranges = []
        start = nums[0]
        prev = nums[0]
        for n in nums[1:]:
            if n == prev + 1:
                prev = n
            else:
                ranges.append((start, prev))
                start = n
                prev = n
        ranges.append((start, prev))
        
        for r_start, r_end in ranges:
            if r_start == r_end:
                range_parts.append(f"{prefix}{r_start}")
            else:
                range_parts.append(f"{prefix}{r_start} - {r_end}")
                
    return ", ".join(range_parts)


def _lines_to_pairs(forward: list[str], rollback: list[str]) -> list[CommandPair]:
    if not forward:
        return []
    return [CommandPair(cmd="\n".join(forward), rollback="\n".join(rollback))]


class BaseCompiler:
    """Interfaccia astratta per i compilatori di comandi per vendor (Strategy Pattern)."""
    def compile(self, device_delta: DeviceDelta, vendor_type: str) -> list[CommandPair]:
        return []


class VpcsCompiler(BaseCompiler):
    def compile(self, device_delta: DeviceDelta, vendor_type: str) -> list[CommandPair]:
        all_pairs = []
        v = device_delta.vpcs_delta
        if v and v.action_needed != "CORRECT":
            mode = "dhcp" if v.action_needed == "NEED_DHCP" else "static"
            fwd, rb = renderer.render_with_rollback(
                "vpcs/host.j2",
                mode=mode, ip=v.desired_ip, mask=v.desired_mask, gateway=v.desired_gw,
            )
            all_pairs += _lines_to_pairs(fwd, rb)
        return all_pairs


def _compile_etherchannel(e: EtherChannelDelta) -> list[CommandPair]:
    pairs = []
    pc_id_m = re.search(r'\d+', e.pc_name)
    pc_id = int(pc_id_m.group(0)) if pc_id_m else 1

    # 1. Crea il logical Port-channel se necessario
    if e.needs_pc_interface:
        fwd, rb = renderer.render_with_rollback(
            "cisco_switch/port_channel_interface.j2",
            pc_id=pc_id,
        )
        pairs += _lines_to_pairs(fwd, rb)

    # 2. Rimuovi i membri obsoleti
    for m in e.stale_members:
        fwd, rb = renderer.render_with_rollback(
            "cisco_switch/channel_member.j2",
            iface=m,
            pc_id=pc_id,
            mode=e.desired_mode,
            remove=True,
        )
        pairs += _lines_to_pairs(fwd, rb)

    # 3. Rimuovi i membri errati dai loro vecchi gruppi
    for m, old_grp, old_md in e.wrong_members:
        fwd, rb = renderer.render_with_rollback(
            "cisco_switch/channel_member.j2",
            iface=m,
            pc_id=old_grp or pc_id,
            mode=old_md or e.desired_mode,
            old_group=old_grp,
            old_mode=old_md,
            remove=True,
        )
        pairs += _lines_to_pairs(fwd, rb)

    # 4. Aggiungi i membri mancanti al gruppo corretto
    for m in e.missing_members:
        fwd, rb = renderer.render_with_rollback(
            "cisco_switch/channel_member.j2",
            iface=m,
            pc_id=pc_id,
            mode=e.desired_mode,
            remove=False,
        )
        pairs += _lines_to_pairs(fwd, rb)

    # 5. Aggiungi i membri precedentemente errati al gruppo corretto
    for m, old_grp, old_md in e.wrong_members:
        fwd, rb = renderer.render_with_rollback(
            "cisco_switch/channel_member.j2",
            iface=m,
            pc_id=pc_id,
            mode=e.desired_mode,
            remove=False,
        )
        pairs += _lines_to_pairs(fwd, rb)

    # 6. Pulisci le configurazioni switchport spurie sui membri fisici
    for m in e.dirty_members:
        fwd, rb = renderer.render_with_rollback(
            "cisco_switch/clean_member.j2",
            iface=m,
        )
        pairs += _lines_to_pairs(fwd, rb)
    return pairs


class CiscoIOSCompiler(BaseCompiler):
    def compile(self, device_delta: DeviceDelta, vendor_type: str) -> list[CommandPair]:
        all_pairs = []
        
        # 1. Base config Cisco
        bc = device_delta.base_config_delta
        if bc and bc.action_needed != "CORRECT":
            cfg = bc.desired
            ct  = cfg.console_timeout or (10, 0)
            vt  = cfg.vty_timeout     or (10, 0)
            access_ports_ctx = [
                {
                    "name": p,
                    "port_security": cfg.port_security,
                    "port_security_max": cfg.port_security_max,
                    "port_security_violation": cfg.port_security_violation,
                    "port_security_mac_sticky": cfg.port_security_mac_sticky,
                    "portfast": cfg.portfast,
                    "bpduguard": cfg.bpduguard,
                }
                for p in cfg.access_ports
            ]
            fwd, rb = renderer.render_with_rollback(
                "cisco_ios/base_config.j2",
                missing=set(bc.missing_commands),
                hostname=cfg.hostname,
                original_hostname=device_delta.router_name,
                banner=cfg.banner,
                enable_secret=cfg.enable_secret,
                username=cfg.username,
                password=cfg.password,
                domain_name=cfg.domain_name,
                ssh_timeout=cfg.ssh_timeout,
                ssh_retries=cfg.ssh_retries,
                login_block_for=cfg.login_block_for,
                login_attempts=cfg.login_attempts,
                login_within=cfg.login_within,
                password_min_length=cfg.password_min_length,
                console_timeout_m=ct[0], console_timeout_s=ct[1],
                vty_lines=cfg.vty_lines,
                vty_timeout_m=vt[0], vty_timeout_s=vt[1],
                service_password_encryption=cfg.service_password_encryption,
                no_cdp_run=cfg.no_cdp_run,
                needs_crypto_key=bc.needs_crypto_key,
                dhcp_snooping_vlans=cfg.dhcp_snooping_vlans,
                access_ports=access_ports_ctx,
                mgmt_vlan=None, mgmt_ip=None, mgmt_mask=None, default_gateway=cfg.default_gateway,
                vtp_mode=cfg.vtp_mode,
            )
            all_pairs += _lines_to_pairs(fwd, rb)

        # 2. DHCP pools
        for dhcp_delta in device_delta.dhcp_deltas:
            if dhcp_delta.action_needed != "CORRECT" and dhcp_delta.desired:
                cfg = dhcp_delta.desired
                fwd, rb = renderer.render_with_rollback(
                    "cisco_ios/dhcp_pool.j2",
                    pool_name=cfg.pool_name, network=cfg.network, netmask=cfg.netmask,
                    default_router=cfg.default_router, dns_server=cfg.dns_server,
                    lease_days=cfg.lease_days, excluded_start=cfg.excluded_start,
                    excluded_end=cfg.excluded_end,
                )
                all_pairs += _lines_to_pairs(fwd, rb)

        # 3. EtherChannel (Cisco switch/router)
        for e in device_delta.etherchannel_deltas:
            all_pairs += _compile_etherchannel(e)

        # 5. Interfacce fisiche / SVIs
        for d in device_delta.interface_deltas:
            if d.action_needed == "CORRECT" or not d.desired_ip or d.desired_ip == "0.0.0.0":
                continue
            mask       = str(ipaddress.IPv4Network(f"0.0.0.0/{d.desired_cidr}").netmask)
            stale_mask = (
                str(ipaddress.IPv4Network(f"0.0.0.0/{d.stale_cidr_to_remove}").netmask)
                if d.stale_ip_to_remove else ""
            )
            fwd, rb = renderer.render_with_rollback(
                "cisco_ios/interface.j2",
                iface=d.iface, ip=d.desired_ip, mask=mask,
                stale_ip=d.stale_ip_to_remove, stale_mask=stale_mask,
            )
            all_pairs += _lines_to_pairs(fwd, rb)

        # 6. Subinterface dot1Q (router Cisco, inter-VLAN routing)
        configured_parents = set()
        for si in device_delta.subinterface_deltas:
            if si.action_needed == "CORRECT":
                continue
            mask       = str(ipaddress.IPv4Network(f"0.0.0.0/{si.desired_cidr}").netmask)
            stale_mask = (
                str(ipaddress.IPv4Network(f"0.0.0.0/{si.stale_cidr_to_remove}").netmask)
                if si.stale_ip_to_remove else ""
            )
            parent = si.parent_iface
            configure_parent = parent not in configured_parents
            configured_parents.add(parent)
            fwd, rb = renderer.render_with_rollback(
                "cisco_ios/subinterface.j2",
                parent_iface=si.parent_iface,
                sub_id=si.sub_id,
                vlan_id=si.vlan_id,
                ip=si.desired_ip,
                mask=mask,
                stale_ip=si.stale_ip_to_remove,
                stale_mask=stale_mask,
                configure_parent=configure_parent,
            )
            all_pairs += _lines_to_pairs(fwd, rb)

        # DHCP Helper address
        for h in device_delta.helper_address_deltas:
            if h.action_needed == "CORRECT":
                continue
            fwd, rb = renderer.render_with_rollback(
                "cisco_ios/helper_address.j2",
                iface=h.iface, helper_ip=h.dhcp_server_ip,
            )
            all_pairs += _lines_to_pairs(fwd, rb)

        # 8. Rotte statiche
        for r in device_delta.route_deltas:
            if r.action_needed == "CORRECT":
                continue
            net_obj = ipaddress.IPv4Network(f"{r.network}/{r.cidr}", strict=False)
            fwd, rb = renderer.render_with_rollback(
                "cisco_ios/static_route.j2",
                network=r.network, mask=str(net_obj.netmask),
                next_hop=r.next_hop,
                stale_next_hop=r.stale_next_hop_to_remove or "",
            )
            all_pairs += _lines_to_pairs(fwd, rb)

        # 9. Spegnimento interfacce inutilizzate
        if device_delta.unused_interfaces_to_shutdown:
            range_str = group_interfaces_for_range(device_delta.unused_interfaces_to_shutdown)
            fwd, rb = renderer.render_with_rollback(
                "cisco_ios/shutdown_interface.j2",
                iface=range_str,
            )
            all_pairs += _lines_to_pairs(fwd, rb)

        # 10. Sweep extra static routes
        for r in device_delta.extra_routes_to_remove:
            net_obj = ipaddress.IPv4Network(f"{r.network}/{r.cidr}", strict=False)
            mask = str(net_obj.netmask)
            fwd = [
                "configure terminal",
                f"no ip route {r.network} {mask} {r.next_hop}",
                "exit"
            ]
            rb = [
                "configure terminal",
                f"ip route {r.network} {mask} {r.next_hop}",
                "exit"
            ]
            all_pairs += _lines_to_pairs(fwd, rb)

        # 11. Sweep extra DHCP pools
        for pool_name, raw_cfg in device_delta.extra_dhcp_pools_to_remove:
            fwd = [
                "configure terminal",
                f"no ip dhcp pool {pool_name}",
                "exit"
            ]
            rb_lines = ["configure terminal"]
            rb_lines += raw_cfg.splitlines()
            rb_lines.append("exit")
            all_pairs += _lines_to_pairs(fwd, rb_lines)

        # 12. Sweep extra subinterfaces
        for sub_name, raw_cfg in device_delta.extra_subinterfaces_to_remove:
            fwd = [
                "configure terminal",
                f"no interface {sub_name}",
                "exit",
                "!sleep 2"
            ]
            rb_lines = ["configure terminal"]
            rb_lines += raw_cfg.splitlines()
            rb_lines.append("exit")
            all_pairs += _lines_to_pairs(fwd, rb_lines)

        return all_pairs


class CiscoSwitchCompiler(BaseCompiler):
    def compile(self, device_delta: DeviceDelta, vendor_type: str) -> list[CommandPair]:
        all_pairs = []

        # 1. Base config Cisco
        bc = device_delta.base_config_delta
        if bc and bc.action_needed != "CORRECT":
            cfg = bc.desired
            ct  = cfg.console_timeout or (10, 0)
            vt  = cfg.vty_timeout     or (10, 0)
            access_ports_ctx = [
                {
                    "name": p,
                    "port_security": cfg.port_security,
                    "port_security_max": cfg.port_security_max,
                    "port_security_violation": cfg.port_security_violation,
                    "port_security_mac_sticky": cfg.port_security_mac_sticky,
                    "portfast": cfg.portfast,
                    "bpduguard": cfg.bpduguard,
                }
                for p in cfg.access_ports
            ]
            fwd, rb = renderer.render_with_rollback(
                "cisco_switch/base_config.j2",
                missing=set(bc.missing_commands),
                hostname=cfg.hostname,
                original_hostname=device_delta.router_name,
                banner=cfg.banner,
                enable_secret=cfg.enable_secret,
                username=cfg.username,
                password=cfg.password,
                domain_name=cfg.domain_name,
                ssh_timeout=cfg.ssh_timeout,
                ssh_retries=cfg.ssh_retries,
                login_block_for=cfg.login_block_for,
                login_attempts=cfg.login_attempts,
                login_within=cfg.login_within,
                password_min_length=cfg.password_min_length,
                console_timeout_m=ct[0], console_timeout_s=ct[1],
                vty_lines=cfg.vty_lines,
                vty_timeout_m=vt[0], vty_timeout_s=vt[1],
                service_password_encryption=cfg.service_password_encryption,
                no_cdp_run=cfg.no_cdp_run,
                needs_crypto_key=bc.needs_crypto_key,
                dhcp_snooping_vlans=cfg.dhcp_snooping_vlans,
                access_ports=access_ports_ctx,
                mgmt_vlan=None, mgmt_ip=None, mgmt_mask=None, default_gateway=cfg.default_gateway,
                vtp_mode=cfg.vtp_mode,
            )
            all_pairs += _lines_to_pairs(fwd, rb)

        # 2. VLAN definitions
        for v in device_delta.vlan_deltas:
            if v.action_needed == "CORRECT":
                continue
            fwd, rb = renderer.render_with_rollback(
                "cisco_switch/vlan.j2",
                vlan_id=v.vlan_id,
                vlan_name=v.desired_name,
            )
            all_pairs += _lines_to_pairs(fwd, rb)

        # 3. EtherChannel (Cisco switch/router)
        for e in device_delta.etherchannel_deltas:
            all_pairs += _compile_etherchannel(e)

        # 4. Switchport access/trunk
        for s in device_delta.switchport_deltas:
            if s.action_needed == "CORRECT":
                continue
            if s.desired_mode == "clean":
                fwd, rb = renderer.render_with_rollback(
                    "cisco_switch/clean_member.j2",
                    iface=s.iface,
                )
            elif s.desired_mode == "access":
                fwd, rb = renderer.render_with_rollback(
                    "cisco_switch/switchport_access.j2",
                    iface=s.iface,
                    access_vlan=s.desired_access_vlan,
                    port_security=s.port_security,
                    port_security_max=s.port_security_max,
                    port_security_violation=s.port_security_violation,
                    portfast=s.portfast,
                    bpduguard=s.bpduguard,
                )
            else:
                fwd, rb = renderer.render_with_rollback(
                    "cisco_switch/switchport_trunk.j2",
                    iface=s.iface,
                    trunk_vlans=s.desired_trunk_vlans,
                    native_vlan=s.desired_native_vlan,
                )
            all_pairs += _lines_to_pairs(fwd, rb)

        # 5. Interfacce fisiche / SVIs
        for d in device_delta.interface_deltas:
            if d.action_needed == "CORRECT" or not d.desired_ip or d.desired_ip == "0.0.0.0":
                continue
            mask       = str(ipaddress.IPv4Network(f"0.0.0.0/{d.desired_cidr}").netmask)
            stale_mask = (
                str(ipaddress.IPv4Network(f"0.0.0.0/{d.stale_cidr_to_remove}").netmask)
                if d.stale_ip_to_remove else ""
            )
            fwd, rb = renderer.render_with_rollback(
                "cisco_ios/interface.j2",
                iface=d.iface, ip=d.desired_ip, mask=mask,
                stale_ip=d.stale_ip_to_remove, stale_mask=stale_mask,
            )
            all_pairs += _lines_to_pairs(fwd, rb)

        # 8. Rotte statiche
        for r in device_delta.route_deltas:
            if r.action_needed == "CORRECT":
                continue
            net_obj = ipaddress.IPv4Network(f"{r.network}/{r.cidr}", strict=False)
            fwd, rb = renderer.render_with_rollback(
                "cisco_switch/static_route.j2",
                network=r.network, mask=str(net_obj.netmask),
                next_hop=r.next_hop,
                stale_next_hop=r.stale_next_hop_to_remove or "",
            )
            all_pairs += _lines_to_pairs(fwd, rb)

        # 9. Spegnimento interfacce inutilizzate
        if device_delta.unused_interfaces_to_shutdown:
            range_str = group_interfaces_for_range(device_delta.unused_interfaces_to_shutdown)
            fwd, rb = renderer.render_with_rollback(
                "cisco_switch/shutdown_interface.j2",
                iface=range_str,
            )
            all_pairs += _lines_to_pairs(fwd, rb)

        # 10. Sweep extra static routes
        for r in device_delta.extra_routes_to_remove:
            net_obj = ipaddress.IPv4Network(f"{r.network}/{r.cidr}", strict=False)
            mask = str(net_obj.netmask)
            fwd = [
                "configure terminal",
                f"no ip route {r.network} {mask} {r.next_hop}",
                "exit"
            ]
            rb = [
                "configure terminal",
                f"ip route {r.network} {mask} {r.next_hop}",
                "exit"
            ]
            all_pairs += _lines_to_pairs(fwd, rb)

        # 11. Sweep extra VLANs
        for vid, name in device_delta.extra_vlans_to_remove:
            fwd = [
                "configure terminal",
                f"no vlan {vid}",
                "exit"
            ]
            rb = ["configure terminal", f"vlan {vid}"]
            if name:
                rb.append(f"name {name}")
            rb.append("exit")
            all_pairs += _lines_to_pairs(fwd, rb)

        return all_pairs


class FrroutingCompiler(BaseCompiler):
    def compile(self, device_delta: DeviceDelta, vendor_type: str) -> list[CommandPair]:
        all_pairs = []

        # 5. Interfacce fisiche / SVIs
        for d in device_delta.interface_deltas:
            if d.action_needed == "CORRECT" or not d.desired_ip or d.desired_ip == "0.0.0.0":
                continue
            fwd, rb = renderer.render_with_rollback(
                "frrouting/interface.j2",
                iface=d.iface, ip=d.desired_ip, cidr=d.desired_cidr,
            )
            all_pairs += _lines_to_pairs(fwd, rb)

        # 7. DHCP pools
        for dhcp_delta in device_delta.dhcp_deltas:
            if dhcp_delta.action_needed != "CORRECT" and dhcp_delta.desired:
                cfg = dhcp_delta.desired
                fwd, rb = renderer.render_with_rollback(
                    "frrouting/dhcp_pool.j2",
                    pool_name=cfg.pool_name, network=cfg.network, netmask=cfg.netmask,
                    default_router=cfg.default_router, dns_server=cfg.dns_server,
                    lease_days=cfg.lease_days, excluded_start=cfg.excluded_start,
                    excluded_end=cfg.excluded_end,
                )
                all_pairs += _lines_to_pairs(fwd, rb)

        # 8. Rotte statiche
        for r in device_delta.route_deltas:
            if r.action_needed == "CORRECT":
                continue
            fwd, rb = renderer.render_with_rollback(
                "frrouting/static_route.j2",
                network=r.network, cidr=r.cidr, next_hop=r.next_hop,
            )
            all_pairs += _lines_to_pairs(fwd, rb)

        # Sweep extra static routes
        for r in device_delta.extra_routes_to_remove:
            fwd = [
                "configure terminal",
                f"no ip route {r.network}/{r.cidr} {r.next_hop}",
                "exit"
            ]
            rb = [
                "configure terminal",
                f"ip route {r.network}/{r.cidr} {r.next_hop}",
                "exit"
            ]
            all_pairs += _lines_to_pairs(fwd, rb)

        # Sweep extra DHCP pools
        for pool_name, raw_cfg in device_delta.extra_dhcp_pools_to_remove:
            fwd = [
                "configure terminal",
                f"no ip dhcp pool {pool_name}",
                "exit"
            ]
            rb_lines = ["configure terminal"]
            rb_lines += raw_cfg.splitlines()
            rb_lines.append("exit")
            all_pairs += _lines_to_pairs(fwd, rb_lines)

        return all_pairs


COMPILER_REGISTRY: dict[str, BaseCompiler] = {
    "vpcs": VpcsCompiler(),
    "cisco_ios": CiscoIOSCompiler(),
    "cisco_switch": CiscoSwitchCompiler(),
    "frrouting": FrroutingCompiler(),
}


def _compile_delta(device_delta: DeviceDelta, vendor_type: str) -> RouterCommands:
    compiler = COMPILER_REGISTRY.get(vendor_type)
    if not compiler:
        raise ValueError(f"No registered compiler strategy for vendor: {vendor_type}")
    pairs = compiler.compile(device_delta, vendor_type)
    return RouterCommands(pairs=pairs)


# ─────────────────────────────────────────────────────────────────────────────
# Worker principale LangGraph
# ─────────────────────────────────────────────────────────────────────────────

async def generate_single_node(state: dict) -> dict:
    router_name: str = state["router_name"]
    router_plan      = state["router_plan"]

    logger.info("[%s] >>> GENERATE <<<", router_name)

    # 1. Stato attuale da Neo4j
    async with AsyncNetworkGraphStore() as store:
        repo = GenerateRepository(store._driver)
        vendor_type, current_interfaces, current_routes, running_config_raw = (
            await repo.get_device_state(router_name)
        )
        from tools.parser import load_inventory
        inventory = load_inventory()
        device_cfg = inventory.get(router_name, {})
        mgmt_ip = device_cfg.get("host", "")

    device_delta = DeviceDelta(router_name=router_name)

    # 3. Delta
    from core.state import DeviceIntent
    from tools.dhcp_config import DhcpPoolConfig, diff_dhcp
    from generate.models.deltas import BaseConfig, SwitchportDelta, EtherChannelDelta, VPCSDelta

    if not isinstance(router_plan, DeviceIntent):
        raise TypeError(f"Expected DeviceIntent, got {type(router_plan)}")

    vendor_type = router_plan.profile

    # Normalizzazione per cisco_switch: sposta eventuali IP da porte fisiche a Vlan1 SVI
    if vendor_type == "cisco_switch":
        svi_ip = None
        physical_ifaces_to_clean = []
        for iface in router_plan.interfaces:
            iface_lower = iface.name.lower()
            is_physical = not (
                iface_lower.startswith("vlan")
                or iface_lower.startswith("port-channel")
                or iface_lower.startswith("po")
                or iface_lower.startswith("loopback")
                or iface_lower.startswith("lo")
                or "." in iface_lower
            )
            if is_physical and iface.ip:
                svi_ip = iface.ip
                physical_ifaces_to_clean.append(iface)

        if svi_ip:
            for iface in physical_ifaces_to_clean:
                logger.info(
                    "[%s] Normalizzazione: Rimosso IP %s dalla porta fisica %s su switch L2.",
                    router_name, iface.ip, iface.name
                )
                iface.ip = None

            from core.state import InterfaceIntent
            vlan1_iface = None
            for iface in router_plan.interfaces:
                if iface.name.lower() in ("vlan1", "vlan 1"):
                    vlan1_iface = iface
                    break

            if vlan1_iface:
                if not vlan1_iface.ip:
                    vlan1_iface.ip = svi_ip
            else:
                vlan1_iface = InterfaceIntent(name="Vlan1", ip=svi_ip)
                router_plan.interfaces.append(vlan1_iface)
                logger.info(
                    "[%s] Normalizzazione: Aggiunta interfaccia virtuale Vlan1 con IP %s su switch L2.",
                    router_name, svi_ip
                )


    # 3.1 Base Config
    if vendor_type in ("cisco_ios", "cisco_switch"):
        access_ports = []
        for iface in router_plan.interfaces:
            if iface.mode == "access":
                access_ports.append(iface.name)
        
        default_gw = None
        for r in router_plan.static_routes:
            if r.network == "0.0.0.0/0" or r.network == "0.0.0.0":
                default_gw = r.next_hop
                break

        if any([
            router_plan.hostname,
            router_plan.banner,
            router_plan.enable_secret,
            router_plan.domain_name,
        ]) or default_gw is not None:
            import os
            enable_secret_val = router_plan.enable_secret
            if enable_secret_val and isinstance(enable_secret_val, str):
                if enable_secret_val.startswith("env:"):
                    var_name = enable_secret_val[4:].strip()
                    enable_secret_val = os.getenv(var_name, "cisco")
                elif enable_secret_val.startswith("${") and enable_secret_val.endswith("}"):
                    var_name = enable_secret_val[2:-1].strip()
                    enable_secret_val = os.getenv(var_name, "cisco")

            desired_base = BaseConfig(
                enabled=True,
                hostname=router_plan.hostname or "",
                banner=router_plan.banner,
                enable_secret=enable_secret_val,
                domain_name=router_plan.domain_name,
                access_ports=access_ports,
                default_gateway=default_gw,
            )
            device_delta.base_config_delta = diff_base_config(
                desired_base,
                running_config_raw,
                is_switch=(vendor_type == "cisco_switch")
            )

    # 3.2 DHCP Pools
    if vendor_type != "cisco_switch":
        for pool in router_plan.dhcp_pools:
            if "/" in pool.network:
                net_part, prefix_part = pool.network.split("/", 1)
                prefix_len = int(prefix_part)
            else:
                net_part = pool.network
                prefix_len = 24
            
            dhcp_cfg = DhcpPoolConfig(
                pool_name=pool.name,
                network=net_part,
                prefix_len=prefix_len,
                default_router=pool.gateway,
                dns_server=pool.dns or "8.8.8.8",
                lease_days=pool.lease or 1,
                excluded_start=pool.excluded_start,
                excluded_end=pool.excluded_end
            )
            device_delta.dhcp_deltas.append(diff_dhcp(dhcp_cfg, running_config_raw))

    # 3.3 Interfaces & Switchports & Subinterfaces
    etherchannel_map = {}
    for iface in router_plan.interfaces:
        # 3.3.1 Subinterface check
        if "." in iface.name:
            parent, sub_id = iface.name.split(".", 1)
            if iface.ip and "/" in iface.ip:
                ip, cidr_str = iface.ip.split("/", 1)
                cidr = int(cidr_str)
            else:
                ip = iface.ip or ""
                cidr = 24
            device_delta.subinterface_deltas.append(
                diff_subinterface(
                    parent_iface=parent,
                    sub_id=int(sub_id),
                    vlan_id=iface.vlan_id or int(sub_id),
                    desired_ip=ip,
                    desired_cidr=cidr,
                    running_config_raw=running_config_raw,
                )
            )
            continue

        is_member = False
        # 3.3.3 EtherChannel LACP configuration
        if iface.channel_group is not None:
            pc_id = iface.channel_group
            pc_mode = iface.channel_mode or "active"
            etherchannel_map.setdefault(pc_id, ([], pc_mode))
            etherchannel_map[pc_id][0].append(iface.name)
            is_member = True

        # 3.3.2 Switchport check
        if iface.mode in ("access", "trunk") and vendor_type == "cisco_switch":
            p_fast = True if iface.mode == "access" else (desired_base.portfast if 'desired_base' in locals() else False)
            p_sec = desired_base.port_security if 'desired_base' in locals() else False
            p_sec_max = desired_base.port_security_max if 'desired_base' in locals() else 1
            p_sec_vio = desired_base.port_security_violation if 'desired_base' in locals() else "restrict"
            p_bpdu = desired_base.bpduguard if 'desired_base' in locals() else False

            device_delta.switchport_deltas.append(
                diff_switchport(
                    iface=iface.name,
                    desired_mode=iface.mode,
                    running_config_raw=running_config_raw,
                    desired_access_vlan=iface.access_vlan or iface.vlan_id or 1,
                    desired_trunk_vlans=iface.trunk_vlans or [],
                    desired_native_vlan=iface.native_vlan or 1,
                    port_security=p_sec,
                    port_security_max=p_sec_max,
                    port_security_violation=p_sec_vio,
                    portfast=p_fast,
                    bpduguard=p_bpdu,
                )
            )
        # 3.3.4 Physical L3 Interface
        elif not is_member:
            if iface.ip:
                if "/" in iface.ip:
                    ip, cidr_str = iface.ip.split("/", 1)
                    cidr = int(cidr_str)
                else:
                    ip = iface.ip
                    cidr = 24
                device_delta.interface_deltas.append(
                    diff_interface(iface.name, ip, cidr, current_interfaces, running_config_raw)
                )

    # Build EtherChannel Deltas
    for pc_id, (members, mode) in etherchannel_map.items():
        pc_name = f"Port-channel{pc_id}"
        device_delta.etherchannel_deltas.append(
            diff_etherchannel(
                pc_name=pc_name,
                desired_members=members,
                desired_mode=mode,
                running_config_raw=running_config_raw,
            )
        )

    # 3.4 Static Routes
    for r in router_plan.static_routes:
        if "/" in r.network:
            net_part, prefix_part = r.network.split("/", 1)
            prefix_len = int(prefix_part)
        else:
            net_part = r.network
            prefix_len = 24

        # Se è uno switch L2 e la rotta è di default (0.0.0.0/0), la gestiamo nella base_config
        # per evitare la duplicazione dei comandi.
        if vendor_type == "cisco_switch" and net_part == "0.0.0.0":
            continue

        device_delta.route_deltas.append(
            diff_route(net_part, prefix_len, r.next_hop, current_routes)
        )

    # 3.5 VLAN definitions
    if vendor_type == "cisco_switch":
        for vlan_id, vlan_name in router_plan.vlans.items():
            device_delta.vlan_deltas.append(
                diff_vlan(int(vlan_id), vlan_name, running_config_raw)
            )

    # 3.6 VPCS Delta
    if vendor_type == "vpcs":
        if router_plan.interfaces:
            iface = router_plan.interfaces[0]
            if iface.ip == "dhcp":
                vpcs_mode = "dhcp"
                desired_ip, desired_mask, desired_gw = "", "", ""
            else:
                vpcs_mode = "static"
                if iface.ip and "/" in iface.ip:
                    desired_ip, cidr_str = iface.ip.split("/", 1)
                    try:
                        desired_mask = str(ipaddress.IPv4Network(f"0.0.0.0/{cidr_str}").netmask)
                    except Exception:
                        desired_mask = "255.255.255.0"
                else:
                    desired_ip = iface.ip or ""
                    desired_mask = "255.255.255.0"
                desired_gw = ""
                if router_plan.static_routes:
                    desired_gw = router_plan.static_routes[0].next_hop

            from tools.parser import normalize_interface_name
            norm_name = normalize_interface_name(iface.name)
            current_ip = "unassigned"
            for k in (norm_name, iface.name, "eth0", "Ethernet0"):
                if k in current_interfaces:
                    current_ip = current_interfaces[k]
                    break
            if vpcs_mode == "dhcp":
                has_valid_ip = (
                    current_ip != "unassigned"
                    and not current_ip.startswith("0.0.0.0")
                )
                device_delta.vpcs_delta = VPCSDelta(
                    action_needed="CORRECT" if has_valid_ip else "NEED_DHCP",
                    current_ip=current_ip,
                )
            else:
                cidr = _mask_to_cidr(desired_mask)
                if _ips_equivalent(desired_ip, cidr, current_ip):
                    device_delta.vpcs_delta = VPCSDelta(action_needed="CORRECT", current_ip=current_ip)
                else:
                    device_delta.vpcs_delta = VPCSDelta(
                        action_needed="STATIC_REQUIRED",
                        desired_ip=desired_ip,
                        desired_mask=desired_mask,
                        desired_gw=desired_gw,
                        current_ip=current_ip,
                    )

    # 3.7 Spegnimento interfacce inutilizzate (solo cisco_ios/cisco_switch)
    if vendor_type in ("cisco_ios", "cisco_switch"):
        from tools.parser import normalize_interface_name
        used_interfaces = set()
        for iface in router_plan.interfaces:
            used_interfaces.add(normalize_interface_name(iface.name))
            if "." in iface.name:
                parent = iface.name.split(".")[0]
                used_interfaces.add(normalize_interface_name(parent))
        
        unused_interfaces = []
        for iface_name in current_interfaces.keys():
            norm_name = normalize_interface_name(iface_name)
            is_logical = any(
                norm_name.lower().startswith(pfx)
                for pfx in ("vlan", "loopback", "null", "port-channel", "tunnel")
            ) or "." in norm_name
            
            if not is_logical and norm_name not in used_interfaces:
                if not re.match(r'^(Ethernet|FastEthernet|GigabitEthernet|Serial|Vlan|Port-channel|Po|Loopback|Lo|e|fa|gi|se|po|eth|ens|eno|enp)\d+([\w/.-]*)$', norm_name, re.IGNORECASE):
                    logger.warning("[%s] Scartata interfaccia malformata: %s", router_name, norm_name)
                    continue
                from generate.diff.engine import _is_interface_shutdown
                from ciscoconfparse import CiscoConfParse
                is_shut = False
                if running_config_raw:
                    parse = CiscoConfParse(running_config_raw.splitlines(), factory=False)
                    is_shut = _is_interface_shutdown(norm_name, parse)
                if not is_shut:
                    unused_interfaces.append(norm_name)
        
        device_delta.unused_interfaces_to_shutdown = sorted(unused_interfaces)

    # 3.8 Riconciliazione Sweep (Fase 1 e Fase 2)
    if vendor_type in ("cisco_ios", "cisco_switch", "frrouting"):
        from generate.diff.engine import (
            diff_routes_sweep,
            diff_vlans_sweep,
            diff_dhcp_pools_sweep,
            diff_subinterfaces_sweep,
        )
        
        # 1. Route Sweep
        device_delta.extra_routes_to_remove = diff_routes_sweep(
            current_routes, router_plan.static_routes
        )
        
        # 2. VLAN Sweep (solo Cisco Switch)
        if vendor_type == "cisco_switch":
            device_delta.extra_vlans_to_remove = diff_vlans_sweep(
                running_config_raw, router_plan.vlans
            )
            
        # 3. DHCP Pools Sweep (solo router L3 / Cisco IOS / FRRouting)
        if vendor_type in ("cisco_ios", "frrouting"):
            device_delta.extra_dhcp_pools_to_remove = diff_dhcp_pools_sweep(
                running_config_raw, router_plan.dhcp_pools
            )
            
        # 4. Subinterfaces Sweep (solo Cisco IOS)
        if vendor_type == "cisco_ios":
            device_delta.extra_subinterfaces_to_remove = diff_subinterfaces_sweep(
                running_config_raw, router_plan.interfaces, mgmt_ip
            )

    # 4. Gate idempotenza
    if device_delta.is_fully_idempotent:
        logger.info("[%s] Idempotente. Nessun comando.", router_name)
        return {"router_commands": {router_name: RouterCommands(pairs=[])}}

    delta_description = device_delta.describe()

    # 5. Compilazione
    can_compile = (
        vendor_type in ("frrouting", "cisco_ios", "cisco_switch")
        and any([
            device_delta.interface_deltas, device_delta.route_deltas,
            device_delta.dhcp_deltas, device_delta.base_config_delta,
            device_delta.helper_address_deltas,
            device_delta.vlan_deltas,           # NUOVO
            device_delta.switchport_deltas,     # NUOVO
            device_delta.subinterface_deltas,   # NUOVO
            device_delta.etherchannel_deltas,   # NUOVO
            device_delta.unused_interfaces_to_shutdown, # NUOVO: spegnimento unused ports
            device_delta.extra_routes_to_remove, # NUOVO: sweep routes
            device_delta.extra_vlans_to_remove, # NUOVO: sweep VLANs
            device_delta.extra_dhcp_pools_to_remove, # NUOVO: sweep DHCP
            device_delta.extra_subinterfaces_to_remove, # NUOVO: sweep subinterfaces
        ])
    ) or (vendor_type == "vpcs" and device_delta.vpcs_delta is not None)

    if can_compile:
        logger.info("[%s] Compilazione Jinja2.\nDelta:\n%s", router_name, delta_description)
        commands = _compile_delta(device_delta, vendor_type)
    else:
        logger.info("[%s] Fallback LLM.\nDelta:\n%s", router_name, delta_description)
        plan_desc = (
            f"[DEVICE TYPE: {vendor_type.upper()}]\n"
            f"Target Interfaces: {', '.join(router_plan.interfaces)}\n"
            f"PRE-COMPUTED IDEMPOTENCY DELTA:\n{delta_description}"
        )
        action_plan = await llm_client.generate_commands(
            router_name=router_name, delta_description=plan_desc
        )
        if not action_plan or not action_plan.actions:
            return {"router_commands": {router_name: RouterCommands(pairs=[])}}
        commands = renderer.compile_action_plan(action_plan, vendor_type=vendor_type)

    if commands is None:
        commands = RouterCommands(pairs=[])

    total_pairs = len(commands.pairs)
    total_lines = sum(len([l for l in p.cmd.splitlines() if l.strip()]) for p in commands.pairs)
    logger.info("[%s] %d coppie comandi (%d righe CLI).", router_name, total_pairs, total_lines)
    for i, pair in enumerate(commands.pairs):
        logger.info(
            "  [%02d] CMD: %-55s | RB: %s",
            i + 1, f"'{pair.cmd}'",
            f"'{pair.rollback}'" if pair.rollback else "(none)",
        )
    return {"router_commands": {router_name: commands}}


async def generate_relay_node(state: dict) -> dict:
    """
    Worker fan-out per un singolo device relay.

    Riceve dallo stato:
      router_name  : nome del device (es. "R1")
      router_plan  : RouterIntent con extra_params contenente DHCP_RELAY
    """
    router_name: str = state["router_name"]
    router_plan      = state["router_plan"]

    logger.info("[%s] >>> GENERATE_RELAY <<<", router_name)

    # Estrae relay_subnets e dhcp_server_router in modo robusto
    relay_subnets, dhcp_server_router = extract_dhcp_relay_params(
        router_plan.extra_params,
        getattr(router_plan, "dhcp_relay_server", None),
        getattr(router_plan, "dhcp_relay_subnets", None)
    )

    if not relay_subnets or not dhcp_server_router:
        logger.warning("[%s] GENERATE_RELAY: DHCP_RELAY o DHCP_SERVER mancante.", router_name)
        return {"router_commands": {router_name: RouterCommands(pairs=[])}}

    # Calcola helper-address con Neo4j aggiornato (no desired_topology needed)
    async with AsyncNetworkGraphStore() as store:
        actions = await compute_helper_addresses(
            relay_subnets, dhcp_server_router, store
        )

    if not actions:
        logger.info("[%s] GENERATE_RELAY: nessun helper-address da configurare.", router_name)
        return {"router_commands": {router_name: RouterCommands(pairs=[])}}

    # Compila i comandi via Jinja2
    all_pairs: list[CommandPair] = []
    for action in actions:
        if action.already_present:
            logger.info(
                "[%s] helper-address %s su %s già presente (idempotente).",
                router_name, action.dhcp_server_ip, action.iface,
            )
            continue

        fwd, rb = renderer.render_with_rollback(
            "cisco_ios/helper_address.j2",
            iface=action.iface,
            helper_ip=action.dhcp_server_ip,
        )
        all_pairs += _lines_to_pairs(fwd, rb)
        logger.info(
            "[%s] helper-address %s su %s da configurare.",
            router_name, action.dhcp_server_ip, action.iface,
        )

    commands = RouterCommands(pairs=all_pairs)
    total_pairs = len(all_pairs)
    total_lines = sum(len([l for l in p.cmd.splitlines() if l.strip()]) for p in all_pairs)
    logger.info("[%s] GENERATE_RELAY: %d coppie comandi (%d righe CLI).", router_name, total_pairs, total_lines)
    for i, pair in enumerate(all_pairs):
        logger.info(
            "  [%02d] CMD: '%-50s' | RB: %s",
            i + 1, pair.cmd,
            f"'{pair.rollback}'" if pair.rollback else "(none)",
        )

    return {"router_commands": {router_name: commands}}
