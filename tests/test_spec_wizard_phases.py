import sys
import pytest
from unittest.mock import MagicMock, patch
from pathlib import Path
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm.spec_wizard import (
    get_system_prompt_for_phase,
    validate_spec_content,
    _run_wizard,
    SyncLLMClient
)

def test_get_system_prompt_for_phase():
    for p in range(1, 6):
        prompt = get_system_prompt_for_phase(p)
        assert f"FASE {p}" in prompt
        assert "PHASE_COMPLETE" in prompt
        assert "<<<SPEC_START>>>" in prompt

def test_validate_spec_content_static_host_gateway_mismatch():
    # Gateway outside subnet
    spec_mismatch = """
devices:
  - name: PC1
    profile: vpcs
    interfaces:
      - name: eth0
        ip: 192.168.10.10/24
    static_routes:
      - network: 0.0.0.0/0
        next_hop: 192.168.20.1
"""
    errors, warnings = validate_spec_content(spec_mismatch)
    warnings = errors + warnings
    assert any("non appartiene alla subnet" in w for w in warnings)

def test_validate_spec_content_static_host_gateway_not_on_router():
    # Gateway not matching any router IP
    spec_not_found = """
devices:
  - name: PC1
    profile: vpcs
    interfaces:
      - name: eth0
        ip: 192.168.10.10/24
    static_routes:
      - network: 0.0.0.0/0
        next_hop: 192.168.10.1
  - name: R1
    profile: cisco_ios
    interfaces:
      - name: Ethernet0/0
        ip: 192.168.10.2/24
"""
    errors, warnings = validate_spec_content(spec_not_found)
    warnings = errors + warnings
    assert any("non corrisponde a nessun indirizzo IP configurato sulle interfacce dei router" in w for w in warnings)

def test_validate_spec_content_static_host_missing_gateway():
    # Static host missing gateway
    spec_missing = """
devices:
  - name: PC1
    profile: vpcs
    interfaces:
      - name: eth0
        ip: 192.168.10.10/24
"""
    errors, warnings = validate_spec_content(spec_missing)
    warnings = errors + warnings
    assert any("non è configurato alcun default gateway" in w for w in warnings)

def test_validate_spec_content_static_host_valid():
    # Valid setup
    spec_valid = """
devices:
  - name: PC1
    profile: vpcs
    interfaces:
      - name: eth0
        ip: 192.168.10.10/24
    static_routes:
      - network: 0.0.0.0/0
        next_hop: 192.168.10.1
  - name: R1
    profile: cisco_ios
    interfaces:
      - name: Ethernet0/0
        ip: 192.168.10.1/24
"""
    errors, warnings = validate_spec_content(spec_valid)
    warnings = errors + warnings
    static_host_warnings = [w for w in warnings if "PC1" in w]
    assert not static_host_warnings

@patch("llm.spec_wizard.SyncLLMClient")
@patch("llm.spec_wizard.pt_prompt")
def test_run_wizard_phase_transitions_and_validation(mock_pt_prompt, mock_client_cls, tmp_path):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_client.provider = "github"
    mock_client.model_name = "gpt-4o-mini"
    
    yaml_phase1 = """
devices:
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/0
"""
    
    yaml_phase2_invalid = """
devices:
  - name: SW1
    profile: cisco_switch
    vlans:
      10: USERS_10
    interfaces:
      - name: Ethernet0/0
      - name: Vlan99
        ip: 192.168.999.10/24
"""

    yaml_phase2_valid = """
devices:
  - name: SW1
    profile: cisco_switch
    vlans:
      10: USERS_10
    interfaces:
      - name: Ethernet0/0
      - name: Vlan99
        ip: 192.168.99.10/24
"""


    responses = [
        # Call 1 (Init Phase 1)
        f"PHASE_COMPLETE\n<<<SPEC_START>>>\n{yaml_phase1}\n<<<SPEC_END>>>",
        # Call 2 (Init Phase 2)
        "Siamo in Fase 2. Che VLAN vuoi configurare?",
        # Call 3 (User message response)
        f"PHASE_COMPLETE\n<<<SPEC_START>>>\n{yaml_phase2_invalid}\n<<<SPEC_END>>>",
        # Call 4 (Auto feedback response)
        f"PHASE_COMPLETE\n<<<SPEC_START>>>\n{yaml_phase2_valid}\n<<<SPEC_END>>>",
        # Call 5 (Init Phase 3)
        "Siamo in Fase 3. Che IP vuoi per R1?"
    ]
    
    mock_client.chat.side_effect = responses
    
    inputs = [
        "Configura VLAN 10 e VLAN 20",
    ]
    
    def side_effect(*args, **kwargs):
        if not inputs:
            raise KeyboardInterrupt()
        return inputs.pop(0)
    
    mock_pt_prompt.side_effect = side_effect
    
    output_file = tmp_path / "test_out.yaml"
    
    _run_wizard(output_path=output_file)
    
    calls = mock_client.chat.call_args_list
    assert len(calls) == 5
    
    # Check Phase 1 init
    assert "FASE 1" in calls[0][0][2]
    # Check Phase 2 init
    assert "FASE 2" in calls[1][0][2]
    # Check Phase 2 user input
    assert "FASE 2" in calls[2][0][2]
    # Check Phase 2 auto validation feedback
    assert "FASE 2" in calls[3][0][2]
    # Check Phase 3 init
    assert "FASE 3" in calls[4][0][2]
    
    assert output_file.exists()
    content = output_file.read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)
    assert parsed["devices"][0]["interfaces"][1]["name"] == "Vlan99"

