# settings.py
import logging
import yaml
from pathlib import Path

logger = logging.getLogger(__name__)


def load_devices_config(filepath: str = "config/devices.yaml") -> dict:
    path = Path(filepath)
    if not path.exists():
        logger.error("Config file not found: %s", filepath)
        return {}
    with open(path) as f:
        return yaml.safe_load(f) or {}


def load_defaults_config(filepath: str = "config/defaults.yaml") -> dict:
    path = Path(filepath)
    defaults = {
        "domain_name": "netagent.local",
        "port_security_max": 1,
        "port_security_violation": "restrict",
        "port_security_mac_sticky": True,
        "vtp_mode": "transparent",
    }
    if not path.exists():
        return defaults
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
            for k, v in data.items():
                defaults[k.lower()] = v
    except Exception as e:
        logger.warning("Error loading defaults config: %s", e)
    return defaults


# Global default settings cached on import
DEFAULTS = load_defaults_config()

