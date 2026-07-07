# tools/dhcp_relay.py
"""
Motore DHCP Relay per NetAgent v2 (Fase 3 roadmap).

Obiettivo: calcolare automaticamente su quali interfacce di transito
configurare `ip helper-address <DHCP_SERVER_IP>` per permettere
a client DHCP su subnet remote di raggiungere il server DHCP centrale.

Approccio:
  - BFS in Python sul grafo topologico letto da Neo4j.
  - NON usa query Cypher ricorsive (fragili, difficili da debuggare).
  - NON usa APOC (dipendenza esterna Neo4j non sempre disponibile).
  - La topologia è già in Neo4j dopo il nodo OBSERVE: sfruttiamo quello
    che abbiamo senza connessioni aggiuntive ai device.

Flusso (v3 — post OBSERVE_RELAY):
  1. Carica tutti i link tra interfacce adiacenti da Neo4j.
     Al momento della chiamata Neo4j è già aggiornato con le subinterface
     dot1Q create da EXECUTE, quindi il BFS trova sempre il percorso corretto.
  2. BFS da ogni router client verso il router DHCP server.
  3. Identifica l'interfaccia del relay_router che si affaccia sulla subnet client.
  4. Risolve l'IP del server DHCP come IP raggiungibile dal relay_router
     (interfaccia condivisa), non un IP arbitrario.
  5. Ritorna una lista di HelperAddressAction (router, iface, dhcp_server_ip).

Idempotenza:
  Prima di generare il comando, verifica nel running_config del router
  se `ip helper-address` è già presente sull'interfaccia target.

Nota: questo modulo non gestisce più il problema Chicken-and-Egg
(desired_topology fallback). Viene chiamato solo dopo OBSERVE_RELAY.
"""

from __future__ import annotations

import ipaddress
import logging
from collections import deque
from dataclasses import dataclass

from tools.graph_store import AsyncNetworkGraphStore

logger = logging.getLogger(__name__)


import re

def extract_dhcp_relay_params(
    extra_params: str | None,
    plan_relay_server: str | None = None,
    plan_relay_subnets: list[str] | None = None
) -> tuple[list[str], str | None]:
    """
    Estrae i parametri di DHCP Relay dando la priorità ai campi strutturati del piano,
    con fallback robusto sul parsing di extra_params.
    """
    if plan_relay_server and plan_relay_subnets:
        return [s.strip() for s in plan_relay_subnets if s.strip()], plan_relay_server.strip()

    if not extra_params:
        return [], None

    # Parsing robusto di extra_params: sostituiamo i punti e virgola con a capo
    normalized = extra_params.replace(";", "\n").replace("\r", "")
    
    relay_subnets = []
    dhcp_server = None

    for line in normalized.splitlines():
        line = line.strip()
        if not line:
            continue
        # Cerca DHCP_RELAY o RELAY_SUBNETS
        m_relay = re.match(r'^(?:DHCP_RELAY|RELAY_SUBNETS)[:\s]+([^\n]+)', line, re.IGNORECASE)
        if m_relay:
            raw_subnets = m_relay.group(1).split(",")
            for s in raw_subnets:
                s_clean = s.strip().strip("'\"[]")
                if s_clean:
                    relay_subnets.append(s_clean)
        
        # Cerca DHCP_SERVER o RELAY_SERVER
        m_server = re.match(r'^(?:DHCP_SERVER|RELAY_SERVER)[:\s]+(\S+)', line, re.IGNORECASE)
        if m_server:
            dhcp_server = m_server.group(1).strip().strip("'\"[]")

    return relay_subnets, dhcp_server


@dataclass
class HelperAddressAction:
    """Un singolo ip helper-address da configurare su un router."""
    router_name: str
    iface: str
    dhcp_server_ip: str
    already_present: bool = False   # True = idempotente, nessun comando necessario