@patch("llm.spec_wizard.SyncLLMClient")
@patch("llm.spec_wizard.pt_prompt")
def test_run_wizard_image_bootstrap(mock_pt_prompt, mock_client_cls, tmp_path):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_client.provider = "github"
    mock_client.model_name = "gpt-4o-mini"
    
    yaml_bootstrapped = """
devices:
  - name: R1
    profile: cisco_ios
"""
    
    mock_client.bootstrap_from_image.return_value = f"<<<SPEC_START>>>\n{yaml_bootstrapped}\n<<<SPEC_END>>>"
    mock_client.chat.return_value = "Siamo in Fase 1. La topologia estratta è corretta?"
    mock_pt_prompt.side_effect = KeyboardInterrupt()
    
    image_file = tmp_path / "topo.png"
    image_file.write_bytes(b"dummy image data")
    
    output_file = tmp_path / "test_out.yaml"
    
    _run_wizard(output_path=output_file, image_path=image_file)
    
    mock_client.bootstrap_from_image.assert_called_once_with(image_file, "", "")
    
    calls = mock_client.chat.call_args_list
    assert len(calls) == 1
    assert "Siamo nella FASE 1" in calls[0][0][1]
    assert "R1" in calls[0][1].get("current_spec", "")


@patch("llm.spec_wizard.SyncLLMClient")
@patch("llm.spec_wizard.pt_prompt")
def test_run_wizard_image_bootstrap_with_reference_spec(mock_pt_prompt, mock_client_cls, tmp_path):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_client.provider = "github"
    mock_client.model_name = "gpt-4o-mini"
    
    yaml_bootstrapped = """
devices:
  - name: R1
    profile: cisco_ios
"""
    
    mock_client.bootstrap_from_image.return_value = f"<<<SPEC_START>>>\n{yaml_bootstrapped}\n<<<SPEC_END>>>"
    mock_client.chat.return_value = "Siamo in Fase 1. La topologia estratta è corretta?"
    mock_pt_prompt.side_effect = KeyboardInterrupt()
    
    image_file = tmp_path / "topo.png"
    image_file.write_bytes(b"dummy image data")
    
    resume_file = tmp_path / "resume.txt"
    resume_file.write_text("DEVICE: R1\nPROFILE: cisco_ios\n", encoding="utf-8")
    
    output_file = tmp_path / "test_out.yaml"
    
    _run_wizard(output_path=output_file, image_path=image_file, resume_path=resume_file)
    
    mock_client.bootstrap_from_image.assert_called_once_with(image_file, "DEVICE: R1\nPROFILE: cisco_ios\n", "")
    
    calls = mock_client.chat.call_args_list
    assert len(calls) == 1
    assert "Siamo nella FASE 1" in calls[0][0][1]
    assert "R1" in calls[0][1].get("current_spec", "")


