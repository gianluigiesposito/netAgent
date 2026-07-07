import sys
import pytest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock, call

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ipaddress
from generate.diff.engine import (
    diff_routes_sweep,
    diff_vlans_sweep,
    diff_dhcp_pools_sweep,
    diff_subinterfaces_sweep,
    get_management_interfaces
)
from tools.parser import normalize_interface_name
from generate.models.deltas import DeviceDelta, RouteDelta
from core.state import StaticRouteIntent, DhcpPoolIntent, InterfaceIntent, RouterCommands, CommandPair
from nodes.generate import CiscoIOSCompiler, CiscoSwitchCompiler, FrroutingCompiler
from nodes.execute import _execute_device


def test_management_interfaces_whitelisting():
    running = """
interface Ethernet0/0
 ip address 192.168.122.10 255.255.255.0
!
interface Ethernet0/1.10
 encapsulation dot1Q 10
 ip address 10.0.10.1 255.255.255.0
!
interface Ethernet0/1
 no ip address
!
"""
    # Test case 1: Management IP is on Ethernet0/0
    protected = get_management_interfaces(running, "192.168.122.10")
    assert "Ethernet0/0" in protected

    # Test case 2: Management IP is on Ethernet0/1.10 (subinterface)
    # The parent interface Ethernet0/1 must also be whitelisted!
    protected = get_management_interfaces(running, "10.0.10.1")
    assert "Ethernet0/1.10" in protected
    assert "Ethernet0/1" in protected


def test_diff_routes_sweep():
    # Desired routes
    desired = [
        StaticRouteIntent(network="10.0.0.0/24", next_hop="192.168.1.1"),
        StaticRouteIntent(network="20.0.0.0/16", next_hop="192.168.1.2")
    ]
    # Current routes as dict
    current = {
        "10.0.0.0/24": "192.168.1.1",        # matches
        "20.0.0.0/16": "192.168.1.2, 192.168.1.3", # extra next hop!
        "30.0.0.0/24": "192.168.1.4"         # completely extra route!
    }

    stale = diff_routes_sweep(current, desired)
    # Should flag 192.168.1.3 for 20.0.0.0/16 and 192.168.1.4 for 30.0.0.0/24
    stale_keys = {(r.network, r.cidr, r.next_hop) for r in stale}
    assert ("20.0.0.0", 16, "192.168.1.3") in stale_keys
    assert ("30.0.0.0", 24, "192.168.1.4") in stale_keys
    assert len(stale) == 2


def test_diff_vlans_sweep():
    running = """
vlan 1
!
vlan 10
 name LAN10
!
vlan 20
 name LAN20
!
vlan 30
 name LAN30
!
interface Ethernet0/1
 switchport mode access
 switchport access vlan 20
!
"""
    # Desired VLANs (dict of ID -> Name)
    desired = {
        "10": "LAN10"
    }

    # VLAN 30 is extra and not used by any port -> should be pruned.
    # VLAN 20 is extra but used by Ethernet0/1 -> should NOT be pruned!
    # VLAN 1 is a default VLAN -> should NOT be pruned.
    stale = diff_vlans_sweep(running, desired)
    assert stale == [(30, "LAN30")]


def test_diff_dhcp_pools_sweep():
    running = """
ip dhcp pool POOL1
 network 192.168.1.0 255.255.255.0
 default-router 192.168.1.254
!
ip dhcp pool POOL2
 network 192.168.2.0 255.255.255.0
 default-router 192.168.2.254
!
"""
    desired = [
        DhcpPoolIntent(
            name="POOL1",
            network="192.168.1.0/24",
            gateway="192.168.1.254"
        )
    ]

    stale = diff_dhcp_pools_sweep(running, desired)
    assert len(stale) == 1
    name, raw_cfg = stale[0]
    assert name == "POOL2"
    
    # Verify raw config normalization: leading spaces stripped, context header present.
    expected_raw = (
        "ip dhcp pool POOL2\n"
        "network 192.168.2.0 255.255.255.0\n"
        "default-router 192.168.2.254"
    )
    assert raw_cfg == expected_raw


def test_diff_subinterfaces_sweep():
    running = """
interface Ethernet0/0.10
 encapsulation dot1Q 10
 ip address 10.0.10.1 255.255.255.0
!
interface Ethernet0/0.20
 encapsulation dot1Q 20
 ip address 10.0.20.1 255.255.255.0
!
interface Ethernet0/1.30
 encapsulation dot1Q 30
 ip address 192.168.122.10 255.255.255.0
!
"""
    # Desired subinterfaces
    desired = [
        InterfaceIntent(name="Ethernet0/0.10", vlan_id=10, ip="10.0.10.1/24")
    ]
    # Management IP is on Ethernet0/1.30 -> should be protected!
    # Therefore, only Ethernet0/0.20 is stale!
    stale = diff_subinterfaces_sweep(running, desired, mgmt_ip="192.168.122.10")
    assert len(stale) == 1
    name, raw_cfg = stale[0]
    assert normalize_interface_name(name) == "Ethernet0/0.20"
    
    expected_raw = (
        "interface Ethernet0/0.20\n"
        "encapsulation dot1Q 20\n"
        "ip address 10.0.20.1 255.255.255.0"
    )
    assert raw_cfg == expected_raw


