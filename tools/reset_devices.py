# reset_devices.py
"""
Utility script — reset all devices in the lab to a clean state.
Run from the project root: python reset_devices.py
"""
import asyncio
import logging
import sys
import yaml
from pathlib import Path

# Allow running from project root without installing the package
sys.path.insert(0, str(Path(__file__).parent))
from tools.connection import AsyncTelnetConnection

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_FRR_RESET = [
    "configure terminal",
    "interface eth0",
    "no ip address",
    "exit",
    "interface eth1",
    "no ip address",
    "exit",
    "no ip route 0.0.0.0/0",
    "no ip route 192.168.10.0/24",
    "no ip route 192.168.20.0/24",
    "end",
    "write memory",
]


def _load_inventory() -> dict:
    path = Path("config/devices.yaml")
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.error("Cannot load inventory: %s", e)
        return {}


async def _reset_device(name: str, info: dict) -> None:
    host = info.get("host", "127.0.0.1")
    port = info.get("port")
    if not port:
        logger.warning("[%s] No port — skipping.", name)
        return

    try:
        async with AsyncTelnetConnection(host, int(port)) as conn:
            if "pc" in name.lower():
                await conn.send_command("clear ip")
                await conn.send_command("save")
                logger.info("[%s] VPCS reset done.", name)
            else:
                for cmd in _FRR_RESET:
                    await conn.send_command(cmd)
                    await asyncio.sleep(0.2)
                logger.info("[%s] FRRouting reset done.", name)
    except Exception as e:
        logger.error("[%s] Reset failed: %s", name, e)


async def main() -> None:
    inventory = _load_inventory()
    if not inventory:
        logger.error("Empty inventory — aborting.")
        return
    logger.info("=== TOPOLOGY RESET START ===")
    await asyncio.gather(*[_reset_device(n, i) for n, i in inventory.items()])
    logger.info("=== TOPOLOGY RESET COMPLETE ===")


if __name__ == "__main__":
    asyncio.run(main())
