import sys
import pytest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generate.diff.engine import diff_etherchannel, diff_subinterface
from tools.parser import normalize_interface_name


def test_normalize_port_channel_keeps_canonical_name():
    assert normalize_interface_name("Port-channel1") == "Port-channel1"
    assert normalize_interface_name("Po1") == "Port-channel1"



def test_subinterface_vlan_mismatch_is_wrong_even_if_ip_matches():
    running = """
interface Ethernet0/0.10
 encapsulation dot1Q 20
 ip address 192.168.10.1 255.255.255.0
end
"""
    delta = diff_subinterface(
        "Ethernet0/0",
        10,
        10,
        "192.168.10.1",
        24,
        running,
    )

    assert delta.action_needed == "WRONG"
    assert delta.current_vlan_id == 20


def test_etherchannel_detects_stale_member_and_ignores_switchport_marker():
    running = """
interface Ethernet0/1
 switchport
 channel-group 1 mode active
interface Ethernet0/2
 switchport
 channel-group 1 mode active
interface Ethernet0/3
 switchport
 channel-group 1 mode active
interface Port-channel1
 switchport
end
"""
    delta = diff_etherchannel(
        "Port-channel1",
        ["Ethernet0/1", "Ethernet0/2"],
        "active",
        running,
    )

    assert delta.action_needed != "CORRECT"
    assert delta.stale_members == ["Ethernet0/3"]
    assert delta.dirty_members == []




@pytest.mark.asyncio
async def test_collect_state_parses_etherchannels():
    import pytest
    from unittest.mock import AsyncMock
    from tools.device_snapshot import _collect_state

    mock_conn = AsyncMock()
    mock_conn.send_command.side_effect = lambda cmd, *args, **kwargs: {
        "terminal length 0": "",
        "show ip interface brief": "Ethernet0/1 192.168.1.1 YES NVRAM up up",
        "show running-config": """
interface Ethernet2/0
 switchport mode trunk
 channel-group 1 mode active
!
interface Ethernet2/1
 switchport mode trunk
 channel-group 1 mode active
!
interface Port-channel1
 switchport mode trunk
""",
        "show interfaces trunk": "",
        "show etherchannel summary": "",
        "show spanning-tree": "",
        "show ip dhcp binding": "",
    }.get(cmd, "")

    snapshot = await _collect_state("SW1", "cisco_switch", mock_conn)
    assert snapshot.etherchannels == {
        "Port-channel1": ["Ethernet2/0", "Ethernet2/1"]
    }


def test_switch_static_route_generation():
    from tools.template_engine import CliRenderer

    renderer = CliRenderer()

    # 1. Default Route (0.0.0.0/0) on cisco_switch (should generate both commands)
    fwd, rb = renderer.render_with_rollback(
        "cisco_switch/static_route.j2",
        network="0.0.0.0",
        mask="0.0.0.0",
        next_hop="192.168.99.1",
        stale_next_hop="",
    )

    assert any("ip default-gateway 192.168.99.1" in line for line in fwd)
    assert not any("ip route 0.0.0.0 0.0.0.0" in line for line in fwd)
    assert any("no ip default-gateway 192.168.99.1" in line for line in rb)
    assert not any("no ip route 0.0.0.0 0.0.0.0" in line for line in rb)

    # 2. Non-default Route (192.168.10.0/24) on cisco_switch (should only generate ip route)
    fwd_non, rb_non = renderer.render_with_rollback(
        "cisco_switch/static_route.j2",
        network="192.168.10.0",
        mask="255.255.255.0",
        next_hop="192.168.99.1",
        stale_next_hop="",
    )

    assert not any("ip default-gateway" in line for line in fwd_non)
    assert any("ip route 192.168.10.0 255.255.255.0 192.168.99.1" in line for line in fwd_non)


