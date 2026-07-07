import sys
import pytest
import yaml
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm.spec_wizard import merge_specifications, validate_spec_content

def test_merge_specifications_keeps_unmodified_blocks():
    old_spec = """
devices:
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/1
        mode: access
        access_vlan: 10
rollback_scope: all
"""
    new_spec = """
devices:
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/1
        mode: access
        access_vlan: 10
      - name: Ethernet0/2
        mode: trunk
        trunk_vlans:
          - 10
          - 20
"""
    merged = merge_specifications(old_spec, new_spec)
    parsed = yaml.safe_load(merged)
    
    assert parsed is not None
    assert "devices" in parsed
    assert len(parsed["devices"]) == 1
    device = parsed["devices"][0]
    assert device["name"] == "SW1"
    assert len(device["interfaces"]) == 2
    assert parsed["rollback_scope"] == "all"


def test_validate_spec_content_flags_vlan1_management():
    # If custom VLANs exist, Vlan1 SVI with an IP triggers a warning
    spec_with_vlans = """
devices:
  - name: SW1
    profile: cisco_switch
    vlans:
      10: USERS_10
    interfaces:
      - name: Vlan1
        ip: 192.168.1.10/24
"""
    errors, warnings = validate_spec_content(spec_with_vlans)
    warnings = errors + warnings
    assert any("Vlan1" in w for w in warnings)

    # If NO custom VLANs exist (flat network), Vlan1 SVI with an IP is allowed
    spec_flat = """
devices:
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Vlan1
        ip: 192.168.1.10/24
"""
    errors, warnings_flat = validate_spec_content(spec_flat)
    warnings_flat = errors + warnings_flat
    assert not any("Vlan1" in w for w in warnings_flat)



def test_validate_spec_content_flags_etherchannel_physical_port_mismatch():
    spec = """
devices:
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Port-channel1
        mode: trunk
        trunk_vlans:
          - 10
          - 20
      - name: Ethernet2/0
        channel_group: 1
        channel_mode: active
        mode: trunk
        trunk_vlans:
          - 10
"""
    errors, warnings = validate_spec_content(spec)
    warnings = errors + warnings
    assert any("Port-channel1" in w and "direttive di trunk/access" in w for w in warnings)


def test_validate_spec_content_flags_invalid_ip_octet():
    spec = """
devices:
  - name: R1
    profile: cisco_ios
    interfaces:
      - name: Ethernet0/1
        ip: 192.168.999.1/24
"""
    errors, warnings = validate_spec_content(spec)
    warnings = errors + warnings
    assert any("Validazione fallita" in w and "ip" in w for w in warnings)


def test_save_spec_enforces_yaml_and_validates_pydantic(tmp_path):
    from llm.spec_wizard import _save_spec
    
    spec = """
devices:
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/1
        mode: access
        access_vlan: 10
"""
    txt_path = tmp_path / "test_spec.txt"
    _save_spec(spec, txt_path)
    
    yaml_path = tmp_path / "test_spec.yaml"
    assert yaml_path.exists()
    assert not txt_path.exists()
    
    content = yaml_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)
    assert parsed["devices"][0]["name"] == "SW1"


def test_validate_spec_content_coerces_extra_params_dict():
    spec = """
devices:
  - name: R1
    profile: cisco_ios
    extra_params:
      DHCP_RELAY: "192.168.10.0/24,192.168.20.0/24"
      DHCP_SERVER: "R2"
    interfaces:
      - name: Ethernet0/0
        ip: 10.0.0.1/30
"""
    errors, warnings = validate_spec_content(spec)
    warnings = errors + warnings
    assert not any("extra_params" in w and "Validazione fallita" in w for w in warnings)


def test_validate_spec_content_coerces_extra_params_dict_with_list():
    spec = """
devices:
  - name: R1
    profile: cisco_ios
    extra_params:
      DHCP_RELAY: [192.168.10.0/24, 192.168.20.0/24]
      DHCP_SERVER: R2
    interfaces:
      - name: Ethernet0/0
        ip: 10.0.0.1/30
"""
    errors, warnings = validate_spec_content(spec)
    warnings = errors + warnings
    assert not any("extra_params" in w and "Validazione fallita" in w for w in warnings)

    # Let's verify the coerced value is correct
    import yaml
    from core.state import NetworkIntentSchema
    data = yaml.safe_load(spec)
    intent = NetworkIntentSchema.model_validate(data)
    extra = intent.devices[0].extra_params
    assert "DHCP_RELAY: 192.168.10.0/24,192.168.20.0/24" in extra
    assert "DHCP_SERVER: R2" in extra


