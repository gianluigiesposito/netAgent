# generate/models/deltas.py
"""
Modelli di dominio del motore GENERATE — Solo dati, zero logica (SRP).

Ogni dataclass rappresenta un aspetto della configurazione desiderata
o il delta tra stato desiderato e stato attuale.

Gerarchia:
  DeviceDelta
  ├── InterfaceDelta[]
  ├── RouteDelta[]
  ├── DhcpStateDelta[]    (definita in tools/dhcp_config.py, riusata qui)
  ├── HelperAddressDelta[]
  ├── VlanDelta[]         ← NUOVO: VLAN su switch Cisco
  ├── SubinterfaceDelta[] ← NUOVO: subinterface dot1q su router Cisco (inter-VLAN)
  ├── VPCSDelta?
  └── BaseConfigDelta?
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


# ─────────────────────────────────────────────────────────────────────────────
# Delta singoli per ogni aspetto di configurazione
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class InterfaceDelta:
    """Delta tra IP desiderato e IP attuale su una singola interfaccia."""
    iface: str
    desired_ip: str
    desired_cidr: int
    current_ip: str
    action_needed: str          # "CORRECT" | "EMPTY" | "WRONG"
    stale_ip_to_remove: str = ""
    stale_cidr_to_remove: int = 24


@dataclass
class RouteDelta:
    """Delta tra rotta statica desiderata e rotta attuale."""
    network: str
    cidr: int
    next_hop: str
    action_needed: str          # "CORRECT" | "MISSING"
    stale_next_hop_to_remove: str = ""


@dataclass
class VPCSDelta:
    """Delta per un host VPCS (static IP oppure DHCP)."""
    action_needed: str          # "CORRECT" | "NEED_DHCP" | "STATIC_REQUIRED"
    desired_ip: str = ""
    desired_mask: str = ""
    desired_gw: str = ""
    current_ip: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# NUOVO: Delta VLAN (switch L2 Cisco)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class VlanDelta:
    """
    Delta per la definizione di una VLAN su uno switch Cisco.

    Copre sia la creazione della VLAN nel database VTP
    (vlan X / name Y) sia la modalità porta (access o trunk).

    action_needed:
      "CORRECT"     → VLAN già presente con il nome corretto.
      "MISSING"     → VLAN assente, va creata.
      "WRONG_NAME"  → VLAN presente ma con nome diverso.
    """
    vlan_id: int
    desired_name: str
    action_needed: str          # "CORRECT" | "MISSING" | "WRONG_NAME"
    current_name: str = ""


@dataclass
class SwitchportDelta:
    """
    Delta per la modalità switchport di una singola porta fisica.

    action_needed:
      "CORRECT"  → Porta già configurata correttamente.
      "MISSING"  → Manca la configurazione di questa porta.
      "WRONG"    → Configurazione presente ma diversa (es. era access, ora trunk).
    """
    iface: str
    desired_mode: str           # "access" | "trunk"
    action_needed: str          # "CORRECT" | "MISSING" | "WRONG"

    # Campi access
    desired_access_vlan: int = 0

    # Campi trunk
    desired_trunk_vlans: list[int] = field(default_factory=list)
    desired_native_vlan: int = 1
    current_mode: str = ""
    current_trunk_vlans: list[int] = field(default_factory=list)
    current_native_vlan: int = 1
    extra_trunk_vlans: list[int] = field(default_factory=list)
    missing_trunk_vlans: list[int] = field(default_factory=list)

    # Sicurezza porta (opzionale, portata dal BaseConfig ma applicata per porta)
    port_security: bool = False
    port_security_max: int = 1
    port_security_violation: str = "restrict"
    portfast: bool = False
    bpduguard: bool = False


@dataclass
class SubinterfaceDelta:
    """
    Delta per una subinterface dot1Q su router Cisco (inter-VLAN routing,
    pattern "router-on-a-stick").

    Esempio: interface Ethernet0/0.10
               encapsulation dot1Q 10
               ip address 192.168.10.1 255.255.255.0

    action_needed:
      "CORRECT" → Subinterface già presente con IP e VLAN corretti.
      "EMPTY"   → Subinterface assente, va creata.
      "WRONG"   → Subinterface presente ma con IP o VLAN diversi.
    """
    parent_iface: str           # es. "Ethernet0/0"
    sub_id: int                 # es. 10  → Ethernet0/0.10
    vlan_id: int                # ID VLAN per encapsulation dot1Q
    desired_ip: str
    desired_cidr: int
    action_needed: str          # "CORRECT" | "EMPTY" | "WRONG"
    current_ip: str = ""
    current_vlan_id: Optional[int] = None
    stale_ip_to_remove: str = ""
    stale_cidr_to_remove: int = 24

    @property
    def iface_name(self) -> str:
        """Nome completo della subinterface (es. 'Ethernet0/0.10')."""
        return f"{self.parent_iface}.{self.sub_id}"


# ─────────────────────────────────────────────────────────────────────────────
# Configurazione base (invariata)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class BaseConfig:
    """
    Configurazione base desiderata per Cisco IOS/Switch.

    Ogni campo corrisponde a un comando CLI specifico.
    I valori None indicano parametri non richiesti dalla specifica.
    """
    enabled: bool = False
    hostname: str = ""
    banner: Optional[str] = None
    enable_secret: Optional[str] = None
    username: str = "admin"
    password: str = "cisco"
    domain_name: str = "netagent.local"
    ssh_timeout: int = 60
    ssh_retries: int = 3
    login_block_for: Optional[int] = None
    login_attempts: Optional[int] = None
    login_within: Optional[int] = None
    password_min_length: Optional[int] = None
    console_timeout: Optional[tuple[int, int]] = None
    vty_timeout: Optional[tuple[int, int]] = None
    vty_lines: str = "0 4"
    service_password_encryption: bool = True
    no_cdp_run: bool = False
    default_gateway: Optional[str] = None
    access_ports: list[str] = field(default_factory=list)
    uplink_ports: list[str] = field(default_factory=list)
    trusted_ports: list[str] = field(default_factory=list)
    dhcp_snooping_vlans: list[str] = field(default_factory=list)
    arp_inspection_vlans: list[str] = field(default_factory=list)
    port_security: bool = False
    port_security_max: int = 1
    port_security_violation: str = "restrict"
    portfast: bool = False
    bpduguard: bool = False
    dhcp_rate_limit: Optional[int] = None
    vtp_mode: str = "transparent"
    port_security_mac_sticky: bool = True


@dataclass
class BaseConfigDelta:
    """Delta tra base config desiderata e running config attuale."""
    desired: BaseConfig
    action_needed: str                          # "CORRECT" | "MISSING"
    missing_commands: list[str] = field(default_factory=list)
    needs_crypto_key: bool = False


@dataclass
class HelperAddressDelta:
    """Delta per un singolo ip helper-address su un'interfaccia."""
    iface: str
    dhcp_server_ip: str
    action_needed: str          # "CORRECT" | "MISSING"


