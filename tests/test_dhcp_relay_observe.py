import sys
from pathlib import Path
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.state import IntentModel, RouterIntent, NetworkIntentSchema, DeviceIntent, InterfaceIntent
from nodes.observe import _find_relay_devices
from nodes.troubleshoot import _build_desired_state

def test_find_relay_devices_legacy():
    plan = IntentModel(
        protocol="static",
        router_plans=[
            RouterIntent(
                router_name="R1",
                interfaces=["Ethernet0/1"],
                extra_params="DHCP_RELAY: 192.168.10.0/24\nDHCP_SERVER: R2"
            ),
            RouterIntent(
                router_name="R2",
                interfaces=["Ethernet0/0"],
                extra_params=""
            )
        ]
    )
    devices = _find_relay_devices(plan)
    assert set(devices) == {"R1", "R2"}

def test_find_relay_devices_modern():
    plan = NetworkIntentSchema(
        devices=[
            DeviceIntent(
                name="R1",
                profile="cisco_ios",
                interfaces=[InterfaceIntent(name="Ethernet0/1", ip="192.168.10.1/24")],
                extra_params="DHCP_RELAY: 192.168.10.0/24\nDHCP_SERVER: R2"
            ),
            DeviceIntent(
                name="R2",
                profile="cisco_ios",
                interfaces=[InterfaceIntent(name="Ethernet0/0", ip="10.0.0.2/30")],
                extra_params=""
            )
        ],
        rollback_scope="device-only"
    )
    devices = _find_relay_devices(plan)
    assert set(devices) == {"R1", "R2"}

def test_build_desired_state_legacy():
    plan = IntentModel(
        protocol="static",
        router_plans=[
            RouterIntent(
                router_name="R1",
                interfaces=["Ethernet0/1"],
                extra_params="DHCP_RELAY: 192.168.10.0/24\nDHCP_SERVER: R2"
            )
        ]
    )
    out = _build_desired_state(plan, ["R1"])
    assert "DEVICE: R1" in out
    assert "Interfaces: Ethernet0/1" in out
    assert "DHCP_RELAY: 192.168.10.0/24" in out

def test_build_desired_state_modern():
    plan = NetworkIntentSchema(
        devices=[
            DeviceIntent(
                name="R1",
                profile="cisco_ios",
                interfaces=[InterfaceIntent(name="Ethernet0/1", ip="192.168.10.1/24")],
                extra_params="DHCP_RELAY: 192.168.10.0/24\nDHCP_SERVER: R2"
            )
        ],
        rollback_scope="device-only"
    )
    out = _build_desired_state(plan, ["R1"])
    assert "DEVICE: R1" in out
    assert "Interfaces: Ethernet0/1" in out
    assert "DHCP_RELAY: 192.168.10.0/24" in out

def test_extract_dhcp_relay_params_semicolon():
    from tools.dhcp_relay import extract_dhcp_relay_params
    extra = "DHCP_RELAY: 192.168.10.0/24,192.168.20.0/24; DHCP_SERVER: R2"
    subnets, server = extract_dhcp_relay_params(extra)
    assert subnets == ["192.168.10.0/24", "192.168.20.0/24"]
    assert server == "R2"

def test_extract_dhcp_relay_params_structured():
    from tools.dhcp_relay import extract_dhcp_relay_params
    subnets, server = extract_dhcp_relay_params(
        extra_params="DHCP_RELAY: 10.0.0.0/24\nDHCP_SERVER: R3",
        plan_relay_server="R2",
        plan_relay_subnets=["192.168.10.0/24", "192.168.20.0/24"]
    )
    # Structured fields must take precedence
    assert subnets == ["192.168.10.0/24", "192.168.20.0/24"]
    assert server == "R2"

def test_find_relay_devices_with_structured_fields():
    plan = NetworkIntentSchema(
        devices=[
            DeviceIntent(
                name="R1",
                profile="cisco_ios",
                interfaces=[InterfaceIntent(name="Ethernet0/1", ip="192.168.10.1/24")],
                dhcp_relay_server="R2",
                dhcp_relay_subnets=["192.168.10.0/24", "192.168.20.0/24"]
            ),
            DeviceIntent(
                name="R2",
                profile="cisco_ios",
                interfaces=[InterfaceIntent(name="Ethernet0/0", ip="10.0.0.2/30")],
            )
        ],
        rollback_scope="device-only"
    )
    devices = _find_relay_devices(plan)
    assert set(devices) == {"R1", "R2"}
