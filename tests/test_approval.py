# tests/test_approval.py
import sys
import os
import pytest
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.state import AgentState, RouterCommands, CommandPair
from nodes.approval import approval_node

@pytest.fixture
def base_state():
    router_cmds = {
        "R1": RouterCommands(pairs=[
            CommandPair(
                cmd="interface Ethernet0/0\n ip address 10.0.0.1 255.255.255.0",
                rollback="interface Ethernet0/0\n no ip address"
            )
        ])
    }
    return {
        "router_commands": router_cmds,
        "troubleshoot_attempt": 0,
        "execution_log": []
    }

@pytest.mark.asyncio
async def test_approval_automated_mode(base_state):
    # If DEPLOY_MODE is automated, it should return approval logs directly without asking stdin
    with patch.dict(os.environ, {"DEPLOY_MODE": "automated"}):
        res = await approval_node(base_state)
        assert "Approvazione automatica" in res["execution_log"][0]

@pytest.mark.asyncio
async def test_approval_no_commands_to_apply():
    # If no router_commands or empty, returns directly
    state = {
        "router_commands": {},
        "troubleshoot_attempt": 0,
        "execution_log": []
    }
    res = await approval_node(state)
    assert "Nessun comando da applicare" in res["execution_log"][0]

@pytest.mark.asyncio
async def test_approval_human_in_the_loop_accept(base_state):
    # Mock deploy mode, isatty, select, and readline for 'y'
    with patch.dict(os.environ, {"DEPLOY_MODE": "human-in-the-loop"}), \
         patch("sys.stdin.isatty", return_value=True), \
         patch("select.select", return_value=([sys.stdin], [], [])), \
         patch("sys.stdin.readline", return_value="y\n"):
        
        res = await approval_node(base_state)
        assert "Modifiche approvate dall'operatore" in res["execution_log"][0]

@pytest.mark.asyncio
async def test_approval_human_in_the_loop_reject(base_state):
    # Mock deploy mode, isatty, select, and readline for 'N'
    with patch.dict(os.environ, {"DEPLOY_MODE": "human-in-the-loop", "TEST_TROUBLESHOOT": "false"}), \
         patch("sys.stdin.isatty", return_value=True), \
         patch("select.select", return_value=([sys.stdin], [], [])), \
         patch("sys.stdin.readline", return_value="n\n"):
        
        with pytest.raises(RuntimeError) as exc_info:
            await approval_node(base_state)
        assert "Deployment aborted by operator decision" in str(exc_info.value)

@pytest.mark.asyncio
async def test_approval_human_in_the_loop_no_tty(base_state):
    # Mock deploy mode, and isatty = False
    with patch.dict(os.environ, {"DEPLOY_MODE": "human-in-the-loop"}), \
         patch("sys.stdin.isatty", return_value=False):
        
        with pytest.raises(RuntimeError) as exc_info:
            await approval_node(base_state)
        assert "CRITICAL CONFORMITY ERROR" in str(exc_info.value)

@pytest.mark.asyncio
async def test_approval_human_in_the_loop_timeout(base_state):
    # Mock deploy mode, isatty, and select = [] (timeout)
    with patch.dict(os.environ, {"DEPLOY_MODE": "human-in-the-loop"}), \
         patch("sys.stdin.isatty", return_value=True), \
         patch("select.select", return_value=([], [], [])):
        
        with pytest.raises(RuntimeError) as exc_info:
            await approval_node(base_state)
        assert "Timeout waiting for operator approval" in str(exc_info.value)

@pytest.mark.asyncio
async def test_approval_troubleshooting_header(base_state):
    # Set troubleshoot_attempt > 0 and verify it prints the troubleshooting header
    base_state["troubleshoot_attempt"] = 1
    
    with patch.dict(os.environ, {"DEPLOY_MODE": "automated"}), \
         patch("builtins.print") as mock_print:
        
        await approval_node(base_state)
        
        # Check that the troubleshooting specific text was printed
        printed_texts = [call[0][0] for call in mock_print.call_args_list if len(call[0]) > 0 and isinstance(call[0][0], str)]
        assert any("NETAGENT TROUBLESHOOT PLAN" in text for text in printed_texts)
        assert any("The following corrective changes will be applied" in text for text in printed_texts)