def test_compilers_sweep_generation():
    # Cisco IOS Compiler
    delta = DeviceDelta(
        router_name="R1",
        extra_routes_to_remove=[
            RouteDelta(network="30.0.0.0", cidr=24, next_hop="192.168.1.4", action_needed="REMOVE")
        ],
        extra_dhcp_pools_to_remove=[
            ("POOL2", "ip dhcp pool POOL2\nnetwork 192.168.2.0 255.255.255.0\ndefault-router 192.168.2.254")
        ],
        extra_subinterfaces_to_remove=[
            ("Ethernet0/0.20", "interface Ethernet0/0.20\nencapsulation dot1Q 20\nip address 10.0.20.1 255.255.255.0")
        ]
    )
    
    compiler = CiscoIOSCompiler()
    pairs = compiler.compile(delta, "cisco_ios")
    
    # Assert commands are compiled and matching
    assert len(pairs) == 3
    
    routes_pair = pairs[0]
    assert "no ip route 30.0.0.0 255.255.255.0 192.168.1.4" in routes_pair.cmd
    assert "ip route 30.0.0.0 255.255.255.0 192.168.1.4" in routes_pair.rollback
    
    dhcp_pair = pairs[1]
    assert "no ip dhcp pool POOL2" in dhcp_pair.cmd
    assert "ip dhcp pool POOL2\nnetwork 192.168.2.0 255.255.255.0\ndefault-router 192.168.2.254" in dhcp_pair.rollback
    
    subif_pair = pairs[2]
    assert "no interface Ethernet0/0.20" in subif_pair.cmd
    assert "!sleep 2" in subif_pair.cmd
    assert "interface Ethernet0/0.20\nencapsulation dot1Q 20\nip address 10.0.20.1 255.255.255.0" in subif_pair.rollback


def test_switch_compiler_sweep_generation():
    # Cisco Switch Compiler
    delta = DeviceDelta(
        router_name="SW1",
        extra_vlans_to_remove=[(30, "LAN30")]
    )
    
    compiler = CiscoSwitchCompiler()
    pairs = compiler.compile(delta, "cisco_switch")
    
    assert len(pairs) == 1
    vlan_pair = pairs[0]
    assert "no vlan 30" in vlan_pair.cmd
    assert "vlan 30\nname LAN30" in vlan_pair.rollback


def test_frr_compiler_sweep_generation():
    # FRRouting Compiler
    delta = DeviceDelta(
        router_name="R2",
        extra_routes_to_remove=[
            RouteDelta(network="30.0.0.0", cidr=24, next_hop="192.168.1.4", action_needed="REMOVE")
        ]
    )
    
    compiler = FrroutingCompiler()
    pairs = compiler.compile(delta, "frrouting")
    
    assert len(pairs) == 1
    routes_pair = pairs[0]
    assert "no ip route 30.0.0.0/24 192.168.1.4" in routes_pair.cmd
    assert "ip route 30.0.0.0/24 192.168.1.4" in routes_pair.rollback