@patch("llm.spec_wizard.SyncLLMClient")
@patch("llm.spec_wizard.pt_prompt")
def test_run_wizard_image_bootstrap_auto_correction_loop(mock_pt_prompt, mock_client_cls, tmp_path):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_client.provider = "github"
    mock_client.model_name = "gpt-4o-mini"
    
    yaml_invalid = """
devices:
  - name: PC1
    profile: vpcs
    interfaces:
      - name: eth0
  - name: PC2
    profile: vpcs
    interfaces:
      - name: eth0
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/1
links:
  - endpoints: ["PC1:eth0", "SW1:Ethernet0/1"]
  - endpoints: ["PC2:eth0", "SW1:Ethernet0/1"]
"""
    yaml_valid = """
devices:
  - name: PC1
    profile: vpcs
    interfaces:
      - name: eth0
  - name: PC2
    profile: vpcs
    interfaces:
      - name: eth0
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/1
      - name: Ethernet0/2
links:
  - endpoints: ["PC1:eth0", "SW1:Ethernet0/1"]
  - endpoints: ["PC2:eth0", "SW1:Ethernet0/2"]
"""
    
    # First call returns invalid, second call returns valid
    mock_client.bootstrap_from_image.side_effect = [
        f"<<<SPEC_START>>>\n{yaml_invalid}\n<<<SPEC_END>>>",
        f"<<<SPEC_START>>>\n{yaml_valid}\n<<<SPEC_END>>>",
    ]
    mock_client.chat.return_value = "Siamo in Fase 1. La topologia estratta è corretta?"
    mock_pt_prompt.side_effect = KeyboardInterrupt()
    
    image_file = tmp_path / "topo.png"
    image_file.write_bytes(b"dummy image data")
    
    output_file = tmp_path / "test_out.yaml"
    
    _run_wizard(output_path=output_file, image_path=image_file)
    
    # Assert bootstrap_from_image was called twice
    assert mock_client.bootstrap_from_image.call_count == 2
    calls = mock_client.bootstrap_from_image.call_args_list
    # First call had no feedback
    assert calls[0][0][2] == ""
    # Second call received the duplicate port feedback!
    feedback = calls[1][0][2]
    assert "utilizzata in 2 collegamenti diversi" in feedback
    assert "ma non ci sono altre interfacce dichiarate nella sua lista 'interfaces'" in feedback
    assert "Aggiungi una nuova interfaccia" in feedback


@patch("llm.spec_wizard.SyncLLMClient")
@patch("llm.spec_wizard.pt_prompt")
def test_run_wizard_image_bootstrap_auto_correction_loop_with_available_ports(mock_pt_prompt, mock_client_cls, tmp_path):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_client.provider = "github"
    mock_client.model_name = "gpt-4o-mini"
    
    yaml_invalid = """
devices:
  - name: PC1
    profile: vpcs
    interfaces:
      - name: eth0
  - name: PC2
    profile: vpcs
    interfaces:
      - name: eth0
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/1
      - name: Ethernet0/2
links:
  - endpoints: ["PC1:eth0", "SW1:Ethernet0/1"]
  - endpoints: ["PC2:eth0", "SW1:Ethernet0/1"]
"""
    yaml_valid = """
devices:
  - name: PC1
    profile: vpcs
    interfaces:
      - name: eth0
  - name: PC2
    profile: vpcs
    interfaces:
      - name: eth0
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/1
      - name: Ethernet0/2
links:
  - endpoints: ["PC1:eth0", "SW1:Ethernet0/1"]
  - endpoints: ["PC2:eth0", "SW1:Ethernet0/2"]
"""
    
    mock_client.bootstrap_from_image.side_effect = [
        f"<<<SPEC_START>>>\n{yaml_invalid}\n<<<SPEC_END>>>",
        f"<<<SPEC_START>>>\n{yaml_valid}\n<<<SPEC_END>>>",
    ]
    mock_client.chat.return_value = "Siamo in Fase 1. La topologia estratta è corretta?"
    mock_pt_prompt.side_effect = KeyboardInterrupt()
    
    image_file = tmp_path / "topo.png"
    image_file.write_bytes(b"dummy image data")
    
    output_file = tmp_path / "test_out2.yaml"
    
    _run_wizard(output_path=output_file, image_path=image_file)
    
    assert mock_client.bootstrap_from_image.call_count == 2
    calls = mock_client.bootstrap_from_image.call_args_list
    assert calls[0][0][2] == ""
    feedback = calls[1][0][2]
    assert "utilizzata in 2 collegamenti diversi" in feedback
    assert "Interfacce libere/disponibili su 'SW1': ['Ethernet0/2']" in feedback