def test_validate_spec_content_coerces_extra_params_list_of_dicts():
    spec = """
devices:
  - name: R1
    profile: cisco_ios
    extra_params:
      - DHCP_RELAY: [192.168.10.0/24, 192.168.20.0/24]
      - DHCP_SERVER: R2
    interfaces:
      - name: Ethernet0/0
        ip: 10.0.0.1/30
"""
    errors, warnings = validate_spec_content(spec)
    warnings = errors + warnings
    assert not any("extra_params" in w and "Validazione fallita" in w for w in warnings)

    import yaml
    from core.state import NetworkIntentSchema
    data = yaml.safe_load(spec)
    intent = NetworkIntentSchema.model_validate(data)
    extra = intent.devices[0].extra_params
    assert "DHCP_RELAY: 192.168.10.0/24,192.168.20.0/24" in extra
    assert "DHCP_SERVER: R2" in extra


def test_save_spec_coerces_and_saves_extra_params_as_string(tmp_path):
    from llm.spec_wizard import _save_spec
    spec = """
devices:
  - name: R1
    profile: cisco_ios
    extra_params:
      DHCP_RELAY: [192.168.10.0/24, 192.168.20.0/24]
      DHCP_SERVER: R2
    interfaces:
      - name: Ethernet0/0
        ip: 10.0.0.1/30
"""
    yaml_path = tmp_path / "test_spec.yaml"
    _save_spec(spec, yaml_path)
    
    content = yaml_path.read_text(encoding="utf-8")
    parsed = yaml.safe_load(content)
    
    extra_params_saved = parsed["devices"][0]["extra_params"]
    assert isinstance(extra_params_saved, str)
    assert "DHCP_RELAY: 192.168.10.0/24,192.168.20.0/24" in extra_params_saved
    assert "DHCP_SERVER: R2" in extra_params_saved


def test_validate_spec_content_flags_native_vlan_terminated_on_router():
    spec = """
devices:
  - name: R1
    profile: cisco_ios
    interfaces:
      - name: Ethernet0/1.999
        ip: 192.168.254.1/24
        vlan_id: 999
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/0
        mode: trunk
        native_vlan: 999
"""
    errors, warnings = validate_spec_content(spec)
    warnings = errors + warnings
    assert any("Native VLAN 999" in w and "indirizzo IP configurato sul router" in w for w in warnings)


def test_validate_spec_content_allows_management_vlan_on_router():
    spec = """
devices:
  - name: R1
    profile: cisco_ios
    interfaces:
      - name: Ethernet0/1.99
        ip: 192.168.99.1/24
        vlan_id: 99
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/0
        mode: trunk
        native_vlan: 999
        trunk_vlans: [10, 20, 99]
"""
    errors, warnings = validate_spec_content(spec)
    warnings = errors + warnings
    assert not any("Native VLAN 99" in w or "Native VLAN 999" in w for w in warnings)


def test_validate_spec_content_respects_env_mgmt_vlan(monkeypatch):
    monkeypatch.setenv("NETAGENT_MGMT_VLAN", "123")
    spec = """
devices:
  - name: SW1
    profile: cisco_switch
    vlans:
      123: MGMT
    interfaces:
      - name: Vlan1
        ip: 192.168.1.10/24
"""
    errors, warnings = validate_spec_content(spec)
    warnings = errors + warnings
    assert any("Vlan1" in w and "VLAN 123" in w for w in warnings)



def test_system_prompt_respects_env_vlans(monkeypatch):
    monkeypatch.setenv("NETAGENT_MGMT_VLAN", "123")
    monkeypatch.setenv("NETAGENT_NATIVE_VLAN", "456")
    from llm.spec_wizard import get_system_prompt
    prompt = get_system_prompt()
    assert "123" in prompt
    assert "456" in prompt


def test_validate_spec_content_flags_duplicate_links():
    spec = """
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
    errors, warnings = validate_spec_content(spec)
    warnings = errors + warnings
    assert any("utilizzata in 2 collegamenti diversi" in w and "SW1" in w and "Ethernet0/1" in w for w in warnings)


def test_validate_spec_content_flags_link_non_existent_device():
    spec = """
devices:
  - name: PC1
    profile: vpcs
    interfaces:
      - name: eth0
links:
  - endpoints: ["PC1:eth0", "SW1:Ethernet0/1"]