@pytest.mark.asyncio
async def test_collect_state_parses_switch_routes():
    from unittest.mock import AsyncMock
    from tools.device_snapshot import _collect_state

    mock_conn = AsyncMock()
    mock_conn.send_command.side_effect = lambda cmd, *args, **kwargs: {
        "terminal length 0": "",
        "show ip interface brief": "Ethernet0/1 192.168.1.1 YES NVRAM up up",
        "show running-config": """
interface Vlan99
 ip address 192.168.99.2 255.255.255.0
!
ip default-gateway 192.168.99.1
ip route 192.168.10.0 255.255.255.0 192.168.99.10
""",
        "show interfaces trunk": "",
        "show etherchannel summary": "",
        "show spanning-tree": "",
        "show ip dhcp binding": "",
    }.get(cmd, "")

    snapshot = await _collect_state("SW1", "cisco_switch", mock_conn)
    assert snapshot.static_routes == {
        "0.0.0.0/0": "192.168.99.1",
        "192.168.10.0/24": "192.168.99.10"
    }


@pytest.mark.asyncio
async def test_snapshot_device_saves_switch_routes():
    from unittest.mock import AsyncMock, patch, MagicMock
    from tools.device_snapshot import snapshot_device

    mock_store = AsyncMock()
    mock_store.upsert_device = AsyncMock()
    mock_store.upsert_interface = AsyncMock()
    mock_store.clear_static_routes = AsyncMock()
    mock_store.upsert_static_route = AsyncMock()
    mock_store.store_running_config = AsyncMock()

    mock_snapshot = MagicMock()
    mock_snapshot.interfaces = {"Vlan99": "192.168.99.3"}
    mock_snapshot.etherchannels = {}
    mock_snapshot.l2_neighbors = []
    mock_snapshot.running_config = "ip default-gateway 192.168.99.1"
    mock_snapshot.static_routes = {"0.0.0.0/0": "192.168.99.1"}

    with patch("tools.device_snapshot.resolve_vendor", return_value="cisco_switch"), \
         patch("tools.device_snapshot.get_connection"), \
         patch("tools.device_snapshot._collect_state", new_callable=AsyncMock, return_value=mock_snapshot):

        await snapshot_device("SW2", {"port": 5001}, mock_store)

        mock_store.clear_static_routes.assert_called_once_with("SW2")
        mock_store.upsert_static_route.assert_called_once_with("SW2", "0.0.0.0/0", "192.168.99.1")


@pytest.mark.asyncio
async def test_telnet_login_fail_aborts_loop():
    from tools.connection import AsyncTelnetConnection, Vendor
    from unittest.mock import AsyncMock, patch, MagicMock

    conn = AsyncTelnetConnection(host="127.0.0.1", port=5000, vendor=Vendor.CISCO_SWITCH)
    conn._reader = AsyncMock()
    conn._writer = MagicMock()

    # Simulate sequence:
    # 1. returns Username:
    # 2. returns Password:
    # 3. returns % Login invalid
    mock_responses = [
        "Username: ",
        "Password: ",
        "% Login invalid\nUsername: ",
    ]
    conn._read_until_prompt = AsyncMock(side_effect=mock_responses)

    with patch("tools.connection.telnetlib3.open_connection", new_callable=AsyncMock, return_value=(conn._reader, conn._writer)), \
         patch("tools.connection.asyncio.sleep", new_callable=AsyncMock):

        with pytest.raises(PermissionError) as exc_info:
            await conn._boot_wait()

        assert "Accesso negato" in str(exc_info.value)


