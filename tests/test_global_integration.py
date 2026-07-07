import sys
import os
import pytest
import asyncio
import uuid
import aiosqlite
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import yaml
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from core.graph import build_graph
from core.state import RouterCommands, CommandPair, NetworkIntentSchema, DeviceIntent
from generate.models.deltas import DeviceDelta


# Mocks for external dependencies
class MockGraphStore:
    def __init__(self, *args, **kwargs):
        self._driver = MagicMock()
        self._inventory = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def get_topology_summary(self):
        return "mock topology summary"

    async def upsert_l2_link(self, *args, **kwargs):
        pass

    async def compute_topology_links(self):
        pass

    async def compute_l2_topology(self):
        pass

    async def clear_inactive_interfaces(self, *args):
        pass

    async def close(self):
        pass


class MockGenerateRepository:
    def __init__(self, driver):
        pass

    async def get_device_state(self, router_name):
        if router_name == "R1":
            return "cisco_ios", {}, {}, "!"
        elif router_name == "SW1":
            return "cisco_switch", {}, {}, "!"
        elif router_name == "PC1":
            return "vpcs", {}, {}, ""
        return "cisco_ios", {}, {}, "!"


class MockConnection:
    def __init__(self, *args, **kwargs):
        self.current_prompt = "R1#"

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def send_command(self, cmd, *args, **kwargs):
        if "show spanning-tree" in cmd:
            return "Vlan VLAN0010 Desg FWD 100 128.1 Shr Edge"
        return "R1(config)#"

    async def save_config(self):
        return True


async def mock_snapshot_device(router_name, cfg, store):
    snapshot = MagicMock()
    snapshot.static_routes = {}
    snapshot.vlans = {}
    snapshot.l2_neighbors = []
    snapshot.running_config = "!"
    return snapshot


def mock_load_inventory():
    return {
        "R1": {"host": "127.0.0.1", "port": 5011, "vendor": "cisco_ios"},
        "SW1": {"host": "127.0.0.1", "port": 5012, "vendor": "cisco_switch"},
        "PC1": {"host": "127.0.0.1", "port": 5013, "vendor": "vpcs"},
    }