"""
    errors, warnings = validate_spec_content(spec)
    warnings = errors + warnings
    assert any("non esiste nella sezione 'devices'" in w and "SW1" in w for w in warnings)


def test_validate_spec_content_flags_link_non_existent_interface():
    spec = """
devices:
  - name: PC1
    profile: vpcs
    interfaces:
      - name: eth0
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/0
links:
  - endpoints: ["PC1:eth0", "SW1:Ethernet0/1"]
"""
    errors, warnings = validate_spec_content(spec)
    warnings = errors + warnings
    assert any("non è dichiarata nella sua lista 'interfaces'" in w and "SW1" in w and "Ethernet0/1" in w for w in warnings)


def test_devices_dict_coercion():
    from core.state import NetworkIntentSchema
    import yaml
    
    spec_yaml = """
devices:
  R1:
    profile: frrouting
    interfaces:
      - name: eth0
        ip: 1.10.0.1/24
  Switch1:
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/0
  PC1: vpcs
links:
  - endpoints: ["R1:eth0", "Switch1:Ethernet0/0"]
"""
    data = yaml.safe_load(spec_yaml)
    intent = NetworkIntentSchema.model_validate(data)
    
    assert isinstance(intent.devices, list)
    assert len(intent.devices) == 3
    
    r1 = next(d for d in intent.devices if d.name == "R1")
    assert r1.profile == "frrouting"
    assert r1.interfaces[0].name == "eth0"
    assert r1.interfaces[0].ip == "1.10.0.1/24"
    
    sw = next(d for d in intent.devices if d.name == "Switch1")
    assert sw.profile == "cisco_switch"
    assert sw.interfaces[0].name == "Ethernet0/0"
    
    pc = next(d for d in intent.devices if d.name == "PC1")
    assert pc.profile == "vpcs"
    
    errors, warnings = validate_spec_content(spec_yaml)
    warnings = errors + warnings
    # Check that links don't raise non-existent device warnings
    assert not any("non esiste nella sezione 'devices'" in w for w in warnings)


def test_validate_spec_content_flat_network_vlan1_management():
    # No custom VLANs declared on SW1. Vlan1 management IP should NOT trigger security warning.
    spec = """
devices:
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Vlan1
        ip: 195.100.50.3/24
"""
    errors, warnings = validate_spec_content(spec)
    warnings = errors + warnings
    assert not any("Vlan1" in w and "vulnerabilità di sicurezza" in w for w in warnings)


def test_validate_spec_content_vlan1_management_warning_when_custom_vlans():
    # Custom VLANs declared. Vlan1 management IP SHOULD trigger security warning.
    spec = """
devices:
  - name: SW1
    profile: cisco_switch
    vlans:
      10: DATA
    interfaces:
      - name: Vlan1
        ip: 195.100.50.3/24
"""
    errors, warnings = validate_spec_content(spec)
    warnings = errors + warnings
    assert any("Vlan1" in w and "vulnerabilità di sicurezza" in w for w in warnings)


def test_validate_spec_content_switch_management_requires_default_gateway():
    spec_missing_gateway = """
devices:
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Vlan1
        ip: 192.168.1.2/24
"""
    errors, warnings = validate_spec_content(spec_missing_gateway)
    warnings = errors + warnings
    assert any("Switch L2" in w and "non ha una default route" in w for w in warnings)

    spec_valid_gateway = """
devices:
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Vlan1
        ip: 192.168.1.2/24
    static_routes:
      - network: 0.0.0.0/0
        next_hop: 192.168.1.1