def test_frrouting_multi_ip_parsing():
    from tools.parser import parse_interfaces
    raw = (
        "eth0            up      default         1.10.0.1/24\n"
        "                                        192.168.1.1/24\n"
        "eth1            up      default         11.11.11.1/30\n"
        "eth2            up      default         128.50.0.1/24\n"
        "                                        192.168.4.1/24\n"
        "lo              up      default\n"
    )
    res = parse_interfaces(raw, "frrouting")
    by_name = {r["name"]: r for r in res}
    assert "eth0" in by_name
    assert "eth1" in by_name
    assert "eth2" in by_name
    assert "lo" not in by_name
    
    assert by_name["eth0"]["ip"] == "1.10.0.1/24,192.168.1.1/24"
    assert by_name["eth1"]["ip"] == "11.11.11.1/30"
    assert by_name["eth2"]["ip"] == "128.50.0.1/24,192.168.4.1/24"


def test_ips_equivalent_supports_comma_separated_ips():
    from generate.diff.engine import _ips_equivalent
    assert _ips_equivalent("192.168.1.1", 24, "1.10.0.1/24,192.168.1.1/24") is True
    assert _ips_equivalent("1.10.0.1", 24, "1.10.0.1/24,192.168.1.1/24") is True
    assert _ips_equivalent("192.168.1.2", 24, "1.10.0.1/24,192.168.1.1/24") is False


@pytest.mark.asyncio
async def test_collect_state_parses_multi_hop_routes():
    from unittest.mock import AsyncMock
    from tools.device_snapshot import _collect_state

    mock_conn = AsyncMock()
    mock_conn.send_command.side_effect = lambda cmd, *args, **kwargs: {
        "terminal length 0": "",
        "show interface brief": "eth0 up [up] 1.10.0.1/24",
        "show running-config": """
ip route 192.168.2.0/24 11.11.11.2
ip route 192.168.2.0/24 12.12.12.1
ip route 192.168.3.0/24 11.11.11.2
""",
    }.get(cmd, "")

    snapshot = await _collect_state("R1", "frrouting", mock_conn)
    assert snapshot.static_routes == {
        "192.168.2.0/24": "11.11.11.2,12.12.12.1",
        "192.168.3.0/24": "11.11.11.2"
    }


def test_diff_route_supports_comma_separated_hops():
    from generate.diff.engine import diff_route
    current_routes = {
        "192.168.2.0/24": "11.11.11.2,12.12.12.1"
    }
    
    # Matches one of the hops -> CORRECT
    delta1 = diff_route("192.168.2.0", 24, "11.11.11.2", current_routes)
    assert delta1.action_needed == "CORRECT"
    
    # Matches another hop -> CORRECT
    delta2 = diff_route("192.168.2.0", 24, "12.12.12.1", current_routes)
    assert delta2.action_needed == "CORRECT"
    
    # Desired hop not present -> MISSING (with stale_next_hop_to_remove as the concatenated list to replace)
    delta3 = diff_route("192.168.2.0", 24, "13.13.13.1", current_routes)
    assert delta3.action_needed == "MISSING"
    assert delta3.stale_next_hop_to_remove == "11.11.11.2,12.12.12.1"


def test_etherchannel_detects_dirty_members():
    # 1. Config with redundant switchport properties on member interfaces
    running = """
interface Ethernet0/1
 switchport mode trunk
 switchport trunk allowed vlan 10,20
 channel-group 1 mode active
interface Ethernet0/2
 switchport access vlan 10
 channel-group 1 mode active
interface Port-channel1
 switchport mode trunk
 switchport trunk allowed vlan 10,20
end
"""
    delta = diff_etherchannel(
        "Port-channel1",
        ["Ethernet0/1", "Ethernet0/2"],
        "active",
        running,
    )
    assert delta.action_needed == "WRONG"
    assert set(delta.dirty_members) == {"Ethernet0/1", "Ethernet0/2"}

    # 2. Config with only "switchport" (no redundant properties) should not be dirty
    running_clean = """
interface Ethernet0/1
 switchport
 channel-group 1 mode active
interface Ethernet0/2
 switchport
 channel-group 1 mode active
interface Port-channel1
 switchport mode trunk
end
"""
    delta_clean = diff_etherchannel(
        "Port-channel1",
        ["Ethernet0/1", "Ethernet0/2"],
        "active",
        running_clean,
    )
    assert delta_clean.action_needed == "CORRECT"
    assert delta_clean.dirty_members == []