@pytest.mark.asyncio
async def test_global_workflow_success_path(tmp_path):
    """
    Test di integrazione globale: compila ed esegue il grafo LangGraph completo.
    In questo scenario il deployment e la verifica (pings) hanno successo.
    """
    spec_file = tmp_path / "test_spec.yaml"
    spec_content = """
devices:
  - name: R1
    profile: cisco_ios
    interfaces:
      - name: Ethernet0/0
        ip: 192.168.1.1/24
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/1
        mode: access
        access_vlan: 10
  - name: PC1
    profile: vpcs
    interfaces:
      - name: eth0
        ip: 192.168.1.10/24
"""
    spec_file.write_text(spec_content, encoding="utf-8")

    # Mocks patches
    patches = [
        patch("nodes.observe.snapshot_device", new=mock_snapshot_device),
        patch("nodes.observe.run_l2_discovery", new=AsyncMock()),
        # Local graph stores
        patch("nodes.observe.AsyncNetworkGraphStore", new=MockGraphStore),
        patch("nodes.plan.AsyncNetworkGraphStore", new=MockGraphStore),
        patch("nodes.generate.AsyncNetworkGraphStore", new=MockGraphStore),
        patch("nodes.verify.AsyncNetworkGraphStore", new=MockGraphStore),
        # Local load_inventory
        patch("nodes.observe.load_inventory", new=mock_load_inventory),
        patch("nodes.execute.load_inventory", new=mock_load_inventory),
        patch("nodes.verify.load_inventory", new=mock_load_inventory),
        patch("tools.parser.load_inventory", new=mock_load_inventory),
        # Local get_connection
        patch("nodes.execute.get_connection", return_value=MockConnection()),
        patch("nodes.verify.get_connection", return_value=MockConnection()),
        patch("tools.connection.get_connection", return_value=MockConnection()),
        patch("nodes.generate.GenerateRepository", new=MockGenerateRepository),
        # Verification helpers
        patch("nodes.verify._post_execute_graph_sync", new=AsyncMock()),
        patch("nodes.verify._fetch_live_ips_from_graph", new_callable=AsyncMock, return_value={"PC1": "192.168.1.10"}),
        patch("nodes.verify._fetch_network_device_ips", new_callable=AsyncMock, return_value={"R1": ["192.168.2.1"]}),
        patch("nodes.verify._verify_control_plane", new_callable=AsyncMock, return_value=[]),
        patch("nodes.verify._heal_static_vpcs_if_needed", new_callable=AsyncMock, return_value=False),
        # Mock _run_ping to return success
        patch("nodes.verify._run_ping", new_callable=AsyncMock, return_value=("PC1", "192.168.2.1", True)),
        patch.dict("os.environ", {"DEPLOY_MODE": "automated"}),
        patch("prompt_toolkit.PromptSession")
    ]

    # Apply patches
    for p in patches:
        p.start()

    # Configure PromptSession Mock to return Yes for operator approval
    mock_session = MagicMock()
    mock_session.prompt_async = AsyncMock(return_value="y")
    import prompt_toolkit
    prompt_toolkit.PromptSession.return_value = mock_session

    try:
        # State database initialization
        db_path = tmp_path / "test_netagent_state.db"
        async with aiosqlite.connect(str(db_path)) as conn:
            checkpointer = AsyncSqliteSaver(conn)
            # Compile graph
            graph = build_graph().compile(checkpointer=checkpointer)

            # Initial input state
            thread_id = f"run-{uuid.uuid4().hex[:8]}"
            config = {"configurable": {"thread_id": thread_id}}
            input_state = {
                "user_task": "Configure lab",
                "image_path": None,
                "spec_path": str(spec_file),
                "specification_raw": "",
                "raw_input": "",
                "reachability": {},
                "router_commands": {},
                "execution_log": [],
                "final_status": "UNKNOWN",
            }

            # Run workflow
            final_state = await graph.ainvoke(input_state, config)

            # Verification assertions
            assert final_state["final_status"] == "SUCCESS"
            assert "PARSE_INPUT: Specifica YAML validata con successo" in final_state["execution_log"][0]
            assert any("OBSERVE: Scansione conclusa" in line for line in final_state["execution_log"])
            assert any("PLAN: Specifica strutturata già presente" in line for line in final_state["execution_log"])
            assert any("Cross-Validation PASSED" in line for line in final_state["execution_log"])

    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_global_workflow_troubleshoot_to_rollback_path(tmp_path):
    """
    Test di integrazione globale: simula un guasto in fase di VERIFY.
    Il troubleshooter viene innescato ma fallisce la riparazione per più tentativi,
    portando al ripristino sicuro (rollback) di tutti i comandi deployati.
    """
    spec_file = tmp_path / "test_spec.yaml"
    spec_content = """
devices:
  - name: R1
    profile: cisco_ios
    interfaces:
      - name: Ethernet0/0
        ip: 192.168.1.1/24
"""
    spec_file.write_text(spec_content, encoding="utf-8")

    # Mock response from LLM for troubleshoot fixes
    mock_llm_fix = """
{
  "analysis": "R1 interface remains down",
  "fixes": [
    {
      "device": "R1",
      "vendor": "cisco_ios",
      "cmd": "interface Ethernet0/0\\nno shutdown",
      "rollback": "interface Ethernet0/0\\nshutdown"
    }
  ]
}
"""

    # Mocks patches
    patches = [
        patch("nodes.observe.snapshot_device", new=mock_snapshot_device),
        patch("nodes.observe.run_l2_discovery", new=AsyncMock()),
        # Local graph stores
        patch("nodes.observe.AsyncNetworkGraphStore", new=MockGraphStore),
        patch("nodes.plan.AsyncNetworkGraphStore", new=MockGraphStore),
        patch("nodes.generate.AsyncNetworkGraphStore", new=MockGraphStore),
        patch("nodes.verify.AsyncNetworkGraphStore", new=MockGraphStore),
        patch("nodes.troubleshoot.AsyncNetworkGraphStore", new=MockGraphStore),
        # Local load_inventory
        patch("nodes.observe.load_inventory", new=mock_load_inventory),
        patch("nodes.execute.load_inventory", new=mock_load_inventory),
        patch("nodes.verify.load_inventory", new=mock_load_inventory),
        patch("nodes.troubleshoot.load_inventory", new=mock_load_inventory),
        patch("tools.parser.load_inventory", new=mock_load_inventory),
        # Local get_connection / diagnostics
        patch("nodes.execute.get_connection", return_value=MockConnection()),
        patch("nodes.verify.get_connection", return_value=MockConnection()),
        patch("tools.connection.get_connection", return_value=MockConnection()),
        patch("nodes.troubleshoot.live_snapshot_for_diagnostics", new_callable=AsyncMock, return_value={"running_config": "!", "interfaces": "", "operational_status": "", "error": ""}),
        patch("nodes.generate.GenerateRepository", new=MockGenerateRepository),
        # Verification helpers
        patch("nodes.verify._post_execute_graph_sync", new=AsyncMock()),
        patch("nodes.verify._fetch_live_ips_from_graph", new_callable=AsyncMock, return_value={"PC1": "192.168.1.10"}),
        patch("nodes.verify._fetch_network_device_ips", new_callable=AsyncMock, return_value={"R1": ["192.168.2.1"]}),
        patch("nodes.verify._verify_control_plane", new_callable=AsyncMock, return_value=[]),
        patch("nodes.verify._heal_static_vpcs_if_needed", new_callable=AsyncMock, return_value=False),
        # Mock _run_ping to fail
        patch("nodes.verify._run_ping", new_callable=AsyncMock, return_value=("PC1", "192.168.2.1", False)),
        # Mock verify rollback executor
        patch("nodes.verify._rollback_device", new_callable=AsyncMock, return_value=True),
        # Mock LLM Troubleshooter completion call
        patch("llm.async_client.llm_client.raw_completion", new_callable=AsyncMock, return_value=mock_llm_fix),
        patch.dict("os.environ", {"DEPLOY_MODE": "automated"}),
        patch("prompt_toolkit.PromptSession")
    ]

    # Apply patches
    for p in patches:
        p.start()

    # Configure PromptSession Mock to return Yes for operator approval
    mock_session = MagicMock()
    mock_session.prompt_async = AsyncMock(return_value="y")
    import prompt_toolkit
    prompt_toolkit.PromptSession.return_value = mock_session

    try:
        db_path = tmp_path / "test_netagent_state.db"
        async with aiosqlite.connect(str(db_path)) as conn:
            checkpointer = AsyncSqliteSaver(conn)
            graph = build_graph().compile(checkpointer=checkpointer)

            thread_id = f"run-{uuid.uuid4().hex[:8]}"
            config = {"configurable": {"thread_id": thread_id}}
            input_state = {
                "user_task": "Configure lab",
                "image_path": None,
                "spec_path": str(spec_file),
                "specification_raw": "",
                "raw_input": "",
                "reachability": {},
                "router_commands": {},
                "execution_log": [],
                "final_status": "UNKNOWN",
            }

            final_state = await graph.ainvoke(input_state, config)

            # Verification assertions for failed/rollback path
            assert final_state["final_status"] == "FAILED"
            assert final_state["troubleshoot_attempt"] >= 3
            assert any("Cross-Validation DEGRADED" in line for line in final_state["execution_log"])
            assert any("TROUBLESHOOT attempt" in line for line in final_state["execution_log"])
            assert any("Rollback R1: SUCCESS" in line for line in final_state["execution_log"])

    finally:
        for p in patches:
            p.stop()


