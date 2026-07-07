import sys
import json
import yaml
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.generate_devices_from_gns3 import generate_devices_from_gns3

def test_generate_devices_from_gns3(tmp_path):
    gns3_data = {
        "topology": {
            "nodes": [
                {
                    "name": "R1",
                    "node_type": "iou",
                    "console": 5011,
                    "console_host": "127.0.0.1"
                },
                {
                    "name": "SW1",
                    "node_type": "ethernet_switch",
                    "console": 5013,
                    "console_host": "127.0.0.1"
                },
                {
                    "name": "PC1",
                    "node_type": "vpcs",
                    "console": 5000,
                    "console_host": "127.0.0.1"
                }
            ],
            "links": []
        }
    }
    
    gns3_file = tmp_path / "project.gns3"
    with open(gns3_file, "w", encoding="utf-8") as f:
        json.dump(gns3_data, f)
        
    output_file = tmp_path / "devices.yaml"
    generate_devices_from_gns3(gns3_file, output_file)
    
    assert output_file.exists()
    with open(output_file, "r", encoding="utf-8") as f:
        devices = yaml.safe_load(f)
        
    assert "R1" in devices
    assert devices["R1"]["vendor"] == "cisco_ios"
    assert devices["R1"]["port"] == 5011
    assert devices["R1"]["connection_type"] == "cisco_telnet"
    assert devices["R1"]["username"] == "admin"
    assert devices["R1"]["password"] == "cisco"
    
    assert "SW1" in devices
    assert devices["SW1"]["vendor"] == "cisco_switch"
    assert devices["SW1"]["port"] == 5013
    assert devices["SW1"]["connection_type"] == "cisco_telnet"
    
    assert "PC1" in devices
    assert devices["PC1"]["vendor"] == "vpcs"
    assert devices["PC1"]["port"] == 5000
    assert devices["PC1"]["connection_type"] == "vpcs_telnet"
    assert "username" not in devices["PC1"]


def test_generate_devices_iou_symbol_and_path(tmp_path):
    gns3_data = {
        "topology": {
            "nodes": [
                {
                    "name": "IOU1",
                    "node_type": "iou",
                    "console": 5011,
                    "console_host": "127.0.0.1",
                    "symbol": ":/symbols/ethernet_switch.svg",
                    "properties": {
                        "path": "i186-l2-adventerprisek9-m.152-d1.3a.bin"
                    }
                },
                {
                    "name": "IOU2",
                    "node_type": "iou",
                    "console": 5012,
                    "console_host": "127.0.0.1",
                    "symbol": ":/symbols/router.svg",
                    "properties": {
                        "path": "i186-adventerprisek9-ms.155-2.T.bin"
                    }
                }
            ],
            "links": []
        }
    }
    
    gns3_file = tmp_path / "project2.gns3"
    with open(gns3_file, "w", encoding="utf-8") as f:
        json.dump(gns3_data, f)
        
    output_file = tmp_path / "devices2.yaml"
    generate_devices_from_gns3(gns3_file, output_file)
    
    assert output_file.exists()
    with open(output_file, "r", encoding="utf-8") as f:
        devices = yaml.safe_load(f)
        
    assert "IOU1" in devices
    assert devices["IOU1"]["vendor"] == "cisco_switch"
    assert devices["IOU1"]["port"] == 5011
    
    assert "IOU2" in devices
    assert devices["IOU2"]["vendor"] == "cisco_ios"
    assert devices["IOU2"]["port"] == 5012
