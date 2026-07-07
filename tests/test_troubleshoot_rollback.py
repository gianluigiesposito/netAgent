# tests/test_troubleshoot_rollback.py
import sys
import asyncio
from pathlib import Path
import pytest
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.state import AgentState, RouterCommands, CommandPair, NetworkIntentSchema, DeviceIntent
from core.graph import _route_after_troubleshoot, _route_to_relay_workers
from langgraph.graph import END

@pytest.mark.asyncio
async def test_route_after_troubleshoot():
    # Exhausted status -> END
    state_exhausted = {"final_status": "TROUBLESHOOT_EXHAUSTED"}
    assert _route_after_troubleshoot(state_exhausted) == END

    # Other status -> APPROVAL_TROUBLESHOOT
    state_other = {"final_status": "FAILED"}
    assert _route_after_troubleshoot(state_other) == "APPROVAL_TROUBLESHOOT"


def test_troubleshoot_filters_vlan_commands_for_flat_network():
    from nodes.troubleshoot import _fix_to_router_commands

    fix_data = {
        "fixes": [
            {
                "device": "SW1",
                "commands": [
                    {"cmd": "configure terminal", "rollback": "exit"},
                    {"cmd": "switchport mode access", "rollback": "no switchport mode"},
                    {"cmd": "switchport trunk native vlan 999", "rollback": "no switchport trunk native vlan"},
                    {"cmd": "ip default-gateway 192.168.1.1", "rollback": "no ip default-gateway 192.168.1.1"},
                ],
            }
        ]
    }

    commands = _fix_to_router_commands(fix_data, flat_network=True)
    rendered = "\n".join(pair.cmd for pair in commands["SW1"].pairs)

    assert "ip default-gateway 192.168.1.1" in rendered
    assert "switchport mode access" not in rendered
    assert "switchport trunk native vlan" not in rendered