@patch("llm.spec_wizard.SyncLLMClient")
@patch("llm.spec_wizard.pt_prompt")
def test_run_wizard_resume_text_spec_coercion(mock_pt_prompt, mock_client_cls, tmp_path):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_client.provider = "github"
    mock_client.model_name = "gpt-4o-mini"
    
    text_spec = "Questa è una specifica descrittiva di test con PC1 e R1."
    yaml_converted = """
devices:
  - name: PC1
    profile: vpcs
    interfaces:
      - name: eth0
        ip: 192.168.1.2/24
  - name: R1
    profile: cisco_ios
    interfaces:
      - name: Ethernet0/0
        ip: 192.168.1.1/24
"""
    
    mock_client.parse_text_spec_to_yaml.return_value = f"<<<SPEC_START>>>\n{yaml_converted}\n<<<SPEC_END>>>"
    mock_client.chat.return_value = "Siamo in Fase 1. La topologia convertita è corretta?"
    mock_pt_prompt.side_effect = KeyboardInterrupt()
    
    resume_file = tmp_path / "resume_text.txt"
    resume_file.write_text(text_spec, encoding="utf-8")
    
    output_file = tmp_path / "test_out_coerced.yaml"
    
    _run_wizard(output_path=output_file, resume_path=resume_file)
    
    mock_client.parse_text_spec_to_yaml.assert_called_once_with(text_spec)
    assert mock_client.chat.call_count == 1
    init_msg = mock_client.chat.call_args[0][1]
    current_spec = mock_client.chat.call_args[1].get("current_spec", "")
    assert "Siamo nella FASE 1" in init_msg
    assert "PC1" in current_spec
    assert "R1" in current_spec


@patch("llm.spec_wizard.SyncLLMClient")
@patch("llm.spec_wizard.pt_prompt")
def test_run_wizard_fast_mode(mock_pt_prompt, mock_client_cls, tmp_path):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_client.provider = "github"
    mock_client.model_name = "gpt-4o-mini"
    
    spec_old = """
devices:
  - name: PC1
    profile: vpcs
    interfaces:
      - name: eth0
        ip: 192.168.1.2/24
"""
    spec_new = """
devices:
  - name: PC1
    profile: vpcs
    interfaces:
      - name: eth0
        ip: 192.168.1.10/24
"""
    
    mock_client.chat.return_value = f"<<<SPEC_START>>>\n{spec_new}\n<<<SPEC_END>>>"
    
    # pt_prompt returns 'y' for confirmation
    mock_pt_prompt.return_value = "y"
    
    resume_file = tmp_path / "resume_fast.yaml"
    resume_file.write_text(spec_old, encoding="utf-8")
    
    output_file = tmp_path / "test_out_fast.yaml"
    
    _run_wizard(
        output_path=output_file,
        resume_path=resume_file,
        fast_instruction="Cambia l'IP di PC1 a 192.168.1.10/24"
    )
    
    assert mock_client.chat.call_count == 1
    # Check that output file was written with corrected new spec
    assert output_file.exists()
    content = output_file.read_text(encoding="utf-8")
    assert "192.168.1.10/24" in content
    
    # Verify pt_prompt called for confirmation
    mock_pt_prompt.assert_called_once()
    assert "Confermi l'applicazione" in mock_pt_prompt.call_args[0][0]


@patch("llm.spec_wizard.SyncLLMClient")
@patch("llm.spec_wizard.pt_prompt")
def test_run_wizard_warning_prompt_avanti(mock_pt_prompt, mock_client_cls, tmp_path):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_client.provider = "github"
    mock_client.model_name = "gpt-4o-mini"
    
    yaml_with_warning = """
devices:
  - name: SW1
    profile: cisco_switch
    vlans:
      10: DATA
    interfaces:
      - name: Vlan1
        ip: 192.168.1.10/24
"""
    
    responses = [
        # Call 1: Init Phase 2
        f"PHASE_COMPLETE\n<<<SPEC_START>>>\n{yaml_with_warning}\n<<<SPEC_END>>>",
        # Call 2: Init Phase 3
        "Siamo in Fase 3. Che IP vuoi?",
    ]
    mock_client.chat.side_effect = responses
    
    # First pt_prompt returns "avanti" to proceed with warnings
    # Second pt_prompt raises KeyboardInterrupt to exit loop
    mock_pt_prompt.side_effect = ["avanti", KeyboardInterrupt()]
    
    gns3_file = tmp_path / "project.gns3"
    gns3_file.write_text('{"topology": {"nodes": [], "links": []}}', encoding="utf-8")
    
    output_file = tmp_path / "test_out_warning.yaml"
    _run_wizard(output_path=output_file, gns3_path=gns3_file)
    
    assert mock_client.chat.call_count == 2
    assert "FASE 3" in mock_client.chat.call_args_list[1][0][2]