@pytest.mark.asyncio
async def test_execute_device_local_sleep():
    """
    Verifica che il CLI executor intercetti correttamente i comandi '!sleep <durata>'
    e '__sleep__ <durata>' eseguendo uno sleep locale senza inoltrare nulla al dispositivo.
    """
    router_name = "R1"
    commands_obj = RouterCommands(pairs=[
        CommandPair(
            cmd="configure terminal\n!sleep 0.05\nexit",
            rollback="configure terminal\nexit"
        )
    ])
    reachability = {"R1": "REACHABLE"}
    inventory = {
        "R1": {
            "host": "127.0.0.1",
            "port": 5011,
            "vendor": "cisco_ios",
            "connection_type": "cisco_telnet",
        }
    }
    semaphore = asyncio.Semaphore(1)

    mock_conn = AsyncMock()
    mock_conn.send_command.return_value = "R1(config)#"
    mock_conn.save_config.return_value = True

    mock_get_conn = MagicMock()
    mock_get_conn.__aenter__.return_value = mock_conn

    with patch("nodes.execute.get_connection", return_value=mock_get_conn), \
         patch("nodes.execute.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
        
        msg, success = await _execute_device(
            router_name, commands_obj, reachability, inventory, semaphore
        )

        assert success is True
        # Verifichiamo che sia stata chiamata la sleep locale da 0.05s
        assert any(c[0][0] == 0.05 for c in mock_sleep.call_args_list)
        # Verifichiamo che '!sleep 0.05' non sia mai stato inviato come comando al dispositivo
        sent_commands = [c[0][0] for c in mock_conn.send_command.call_args_list]
        assert not any("!sleep" in cmd for cmd in sent_commands)


def test_diff_routes_sweep_edge_cases():
    # Caso 1: Actual e desired vuoti
    assert diff_routes_sweep({}, []) == []

    # Caso 2: desired_routes con IP senza CIDR (/32 o default /24)
    # Nel nostro codice: se "/" non è presente, assume /24
    desired = [
        StaticRouteIntent(network="10.1.1.1", next_hop="192.168.1.10")
    ]
    current = {
        "10.1.1.0/24": "192.168.1.10"
    }
    assert diff_routes_sweep(current, desired) == []

    # Caso 3: actual route next_hop con spazi extra o casing differente sul next_hop
    desired = [
        StaticRouteIntent(network="10.0.0.0/24", next_hop="192.168.1.1")
    ]
    current = {
        "10.0.0.0/24": "  192.168.1.1  "
    }
    assert diff_routes_sweep(current, desired) == []


def test_diff_vlans_sweep_edge_cases():
    # Caso 1: VLAN attive su trunk con spazi, range o native
    running = """
interface Ethernet0/1
 switchport mode trunk
 switchport trunk native vlan 99
 switchport trunk allowed vlan 10-15, 20-25
!
"""
    desired = {}
    stale = diff_vlans_sweep(running, desired)
    # VLAN 10,11,12,13,14,15, 20,21,22,23,24,25 e 99 sono referenziate -> non devono essere stale!
    stale_ids = {vid for vid, _ in stale}
    for protected_vid in [10, 11, 12, 13, 14, 15, 20, 21, 22, 23, 24, 25, 99]:
        assert protected_vid not in stale_ids


def test_diff_dhcp_pools_normalization_edge_cases():
    # Caso 1: Pool DHCP annidato vuoto o con righe vuote
    running = """
ip dhcp pool POOL_EMPTY
   
   dns-server 8.8.8.8
!
"""
    desired = []
    stale = diff_dhcp_pools_sweep(running, desired)
    assert len(stale) == 1
    name, raw_cfg = stale[0]
    assert name == "POOL_EMPTY"
    assert raw_cfg == "ip dhcp pool POOL_EMPTY\ndns-server 8.8.8.8"


def test_diff_subinterfaces_management_parent_protection_edge_cases():
    # Caso 1: mgmt_ip nullo o locale -> non whitelista alcuna interfaccia
    running = """
interface Ethernet0/0.10
 encapsulation dot1Q 10
 ip address 10.0.10.1 255.255.255.0
!
"""
    desired = []
    stale_none = diff_subinterfaces_sweep(running, desired, mgmt_ip="")
    assert len(stale_none) == 1
    assert stale_none[0][0] == "Ethernet0/0.10"

    stale_local = diff_subinterfaces_sweep(running, desired, mgmt_ip="127.0.0.1")
    assert len(stale_local) == 1

    # Caso 2: Interfaccia fisica con IP di management (non subinterface)
    running_phys = """
interface Ethernet0/0
 ip address 192.168.122.10 255.255.255.0
!
interface Ethernet0/0.20
 encapsulation dot1Q 20
 ip address 10.0.20.1 255.255.255.0
!
"""
    # L'IP di management è sull'interfaccia fisica Ethernet0/0.
    # get_management_interfaces dovrebbe whitelistarne Ethernet0/0.
    # Ethernet0/0.20 è una subinterface spuria -> non ospita il mgmt IP -> deve essere rilevata come stale!
    stale = diff_subinterfaces_sweep(running_phys, desired, mgmt_ip="192.168.122.10")
    assert len(stale) == 1
    assert stale[0][0] == "Ethernet0/0.20"


@pytest.mark.asyncio
async def test_enable_secret_env_resolution():
    import os
    from unittest.mock import patch, MagicMock, AsyncMock
    from core.state import DeviceIntent
    from nodes.generate import generate_single_node
    from generate.models.deltas import BaseConfig

    mock_state = {
        "router_name": "R1",
        "router_plan": DeviceIntent(
            name="R1",
            profile="cisco_ios",
            enable_secret="env:TEST_SECRET_VAR",
            domain_name="test.local"
        ),
        "spec_path": "config/test.yaml",
        "troubleshoot_attempt": 0,
        "executed_commands": {},
        "execution_log": [],
        "final_status": "SUCCESS"
    }

    os.environ["TEST_SECRET_VAR"] = "my_secure_env_password"

    with patch("nodes.generate.AsyncNetworkGraphStore") as mock_store, \
         patch("nodes.generate.GenerateRepository") as mock_repo, \
         patch("nodes.generate.diff_base_config") as mock_diff_base:

         mock_db = AsyncMock()
         mock_db.get_device_state.return_value = ("cisco_ios", {}, {}, "hostname R1\n")
         mock_repo.return_value = mock_db

         mock_store.return_value.__aenter__.return_value = MagicMock()
         mock_diff_base.return_value = MagicMock()

         await generate_single_node(mock_state)

         called_desired = mock_diff_base.call_args[0][0]
         assert isinstance(called_desired, BaseConfig)
         assert called_desired.enable_secret == "my_secure_env_password"