async def compute_helper_addresses(
    client_subnets: list[str],          # ["195.100.50.0/24", ...]
    dhcp_server_router: str,            # "IOU1"
    store: AsyncNetworkGraphStore,
) -> list[HelperAddressAction]:
    """
    Entry point pubblico.

    Per ogni subnet client che non è direttamente attestata sul router DHCP,
    calcola il percorso BFS e identifica le interfacce di transito su cui
    configurare ip helper-address.

    Args:
        client_subnets:      Lista di CIDR delle subnet con client DHCP remoti.
        dhcp_server_router:  Nome del router che ospita il DHCP server (es. "IOU1").
        store:               Istanza del graph store già aperta.

    Returns:
        Lista di HelperAddressAction, una per ogni interfaccia di transito
        che necessita del comando (already_present=False) o che è già OK
        (already_present=True, per il log di idempotenza).
    """
    # 1. Carica il grafo topologico da Neo4j
    topology = await _load_topology(store)
    if not topology:
        logger.warning("[DhcpRelay] Topologia vuota. Impossibile calcolare helper-address.")
        return []

    # 2. Carica i running-config per il check di idempotenza
    running_configs = await _load_running_configs(store)

    results: list[HelperAddressAction] = []

    for subnet_cidr in client_subnets:
        try:
            client_net = ipaddress.IPv4Network(subnet_cidr, strict=False)
        except ValueError:
            logger.error("[DhcpRelay] Subnet client non valida: %s", subnet_cidr)
            continue

        # Trova il router che si affaccia direttamente sulla subnet client
        client_router = _find_router_for_subnet(client_net, topology)
        if not client_router:
            logger.warning("[DhcpRelay] Nessun router trovato per subnet %s.", subnet_cidr)
            continue

        if client_router == dhcp_server_router:
            logger.info("[DhcpRelay] Subnet %s è locale a %s. Nessun relay necessario.",
                        subnet_cidr, dhcp_server_router)
            continue

        # 3. BFS: da client_router verso dhcp_server_router
        path = _bfs_path(client_router, dhcp_server_router, topology)
        if not path or len(path) < 2:
            logger.warning("[DhcpRelay] Percorso non trovato: %s → %s.",
                           client_router, dhcp_server_router)
            continue

        logger.info("[DhcpRelay] Percorso per subnet %s: %s", subnet_cidr, " → ".join(path))

        # 4. Il relay_router è il primo nodo del percorso (connesso ai client).
        #    L'IP del server DHCP deve essere quello sull'interfaccia condivisa
        #    con il relay_router (next-hop raggiungibile), non un IP arbitrario.
        relay_router = path[0]   # il router direttamente connesso alla subnet client
        relay_iface  = _find_iface_for_subnet(relay_router, client_net, topology)

        # Trova l'IP del server DHCP sull'interfaccia condivisa col relay_router.
        # Questo garantisce che il pacchetto DHCP abbia una destinazione raggiungibile.
        dhcp_server_ip = _find_reachable_ip(relay_router, dhcp_server_router, topology)
        if not dhcp_server_ip:
            logger.error(
                "[DhcpRelay] Impossibile determinare IP raggiungibile del server DHCP "
                "%s da %s.", dhcp_server_router, relay_router,
            )
            continue

        if not relay_iface:
            logger.warning("[DhcpRelay] Interfaccia non trovata su %s per subnet %s.",
                           relay_router, subnet_cidr)
            continue

        # 5. Check idempotenza: helper già configurato?
        rc = running_configs.get(relay_router, "")
        already = _helper_already_configured(rc, relay_iface, dhcp_server_ip)

        results.append(HelperAddressAction(
            router_name=relay_router,
            iface=relay_iface,
            dhcp_server_ip=dhcp_server_ip,
            already_present=already,
        ))

        if already:
            logger.info("[DhcpRelay] %s/%s: ip helper-address %s già presente (idempotente).",
                        relay_router, relay_iface, dhcp_server_ip)
        else:
            logger.info("[DhcpRelay] %s/%s: ip helper-address %s da configurare.",
                        relay_router, relay_iface, dhcp_server_ip)

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Caricamento topologia da Neo4j
# ─────────────────────────────────────────────────────────────────────────────

async def _load_topology(store: AsyncNetworkGraphStore) -> dict:
    """
    Ritorna un dizionario:
      {
        router_name: {
          "interfaces": {iface_name: ip_cidr_string},
        }
      }

    Nota: non usiamo i link CONNECTED_TO perché potrebbero non essere
    aggiornati. Calcoliamo l'adiacenza in Python dalla sovrapposizione
    delle subnet delle interfacce.
    """
    topology: dict = {}
    query = (
        "MATCH (d:Device) WHERE d.status = 'REACHABLE' "
        "AND d.vendor IN ['frrouting', 'cisco_ios'] "
        "OPTIONAL MATCH (d)-[:HAS_INTERFACE]->(i:Interface) "
        "WHERE i.ip_address <> 'unassigned' AND i.ip_address IS NOT NULL "
        "RETURN d.name AS device, "
        "       collect({name: i.name, ip: i.ip_address}) AS ifaces"
    )
    async with store._driver.session() as s:
        res = await s.run(query)
        async for rec in res:
            ifaces = {
                r["name"]: r["ip"]
                for r in rec["ifaces"]
                if r.get("name") and r.get("ip")
            }
            topology[rec["device"]] = {"interfaces": ifaces}
    return topology


async def _load_running_configs(store: AsyncNetworkGraphStore) -> dict[str, str]:
    """Carica i running-config dei router per il check di idempotenza."""
    configs: dict[str, str] = {}
    query = (
        "MATCH (d:Device) WHERE d.running_config IS NOT NULL "
        "RETURN d.name AS device, d.running_config AS rc"
    )
    async with store._driver.session() as s:
        res = await s.run(query)
        async for rec in res:
            configs[rec["device"]] = rec["rc"] or ""
    return configs


# ─────────────────────────────────────────────────────────────────────────────
# BFS e helper topologici
# ─────────────────────────────────────────────────────────────────────────────

