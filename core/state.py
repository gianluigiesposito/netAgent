# core/state.py
"""
Modelli di stato di NetAgent v3.

Aggiornamenti rispetto alla v2:
  - Aggiunto campo `troubleshoot_attempt` (int) per il contatore retry.
  - Aggiunto campo `failed_devices` (list[str]) per trasmettere al nodo
    TROUBLESHOOT solo i dispositivi che hanno fallito la VERIFY.
  - Aggiunto campo `diagnostic_report` (str) prodotto dal nodo
    TROUBLESHOOT al termine dei tentativi esauriti.
  - `final_status` ora può assumere il valore "TROUBLESHOOT_EXHAUSTED"
    oltre a "SUCCESS" e "FAILED".
"""

from pydantic import BaseModel, Field, field_validator
from typing import TypedDict, Optional, Annotated, Any, Dict, List, Literal, Union
import operator
import copy
import ipaddress


# =====================================================================
# 1. MODELLI DI INTENTO
# =====================================================================

class RouterIntent(BaseModel):
    router_name: str = Field(
        description="Il nome esatto del dispositivo nell'inventario."
    )
    interfaces: list[str] = Field(
        description="Lista delle interfacce coinvolte."
    )
    extra_params: str = Field(
        description=(
            "Parametri di intento in testo libero. Esempi:\n"
            "  - Assegnazione IP: 'Configure eth0 with 192.168.10.1/24'\n"
            "  - Rotta statica:   'ip route 192.168.20.0/24 10.10.10.2'\n"
            "  - Pool DHCP FRR:   'ip dhcp pool LAN network 192.168.10.0/24 "
            "default-router 192.168.10.1 dns-server 8.8.8.8'\n"
            "  - Host VPCS:       'ip 192.168.10.2 255.255.255.0 192.168.10.1'"
        )
    )
    dhcp_relay_server: Optional[str] = Field(default=None, description="Router name acting as DHCP server (e.g. 'R2')")
    dhcp_relay_subnets: Optional[List[str]] = Field(default=None, description="List of subnet CIDRs to relay (e.g. ['192.168.10.0/24'])")


class IntentModel(BaseModel):
    protocol: str = Field(
        description="Il protocollo di routing o l'azione macro della topologia."
    )
    router_plans: list[RouterIntent] = Field(
        description="Lista delle pianificazioni per tutti i dispositivi della rete."
    )


# =====================================================================
# 2. AZIONI STRUTTURATE
# =====================================================================

class InterfaceConfigAction(BaseModel):
    action_type: Literal["configure_interface"] = "configure_interface"
    iface:  str = Field(description="Nome interfaccia fisica, es. 'eth0'")
    ip:     str = Field(description="Indirizzo IP, es. '192.168.10.1'")
    cidr:   int = Field(description="Prefix length CIDR, es. 24")


class StaticRouteAction(BaseModel):
    action_type: Literal["add_static_route"] = "add_static_route"
    network:  str = Field(description="Rete remota, es. '192.168.20.0'")
    cidr:     int = Field(description="Prefix length CIDR, es. 24")
    next_hop: str = Field(description="Next-hop IP, es. '10.10.10.2'")


class DhcpPoolAction(BaseModel):
    action_type:    Literal["configure_dhcp_pool"] = "configure_dhcp_pool"
    pool_name:      str = Field(description="Nome del pool DHCP")
    network:        str = Field(description="Indirizzo di rete del pool")
    prefix_len:     int = Field(description="Prefix length CIDR")
    default_router: str = Field(description="IP del gateway da distribuire")
    dns_server:     str = Field(default="8.8.8.8")
    lease_days:     int = Field(default=1)
    excluded_start: Optional[str] = Field(default=None)
    excluded_end:   Optional[str] = Field(default=None)


class VPCSConfigAction(BaseModel):
    action_type: Literal["configure_vpcs"] = "configure_vpcs"
    ip:      str = Field(description="IP statico, es. '192.168.10.2'")
    mask:    str = Field(description="Maschera dotted-decimal")
    gateway: str = Field(description="IP del Default Gateway")


class RouterActionPlan(BaseModel):
    router_name: str
    actions: List[
        Union[InterfaceConfigAction, StaticRouteAction, DhcpPoolAction, VPCSConfigAction]
    ] = Field(default=[])