def test_truncate_operational_status():
    import textwrap
    from tools.device_snapshot import truncate_operational_status
    raw_status = textwrap.dedent("""
        --- show ip arp ---
        Protocol  Address          Age (min)  Hardware Addr   Type   Interface
        Internet  192.168.10.1            -   aabb.cc00.0100  ARPA   Ethernet0/0.10
        Internet  192.168.10.2            0   0050.7966.6800  ARPA   Ethernet0/0.10
        Internet  192.168.10.3            0   0050.7966.6802  ARPA   Ethernet0/0.10
        Internet  192.168.20.1            -   aabb.cc00.0100  ARPA   Ethernet0/0.20
        Internet  192.168.20.2            0   0050.7966.6801  ARPA   Ethernet0/0.20
        Internet  192.168.20.3            0   0050.7966.6803  ARPA   Ethernet0/0.20
        Internet  192.168.30.1            -   aabb.cc00.0100  ARPA   Ethernet0/0.30
        Internet  192.168.30.2            0   0050.7966.6804  ARPA   Ethernet0/0.30
        Internet  192.168.99.1            -   aabb.cc00.0100  ARPA   Ethernet0/0.99
        Internet  192.168.99.2            0   0050.7966.6805  ARPA   Ethernet0/0.99
        Internet  192.168.99.3            0   0050.7966.6806  ARPA   Ethernet0/0.99

        --- show ip route ---
        Codes: L - local, C - connected, S - static, R - RIP, M - mobile, B - BGP
        Gateway of last resort is not set
        C        192.168.10.0/24 is directly connected, Ethernet0/0.10
        L        192.168.10.1/32 is directly connected, Ethernet0/0.10
    """).strip()
    truncated = truncate_operational_status(raw_status, max_lines_per_section=2)
    assert "--- show ip arp ---" in truncated
    assert "... (truncated, 10 lines omitted)" in truncated
    assert "--- show ip route ---" in truncated
    assert "... (truncated, 2 lines omitted)" in truncated


def test_cisco_unused_interfaces_shutdown():
    from generate.models.deltas import DeviceDelta
    from nodes.generate import CiscoIOSCompiler, CiscoSwitchCompiler
    
    delta = DeviceDelta(
        router_name="R1",
        unused_interfaces_to_shutdown=["Serial3/2", "Serial3/3"]
    )
    
    # Test IOSCompiler
    ios_compiler = CiscoIOSCompiler()
    ios_commands = ios_compiler.compile(delta, "cisco_ios")
    assert len(ios_commands) == 1
    assert "interface range Serial3/2 - 3\nshutdown" in ios_commands[0].cmd
    assert "no shutdown" in ios_commands[0].rollback
    
    # Test SwitchCompiler
    switch_compiler = CiscoSwitchCompiler()
    switch_commands = switch_compiler.compile(delta, "cisco_switch")
    assert len(switch_commands) == 1
    assert "interface range Serial3/2 - 3\nshutdown" in switch_commands[0].cmd
    assert "no shutdown" in switch_commands[0].rollback


