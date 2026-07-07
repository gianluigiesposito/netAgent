import sys
import yaml
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from llm.spec_wizard import merge_specifications

def test_merge_specifications_deep_interfaces():
    old_spec = """
devices:
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/1
        mode: access
        access_vlan: 10
      - name: Ethernet0/2
        mode: access
        access_vlan: 10
"""
    new_spec = """
devices:
  - name: SW1
    profile: cisco_switch
    interfaces:
      - name: Ethernet0/2
        mode: trunk
        trunk_vlans: [10, 20]
      - name: Ethernet0/3
        mode: access
        access_vlan: 20
"""
    merged = merge_specifications(old_spec, new_spec)
    parsed = yaml.safe_load(merged)
    
    assert parsed is not None
    dev = parsed["devices"][0]
    assert dev["name"] == "SW1"
    
    # We expect 3 interfaces: Ethernet0/1 (from old), Ethernet0/2 (updated from new), and Ethernet0/3 (from new)
    ifaces = {i["name"]: i for i in dev["interfaces"]}
    assert len(ifaces) == 3
    
    # Ethernet0/1 preserved from old
    assert ifaces["Ethernet0/1"]["access_vlan"] == 10
    
    # Ethernet0/2 updated from new
    assert ifaces["Ethernet0/2"]["mode"] == "trunk"
    assert ifaces["Ethernet0/2"]["trunk_vlans"] == [10, 20]
    
    # Ethernet0/3 added from new
    assert ifaces["Ethernet0/3"]["access_vlan"] == 20

def test_merge_specifications_deep_static_routes():
    old_spec = """
devices:
  - name: R1
    profile: cisco_ios
    static_routes:
      - network: 192.168.10.0/24
        next_hop: 10.0.0.2
      - network: 192.168.20.0/24
        next_hop: 10.0.0.2
"""
    new_spec = """
devices:
  - name: R1
    profile: cisco_ios
    static_routes:
      - network: 192.168.20.0/24
        next_hop: 10.0.0.3
      - network: 192.168.30.0/24
        next_hop: 10.0.0.3
"""
    merged = merge_specifications(old_spec, new_spec)
    parsed = yaml.safe_load(merged)
    
    dev = parsed["devices"][0]
    routes = {r["network"]: r for r in dev["static_routes"]}
    assert len(routes) == 3
    assert routes["192.168.10.0/24"]["next_hop"] == "10.0.0.2"
    assert routes["192.168.20.0/24"]["next_hop"] == "10.0.0.3"
    assert routes["192.168.30.0/24"]["next_hop"] == "10.0.0.3"

def test_merge_specifications_deep_dhcp_pools():
    old_spec = """
devices:
  - name: R2
    profile: cisco_ios
    dhcp_pools:
      - name: POOL1
        network: 192.168.10.0/24
        gateway: 192.168.10.1
"""
    new_spec = """
devices:
  - name: R2
    profile: cisco_ios
    dhcp_pools:
      - name: POOL1
        network: 192.168.10.0/24
        gateway: 192.168.10.2
      - name: POOL2
        network: 192.168.20.0/24
        gateway: 192.168.20.1
"""
    merged = merge_specifications(old_spec, new_spec)
    parsed = yaml.safe_load(merged)
    
    dev = parsed["devices"][0]
    pools = {p["name"]: p for p in dev["dhcp_pools"]}
    assert len(pools) == 2
    assert pools["POOL1"]["gateway"] == "192.168.10.2"
    assert pools["POOL2"]["gateway"] == "192.168.20.1"

def test_merge_specifications_deep_vlans():
    old_spec = """
devices:
  - name: SW1
    profile: cisco_switch
    vlans:
      10: USERS_10
      20: USERS_20
"""
    new_spec = """
devices:
  - name: SW1
    profile: cisco_switch
    vlans:
      20: USERS_20_NEW
      30: GUEST
"""
    merged = merge_specifications(old_spec, new_spec)
    parsed = yaml.safe_load(merged)
    
    dev = parsed["devices"][0]
    vlans = dev["vlans"]
    assert len(vlans) == 3
    assert vlans[10] == "USERS_10"
    assert vlans[20] == "USERS_20_NEW"
    assert vlans[30] == "GUEST"


def test_merge_specifications_retains_credentials_and_ips_when_omitted():
    old_spec = """
networks:
  LAN_A:
    cidr: 10.0.0.0/24
    gateway: 10.0.0.1
devices:
  - name: R1
    profile: frrouting
    credentials:
      username: admin
      password: old_password
    interfaces:
      - name: eth0
        ip: 10.0.0.1/24
"""
    new_spec = """
devices:
  - name: R1
    profile: frrouting
    interfaces:
      - name: eth0
"""
    merged = merge_specifications(old_spec, new_spec)
    parsed = yaml.safe_load(merged)
    
    # Check that top-level networks was preserved
    assert parsed.get("networks") == {"LAN_A": {"cidr": "10.0.0.0/24", "gateway": "10.0.0.1"}}
    
    dev = parsed["devices"][0]
    # Check that credentials was preserved
    assert dev.get("credentials") == {"username": "admin", "password": "old_password"}
    
    # Check that eth0 interface IP was preserved
    ifaces = {i["name"]: i for i in dev["interfaces"]}
    assert ifaces["eth0"].get("ip") == "10.0.0.1/24"


def test_merge_specifications_clears_lists_when_null_or_empty():
    old_spec = """
devices:
  - name: R2
    profile: cisco_ios
    dhcp_pools:
      - name: POOL1
        network: 192.168.10.0/24
        gateway: 192.168.10.1
    interfaces:
      - name: e0
        ip: 192.168.10.1/24
    static_routes:
      - network: 0.0.0.0/0
        next_hop: 10.0.0.1
"""
    new_spec = """
devices:
  - name: R2
    profile: cisco_ios
    dhcp_pools: null
    static_routes: []
"""
    merged = merge_specifications(old_spec, new_spec)
    parsed = yaml.safe_load(merged)
    
    dev = parsed["devices"][0]
    assert dev.get("dhcp_pools") == []
    assert dev.get("static_routes") == []
    assert len(dev.get("interfaces", [])) == 1
    assert dev["interfaces"][0]["ip"] == "192.168.10.1/24"

