import sys
import os
import yaml
import pytest
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from nodes.spec_reconcile import spec_reconcile_node

@pytest.mark.asyncio
async def test_spec_reconcile_operator_approval_yes(tmp_path):
    spec_file = tmp_path / "test_spec.yaml"
    initial_content = """
devices:
  - name: R1
    hostname: OldHost
"""
    spec_file.write_text(initial_content, encoding="utf-8")

    # Mock response from LLM
    mock_llm_response = """
<<<SPEC_START>>>
devices:
  - name: R1
    hostname: NewHost
<<<SPEC_END>>>
CHANGE: R1 | hostname | OldHost → NewHost | Modificato hostname
"""

    state = {
        "spec_path": str(spec_file),
        "troubleshoot_attempt": 1,
        "executed_commands": {},
        "execution_log": [],
        "final_status": "SUCCESS"
    }

    with patch("llm.async_client.llm_client.raw_completion", new_callable=AsyncMock) as mock_llm, \
         patch("prompt_toolkit.PromptSession") as mock_session_cls:
        
        mock_session = MagicMock()
        mock_session.prompt_async = AsyncMock(return_value="y")
        mock_session_cls.return_value = mock_session
        mock_llm.return_value = mock_llm_response
        
        result = await spec_reconcile_node(state)
        
        assert result["spec_reconcile_status"] == "SUCCESS"
        mock_session.prompt_async.assert_called_once()
        
        # Verify the file was updated
        updated_content = spec_file.read_text(encoding="utf-8")
        assert "NewHost" in updated_content
        assert "OldHost" not in updated_content

@pytest.mark.asyncio
async def test_spec_reconcile_operator_approval_no(tmp_path):
    spec_file = tmp_path / "test_spec.yaml"
    initial_content = """
devices:
  - name: R1
    hostname: OldHost
"""
    spec_file.write_text(initial_content, encoding="utf-8")

    mock_llm_response = """
<<<SPEC_START>>>
devices:
  - name: R1
    hostname: NewHost
<<<SPEC_END>>>
CHANGE: R1 | hostname | OldHost → NewHost | Modificato hostname
"""

    state = {
        "spec_path": str(spec_file),
        "troubleshoot_attempt": 1,
        "executed_commands": {},
        "execution_log": [],
        "final_status": "SUCCESS"
    }

    with patch("llm.async_client.llm_client.raw_completion", new_callable=AsyncMock) as mock_llm, \
         patch("prompt_toolkit.PromptSession") as mock_session_cls:
        
        mock_session = MagicMock()
        mock_session.prompt_async = AsyncMock(return_value="n")
        mock_session_cls.return_value = mock_session
        mock_llm.return_value = mock_llm_response
        
        result = await spec_reconcile_node(state)
        
        assert result["spec_reconcile_status"] == "SKIPPED"
        mock_session.prompt_async.assert_called_once()
        
        # Verify the file was NOT updated
        updated_content = spec_file.read_text(encoding="utf-8")
        assert "OldHost" in updated_content
        assert "NewHost" not in updated_content

@pytest.mark.asyncio
async def test_spec_reconcile_no_changes(tmp_path):
    spec_file = tmp_path / "test_spec.yaml"
    initial_content = """
devices:
  - name: R1
    hostname: OldHost
"""
    spec_file.write_text(initial_content, encoding="utf-8")

    # The LLM returns the exact same content
    mock_llm_response = """
<<<SPEC_START>>>
devices:
  - name: R1
    hostname: OldHost
<<<SPEC_END>>>
"""

    state = {
        "spec_path": str(spec_file),
        "troubleshoot_attempt": 1,
        "executed_commands": {},
        "execution_log": [],
        "final_status": "SUCCESS"
    }

    with patch("llm.async_client.llm_client.raw_completion", new_callable=AsyncMock) as mock_llm, \
         patch("prompt_toolkit.PromptSession") as mock_session_cls:
        
        mock_session = MagicMock()
        mock_session.prompt_async = AsyncMock()
        mock_session_cls.return_value = mock_session
        mock_llm.return_value = mock_llm_response
        
        result = await spec_reconcile_node(state)
        
        assert result["spec_reconcile_status"] == "SUCCESS"
        mock_session.prompt_async.assert_not_called()


def test_extract_patch_robustness():
    from nodes.spec_reconcile import _extract_patch

    # 1. Standard correct tags
    t1 = """
    <<<SPEC_START>>>
    devices:
      - name: R1
    <<<SPEC_END>>>
    """
    assert _extract_patch(t1) == "devices:\n      - name: R1"

    # 2. Case-insensitive tags
    t2 = """
    <<<spec_start>>>
    devices:
      - name: R2
    <<<spec_end>>>
    """
    assert _extract_patch(t2) == "devices:\n      - name: R2"

    # 3. Tags with markdown block inside
    t3 = """
    <<<SPEC_START>>>
    ```yaml
    devices:
      - name: R3
    ```
    <<<SPEC_END>>>
    """
    assert _extract_patch(t3) == "devices:\n      - name: R3"

    # 4. No tags but markdown block
    t4 = """
    Here is the YAML config:
    ```yaml
    devices:
      - name: R4
    ```
    Let me know if you need anything else.
    """
    assert _extract_patch(t4) == "devices:\n      - name: R4"

    # 5. Direct YAML response (no tags, no markdown blocks)
    t5 = """
    devices:
      - name: R5
    """
    assert _extract_patch(t5) == "devices:\n      - name: R5"

    # 6. Completely malformed (returns None)
    t6 = "This is not YAML at all"
    assert _extract_patch(t6) is None
