import sys
import asyncio
from pathlib import Path
import pytest
import yaml
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

@pytest.mark.asyncio
async def test_verify_node_parsers_yaml_and_legacy():
    from nodes.verify import verify_node
    from core.state import AgentState

    # YAML input
    spec_yaml = """
devices:
  - name: R1
    profile: cisco_ios
    interfaces: []
  - name: PC1
    profile: vpcs
    interfaces:
      - name: eth0
        ip: dhcp
  - name: PC2
    profile: vpcs
    interfaces:
      - name: eth0
        ip: 10.0.0.2/24
"""
    
    state = {
        "specification_raw": spec_yaml,
        "router_commands": {"R1": None, "PC1": None, "PC2": None},
        "intent": None,
        "troubleshoot_attempt": 0,
    }

    # Patch the async dependencies of verify_node so it runs without DB/network
    with patch("nodes.verify.load_inventory", return_value={"R1": {}, "PC1": {}, "PC2": {}}), \
         patch("nodes.verify._post_execute_graph_sync", new_callable=AsyncMock) as mock_sync, \
         patch("nodes.verify._fetch_live_ips_from_graph", new_callable=AsyncMock, return_value={"PC1": "10.0.0.3", "PC2": "10.0.0.2"}), \
         patch("nodes.verify.AsyncNetworkGraphStore") as mock_store:
        
        # mock_store.return_value context manager and methods
        mock_store_inst = mock_store.return_value
        mock_store_inst.clear_inactive_interfaces = AsyncMock()
        mock_store_inst.compute_topology_links = AsyncMock()
        mock_store_inst.close = AsyncMock()

        res = await verify_node(state)
        
        # Verify classification in the synced arguments:
        # infra = ["R1"], touched_dhcp = ["PC1"], touched_static = ["PC2"]
        mock_sync.assert_called_once()
        args = mock_sync.call_args[0]
        # args[0] is infra, args[1] is touched_dhcp, args[2] is touched_static
        assert args[0] == ["R1"]
        assert args[1] == ["PC1"]
        assert args[2] == ["PC2"]

    # Legacy text input
    spec_legacy = """
--- DEVICE: R1 ---
PROFILE: cisco_ios

--- DEVICE: PC1 ---
PROFILE: vpcs
IP_ADDRESS: DHCP

--- DEVICE: PC2 ---
PROFILE: vpcs
IP_ADDRESS: 10.0.0.2
"""
    state_legacy = {
        "specification_raw": spec_legacy,
        "router_commands": {"R1": None, "PC1": None, "PC2": None},
        "intent": None,
        "troubleshoot_attempt": 0,
    }

    with patch("nodes.verify.load_inventory", return_value={"R1": {}, "PC1": {}, "PC2": {}}), \
         patch("nodes.verify._post_execute_graph_sync", new_callable=AsyncMock) as mock_sync, \
         patch("nodes.verify._fetch_live_ips_from_graph", new_callable=AsyncMock, return_value={"PC1": "10.0.0.3", "PC2": "10.0.0.2"}), \
         patch("nodes.verify.AsyncNetworkGraphStore") as mock_store:
        
        mock_store_inst = mock_store.return_value
        mock_store_inst.clear_inactive_interfaces = AsyncMock()
        mock_store_inst.compute_topology_links = AsyncMock()
        mock_store_inst.close = AsyncMock()

        res = await verify_node(state_legacy)
        
        mock_sync.assert_called_once()
        args = mock_sync.call_args[0]
        assert args[0] == ["R1"]
        assert args[1] == ["PC1"]
        assert args[2] == ["PC2"]