def test_build_synthetic_status():
    from tools.device_snapshot import build_synthetic_status, DeviceSnapshot
    
    snapshot = DeviceSnapshot(
        router_name="SW1",
        vendor="cisco_switch",
        running_config="""
interface Vlan99
 ip address 192.168.99.3 255.255.255.0
!
ip ssh version 2
service password-encryption
username admin password cleartextpw
        """,
        interfaces={
            "Ethernet0/0": "192.168.10.1",
            "Ethernet0/1": "unassigned"
        },
        interface_statuses={
            "Ethernet0/0": "up",
            "Ethernet0/1": "down"
        },
        interface_l2={
            "Ethernet0/0": {
                "mode": "trunk",
                "trunk_vlans": [10, 20],
                "native_vlan": 99,
                "stp_state": {"10": "forwarding", "20": "blocking"}
            },
            "Ethernet0/1": {
                "mode": "access",
                "access_vlan": 10
            }
        },
        vlans={
            10: "ClientVlan",
            20: "ServerVlan"
        },
        etherchannels={
            "Port-channel1": ["Ethernet0/2", "Ethernet0/3"]
        },
        static_routes={
            "0.0.0.0/0": "192.168.99.1"
        }
    )
    
    status = build_synthetic_status(snapshot)
    
    assert "=== Synthetic Status for SW1 ===" in status
    assert "Vendor profile: cisco_switch" in status
    assert "[Interfaces L3]" in status
    assert "Ethernet0/0: 192.168.10.1 (up)" in status
    assert "[VLAN Database]" in status
    assert "10: ClientVlan" in status
    assert "[Switchports L2/STP]" in status
    assert "Ethernet0/0: mode=trunk" in status
    assert "STP=[VLAN 10:forwarding, VLAN 20:blocking]" in status
    assert "Ethernet0/1: mode=access, access_vlan=10" in status
    assert "[EtherChannels]" in status
    assert "Port-channel1: members=['Ethernet0/2', 'Ethernet0/3']" in status
    assert "[Static Routes]" in status
    assert "0.0.0.0/0 via 192.168.99.1" in status
    assert "[Security & Management]" in status
    assert "SSH version 2: Configured" in status
    assert "Service Password Encryption: Enabled" in status
    assert "Cleartext Passwords: WARNING" in status


@pytest.mark.asyncio
async def test_graph_store_operational_status_methods():
    from unittest.mock import AsyncMock, patch, MagicMock
    from tools.graph_store import AsyncNetworkGraphStore
    
    with patch("tools.graph_store.AsyncGraphDatabase.driver") as mock_driver_cls:
        mock_driver = MagicMock()
        mock_driver_cls.return_value = mock_driver
        mock_session = AsyncMock()
        mock_session.__aenter__.return_value = mock_session
        mock_driver.session.return_value = mock_session
        
        with patch.dict("os.environ", {"NEO4J_PASSWORD": "test_password"}):
            store = AsyncNetworkGraphStore(uri="bolt://localhost:7687", user="neo4j", password="test_password")
            
            await store.store_operational_status("R1", "some_op_status")
            
            assert mock_session.run.call_count > 0
            last_call = mock_session.run.call_args_list[0]
            query = last_call[0][0]
            params = last_call[1]
            
            assert "MERGE (d:Device {name: $device})" in query
            assert "MERGE (s:DeviceStatus {device: $device})" in query
            assert "SET s.operational_status = $op_status" in query
            assert params["device"] == "R1"
            assert params["op_status"] == "some_op_status"
            
            mock_session.run.reset_mock()
            await store.delete_operational_status("R1")
            
            assert mock_session.run.call_count > 0
            last_call_del = mock_session.run.call_args_list[0]
            query_del = last_call_del[0][0]
            params_del = last_call_del[1]
            
            assert "MATCH (:Device {name:$device})-[:HAS_STATUS]->(s:DeviceStatus {device:$device})" in query_del
            assert "DETACH DELETE s" in query_del
            assert params_del["device"] == "R1"


def test_interface_name_validation():
    from tools.device_snapshot import is_valid_interface_name
    
    assert is_valid_interface_name("Ethernet0/0")
    assert is_valid_interface_name("e0/0")
    assert is_valid_interface_name("GigabitEthernet1/1.100")
    assert is_valid_interface_name("Port-channel1")
    assert is_valid_interface_name("Vlan99")
    assert is_valid_interface_name("Loopback0")
    assert is_valid_interface_name("eth0")
    
    assert not is_valid_interface_name("-remote,")
    assert not is_valid_interface_name("-switch,")
    assert not is_valid_interface_name("MacRelay")
    assert not is_valid_interface_name("-")