"""
    errors, warnings = validate_spec_content(spec_valid_gateway)
    warnings = errors + warnings
    assert not any("Switch L2" in w and "default" in w for w in warnings)


def test_sync_llm_client_retry_success():
    import os
    from unittest.mock import MagicMock, patch
    from llm.spec_wizard import SyncLLMClient
    
    with patch.dict(os.environ, {"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "dummy_gemini", "GITHUB_TOKEN": "dummy_github"}):
        client = SyncLLMClient()
        client._google = MagicMock()
        
        mock_response = MagicMock()
        mock_response.text = "Hello world"
        client._google.models.generate_content.side_effect = [Exception("Temporary Error"), mock_response]
        
        history = []
        result = client.chat(history, "Hello", "System prompt")
        assert result == "Hello world"
        assert client._google.models.generate_content.call_count == 2


def test_sync_llm_client_fallback_to_secondary():
    import os
    from unittest.mock import MagicMock, patch
    from llm.spec_wizard import SyncLLMClient
    
    with patch.dict(os.environ, {"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "dummy_gemini", "GITHUB_TOKEN": "dummy_github"}):
        client = SyncLLMClient()
        client._google = MagicMock()
        client._openai = MagicMock()
        
        client._google.models.generate_content.side_effect = Exception("Gemini Down")
        
        mock_openai_response = MagicMock()
        mock_openai_response.choices = [MagicMock()]
        mock_openai_response.choices[0].message.content = "GitHub response"
        client._openai.chat.completions.create.return_value = mock_openai_response
        
        history = []
        with patch("time.sleep") as mock_sleep:
            result = client.chat(history, "Hello", "System prompt")
            assert result == "GitHub response"
            assert client._google.models.generate_content.call_count == 3
            assert client._openai.chat.completions.create.call_count == 1


def test_sync_llm_client_all_fail_serializes_fallback(tmp_path):
    import os
    import json
    from pathlib import Path as RealPath
    from unittest.mock import MagicMock, patch
    from llm.spec_wizard import SyncLLMClient
    
    with patch.dict(os.environ, {"LLM_PROVIDER": "gemini", "GEMINI_API_KEY": "dummy_gemini", "GITHUB_TOKEN": "dummy_github"}):
        client = SyncLLMClient()
        client._google = MagicMock()
        client._openai = MagicMock()
        
        client._google.models.generate_content.side_effect = Exception("Gemini Down")
        client._openai.chat.completions.create.side_effect = Exception("OpenAI Down")
        
        history = []
        
        class MockPath:
            def __init__(self, *args):
                self.real_path = RealPath(tmp_path) / "config"
            def __truediv__(self, other):
                return self.real_path / other
        
        with patch("time.sleep"), patch("llm.spec_wizard.Path", MockPath):
            with pytest.raises(Exception) as exc_info:
                client.chat(history, "Hello", "System prompt")
            
            assert "OpenAI Down" in str(exc_info.value)
            
            # Find the generated error file in tmp_path/config
            config_dir = RealPath(tmp_path) / "config"
            files = list(config_dir.glob(".wizard_error_session_fallback_*.json"))
            assert len(files) == 1
            fallback_file = files[0]
            
            data = json.loads(fallback_file.read_text(encoding="utf-8"))
            assert data["failed_user_message"] == "Hello"
            assert "OpenAI Down" in data["error"]


def test_validate_spec_content_flags_vpcs_l2_attributes():
    # PC1 with native_vlan or access_vlan or mode or trunk_vlans should fail validation
    spec_vpcs_l2 = """
devices:
  - name: PC1
    profile: vpcs
    interfaces:
      - name: e0
        native_vlan: 10
"""
    errors, warnings = validate_spec_content(spec_vpcs_l2, phase=2)
    assert any("VPCS/Host" in e and "native_vlan" in e for e in errors)

    spec_vpcs_mode = """
devices:
  - name: PC1
    profile: vpcs
    interfaces:
      - name: e0
        mode: access
"""
    errors, warnings = validate_spec_content(spec_vpcs_mode, phase=2)
    assert any("VPCS/Host" in e and "mode" in e for e in errors)


def test_validate_spec_content_flags_switchport_mode_mismatches():
    # Access port with native_vlan or trunk_vlans
    spec_access_native = """
devices:
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/1
        mode: access
        native_vlan: 10
"""
    errors, warnings = validate_spec_content(spec_access_native, phase=2)
    assert any("modalità 'access'" in e and "native_vlan" in e for e in errors)

    spec_access_trunks = """
devices:
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/1
        mode: access
        trunk_vlans: [10, 20]
"""
    errors, warnings = validate_spec_content(spec_access_trunks, phase=2)
    assert any("modalità 'access'" in e and "trunk_vlans" in e for e in errors)

    # Trunk port with access_vlan
    spec_trunk_access = """
devices:
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/1
        mode: trunk
        access_vlan: 10
"""
    errors, warnings = validate_spec_content(spec_trunk_access, phase=2)
    assert any("modalità 'trunk'" in e and "access_vlan" in e for e in errors)


def test_validate_spec_content_flags_vpcs_missing_ip_in_phase_3():
    spec_no_ip = """
devices:
  - name: PC1
    profile: vpcs
    interfaces:
      - name: e0
"""
    # In Phase 2, missing IP is NOT a validation error
    errors, warnings = validate_spec_content(spec_no_ip, phase=2)
    assert not any("non ha specificato l'IP" in e for e in errors)

    # In Phase 3, missing IP IS a validation error
    errors, warnings = validate_spec_content(spec_no_ip, phase=3)
    assert any("non ha specificato l'IP" in e for e in errors)


def test_merge_specifications_deletes_null_keys():
    old_spec = """
