# tests/test_spec_parser.py
"""
Test unitari deterministici per tools/spec_parser.py.

Coprono:
  - Parsing del formato Lab4 (Cisco IOS + VPCS DHCP client)
  - Parsing Lab3 (FRRouting + VPCS statico)
  - Rilevamento automatico del protocollo
  - Vendor fallback euristico
  - EXTRA_PARAMS multiriga
  - Specifica vuota / malformata
  - build_intent_from_specs → IntentModel corretto
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import unittest
from tools.spec_parser import SpecFileParser, parse_spec_file, get_vendor_for_device, parse_spec_to_intent

# ─────────────────────────────────────────────────────────────────────────────
# FIXTURE
# ─────────────────────────────────────────────────────────────────────────────

LAB4_SPEC = """
# config/specificaLab4.txt

=== NETAGENT SYSTEM TARGET SPECIFICATION FILE ===
LAB_REFERENCE: Lab_4_Two_hosts_and_a_DHCP_server
TOPOLOGY_OBJECTIVE: Dynamic IP allocation within a single broadcast domain via Cisco Native Services

--- DEVICE: PC1 ---
PROFILE: vpcs
INTERFACE: eth0
IP_ADDRESS: DHCP

--- DEVICE: R1 ---
PROFILE: cisco_ios
INTERFACE: Ethernet0/0
IP_ADDRESS: 192.168.30.1/24
EXTRA_PARAMS: Configure Ethernet0/0 with 192.168.30.1/24 ip dhcp pool LAN_POOL network 192.168.30.0/24 default-router 192.168.30.1 dns-server 8.8.8.8 lease 1

--- DEVICE: PC2 ---
PROFILE: vpcs
INTERFACE: eth0
IP_ADDRESS: DHCP
"""

LAB3_SPEC = """
=== NETAGENT SYSTEM TARGET SPECIFICATION FILE ===
LAB_REFERENCE: Lab_3_Static_Routing

--- DEVICE: R1 ---
PROFILE: frrouting
INTERFACE: eth0
IP_ADDRESS: 192.168.10.1/24
EXTRA_PARAMS: Configure eth0 with 192.168.10.1/24 Configure eth1 with 10.10.10.1/30 ip route 192.168.20.0/24 10.10.10.2

--- DEVICE: R2 ---
PROFILE: frrouting
INTERFACE: eth0
IP_ADDRESS: 192.168.20.1/24
EXTRA_PARAMS: Configure eth0 with 192.168.20.1/24 Configure eth1 with 10.10.10.2/30 ip route 192.168.10.0/24 10.10.10.1

--- DEVICE: PC1 ---
PROFILE: vpcs
INTERFACE: eth0
IP_ADDRESS: 192.168.10.2/24
EXTRA_PARAMS: ip 192.168.10.2 255.255.255.0 192.168.10.1

--- DEVICE: PC2 ---
PROFILE: vpcs
INTERFACE: eth0
IP_ADDRESS: 192.168.20.2/24
EXTRA_PARAMS: ip 192.168.20.2 255.255.255.0 192.168.20.1
"""

# Multiline EXTRA_PARAMS
MULTILINE_EXTRA_PARAMS_SPEC = """
--- DEVICE: R1 ---
PROFILE: cisco_ios
INTERFACE: Ethernet0/0
IP_ADDRESS: 10.0.0.1/24
EXTRA_PARAMS: Configure Ethernet0/0 with 10.0.0.1/24
    ip dhcp pool MGMT
    network 10.0.0.0/24
    default-router 10.0.0.1
    lease 2