@pytest.mark.asyncio
async def test_verify_node_postpones_and_runs_rollback():
    from nodes.verify import verify_node

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
"""
    # 1. Under MAX_ATTEMPTS (attempt=0) -> rollback is postponed
    state_postpone = {
        "specification_raw": spec_yaml,
        "router_commands": {"R1": RouterCommands(pairs=[CommandPair(cmd="conf t", rollback="exit")]), "PC1": RouterCommands(pairs=[])},
        "intent": None,
        "troubleshoot_attempt": 0,
        "failed_devices": ["R1", "PC1"],
        "executed_commands": {"R1": RouterCommands(pairs=[CommandPair(cmd="conf t", rollback="exit")])},
    }

    with patch("nodes.verify.load_inventory", return_value={"R1": {}}), \
         patch("nodes.verify._post_execute_graph_sync", new_callable=AsyncMock), \
         patch("nodes.verify._fetch_live_ips_from_graph", new_callable=AsyncMock, return_value={}), \
         patch("nodes.verify._rollback_device", new_callable=AsyncMock) as mock_rollback, \
         patch("nodes.verify.AsyncNetworkGraphStore") as mock_store:
        
        mock_store_inst = mock_store.return_value
        mock_store_inst.clear_inactive_interfaces = AsyncMock()
        mock_store_inst.compute_topology_links = AsyncMock()
        mock_store_inst.close = AsyncMock()

        res = await verify_node(state_postpone)
        assert res["final_status"] == "FAILED"
        mock_rollback.assert_not_called()
        assert any("Rollback rimandato" in line for line in res["execution_log"])

    # 2. Over or equal to MAX_ATTEMPTS (attempt=3) -> rollback is run
    state_run = {
        "specification_raw": spec_yaml,
        "router_commands": {"R1": RouterCommands(pairs=[CommandPair(cmd="conf t", rollback="exit")]), "PC1": RouterCommands(pairs=[])},
        "intent": None,
        "troubleshoot_attempt": 3,
        "failed_devices": ["R1", "PC1"],
        "executed_commands": {"R1": RouterCommands(pairs=[CommandPair(cmd="conf t", rollback="exit")])},
    }

    with patch("nodes.verify.load_inventory", return_value={"R1": {}}), \
         patch("nodes.verify._post_execute_graph_sync", new_callable=AsyncMock), \
         patch("nodes.verify._fetch_live_ips_from_graph", new_callable=AsyncMock, return_value={}), \
         patch("nodes.verify._rollback_device", new_callable=AsyncMock, return_value=True) as mock_rollback, \
         patch("nodes.verify.AsyncNetworkGraphStore") as mock_store:
        
        mock_store_inst = mock_store.return_value
        mock_store_inst.clear_inactive_interfaces = AsyncMock()
        mock_store_inst.compute_topology_links = AsyncMock()
        mock_store_inst.close = AsyncMock()

        res = await verify_node(state_run)
        assert res["final_status"] == "FAILED"
        mock_rollback.assert_called_once_with("R1", state_run["executed_commands"]["R1"], {"R1": {}})
        assert any("Rollback R1: SUCCESS" in line for line in res["execution_log"])


@pytest.mark.asyncio
async def test_troubleshoot_node_exhaustion_rollback():
    from nodes.troubleshoot import troubleshoot_node

    # If LLM doesn't return fixes -> TROUBLESHOOT_EXHAUSTED and rollback runs
    state = {
        "failed_devices": ["R1"],
        "troubleshoot_attempt": 1,
        "plan": None,
        "execution_log": [],
        "intent": None,
        "executed_commands": {"R1": RouterCommands(pairs=[CommandPair(cmd="conf t", rollback="exit")])},
    }

    with patch("nodes.troubleshoot.load_inventory", return_value={"R1": {}}), \
         patch("nodes.troubleshoot.TroubleshootRepository.collect_transit_devices", new_callable=AsyncMock, return_value=["R1"]), \
         patch("nodes.troubleshoot.live_snapshot_for_diagnostics", new_callable=AsyncMock, return_value={"running_config": "", "interfaces": "", "operational_status": "", "error": ""}), \
         patch("nodes.troubleshoot._ask_llm_for_fix", new_callable=AsyncMock, return_value=None), \
         patch("nodes.verify._rollback_device", new_callable=AsyncMock, return_value=True) as mock_rollback, \
         patch("nodes.troubleshoot.AsyncNetworkGraphStore") as mock_store:

        mock_store_inst = mock_store.return_value
        mock_store_inst.close = AsyncMock()

        res = await troubleshoot_node(state)
        assert res["final_status"] == "TROUBLESHOOT_EXHAUSTED"
        mock_rollback.assert_called_once_with("R1", state["executed_commands"]["R1"], {"R1": {}})
        assert any("Rollback R1: SUCCESS" in line for line in res["execution_log"])


@pytest.mark.asyncio
async def test_route_to_relay_workers_structured_fields():
    # Setup intent schema with a device having structured DHCP relay config and no extra_params
    dev = DeviceIntent(
        name="R1",
        profile="cisco_ios",
        interfaces=[],
        dhcp_relay_server="R2",
        dhcp_relay_subnets=["192.168.10.0/24"]
    )
    plan = NetworkIntentSchema(devices=[dev], rollback_scope="all")
    
    state = {
        "plan": plan,
        "reachability": {"R1": "REACHABLE", "R2": "REACHABLE"},
    }

    res = _route_to_relay_workers(state)
    assert isinstance(res, list)
    assert len(res) == 1
    assert res[0].node == "GENERATE_RELAY"
    assert res[0].arg["router_name"] == "R1"


@pytest.mark.asyncio
async def test_execute_device_robust_rollback():
    from nodes.execute import _execute_device
    from core.state import RouterCommands, CommandPair

    # 1. Setup RouterCommands with 2 pairs (cmd, rollback)
    commands = RouterCommands(pairs=[
        CommandPair(
            cmd="interface Ethernet0/1\n ip address 10.0.0.1 255.255.255.0",
            rollback="interface Ethernet0/1\n no ip address"
        ),
        CommandPair(
            cmd="interface Ethernet0/2\n ip address 20.0.0.1 255.255.255.0",
            rollback="interface Ethernet0/2\n no ip address"
        ),
    ])

    inventory = {"R1": {"host": "127.0.0.1", "port": 5001, "vendor": "cisco_ios"}}
    reachability = {"R1": "REACHABLE"}
    semaphore = asyncio.Semaphore(1)

    # 2. Setup mock connection and command responses
    mock_conn = AsyncMock()
    
    # Command responses:
    # 1. First cmd: "OK"
    # 2. Second cmd: "OK"
    # 3. Third cmd: "OK"
    # 4. Fourth cmd: CLI error (fails)
    # 5. Prompt recovery (end): "OK"
    responses = [
        "OK",                                       # cmd[0] line 1
        "OK",                                       # cmd[0] line 2
        "OK",                                       # cmd[1] line 1
        "% Invalid input detected at '^' marker.",  # cmd[1] line 2 (fails)
        "OK",                                       # context recovery (end)
    ]
    mock_conn.send_command = AsyncMock(side_effect=responses)

    mock_get_conn = MagicMock()
    mock_get_conn.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_get_conn.__aexit__ = AsyncMock()

    with patch("nodes.execute.get_connection", return_value=mock_get_conn), \
         patch("nodes.execute.load_inventory", return_value=inventory):

        msg, ok = await _execute_device("R1", commands, reachability, inventory, semaphore)

        assert not ok
        assert "FAILED (CLI errors encountered)" in msg

        # Verify calls to send_command
        calls = [c[0][0] for c in mock_conn.send_command.call_args_list]
        
        # Expected sequence:
        # - Send cmd[0] lines
        # - Send cmd[1] lines (second fails)
        # - Recover context (end)
        assert calls == [
            "interface Ethernet0/1",
            "ip address 10.0.0.1 255.255.255.0",
            "interface Ethernet0/2",
            "ip address 20.0.0.1 255.255.255.0",
            "end"
        ]


@pytest.mark.asyncio
async def test_execute_bypass_with_test_troubleshoot_active():
    from nodes.execute import execute_node, execute_hosts_node
    from core.state import RouterCommands, CommandPair
    
    state = {
        "router_commands": {
            "R1": RouterCommands(pairs=[CommandPair(cmd="hostname R1", rollback="hostname router")]),
            "PC1": RouterCommands(pairs=[CommandPair(cmd="ip 10.0.0.2/24", rollback="")]),
        },
        "executed_commands": {},
        "test_troubleshoot_skip_execute": True,
        "final_status": "SUCCESS",
    }
    
    with patch("nodes.execute.load_inventory", return_value={"R1": {"vendor": "cisco_ios"}, "PC1": {"vendor": "vpcs"}}):
        # 1. Run execute_node (infra only, R1)
        res_infra = await execute_node(state)
        
        # Verify R1 commands were consumed and put in executed_commands, but no actual commands were run (since we skip execution)
        assert res_infra["final_status"] == "SUCCESS"
        assert res_infra["router_commands"]["R1"].pairs == []
        assert "SKIPPED" in res_infra["execution_log"][0]
        assert len(res_infra["executed_commands"]["R1"].pairs) == 1
        
        # Keep R1 consumed commands for the next stage
        state["router_commands"] = res_infra["router_commands"]
        state["executed_commands"] = res_infra["executed_commands"]
        state["final_status"] = res_infra["final_status"]
        
        # 2. Run execute_hosts_node (hosts only, PC1)
        res_hosts = await execute_hosts_node(state)
        
        assert res_hosts["final_status"] == "SUCCESS"
        assert res_hosts["router_commands"]["PC1"].pairs == []
        assert "SKIPPED" in res_hosts["execution_log"][0]
        assert len(res_hosts["executed_commands"]["PC1"].pairs) == 1