# =====================================================================
# 3. MODELLI DI ESECUZIONE
# =====================================================================

class CommandPair(BaseModel):
    cmd:      str = Field(description="Comando CLI da trasmettere.")
    rollback: str = Field(description="Contromisura per annullare cmd.")


class RouterCommands(BaseModel):
    pairs: List[CommandPair] = Field(default=[])

    @property
    def commands(self) -> list[str]:
        return [p.cmd for p in self.pairs]

    @property
    def rollback_commands(self) -> list[str]:
        return [p.rollback for p in self.pairs]


# =====================================================================
# 4. REDUCERS E STATO LANGGRAPH
# =====================================================================

def update_commands(
    existing: Dict[str, RouterCommands],
    new: Dict[str, RouterCommands],
) -> Dict[str, RouterCommands]:
    merged = copy.deepcopy(existing)
    merged.update(new)
    return merged


def update_reachability(
    existing: Dict[str, str],
    new: Dict[str, str],
) -> Dict[str, str]:
    merged = copy.deepcopy(existing)
    merged.update(new)
    return merged


class AgentState(TypedDict):
    user_task:         str
    image_path:        Optional[str]
    spec_path:         Optional[str]
    specification_raw: str
    raw_input:         str
    intent:            Optional[IntentModel]
    plan:              Any

    router_commands: Annotated[Dict[str, RouterCommands], update_commands]
    reachability:    Annotated[Dict[str, str], update_reachability]

    retry_count:    int
    execution_log:  Annotated[List[str], operator.add]
    final_status:   str

    # ── Troubleshooting ──────────────────────────────────────────────
    # Contatore tentativi di troubleshooting (0 = nessuno eseguito).
    # Incrementato da TROUBLESHOOT prima di ogni tentativo di fix.
    troubleshoot_attempt: int

    # Dispositivi che hanno fallito la VERIFY nell'ultimo ciclo.
    # VERIFY lo popola; TROUBLESHOOT lo legge per focalizzare lo snapshot.
    failed_devices: List[str]

    # Report diagnostico finale prodotto quando i tentativi si esauriscono.
    # Stringa Markdown strutturata leggibile dall'operatore.
    diagnostic_report: str
    rollback_scope: Optional[str]
    executed_commands: Optional[Dict[str, RouterCommands]]
    test_troubleshoot_skip_execute: Optional[bool]



# =====================================================================
# 5. MODELLI DI STRUTTURA YAML (Fase 1 & 3)
# =====================================================================

class InterfaceIntent(BaseModel):
    name: str = Field(description="Nome dell'interfaccia, es. Ethernet0/0 o Port-channel1")
    ip: Optional[str] = Field(default=None, description="IP/CIDR, es. 192.168.10.1/24 o 'dhcp'")
    vlan_id: Optional[int] = Field(default=None, description="VLAN ID per subinterfacce o switchport access")
    
    # Switchport Settings
    mode: Optional[Literal["access", "trunk"]] = Field(default=None, description="Modalità porta")
    access_vlan: Optional[int] = Field(default=None, description="VLAN associata alla modalità access")
    trunk_vlans: Optional[List[int]] = Field(default=None, description="VLAN permesse in modalità trunk")
    native_vlan: Optional[int] = Field(default=None, description="VLAN nativa per trunk")
    
    # EtherChannel Settings
    channel_group: Optional[int] = Field(default=None, description="Numero del Port-channel")
    channel_mode: Optional[Literal["active", "passive", "on"]] = Field(default=None, description="Modalità LACP")

    @field_validator("ip")
    @classmethod
    def validate_ip(cls, v: Optional[str]) -> Optional[str]:
        if not v or v.lower() == "dhcp":
            return v
        try:
            ipaddress.IPv4Interface(v)
        except ValueError:
            raise ValueError(f"Formato IP/CIDR non valido: {v}")
        return v

    @field_validator("vlan_id", "access_vlan", "native_vlan")
    @classmethod
    def validate_vlan(cls, v: Optional[int]) -> Optional[int]:
        if v is not None and not (1 <= v <= 4094):
            raise ValueError(f"VLAN ID deve essere compreso tra 1 e 4094: {v}")
        return v