"""

UNKNOWN_VENDOR_SPEC = """
--- DEVICE: myrouter ---
PROFILE: juniper_junos
INTERFACE: ge-0/0/0
IP_ADDRESS: 172.16.0.1/24
EXTRA_PARAMS: Configure ge-0/0/0 with 172.16.0.1/24
"""

EMPTY_SPEC    = ""
MALFORMED_SPEC = "This is not a valid spec file at all."


# ─────────────────────────────────────────────────────────────────────────────
# TEST CASES
# ─────────────────────────────────────────────────────────────────────────────

class TestSpecParser(unittest.TestCase):

    def setUp(self):
        self.parser = SpecFileParser()

    # ── Lab4 ──────────────────────────────────────────────────────────────────

    def test_lab4_device_count(self):
        specs, _ = parse_spec_file(LAB4_SPEC)
        self.assertEqual(len(specs), 3, "Lab4 deve avere esattamente 3 device")

    def test_lab4_device_names(self):
        specs, _ = parse_spec_file(LAB4_SPEC)
        names = [s.name for s in specs]
        self.assertIn("PC1", names)
        self.assertIn("R1",  names)
        self.assertIn("PC2", names)

    def test_lab4_r1_vendor_cisco(self):
        specs, _ = parse_spec_file(LAB4_SPEC)
        r1 = next(s for s in specs if s.name == "R1")
        self.assertEqual(r1.vendor, "cisco_ios")

    def test_lab4_pc1_vendor_vpcs(self):
        specs, _ = parse_spec_file(LAB4_SPEC)
        pc1 = next(s for s in specs if s.name == "PC1")
        self.assertEqual(pc1.vendor, "vpcs")

    def test_lab4_pc1_needs_dhcp_client(self):
        specs, _ = parse_spec_file(LAB4_SPEC)
        pc1 = next(s for s in specs if s.name == "PC1")
        self.assertTrue(pc1.needs_dhcp_client)

    def test_lab4_pc2_needs_dhcp_client(self):
        specs, _ = parse_spec_file(LAB4_SPEC)
        pc2 = next(s for s in specs if s.name == "PC2")
        self.assertTrue(pc2.needs_dhcp_client)

    def test_lab4_r1_extra_params_contains_dhcp_pool(self):
        specs, _ = parse_spec_file(LAB4_SPEC)
        r1 = next(s for s in specs if s.name == "R1")
        self.assertIn("ip dhcp pool", r1.extra_params.lower())
        self.assertIn("LAN_POOL",     r1.extra_params)

    def test_lab4_r1_interface(self):
        specs, _ = parse_spec_file(LAB4_SPEC)
        r1 = next(s for s in specs if s.name == "R1")
        self.assertIn("Ethernet0/0", r1.interfaces)

    def test_lab4_protocol_dhcp(self):
        _, intent = parse_spec_file(LAB4_SPEC)
        self.assertEqual(intent.protocol, "DHCP")

    def test_lab4_intent_router_plans_count(self):
        _, intent = parse_spec_file(LAB4_SPEC)
        self.assertEqual(len(intent.router_plans), 3)

    def test_lab4_pc1_intent_extra_params_dhcp(self):
        """intent di PC1 deve contenere 'ip dhcp' per il diff VPCS."""
        _, intent = parse_spec_file(LAB4_SPEC)
        pc1_plan = next(rp for rp in intent.router_plans if rp.router_name == "PC1")
        self.assertIn("dhcp", pc1_plan.extra_params.lower())

    # ── Lab3 ──────────────────────────────────────────────────────────────────

    def test_lab3_device_count(self):
        specs, _ = parse_spec_file(LAB3_SPEC)
        self.assertEqual(len(specs), 4)

    def test_lab3_r1_vendor_frr(self):
        specs, _ = parse_spec_file(LAB3_SPEC)
        r1 = next(s for s in specs if s.name == "R1")
        self.assertEqual(r1.vendor, "frrouting")

    def test_lab3_r1_extra_params_static_route(self):
        specs, _ = parse_spec_file(LAB3_SPEC)
        r1 = next(s for s in specs if s.name == "R1")
        self.assertIn("ip route", r1.extra_params.lower())

    def test_lab3_pc1_not_dhcp_client(self):
        specs, _ = parse_spec_file(LAB3_SPEC)
        pc1 = next(s for s in specs if s.name == "PC1")
        self.assertFalse(pc1.needs_dhcp_client)

    def test_lab3_protocol_static(self):
        _, intent = parse_spec_file(LAB3_SPEC)
        self.assertEqual(intent.protocol, "Static")

    # ── Multiline EXTRA_PARAMS ────────────────────────────────────────────────

    def test_multiline_extra_params_joined(self):
        specs, _ = parse_spec_file(MULTILINE_EXTRA_PARAMS_SPEC)
        self.assertEqual(len(specs), 1)
        r1 = specs[0]
        # Le righe indentate devono essere unite
        self.assertIn("ip dhcp pool MGMT", r1.extra_params)
        self.assertIn("default-router 10.0.0.1", r1.extra_params)

    # ── Vendor fallback ───────────────────────────────────────────────────────

    def test_unknown_vendor_fallback_to_frr(self):
        """Vendor non riconosciuto: fallback euristico → frrouting (non PC)."""
        specs, _ = parse_spec_file(UNKNOWN_VENDOR_SPEC)
        self.assertEqual(len(specs), 1)
        self.assertEqual(specs[0].vendor, "frrouting")

    def test_get_vendor_for_device_found(self):
        specs, _ = parse_spec_file(LAB4_SPEC)
        self.assertEqual(get_vendor_for_device("R1",  specs), "cisco_ios")
        self.assertEqual(get_vendor_for_device("PC1", specs), "vpcs")

    def test_get_vendor_for_device_not_found_heuristic(self):
        """Dispositivo non in specifica: fallback euristico sul nome."""
        specs, _ = parse_spec_file(LAB4_SPEC)
        self.assertEqual(get_vendor_for_device("PC3",      specs), "vpcs")
        self.assertEqual(get_vendor_for_device("R2",       specs), "frrouting")
        self.assertEqual(get_vendor_for_device("ciscoGW",  specs), "cisco_ios")

    # ── Edge cases ────────────────────────────────────────────────────────────

    def test_empty_spec_returns_empty_list(self):
        specs, intent = parse_spec_file(EMPTY_SPEC)
        self.assertEqual(specs, [])
        self.assertEqual(intent.router_plans, [])

    def test_malformed_spec_returns_empty_list(self):
        specs, intent = parse_spec_file(MALFORMED_SPEC)
        self.assertEqual(specs, [])
        self.assertEqual(intent.router_plans, [])

    def test_no_extra_params_builds_synthetic(self):
        """Se EXTRA_PARAMS è assente, il parser deve costruirne uno sintetico."""
        minimal = """
