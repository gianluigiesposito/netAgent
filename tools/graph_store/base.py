# tools/graph_store/base.py
from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from neo4j import AsyncGraphDatabase

logger = logging.getLogger(__name__)

_TS = lambda: datetime.now(timezone.utc).isoformat()

class GraphStoreBase:
    def __init__(self, uri: str = None, user: str = None, password: str = None):
        self._uri      = uri      or os.getenv("NEO4J_URI",      "bolt://localhost:7687")
        self._user     = user     or os.getenv("NEO4J_USER",     "neo4j")
        self._password = password or os.getenv("NEO4J_PASSWORD")
        self._verified_connectivity = False

        if not self._password:
            raise ValueError(
                "CRITICAL: Configurazione Neo4j incompleta. Manca NEO4J_PASSWORD nel .env"
            )
        self._driver = AsyncGraphDatabase.driver(
            self._uri, auth=(self._user, self._password)
        )
        # Cache dell'inventario in memoria per evitare accessi I/O ripetuti
        from tools.parser import load_inventory
        self._inventory = load_inventory()
        logger.info("[Neo4j Store] Connessione inizializzata verso %s", self._uri)

    async def close(self):
        if self._driver:
            await self._driver.close()

    async def verify_connectivity(self):
        if not self._verified_connectivity:
            try:
                import inspect
                coro = self._driver.verify_connectivity()
                if inspect.isawaitable(coro):
                    await coro
                self._verified_connectivity = True
                logger.info("[Neo4j Store] Connessione verificata con successo.")
            except Exception as e:
                logger.error("[Neo4j Store] Connessione a Neo4j fallita: %s", e)
                raise ConnectionError(f"Impossibile connettersi a Neo4j su {self._uri}: {e}") from e

    async def __aenter__(self):
        await self.verify_connectivity()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()
        return False

    def _normalize_port(self, port_name: str, device_name: str) -> str:
        if not port_name:
            return ""

        from tools.parser import resolve_vendor, normalize_interface_name

        device_cfg = self._inventory.get(device_name, {})
        vendor = resolve_vendor(device_cfg, device_name)

        port_clean = port_name.lower().strip()
        sub = ""
        if "." in port_clean:
            port_clean, sub = port_clean.split(".", 1)
            sub = "." + sub

        if vendor == "vpcs":
            match = re.match(r'^(eth|e|ethernet)(\d+)$', port_clean)
            if match:
                digit = match.group(2)
                return f"eth{digit}{sub}"

        return normalize_interface_name(port_name)

    async def _run(self, query: str, **params):
        async def _body():
            async with self._driver.session() as s:
                await s.run(query, **params)
        await self._execute_with_retry(_body)

    async def _execute_with_retry(self, func, *args, **kwargs):
        await self.verify_connectivity()
        from neo4j.exceptions import TransientError, Neo4jError
        max_retries = 5
        backoff = 0.5
        for attempt in range(max_retries):
            try:
                return await func(*args, **kwargs)
            except (TransientError, Neo4jError) as e:
                code = getattr(e, "code", "") or ""
                if "DeadlockDetected" in code or "DeadlockDetected" in str(e):
                    if attempt < max_retries - 1:
                        logger.warning(
                            "[Neo4j Store] Deadlock, retry %d/%d in %.1fs...",
                            attempt + 1, max_retries, backoff,
                        )
                        await asyncio.sleep(backoff)
                        backoff *= 2
                        continue
                raise