devices:
  - name: R1
    profile: cisco_ios
    interfaces:
      - name: e0
        ip: 192.168.1.1/24
"""
    # LLM proposes setting ip to null (None) to remove it
    new_spec = """
devices:
  - name: R1
    profile: cisco_ios
    interfaces:
      - name: e0
        ip: null
"""
    merged = merge_specifications(old_spec, new_spec)
    parsed = yaml.safe_load(merged)
    iface = parsed["devices"][0]["interfaces"][0]
    assert "ip" not in iface or iface["ip"] is None


def test_parse_gns3_project_precise_router_profile(tmp_path):
    from llm.spec_wizard import parse_gns3_project
    gns3_data = """{
      "topology": {
        "nodes": [
          {"node_id": "1", "name": "server-print", "node_type": "ethernet_switch"},
          {"node_id": "2", "name": "R1", "node_type": "ethernet_switch"}
        ],
        "links": [
          {"nodes": [{"node_id": "1", "port_number": 1}, {"node_id": "2", "port_number": 2}]}
        ]
      }
    }"""
    gns3_file = tmp_path / "test.gns3"
    gns3_file.write_text(gns3_data, encoding="utf-8")
    
    yaml_out = parse_gns3_project(gns3_file)
    parsed = yaml.safe_load(yaml_out)
    
    # "server-print" contains letter 'r' but should NOT be classified as cisco_ios (router)
    server_node = next(d for d in parsed["devices"] if d["name"] == "server-print")
    assert server_node["profile"] == "cisco_switch" # matches "switch" in node_type or "sw"
    
    # R1 matches r\d+ -> cisco_ios
    r1_node = next(d for d in parsed["devices"] if d["name"] == "R1")
    assert r1_node["profile"] == "cisco_ios"


def test_secrets_masking_in_session_logs():
    from llm.spec_wizard import _sanitize_secrets, _sanitize_session_data
    
    raw_prompt = "Configure SW1 with enable secret admin123 and password testpassword"
    sanitized = _sanitize_secrets(raw_prompt)
    assert "admin123" not in sanitized
    assert "testpassword" not in sanitized
    assert "enable secret ********" in sanitized
    assert "password ********" in sanitized

    session = {
        "system_prompt": "enable secret supersecret",
        "history": [
            {"role": "user", "content": "password admin_pass"},
            {"role": "assistant", "content": "enable_secret: 'foo'"}
        ]
    }
    sanitized_session = _sanitize_session_data(session)
    assert "supersecret" not in sanitized_session["system_prompt"]
    assert "admin_pass" not in sanitized_session["history"][0]["content"]
    assert "foo" not in sanitized_session["history"][1]["content"]
    assert "********" in sanitized_session["system_prompt"]
    assert "********" in sanitized_session["history"][0]["content"]
    assert "********" in sanitized_session["history"][1]["content"]


def test_save_and_resume_session(tmp_path):
    import json
    import re
    from llm.spec_wizard import _save_session

    session_file = tmp_path / ".wizard_session_test.json"
    history = [
        {"role": "user", "content": "Start phase 3"},
        {"role": "assistant", "content": "Understood, starting FASE 3 L3 configuration."}
    ]
    
    _save_session(history, session_file, phase=3)
    
    # Verify file saved
    assert session_file.exists()
    
    # Read back and verify structure
    with open(session_file, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    assert data["phase"] == 3
    assert data["history"] == history

    # Test phase extraction from raw list history format
    history_list = history
    phase = 1
    for msg in reversed(history_list):
        content = msg.get("content", "")
        m = re.search(r'FASE\s+(\d+)', content, re.IGNORECASE)
        if m:
            phase = int(m.group(1))
            break
            
    assert phase == 3


def test_history_token_pruning():
    from llm.spec_wizard import _prune_history_specs

    history = [
        {"role": "user", "content": "Initial request"},
        {"role": "assistant", "content": "Here is YAML 1:\n<<<SPEC_START>>>\ndevices:\n- name: R1\n<<<SPEC_END>>>\nDone."},
        {"role": "user", "content": "Add R2"},
        {"role": "assistant", "content": "Here is YAML 2:\n<<<SPEC_START>>>\ndevices:\n- name: R1\n- name: R2\n<<<SPEC_END>>>\nFinalized."}
    ]

    pruned = _prune_history_specs(history)

    # First assistant message should have its spec pruned
    assert "YAML 1" in pruned[1]["content"]
    assert "# [Specifica YAML precedente omessa per risparmio token]" in pruned[1]["content"]
    assert "devices:\n- name: R1" not in pruned[1]["content"]

    # Second assistant message (the last one) should remain intact
    assert "YAML 2" in pruned[3]["content"]
    assert "devices:\n- name: R1\n- name: R2" in pruned[3]["content"]


def test_validate_spec_content_native_vlan_security_best_practices():
    # 1. Warn if native VLAN is allowed on the trunk (i.e. present in trunk_vlans)
    spec_with_native_allowed = """