def test_is_exec_prompt():
    from nodes.execute import _is_exec_prompt
    
    assert _is_exec_prompt("R1#")
    assert _is_exec_prompt("SW1#")
    assert _is_exec_prompt("  frr# ")
    
    assert not _is_exec_prompt("R1(config)#")
    assert not _is_exec_prompt("SW1(config-if)#")
    assert not _is_exec_prompt("R1(dhcp-config)#")
    assert not _is_exec_prompt("R1(config-router)#")
    assert not _is_exec_prompt("SW1(vlan)#")
    assert not _is_exec_prompt("PC1>")


@pytest.mark.asyncio
async def test_switch_ip_svi_normalization():
    from nodes.generate import generate_single_node
    from core.state import DeviceIntent, InterfaceIntent
    from unittest.mock import AsyncMock, patch
    
    router_plan = DeviceIntent(
        name="SW1",
        profile="cisco_switch",
        interfaces=[
            InterfaceIntent(name="Ethernet0/0", ip="192.168.10.2/24"),
            InterfaceIntent(name="Ethernet0/1")
        ]
    )
    
    state = {
        "router_name": "SW1",
        "router_plan": router_plan,
        "router_commands": {}
    }
    
    mock_repo = AsyncMock()
    mock_repo.get_device_state.return_value = (
        "cisco_switch",
        {"Ethernet0/0": {"ip": "unassigned"}, "Ethernet0/1": {"ip": "unassigned"}},
        {},
        "!"
    )
    
    with patch("nodes.generate.AsyncNetworkGraphStore") as mock_store_cls, \
         patch("nodes.generate.GenerateRepository", return_value=mock_repo):
        mock_store = AsyncMock()
        mock_store_cls.return_value = mock_store
        
        await generate_single_node(state)
        
        # Assertions
        normalized_interfaces = router_plan.interfaces
        # Physical interface should have no IP
        eth0 = next(i for i in normalized_interfaces if i.name == "Ethernet0/0")
        assert eth0.ip is None
        
        # Vlan1 interface should have been added with the SVI IP
        vlan1 = next(i for i in normalized_interfaces if i.name == "Vlan1")
        assert vlan1.ip == "192.168.10.2/24"


def test_vty_lines_flexible_matching():
    from ciscoconfparse import CiscoConfParse
    from generate.diff.engine import _has_command_in_block
    
    running = (
        "line vty 0 15\n"
        " login local\n"
        " transport input ssh\n"
    )
    parse = CiscoConfParse(running.splitlines(), factory=False)
    vty_header = r"line vty\s+0\b"
    
    assert _has_command_in_block(parse, vty_header, "login local")
    assert _has_command_in_block(parse, vty_header, "transport input ssh")
    assert not _has_command_in_block(parse, vty_header, "exec-timeout")


def test_banner_motd_comparison_handles_cisco_representation():
    from generate.models.deltas import BaseConfig
    from generate.diff.engine import diff_base_config
    
    desired = BaseConfig(
        hostname="R2",
        banner="Forza Napoli Sempre",
        enable_secret="cisco",
    )
    
    # Test case 1: literal banner match with caret-C delimiter in running config
    running_config_1 = "!\nbanner motd ^CForza Napoli Sempre^C\n!"
    delta1 = diff_base_config(desired, running_config_1, is_switch=False)
    assert not any(c.startswith("banner motd ") for c in delta1.missing_commands)
    
    # Test case 2: banner has carriage returns, running config has carriage returns
    running_config_2 = "!\nbanner motd ^C\r\nForza Napoli Sempre\r\n^C\n!"
    delta2 = diff_base_config(desired, running_config_2, is_switch=False)
    assert not any(c.startswith("banner motd ") for c in delta2.missing_commands)








