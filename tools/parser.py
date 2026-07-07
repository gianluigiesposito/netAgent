# tools/parser.py
import re

def normalize_interface_name(name: str) -> str:
    """
    Normalizza i nomi delle interfacce in un formato standard coerente (es. Ethernet0/0, GigabitEthernet1/2.100).
    Rimuove spazi e normalizza abbreviazioni come Gi0/0 -> GigabitEthernet0/0, Eth0/1 -> Ethernet0/1.
    """
    if not name:
        return ""
    
    # Rimuovi spazi per normalizzazione uniforme.
    s = re.sub(r'\s+', '', name).strip()
    
    # Estrai eventuale subinterface (es. .100)
    sub = ""
    if "." in s:
        s, sub = s.split(".", 1)
        sub = "." + sub
        
    # Pattern per separare prefisso e suffisso. Il prefisso può contenere
    # trattini: Port-channel1 deve restare un Port-channel, non "Port".
    match = re.match(r'^([a-zA-Z-]+)(.*)$', s)
    if not match:
        return name
        
    prefix, suffix = match.groups()
    prefix = prefix.lower()
    
    # Mappatura dei prefissi comuni dei vari vendor ai nomi canonici Cisco/Linux
    mapping = {
        "e": "Ethernet",
        "et": "Ethernet",
        "eth": "Ethernet",
        "ethernet": "Ethernet",
        "fa": "FastEthernet",
        "fastethernet": "FastEthernet",
        "gi": "GigabitEthernet",
        "gig": "GigabitEthernet",
        "gigabitethernet": "GigabitEthernet",
        "se": "Serial",
        "serial": "Serial",
        "vl": "Vlan",
        "vlan": "Vlan",
        "lo": "Loopback",
        "loopback": "Loopback",
        "port-channel": "Port-channel",
        "portchannel": "Port-channel",
        "po": "Port-channel",
        "ens": "ens",
        "eno": "eno",
        "enp": "enp",
    }
    
    if prefix in ("eth", "ethernet") and "/" not in suffix:
        # Probabilmente interfaccia Linux/FRRouting (es. eth0)
        canonical_prefix = "eth"
    else:
        canonical_prefix = mapping.get(prefix, prefix)
        
    return f"{canonical_prefix}{suffix}{sub}"


def parse_interfaces(raw_output: str, vendor: str) -> list[dict]:
    """
    Extract physical interfaces from CLI show output.
    Returns list of {name, status, ip}.
    Ignores loopback and internal interfaces.
    """
    interfaces = []

    if vendor == "frrouting":
        current_iface = None
        for line in raw_output.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            # Accept eth*, ens*, eno*, enp* — any physical Ethernet naming
            match = re.match(r'^(eth|ens|eno|enp)\d', stripped)
            if match:
                parts = stripped.split()
                if len(parts) < 2:
                    continue
                name = normalize_interface_name(parts[0])
                status = "up" if "up" in parts[1].lower() else "down"
                ip = "unassigned"
                for part in parts[2:]:
                    if "/" in part and re.match(r'^\d{1,3}(?:\.\d{1,3}){3}/\d+$', part):
                        ip = part
                        break
                current_iface = {"name": name, "status": status, "ips": []}
                if ip != "unassigned":
                    current_iface["ips"].append(ip)
                interfaces.append(current_iface)
            else:
                if current_iface:
                    parts = stripped.split()
                    for part in parts:
                        if "/" in part and re.match(r'^\d{1,3}(?:\.\d{1,3}){3}/\d+$', part):
                            if part not in current_iface["ips"]:
                                current_iface["ips"].append(part)
        for iface in interfaces:
            if "ips" in iface:
                iface["ip"] = ",".join(iface["ips"]) if iface["ips"] else "unassigned"
                del iface["ips"]

    elif vendor in ("cisco_ios", "cisco_switch"):
        # Parsing dell'output tabellare del comando 'show ip interface brief' di Cisco
        for line in raw_output.splitlines():
            stripped = line.strip()
            # Intercetta EthernetX/Y, FastEthernetX/Y, GigabitEthernetX/Y, eX/Y, faX/Y, giX/Y, Port-channelX ecc.
            if not re.match(r'^(Ethernet|FastEthernet|GigabitEthernet|Serial|Vlan|Port-channel|Po|e|fa|gi|se|po)\d', stripped, re.IGNORECASE):
                continue
            parts = stripped.split()
            if len(parts) < 5:
                continue
            
            name = normalize_interface_name(parts[0])
            ip_raw = parts[1]
            # Se non c'è IP configurato su Cisco, l'output mostra esplicitamente 'unassigned'
            ip = "unassigned" if ip_raw.lower() == "unassigned" else ip_raw
            
            # Lo stato amministrativo ("Status") e il protocollo di linea ("Protocol") sono gli ultimi due token
            # Se la porta è spenta o in shutdown, intercettiamo lo stato 'down'
            status = "down" if "down" in parts[-2].lower() or "down" in parts[-1].lower() else "up"
            
            interfaces.append({"name": name, "status": status, "ip": ip})

    elif vendor == "vpcs":
        ip_match = re.search(r'IP/MASK\s*:\s*([\d\.]+/\d+)', raw_output)
        if ip_match:
            ip = ip_match.group(1)
            ip = "unassigned" if ip.startswith("0.0.0.0") else ip
            interfaces.append({"name": "eth0", "status": "up", "ip": ip})

    return interfaces


def load_inventory() -> dict:
    from pathlib import Path
    import yaml
    path = Path("config/devices.yaml")
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        import logging
        logging.getLogger(__name__).error("Cannot load inventory: %s", e)
        return {}


def resolve_vendor(cfg: dict, router_name: str) -> str:
    vendor = (cfg.get("vendor") or "").lower()
    if vendor:
        return vendor
    # Heuristic based on name
    if "pc" in router_name.lower():
        return "vpcs"
    if "sw" in router_name.lower():
        return "cisco_switch"
    return "frrouting"

