import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm.spec_wizard import validate_spec_content
from nodes.generate import generate_single_node
from core.state import DeviceIntent, InterfaceIntent, NetworkIntentSchema, StaticRouteIntent

def test_etherchannel_symmetry_validation_ok():
    # Symmetric: Port-channel1 and members match
    spec = """
devices:
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Port-channel1
        mode: trunk
        trunk_vlans: [10, 20]
        native_vlan: 999
      - name: Ethernet2/0
        mode: trunk
        trunk_vlans: [10, 20]
        native_vlan: 999
        channel_group: 1
        channel_mode: active
      - name: Ethernet2/1
        mode: trunk
        trunk_vlans: [10, 20]
        native_vlan: 999
        channel_group: 1
        channel_mode: active
rollback_scope: all
"""
    errors, warnings = validate_spec_content(spec)
    warnings = errors + warnings
    # Filter out any warnings about Router-on-a-Stick since we have no router here
    warnings = [w for w in warnings if "Router-on-a-Stick" not in w]
    assert len(warnings) == 0

def test_etherchannel_symmetry_validation_mismatch():
    # Mismatch: members have different trunk allowed VLANs or native VLAN
    spec = """
devices:
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Port-channel1
        mode: trunk
        trunk_vlans: [10, 20]
        native_vlan: 999
      - name: Ethernet2/0
        mode: trunk
        trunk_vlans: [10]
        native_vlan: 999
        channel_group: 1
        channel_mode: active
rollback_scope: all
"""
    errors, warnings = validate_spec_content(spec)
    warnings = errors + warnings
    assert any("la sua configurazione switchport non coincide perfettamente" in w for w in warnings)

def test_etherchannel_missing_logical_port():
    # Missing Port-channel1
    spec = """
devices:
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet2/0
        mode: trunk
        trunk_vlans: [10]
        native_vlan: 999
        channel_group: 1
        channel_mode: active
rollback_scope: all
"""
    errors, warnings = validate_spec_content(spec)
    warnings = errors + warnings
    assert any("non è configurata nella lista delle interfacce" in w for w in warnings)

def test_router_on_a_stick_uplink_validation_ok():
    # Router has subinterfaces, SW1 has a physical trunk uplink covering those VLANs
    spec = """
devices:
  - name: R1
    profile: cisco_ios
    interfaces:
      - name: Ethernet0/1.10
        ip: 192.168.10.1/24
        vlan_id: 10
      - name: Ethernet0/1.20
        ip: 192.168.20.1/24
        vlan_id: 20
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/0
        mode: trunk
        trunk_vlans: [10, 20]
        native_vlan: 999
rollback_scope: all
"""
    errors, warnings = validate_spec_content(spec)
    warnings = errors + warnings
    assert len(warnings) == 0

def test_router_on_a_stick_uplink_validation_missing():
    # Router has subinterfaces, but SW1 only has access ports and port-channels, no physical trunk port
    spec = """
devices:
  - name: R1
    profile: cisco_ios
    interfaces:
      - name: Ethernet0/1.10
        ip: 192.168.10.1/24
        vlan_id: 10
      - name: Ethernet0/1.20
        ip: 192.168.20.1/24
        vlan_id: 20
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/0
        mode: access
        access_vlan: 10
      - name: Port-channel1
        mode: trunk
        trunk_vlans: [10, 20]
rollback_scope: all
"""
    errors, warnings = validate_spec_content(spec)
    warnings = errors + warnings
    assert any("in modalità Router-on-a-Stick" in w for w in warnings)

@pytest.mark.asyncio
async def test_nodes_generate_decoupled_switchport_etherchannel():
    # Test that nodes.generate correctly outputs switchport deltas and etherchannel deltas
    # when an interface has both channel_group and mode: trunk configured.
    from nodes.generate import generate_single_node
    from core.state import DeviceIntent, InterfaceIntent

    device_plan = DeviceIntent(
        name="SW1",
        profile="cisco_switch",
        interfaces=[
            InterfaceIntent(
                name="Ethernet2/0",
                mode="trunk",
                trunk_vlans=[10, 20, 99],
                native_vlan=999,
                channel_group=1,
                channel_mode="active"
            ),
            InterfaceIntent(
                name="Port-channel1",
                mode="trunk",
                trunk_vlans=[10, 20, 99],
                native_vlan=999
            )
        ]
    )

    state = {
        "router_name": "SW1",
        "router_plan": device_plan,
        "reachability": {"SW1": "REACHABLE"},
        "retry_count": 0,
        "troubleshoot_attempt": 0,
    }

    # Mock DB snapshot return (empty running config so all desired changes are computed as deltas)
    from unittest.mock import AsyncMock, patch
    with patch("nodes.generate.GenerateRepository") as mock_repo_cls, \
         patch("nodes.generate.AsyncNetworkGraphStore") as mock_store_cls:
        
        mock_repo = mock_repo_cls.return_value
        mock_repo.get_device_state = AsyncMock(return_value=("cisco_switch", {}, {}, ""))
        
        inst = mock_store_cls.return_value
        inst.close = AsyncMock()

        res = await generate_single_node(state)
        
        # Verify generated commands in router_commands for SW1
        cmds = res["router_commands"]["SW1"].commands
        
        # It must contain switchport commands for Ethernet2/0
        assert any("interface Ethernet2/0" in c for c in cmds)
        assert any("switchport mode trunk" in c for c in cmds)
        assert any("switchport trunk allowed vlan 10,20,99" in c for c in cmds)
        assert any("switchport trunk native vlan 999" in c for c in cmds)
        
        # It must contain channel-group command for Ethernet2/0
        assert any("channel-group 1 mode active" in c for c in cmds)

        # It must contain Port-channel1 trunk config commands
        assert any("interface Port-channel1" in c for c in cmds)