def test_parse_vpcs_show_ip():
    from nodes.verify import _parse_vpcs_show_ip
    
    raw_output = """
NAME      : PC1[1]
IP/MASK   : 10.0.0.10/24
GATEWAY   : 10.0.0.1
DNS       : 8.8.8.8
MAC       : 00:50:79:66:68:00
LPORT     : 20000
RHOST:PORT: 127.0.0.1:20001
MTU       : 1500
"""
    ip, mask, gw = _parse_vpcs_show_ip(raw_output)
    assert ip == "10.0.0.10"
    assert mask == "255.255.255.0"
    assert gw == "10.0.0.1"
    
    # Test unassigned gateway
    raw_output_no_gw = """
IP/MASK   : 192.168.1.5/30
GATEWAY   : 0.0.0.0
"""
    ip, mask, gw = _parse_vpcs_show_ip(raw_output_no_gw)
    assert ip == "192.168.1.5"
    assert mask == "255.255.255.252"
    assert gw == ""


@pytest.mark.asyncio
async def test_heal_static_vpcs_if_needed():
    from nodes.verify import _heal_static_vpcs_if_needed
    
    static_configs = {
        "PC2": {
            "ip": "10.0.0.2",
            "mask": "255.255.255.0",
            "gateway": "10.0.0.1"
        }
    }
    
    live_map = {"PC2": "10.0.0.3"} # Mismatching IP!
    missing_static = []
    inventory = {"PC2": {"host": "127.0.0.1", "port": 5000, "vendor": "vpcs"}}
    
    mock_conn = MagicMock()
    mock_conn.send_command = AsyncMock(side_effect=[
        # show ip response
        """
NAME      : PC2[1]
IP/MASK   : 10.0.0.3/24
GATEWAY   : 10.0.0.1
""",
        # ip 10.0.0.2 255.255.255.0 10.0.0.1 response
        "Checking for duplicate IP...",
        # save response
        "Configuration saved"
    ])
    
    mock_store = MagicMock()
    
    with patch("nodes.verify.get_connection", return_value=MagicMock(__aenter__=AsyncMock(return_value=mock_conn), __aexit__=AsyncMock())), \
         patch("nodes.verify.snapshot_device", new_callable=AsyncMock) as mock_snap:
         
        corrected = await _heal_static_vpcs_if_needed(
            static_configs, live_map, missing_static, inventory, mock_store
        )
        
        assert corrected is True
        
        # Verify show ip command and config commands sent
        mock_conn.send_command.assert_any_call("show ip")
        mock_conn.send_command.assert_any_call("ip 10.0.0.2 255.255.255.0 10.0.0.1")
        mock_conn.send_command.assert_any_call("save")
        
        # Verify snapshot_device triggered to update Neo4j
        mock_snap.assert_called_once_with("PC2", inventory["PC2"], mock_store)


@pytest.mark.asyncio
async def test_run_ping_retries_before_declaring_failure():
    from nodes.verify import _run_ping

    outputs = [
        "84 bytes from 10.0.0.2 icmp_seq=1 ttl=64 time=0.557 ms\n"
        "10.0.0.2 ping statistics\n"
        "3 packets transmitted, 0 packets received, 100% packet loss",
        "84 bytes from 10.0.0.2 icmp_seq=1 ttl=64 time=0.557 ms\n"
        "84 bytes from 10.0.0.2 icmp_seq=2 ttl=64 time=0.509 ms\n"
        "10.0.0.2 ping statistics\n"
        "3 packets transmitted, 2 packets received, 33% packet loss",
    ]
    mock_conn = MagicMock()
    mock_conn.send_command = AsyncMock(side_effect=outputs)

    with patch("nodes.verify.get_connection", return_value=MagicMock(__aenter__=AsyncMock(return_value=mock_conn), __aexit__=AsyncMock())), \
         patch("nodes.verify.asyncio.sleep", new_callable=AsyncMock):
        src, dst, ok = await _run_ping("PC1", {"vendor": "vpcs"}, "10.0.0.2", asyncio.Lock(), attempts=2, retry_delay=0)

    assert (src, dst, ok) == ("PC1", "10.0.0.2", True)
    assert mock_conn.send_command.call_count == 2