def _adjacency(topology: dict) -> dict[str, set[str]]:
    """
    Calcola l'adiacenza tra router dalla sovrapposizione delle subnet.
    Ottimizzato con una hash map per mappare subnet -> router, riducendo la complessità a O(D * I).
    """
    adj: dict[str, set[str]] = {name: set() for name in topology}
    subnet_to_routers: dict[ipaddress.IPv4Network, list[str]] = {}

    for router, data in topology.items():
        for ip_str in data.get("interfaces", {}).values():
            try:
                net = ipaddress.IPv4Interface(ip_str).network
                subnet_to_routers.setdefault(net, []).append(router)
            except ValueError:
                pass

    for routers_list in subnet_to_routers.values():
        unique_routers = list(set(routers_list))
        if len(unique_routers) > 1:
            for i, r1 in enumerate(unique_routers):
                for r2 in unique_routers[i+1:]:
                    adj[r1].add(r2)
                    adj[r2].add(r1)
    return adj


def _bfs_path(start: str, end: str, topology: dict) -> list[str] | None:
    """BFS iterativo. Ritorna il percorso più corto [start, ..., end] o None."""
    adj = _adjacency(topology)
    if start not in adj or end not in adj:
        return None

    queue: deque[list[str]] = deque([[start]])
    visited: set[str] = {start}

    while queue:
        path = queue.popleft()
        current = path[-1]
        if current == end:
            return path
        for neighbor in sorted(adj[current]):   # sorted: risultato deterministico
            if neighbor not in visited:
                visited.add(neighbor)
                queue.append(path + [neighbor])
    return None


def _find_router_for_subnet(
    subnet: ipaddress.IPv4Network, topology: dict
) -> str | None:
    """Trova il router che ha un'interfaccia nella subnet specificata."""
    for router, data in topology.items():
        for ip_str in data["interfaces"].values():
            try:
                if ipaddress.IPv4Interface(ip_str).network == subnet:
                    return router
            except ValueError:
                pass
    return None


def _find_iface_for_subnet(
    router: str, subnet: ipaddress.IPv4Network, topology: dict
) -> str | None:
    """Trova il nome dell'interfaccia del router che si affaccia sulla subnet."""
    ifaces = topology.get(router, {}).get("interfaces", {})
    for iface_name, ip_str in ifaces.items():
        try:
            if ipaddress.IPv4Interface(ip_str).network == subnet:
                return iface_name
        except ValueError:
            pass
    return None


def _find_ip_on_subnet(
    router: str, subnet: ipaddress.IPv4Network | None, topology: dict
) -> str | None:
    """Ritorna il primo IP del router su una subnet specifica (o qualsiasi se subnet=None)."""
    ifaces = topology.get(router, {}).get("interfaces", {})
    for ip_str in ifaces.values():
        try:
            iface = ipaddress.IPv4Interface(ip_str)
            if subnet is None or iface.network == subnet:
                return str(iface.ip)
        except ValueError:
            pass
    return None


def _find_any_ip(router: str, topology: dict) -> str | None:
    """Ritorna il primo IP disponibile del router (qualsiasi interfaccia)."""
    return _find_ip_on_subnet(router, None, topology)


def _find_reachable_ip(
    from_router: str, to_router: str, topology: dict
) -> str | None:
    """
    Ritorna l'IP di to_router sull'interfaccia condivisa con from_router.

    Questo è l'IP corretto da usare come destinazione ip helper-address:
    deve essere raggiungibile direttamente da from_router, non un IP
    su una subnet che from_router non conosce.

    Strategia: cerca coppie di interfacce in cui from_router e to_router
    condividono la stessa subnet, poi restituisce l'IP di to_router su quella subnet.
    """
    from_ifaces = topology.get(from_router, {}).get("interfaces", {})
    to_ifaces   = topology.get(to_router,   {}).get("interfaces", {})

    for ip_from in from_ifaces.values():
        for ip_to in to_ifaces.values():
            try:
                net_from = ipaddress.IPv4Interface(ip_from).network
                net_to   = ipaddress.IPv4Interface(ip_to).network
                if net_from == net_to:
                    return str(ipaddress.IPv4Interface(ip_to).ip)
            except ValueError:
                pass
    # Fallback: qualsiasi IP del to_router (topologia non ancora completa)
    return _find_any_ip(to_router, topology)


def _helper_already_configured(
    running_config: str, iface: str, dhcp_server_ip: str
) -> bool:
    """
    Verifica se `ip helper-address <dhcp_server_ip>` è già configurato
    sull'interfaccia target nel running-config.
    """
    import re
    # Estrai il blocco dell'interfaccia
    m = re.search(
        rf'(?ms)^interface\s+{re.escape(iface)}\s*\n(.*?)(?=^interface\s+|^end\s*$|\Z)',
        running_config,
    )
    if not m:
        return False
    block = m.group(1)
    return f"ip helper-address {dhcp_server_ip}" in block