@dataclass
class EtherChannelDelta:
    """
    Delta per la configurazione di un bundle EtherChannel (Port-channel).

    action_needed:
      "CORRECT" -> I membri fisici e la modalità sono già corretti.
      "MISSING" -> Uno o più membri devono essere associati al channel-group.
      "WRONG"   -> Uno o più membri sono associati a gruppi diversi o con modalità errata.
    """
    pc_name: str
    desired_members: list[str]
    desired_mode: str
    action_needed: str
    missing_members: list[str] = field(default_factory=list)
    wrong_members: list[tuple[str, Optional[int], Optional[str]]] = field(default_factory=list)
    stale_members: list[str] = field(default_factory=list)
    needs_pc_interface: bool = False
    dirty_members: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Aggregatore per device (aggiornato con VLAN)
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DeviceDelta:
    """
    Contenitore aggregato di tutti i delta per un singolo dispositivo.

    Il gate di idempotenza (`is_fully_idempotent`) è il punto decisionale
    centrale: se True, non serve generare alcun comando.
    """
    router_name: str
    interface_deltas: list[InterfaceDelta] = field(default_factory=list)
    route_deltas: list[RouteDelta] = field(default_factory=list)
    dhcp_deltas: list = field(default_factory=list)
    helper_address_deltas: list[HelperAddressDelta] = field(default_factory=list)
    vlan_deltas: list[VlanDelta] = field(default_factory=list)               # NUOVO
    switchport_deltas: list[SwitchportDelta] = field(default_factory=list)   # NUOVO
    subinterface_deltas: list[SubinterfaceDelta] = field(default_factory=list)  # NUOVO
    etherchannel_deltas: list[EtherChannelDelta] = field(default_factory=list)  # NUOVO
    vpcs_delta: Optional[VPCSDelta] = None
    base_config_delta: Optional[BaseConfigDelta] = None
    unused_interfaces_to_shutdown: list[str] = field(default_factory=list)
    
    # Sweep lists for removing extra configuration
    extra_routes_to_remove: list[RouteDelta] = field(default_factory=list)
    extra_vlans_to_remove: list[tuple[int, str]] = field(default_factory=list)
    extra_dhcp_pools_to_remove: list[tuple[str, str]] = field(default_factory=list)
    extra_subinterfaces_to_remove: list[tuple[str, str]] = field(default_factory=list)

    @property
    def is_fully_idempotent(self) -> bool:
        """
        True se ogni singolo aspetto della configurazione è già nello stato desiderato.
        La valutazione è esaustiva: basta un delta non-CORRECT per essere False.
        """
        interfaces_ok = all(
            d.action_needed == "CORRECT" for d in self.interface_deltas
        )
        routes_ok = all(
            r.action_needed == "CORRECT" for r in self.route_deltas
        )
        dhcp_ok = all(
            d.action_needed == "CORRECT" for d in self.dhcp_deltas
        )
        helpers_ok = all(
            h.action_needed == "CORRECT" for h in self.helper_address_deltas
        )
        vlans_ok = all(                                      # NUOVO
            v.action_needed == "CORRECT" for v in self.vlan_deltas
        )
        switchports_ok = all(                                # NUOVO
            s.action_needed == "CORRECT" for s in self.switchport_deltas
        )
        subinterfaces_ok = all(                              # NUOVO
            s.action_needed == "CORRECT" for s in self.subinterface_deltas
        )
        etherchannels_ok = all(                              # NUOVO
            e.action_needed == "CORRECT" for e in self.etherchannel_deltas
        )
        vpcs_ok = (
            self.vpcs_delta is None
            or self.vpcs_delta.action_needed == "CORRECT"
        )
        base_config_ok = (
            self.base_config_delta is None
            or self.base_config_delta.action_needed == "CORRECT"
        )
        unused_ok = not self.unused_interfaces_to_shutdown
        
        # Sweep checks
        extra_routes_ok = not self.extra_routes_to_remove
        extra_vlans_ok = not self.extra_vlans_to_remove
        extra_dhcp_ok = not self.extra_dhcp_pools_to_remove
        extra_subinterfaces_ok = not self.extra_subinterfaces_to_remove

        return (
            interfaces_ok
            and routes_ok
            and dhcp_ok
            and helpers_ok
            and vlans_ok
            and switchports_ok
            and subinterfaces_ok
            and etherchannels_ok
            and vpcs_ok
            and base_config_ok
            and unused_ok
            and extra_routes_ok
            and extra_vlans_ok
            and extra_dhcp_ok
            and extra_subinterfaces_ok
        )

    def describe(self) -> str:
        """
        Produce una descrizione leggibile del delta complessivo.
        Usata per il logging strutturato e come contesto per il fallback LLM.
        """
        if self.is_fully_idempotent:
            return "NO_CHANGES_NEEDED"

        lines: list[str] = []

        if self.base_config_delta is not None:
            lines.append(
                f"[{self.base_config_delta.action_needed}] BASE_CONFIG "
                f"missing={self.base_config_delta.missing_commands} "
                f"needs_crypto_key={self.base_config_delta.needs_crypto_key}"
            )

        for dh in self.dhcp_deltas:
            lines.append(f"[{dh.action_needed}] DHCP_POOL {dh.pool_name}")

        for i in self.interface_deltas:
            lines.append(
                f"[{i.action_needed}] Interface {i.iface} "
                f"-> {i.desired_ip}/{i.desired_cidr}"
            )

        for r in self.route_deltas:
            lines.append(
                f"[{r.action_needed}] Route {r.network}/{r.cidr} "
                f"via {r.next_hop}"
            )

        for h in self.helper_address_deltas:
            lines.append(
                f"[{h.action_needed}] helper-address {h.iface} "
                f"-> {h.dhcp_server_ip}"
            )

        for v in self.vlan_deltas:                          # NUOVO
            lines.append(
                f"[{v.action_needed}] VLAN {v.vlan_id} "
                f"name='{v.desired_name}'"
            )

        for s in self.switchport_deltas:                    # NUOVO
            if s.desired_mode == "access":
                lines.append(
                    f"[{s.action_needed}] Switchport {s.iface} "
                    f"access vlan {s.desired_access_vlan}"
                )
            else:
                lines.append(
                    f"[{s.action_needed}] Switchport {s.iface} "
                    f"trunk vlans={s.desired_trunk_vlans} native={s.desired_native_vlan}"
                )

        for si in self.subinterface_deltas:                 # NUOVO
            lines.append(
                f"[{si.action_needed}] Subinterface {si.iface_name} "
                f"dot1Q {si.vlan_id} -> {si.desired_ip}/{si.desired_cidr}"
            )

        for e in self.etherchannel_deltas:                  # NUOVO
            lines.append(
                f"[{e.action_needed}] EtherChannel {e.pc_name} "
                f"members={e.desired_members} mode={e.desired_mode}"
            )

        if self.vpcs_delta is not None:
            lines.append(f"[{self.vpcs_delta.action_needed}] VPCS host")

        for ui in self.unused_interfaces_to_shutdown:
            lines.append(f"[UNUSED_SHUTDOWN] Interface {ui} needs shutdown")

        # Describe sweep deletions
        for er in self.extra_routes_to_remove:
            lines.append(f"[EXTRA_ROUTE_REMOVE] Route {er.network}/{er.cidr} via {er.next_hop} needs removal")
        for ev, _ in self.extra_vlans_to_remove:
            lines.append(f"[EXTRA_VLAN_REMOVE] VLAN {ev} needs removal")
        for ep, _ in self.extra_dhcp_pools_to_remove:
            lines.append(f"[EXTRA_DHCP_REMOVE] DHCP Pool {ep} needs removal")
        for es, _ in self.extra_subinterfaces_to_remove:
            lines.append(f"[EXTRA_SUBINTERFACE_REMOVE] Subinterface {es} needs removal")

        return "\n".join(lines)