--- DEVICE: R1 ---
PROFILE: frrouting
INTERFACE: eth0
IP_ADDRESS: 10.0.0.1/24
"""
        specs, _ = parse_spec_file(minimal)
        self.assertEqual(len(specs), 1)
        # extra_params sintetico deve contenere l'IP
        self.assertIn("10.0.0.1", specs[0].extra_params)

    def test_cisco_is_not_dhcp_client(self):
        """R1 Cisco con IP statico NON deve essere marcato come dhcp_client."""
        specs, _ = parse_spec_file(LAB4_SPEC)
        r1 = next(s for s in specs if s.name == "R1")
        self.assertFalse(r1.needs_dhcp_client)

    def test_malformed_access_port_with_extra_text_logs_warning(self):
        spec = """
=== NETAGENT SYSTEM TARGET SPECIFICATION FILE ===
LAB_REFERENCE: Test
--- DEVICE: SW1 ---
PROFILE: cisco_switch
ACCESS_PORT: Ethernet0/1 10 PORTFAST: true
"""
        with self.assertLogs('tools.spec_parser', level='WARNING') as log_capture:
            parse_spec_file(spec)
        self.assertTrue(any("ACCESS_PORT malformato" in log for log in log_capture.output))

    def test_malformed_trunk_port_with_extra_text_logs_warning(self):
        spec = """
=== NETAGENT SYSTEM TARGET SPECIFICATION FILE ===
LAB_REFERENCE: Test
--- DEVICE: SW1 ---
PROFILE: cisco_switch
TRUNK_PORT: Ethernet0/1 VLANS 10,20 NATIVE 1 BLAH BLAH
"""
        with self.assertLogs('tools.spec_parser', level='WARNING') as log_capture:
            parse_spec_file(spec)
        self.assertTrue(any("TRUNK_PORT malformato" in log for log in log_capture.output))

    def test_malformed_subinterface_with_extra_text_logs_warning(self):
        spec = """
=== NETAGENT SYSTEM TARGET SPECIFICATION FILE ===
LAB_REFERENCE: Test
--- DEVICE: R1 ---
PROFILE: cisco_ios
SUBINTERFACE: Ethernet0/0.10 VLAN 10 IP 10.0.0.1/24 EXTRA GARBAGE
"""
        with self.assertLogs('tools.spec_parser', level='WARNING') as log_capture:
            parse_spec_file(spec)
        self.assertTrue(any("SUBINTERFACE malformato" in log for log in log_capture.output))

    def test_spec_parser_keeps_etherchannel_members_with_spaces(self):
        spec = """
--- DEVICE: SW1 ---
PROFILE: cisco_switch
ETHERCHANNEL: Port-channel1 members Ethernet0/1, Ethernet0/2 mode active
"""
        intent = parse_spec_to_intent(spec)

        self.assertIsNotNone(intent)
        extra = intent.router_plans[0].extra_params
        self.assertIn("ETHERCHANNEL: Port-channel1 members Ethernet0/1,Ethernet0/2 mode active", extra)

    def test_spec_parser_propagates_logical_ports(self):
        spec = """
--- DEVICE: SW1 ---
PROFILE: cisco_switch
ETHERCHANNEL: Port-channel1 members Ethernet2/0, Ethernet2/1 mode active
TRUNK_PORT: Port-channel1 VLANS 10,20 NATIVE 1
"""
        intent = parse_spec_to_intent(spec)
        self.assertIsNotNone(intent)
        extra = intent.router_plans[0].extra_params

        # Verify TRUNK_PORT is propagated to members to prevent LACP mismatch
        self.assertIn("TRUNK_PORT: Port-channel1 VLANS 10,20 NATIVE 1", extra)
        self.assertIn("TRUNK_PORT: Ethernet2/0 VLANS 10,20 NATIVE 1", extra)
        self.assertIn("TRUNK_PORT: Ethernet2/1 VLANS 10,20 NATIVE 1", extra)


# ─────────────────────────────────────────────────────────────────────────────
# RUNNER
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    unittest.main(verbosity=2)
