#!/usr/bin/env python3
import sys
import json
import re
import os
import shutil
from pathlib import Path
import yaml

def generate_devices_from_gns3(gns3_path: Path, output_path: Path):
    try:
        with open(gns3_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"Errore lettura file GNS3: {e}")
        sys.exit(1)

    topology = data.get("topology", {})
    nodes = topology.get("nodes", [])

    # Supporta l'override delle credenziali di default tramite variabili d'ambiente (SecOps)
    cisco_user = os.getenv("NETAGENT_DEFAULT_USER", "admin")
    cisco_pass = os.getenv("NETAGENT_DEFAULT_PASS", "cisco")
    frr_user = os.getenv("NETAGENT_DEFAULT_FRR_USER", "frr")
    frr_pass = os.getenv("NETAGENT_DEFAULT_FRR_PASS", "frr")

    devices = {}
    for node in nodes:
        name = node.get("name")
        if not name:
            continue

        node_type = node.get("node_type", "").lower()
        console = node.get("console")
        console_host = node.get("console_host", "127.0.0.1")

        # Salta i nodi che non hanno una porta console
        if console is None:
            continue

        name_lower = name.lower()
        symbol = node.get("symbol", "").lower()
        properties = node.get("properties", {})
        path = (properties.get("path") or properties.get("image") or "").lower()
        
        # Mappatura in base al nome, tipo, symbol e path immagine del nodo
        if node_type == "vpcs" or "pc" in name_lower or "host" in name_lower or "vpcs" in symbol:
            vendor = "vpcs"
            connection_type = "vpcs_telnet"
            dev_dict = {
                "connection_type": connection_type,
                "host": console_host,
                "port": console,
                "vendor": vendor,
            }
        elif "frr" in name_lower or "frrouting" in name_lower or "frr" in path:
            vendor = "frrouting"
            connection_type = "telnet"
            dev_dict = {
                "connection_type": connection_type,
                "host": console_host,
                "port": console,
                "vendor": vendor,
                "username": frr_user,
                "password": frr_pass,
            }
        elif "switch" in symbol or "sw" in name_lower or "switch" in name_lower or node_type == "ethernet_switch" or "l2" in path:
            vendor = "cisco_switch"
            connection_type = "cisco_telnet"
            dev_dict = {
                "connection_type": connection_type,
                "host": console_host,
                "port": console,
                "vendor": vendor,
                "username": cisco_user,
                "password": cisco_pass,
            }
        elif "router" in symbol or re.search(r'\br\d+\b|^r\d+|r-\d+', name_lower) or "router" in name_lower or node_type == "dynamips" or "l3" in path:
            vendor = "cisco_ios"
            connection_type = "cisco_telnet"
            dev_dict = {
                "connection_type": connection_type,
                "host": console_host,
                "port": console,
                "vendor": vendor,
                "username": cisco_user,
                "password": cisco_pass,
            }
        else:
            # Fallback intelligente per immagini IOU (es. IOU1, IOU2)
            if node_type == "iou" and "l2" in path:
                vendor = "cisco_switch"
            else:
                vendor = "cisco_ios"
            
            connection_type = "cisco_telnet"
            dev_dict = {
                "connection_type": connection_type,
                "host": console_host,
                "port": console,
                "vendor": vendor,
                "username": cisco_user,
                "password": cisco_pass,
            }

        devices[name] = dev_dict

    # Scrittura atomica tramite file temporaneo e move atomico per evitare corruzioni in caso di crash
    tmp_path = output_path.with_suffix(".yaml.tmp")
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.dump(devices, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
        shutil.move(str(tmp_path), str(output_path))
        print(f"Inventario generato con successo in {output_path} ({len(devices)} dispositivi trovati).")
    except Exception as e:
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        print(f"Errore scrittura {output_path}: {e}")
        sys.exit(1)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python tools/generate_devices_from_gns3.py <file.gns3> [output_path]")
        sys.exit(1)

    gns3_file = Path(sys.argv[1])
    output_file = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("config/devices.yaml")

    generate_devices_from_gns3(gns3_file, output_file)