class StaticRouteIntent(BaseModel):
    network: str = Field(description="Subnet di destinazione, es. 192.168.20.0/24")
    next_hop: str = Field(description="Indirizzo IP del next hop")

    @field_validator("network")
    @classmethod
    def validate_network(cls, v: str) -> str:
        try:
            ipaddress.IPv4Network(v, strict=False)
        except ValueError:
            raise ValueError(f"Subnet CIDR non valida: {v}")
        return v

    @field_validator("next_hop")
    @classmethod
    def validate_next_hop(cls, v: str) -> str:
        try:
            ipaddress.IPv4Address(v)
        except ValueError:
            raise ValueError(f"IP Next Hop non valido: {v}")
        return v

class DhcpPoolIntent(BaseModel):
    name: str = Field(description="Nome del pool DHCP")
    network: str = Field(description="Rete gestita, es. 192.168.10.0/24")
    gateway: str = Field(description="Default gateway distribuito")
    dns: str = Field(default="8.8.8.8", description="Server DNS")
    lease: int = Field(default=1, description="Giorni di lease")
    excluded_start: Optional[str] = Field(default=None, description="Inizio range esclusione")
    excluded_end: Optional[str] = Field(default=None, description="Fine range esclusione")

class DeviceIntent(BaseModel):
    name: str = Field(description="Nome univoco del dispositivo")
    profile: Literal["cisco_ios", "cisco_switch", "frrouting", "vpcs"] = Field(description="Profilo OS")
    interfaces: List[InterfaceIntent] = Field(default=[], description="Interfacce configurate")
    static_routes: List[StaticRouteIntent] = Field(default=[], description="Rotte statiche")
    dhcp_pools: List[DhcpPoolIntent] = Field(default=[], description="Pool DHCP attivi")
    vlans: Dict[int, str] = Field(default={}, description="Mappa VLAN locali (id -> name)")
    
    # Base Config
    hostname: Optional[str] = None
    banner: Optional[str] = None
    enable_secret: Optional[str] = None
    domain_name: Optional[str] = None
    extra_params: Optional[str] = None
    dhcp_relay_server: Optional[str] = Field(default=None, description="Router name acting as DHCP server (e.g. 'R2')")
    dhcp_relay_subnets: Optional[List[str]] = Field(default=None, description="List of subnet CIDRs to relay (e.g. ['192.168.10.0/24'])")

    @field_validator("extra_params", mode="before")
    @classmethod
    def validate_extra_params(cls, v: Any) -> Optional[str]:
        if v is None:
            return v
        if isinstance(v, str):
            return v

        def format_val(val: Any) -> str:
            if isinstance(val, list):
                return ",".join(str(x).strip(" '\"[]") for x in val)
            return str(val).strip(" '\"[]")

        if isinstance(v, dict):
            return "\n".join(f"{k}: {format_val(val)}" for k, val in v.items())

        if isinstance(v, list):
            lines = []
            for item in v:
                if isinstance(item, dict):
                    for k, val in item.items():
                        lines.append(f"{k}: {format_val(val)}")
                else:
                    lines.append(format_val(item))
            return "\n".join(lines)

        return str(v)

class LinkIntent(BaseModel):
    endpoints: List[str] = Field(description="Lista di due endpoint connessi, es. ['PC1:eth0', 'Switch1:Ethernet0/2']")

class NetworkIntentSchema(BaseModel):
    devices: List[DeviceIntent] = Field(description="Lista dispositivi")
    links: List[LinkIntent] = Field(default=[], description="Lista di collegamenti fisici della topologia")
    rollback_scope: Literal["all", "device-only"] = Field(default="all", description="Ambito di rollback")

    @field_validator("devices", mode="before")
    @classmethod
    def validate_devices(cls, v: Any) -> Any:
        if isinstance(v, dict):
            coerced = []
            for name, data in v.items():
                if isinstance(data, dict):
                    d = data.copy()
                    if "name" not in d:
                        d["name"] = name
                    coerced.append(d)
                elif isinstance(data, str):
                    coerced.append({"name": name, "profile": data})
                elif data is None:
                    coerced.append({"name": name})
                else:
                    coerced.append(data)
            return coerced
        return v