devices:
  - name: SW1
    profile: cisco_switch
    vlans:
      10: DATA
      999: BlackHole_Native
    interfaces:
      - name: Ethernet0/0
        mode: trunk
        native_vlan: 999
        trunk_vlans: [10, 999]
"""
    errors, warnings = validate_spec_content(spec_with_native_allowed)
    assert any("esclusa dai trunk consentiti" in w for w in warnings)

    # 2. Warn if native VLAN is used but not defined in local VLANs database
    spec_missing_local_vlan = """
devices:
  - name: SW1
    profile: cisco_switch
    vlans:
      10: DATA
    interfaces:
      - name: Ethernet0/0
        mode: trunk
        native_vlan: 999
        trunk_vlans: [10]
"""
    errors, warnings = validate_spec_content(spec_missing_local_vlan)
    assert any("non è dichiarata nel database VLAN" in w for w in warnings)

    # 3. Warn if an SVI for the native VLAN is active (has an IP)
    spec_active_native_svi = """
devices:
  - name: SW1
    profile: cisco_switch
    vlans:
      10: DATA
      999: BlackHole_Native
    interfaces:
      - name: Ethernet0/0
        mode: trunk
        native_vlan: 999
        trunk_vlans: [10]
      - name: Vlan999
        ip: 192.168.99.10/24
"""
    errors, warnings = validate_spec_content(spec_active_native_svi)
    assert any("La Native VLAN dummy non deve avere interfacce logiche attive" in w for w in warnings)

    # 4. No warning if best practices are followed correctly
    spec_correct = """
devices:
  - name: SW1
    profile: cisco_switch
    vlans:
      10: DATA
      999: BlackHole_Native
    interfaces:
      - name: Ethernet0/0
        mode: trunk
        native_vlan: 999
        trunk_vlans: [10]
"""
    errors, warnings = validate_spec_content(spec_correct)
    assert not any("esclusa dai trunk consentiti" in w for w in warnings)
    assert not any("non è dichiarata nel database VLAN" in w for w in warnings)
    assert not any("interfacce logiche attive" in w for w in warnings)


def test_validate_no_device_loss_allows_explicit_deletions():
    from llm.spec_wizard import _validate_no_device_loss
    
    known_names = {"IOU1", "IOU2"}
    
    # Candidate spec where IOU1 is deleted, but patch does NOT mention it -> missing IOU1
    candidate_no_patch = """
devices:
  - name: IOU2
    profile: cisco_switch
"""
    missing = _validate_no_device_loss(known_names, candidate_no_patch)
    assert "IOU1" in missing

    # Candidate spec where IOU1 is deleted, and patch explicitly deletes it -> no loss triggered
    patch_with_delete = """
devices:
  - name: IOU1
    delete: true
"""
    missing_with_patch = _validate_no_device_loss(known_names, candidate_no_patch, patch_with_delete)
    assert "IOU1" not in missing_with_patch
    
    # Candidate spec where IOU1 is deleted, and patch has state: absent -> no loss triggered
    patch_with_state_absent = """
devices:
  - name: IOU1
    state: absent
"""
    missing_with_state_absent = _validate_no_device_loss(known_names, candidate_no_patch, patch_with_state_absent)
    assert "IOU1" not in missing_with_state_absent


def test_merge_specifications_deletes_device_level_null_keys():
    from llm.spec_wizard import merge_specifications
    
    old_spec = """
devices:
  - name: SW1
    profile: cisco_switch
    vlans:
      10: DATA
    banner: "Welcome"
"""
    new_spec = """
devices:
  - name: SW1
    profile: cisco_switch
    vlans: null
    banner: null
"""
    merged = merge_specifications(old_spec, new_spec)
    parsed = yaml.safe_load(merged)
    device = parsed["devices"][0]
    assert "vlans" not in device
    assert "banner" not in device