@pytest.mark.asyncio
async def test_global_workflow_sweep_reconciliation(tmp_path):
    """
    Test di integrazione globale per Riconciliazione Sweep (Actual minus Desired):
    Verifica che le VLAN extra e le rotte statiche extra vengano rilevate dallo
    stato corrente e rimosse compilando ed eseguendo i comandi di 'no ip route' e 'no vlan'.
    """
    spec_file = tmp_path / "test_spec.yaml"
    spec_content = """
devices:
  - name: R1
    profile: cisco_ios
    interfaces:
      - name: Ethernet0/0
        ip: 192.168.1.1/24
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/1
        mode: access
        access_vlan: 10
  - name: PC1
    profile: vpcs
    interfaces:
      - name: eth0
        ip: 192.168.1.10/24
"""
    spec_file.write_text(spec_content, encoding="utf-8")

    # Custom repository mock to return extra VLANs and extra routes
    class MockSweepGenerateRepository:
        def __init__(self, driver):
            pass

        async def get_device_state(self, router_name):
            if router_name == "R1":
                # Returns extra route: 10.99.99.0/24 via 192.168.1.99
                return (
                    "cisco_ios",
                    {"Ethernet0/0": "192.168.1.1/24"},
                    {"10.99.99.0/24": "192.168.1.99"},
                    "!"
                )
            elif router_name == "SW1":
                # Returns extra VLAN 99 in running_config
                running_config = "!\nvlan 99\n name extra_vlan\n!\ninterface Ethernet0/1\n switchport mode access\n switchport access vlan 10\n!"
                return (
                    "cisco_switch",
                    {"Ethernet0/1": "unassigned"},
                    {},
                    running_config
                )
            elif router_name == "PC1":
                return "vpcs", {}, {}, ""
            return "cisco_ios", {}, {}, "!"

    # Mocks patches
    patches = [
        patch("nodes.observe.snapshot_device", new=mock_snapshot_device),
        patch("nodes.observe.run_l2_discovery", new=AsyncMock()),
        # Local graph stores
        patch("nodes.observe.AsyncNetworkGraphStore", new=MockGraphStore),
        patch("nodes.plan.AsyncNetworkGraphStore", new=MockGraphStore),
        patch("nodes.generate.AsyncNetworkGraphStore", new=MockGraphStore),
        patch("nodes.verify.AsyncNetworkGraphStore", new=MockGraphStore),
        # Local load_inventory
        patch("nodes.observe.load_inventory", new=mock_load_inventory),
        patch("nodes.execute.load_inventory", new=mock_load_inventory),
        patch("nodes.verify.load_inventory", new=mock_load_inventory),
        patch("tools.parser.load_inventory", new=mock_load_inventory),
        # Local get_connection
        patch("nodes.execute.get_connection", return_value=MockConnection()),
        patch("nodes.verify.get_connection", return_value=MockConnection()),
        patch("tools.connection.get_connection", return_value=MockConnection()),
        # Use our custom repo mock for sweep
        patch("nodes.generate.GenerateRepository", new=MockSweepGenerateRepository),
        # Verification helpers
        patch("nodes.verify._post_execute_graph_sync", new=AsyncMock()),
        patch("nodes.verify._fetch_live_ips_from_graph", new_callable=AsyncMock, return_value={"PC1": "192.168.1.10"}),
        patch("nodes.verify._fetch_network_device_ips", new_callable=AsyncMock, return_value={"R1": ["192.168.2.1"]}),
        patch("nodes.verify._verify_control_plane", new_callable=AsyncMock, return_value=[]),
        patch("nodes.verify._heal_static_vpcs_if_needed", new_callable=AsyncMock, return_value=False),
        # Mock _run_ping to return success
        patch("nodes.verify._run_ping", new_callable=AsyncMock, return_value=("PC1", "192.168.2.1", True)),
        patch.dict("os.environ", {"DEPLOY_MODE": "automated"}),
        patch("prompt_toolkit.PromptSession")
    ]

    for p in patches:
        p.start()

    mock_session = MagicMock()
    mock_session.prompt_async = AsyncMock(return_value="y")
    import prompt_toolkit
    prompt_toolkit.PromptSession.return_value = mock_session

    try:
        db_path = tmp_path / "test_netagent_state.db"
        async with aiosqlite.connect(str(db_path)) as conn:
            checkpointer = AsyncSqliteSaver(conn)
            graph = build_graph().compile(checkpointer=checkpointer)

            thread_id = f"run-{uuid.uuid4().hex[:8]}"
            config = {"configurable": {"thread_id": thread_id}}
            input_state = {
                "user_task": "Configure lab",
                "image_path": None,
                "spec_path": str(spec_file),
                "specification_raw": "",
                "raw_input": "",
                "reachability": {},
                "router_commands": {},
                "execution_log": [],
                "final_status": "UNKNOWN",
            }

            final_state = await graph.ainvoke(input_state, config)

            # Verification assertions
            assert final_state["final_status"] == "SUCCESS"
            
            # Verify that deletion commands were indeed generated and executed
            r1_cmds = final_state["executed_commands"]["R1"]
            r1_flat_cmd = "\n".join(p.cmd for p in r1_cmds.pairs)
            # Should contain the command to remove the extra route
            assert "no ip route 10.99.99.0 255.255.255.0 192.168.1.99" in r1_flat_cmd

            sw1_cmds = final_state["executed_commands"]["SW1"]
            sw1_flat_cmd = "\n".join(p.cmd for p in sw1_cmds.pairs)
            # Should contain the command to remove the extra VLAN
            assert "no vlan 99" in sw1_flat_cmd

    finally:
        for p in patches:
            p.stop()