@patch("llm.spec_wizard.SyncLLMClient")
@patch("llm.spec_wizard.pt_prompt")
def test_run_wizard_warning_prompt_correggi(mock_pt_prompt, mock_client_cls, tmp_path):
    mock_client = MagicMock()
    mock_client_cls.return_value = mock_client
    mock_client.provider = "github"
    mock_client.model_name = "gpt-4o-mini"
    
    yaml_with_warning = """
devices:
  - name: SW1
    profile: cisco_switch
    vlans:
      10: DATA
    interfaces:
      - name: Vlan1
        ip: 192.168.1.10/24
"""
    yaml_fixed = """
devices:
  - name: SW1
    profile: cisco_switch
    vlans:
      10: DATA
    interfaces:
      - name: Vlan1
        delete: true
      - name: Vlan10
        ip: 192.168.10.10/24
"""
    
    responses = [
        # Call 1: Init Phase 2 -> returns PHASE_COMPLETE with warning
        f"PHASE_COMPLETE\n<<<SPEC_START>>>\n{yaml_with_warning}\n<<<SPEC_END>>>",
        # Call 2: Feedback for warnings (correggi) -> LLM corrects it
        f"PHASE_COMPLETE\n<<<SPEC_START>>>\n{yaml_fixed}\n<<<SPEC_END>>>",
        # Call 3: Init Phase 3 (advanced after warnings fixed)
        "Siamo in Fase 3. Che IP vuoi?",
    ]
    mock_client.chat.side_effect = responses
    
    mock_pt_prompt.side_effect = ["correggi", KeyboardInterrupt()]
    
    gns3_file = tmp_path / "project.gns3"
    gns3_file.write_text('{"topology": {"nodes": [], "links": []}}', encoding="utf-8")
    
    output_file = tmp_path / "test_out_warning_fixed.yaml"
    _run_wizard(output_path=output_file, gns3_path=gns3_file)
    
    assert mock_client.chat.call_count == 3
    feedback_call = mock_client.chat.call_args_list[1][0][1]
    assert "sospeso per correggere i seguenti avvisi" in feedback_call
    assert "Vlan1" in feedback_call


def test_get_system_prompt_flat_network_detection():
    # 1. Flat network (no VLANs defined, no trunks, no access vlan != 1)
    flat_spec = """
devices:
  - name: R1
    profile: cisco_ios
    interfaces:
      - name: Ethernet0/0
        ip: 192.168.1.1/24
  - name: PC1
    profile: vpcs
    interfaces:
      - name: eth0
        ip: 192.168.1.2/24
"""
    prompt_flat = get_system_prompt_for_phase(3, current_spec=flat_spec)
    assert "RETE PIATTA RILEVATA" in prompt_flat
    assert "NON proporre, consigliare o inserire configurazioni o attributi relativi a VLAN" in prompt_flat

    # 2. Non-flat: Custom VLAN database declared
    spec_with_vlans = """
devices:
  - name: SW1
    profile: cisco_switch
    vlans:
      10: CLIENTS
"""
    prompt_vlans = get_system_prompt_for_phase(3, current_spec=spec_with_vlans)
    assert "RETE PIATTA RILEVATA" not in prompt_vlans

    # 3. Non-flat: Trunk mode configured
    spec_with_trunk = """
devices:
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/0
        mode: trunk
"""
    prompt_trunk = get_system_prompt_for_phase(3, current_spec=spec_with_trunk)
    assert "RETE PIATTA RILEVATA" not in prompt_trunk

    # 4. Non-flat: Access VLAN custom configured (not 1)
    spec_with_access_vlan = """
devices:
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/0
        mode: access
        access_vlan: 10
"""
    prompt_access_vlan = get_system_prompt_for_phase(3, current_spec=spec_with_access_vlan)
    assert "RETE PIATTA RILEVATA" not in prompt_access_vlan

    # 5. Non-flat: Subinterface with vlan_id configured
    spec_with_vlan_id = """
devices:
  - name: R1
    profile: cisco_ios
    interfaces:
      - name: Ethernet0/0.10
        vlan_id: 10
"""
    prompt_vlan_id = get_system_prompt_for_phase(3, current_spec=spec_with_vlan_id)
    assert "RETE PIATTA RILEVATA" not in prompt_vlan_id






