# tools/dhcp_config.py
"""
DHCP Engine per FRRouting e Cisco IOS — Standard Industriale Network-OS Compliant.

Architettura decisionale:
  Gestisce la mappatura dichiarativa del blocco 'ip dhcp pool' per ambienti multi-vendor,
  garantendo un'estrazione e un diff iper-resiliente al Name-Case e ai caratteri Telnet.
  Include un sistema esteso di logging diagnostico per il tracciamento del diff.
"""

from __future__ import annotations

import re
import logging
import ipaddress
from dataclasses import dataclass, field
from typing import Optional, Literal

from core.state import RouterCommands, CommandPair

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# 1. MODELLI DICHIARATIVI
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class DhcpPoolConfig:
    """
    Descrizione dichiarativa di un pool DHCP su FRRouting / Cisco IOS.
    Mappa 1:1 sul blocco 'ip dhcp pool' del dialetto Cisco-IOS-like.
    """
    pool_name: str                         
    network: str                           
    prefix_len: int                        
    default_router: str                    
    dns_server: str = "8.8.8.8"
    lease_days: int = 1
    excluded_start: Optional[str] = None  
    excluded_end: Optional[str] = None    

    @property
    def network_cidr(self) -> str:
        return f"{self.network}/{self.prefix_len}"

    @property
    def netmask(self) -> str:
        return str(ipaddress.IPv4Network(self.network_cidr, strict=False).netmask)


@dataclass
class DhcpStateDelta:
    """Risultato della comparazione desired-state vs actual-state per DHCP."""
    pool_name: str
    action_needed: Literal["CORRECT", "MISSING", "WRONG"]
    desired: Optional[DhcpPoolConfig] = None
    diff_notes: list[str] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# 2. ISPEZIONE DELLO STATO REALE CON DEBUG STRUTTURATO
# ─────────────────────────────────────────────────────────────────────────────

class DhcpStateInspector:
    """Legge la configurazione DHCP attiva dal router via running-config."""

    _POOL_BLOCK_RE  = re.compile(r'ip dhcp pool (\S+)\s*\n(.*?)(?=\n\S|\n!|\Z)', re.DOTALL)
    _NETWORK_RE     = re.compile(r'network\s+([\d\.]+)\s+([\d\.]+)')
    _ROUTER_RE      = re.compile(r'default-router\s+([\d\.]+)')
    _DNS_RE         = re.compile(r'dns-server\s+([\d\.]+)')
    _LEASE_RE       = re.compile(r'lease\s+(\d+)')

    def parse_running_config(self, raw: str) -> dict[str, dict]:
        pools: dict[str, dict] = {}
        if not raw:
            return pools

        matches = list(self._POOL_BLOCK_RE.finditer(raw))
        
        for m in matches:
            name = m.group(1).strip().lower()
            block = m.group(2)

            # Estrazione flessibile (Cisco o CIDR nativo)
            net_m = re.search(r'network\s+([\d\.]+)(?:\s+|/)([\d\.]+)', block)
            router_m = re.search(r'default-router\s+([\d\.]+)', block)
            dns_m = re.search(r'dns-server\s+([\d\.]+)', block)
            lease_m = re.search(r'lease\s+(\d+)', block)

            extracted_net = net_m.group(1).strip() if net_m else None
            extracted_mask = net_m.group(2).strip() if net_m else None

            # Normalizzazione in formato mask decimale per compatibilità diff
            if extracted_mask and extracted_mask.isdigit():
                try:
                    extracted_mask = str(ipaddress.IPv4Network(f"0.0.0.0/{extracted_mask}").netmask)
                except ValueError:
                    pass

            pools[name] = {
                "network": extracted_net,
                "netmask": extracted_mask,
                "router": router_m.group(1).strip() if router_m else None,
                "dns": dns_m.group(1).strip() if dns_m else "8.8.8.8",
                "lease": int(lease_m.group(1)) if lease_m else 1,
            }
            
            logger.debug(f"[DHCP] Parsato pool '{name}': net={extracted_net}, mask={extracted_mask}")

        return pools

    def diff(self, desired: DhcpPoolConfig, running_config_raw: str) -> DhcpStateDelta:
        logger.info(f"[DEBUG-DHCP] >>> Avvio controllo di Diff DHCP per il pool richiesto: '{desired.pool_name}' <<<")
        
        pools = self.parse_running_config(running_config_raw)
        lookup_name = desired.pool_name.strip().lower()
        logger.info(f"[DEBUG-DHCP] Verifica presenza chiave: Cerco '{lookup_name}' all'interno di {list(pools.keys())}")

        if lookup_name not in pools:
            logger.error(f"[DEBUG-DHCP] Idempotenza FALLITA: Il pool '{desired.pool_name}' NON esiste. Stato = MISSING")
            return DhcpStateDelta(pool_name=desired.pool_name, action_needed="MISSING", desired=desired)

        current = pools[lookup_name]
        notes: list[str] = []

        try:
            desired_net = ipaddress.IPv4Network(desired.network_cidr, strict=False)
            current_net_str = f"{current['network']}/{current['netmask']}"
            current_net = ipaddress.IPv4Network(current_net_str, strict=False)
            
            if desired_net != current_net:
                notes.append(f"network: {current_net} -> {desired_net}")
        except Exception as ex:
            logger.error(f"[DEBUG-DHCP][{desired.pool_name}] Errore parsing ipaddress network: {ex}")

        desired_gw = desired.default_router.strip()
        current_gw = current.get("router")
        if current_gw and current_gw != desired_gw:
            notes.append(f"router: {current_gw} -> {desired_gw}")

        desired_dns = desired.dns_server.strip()
        current_dns = current.get("dns")
        if current_dns and current_dns != desired_dns:
            notes.append(f"dns: {current_dns} -> {desired_dns}")

        if notes:
            return DhcpStateDelta(pool_name=desired.pool_name, action_needed="WRONG", desired=desired, diff_notes=notes)

        return DhcpStateDelta(pool_name=desired.pool_name, action_needed="CORRECT")


# ─────────────────────────────────────────────────────────────────────────────
# 3. COMPILAZIONE COMANDI CLI
# ─────────────────────────────────────────────────────────────────────────────

class DhcpCommandCompiler:
    """Produce RouterCommands (pairs cmd/rollback) per la configurazione DHCP."""

    def compile_add(self, cfg: DhcpPoolConfig) -> RouterCommands:
        print(f"[DEBUG-DHCP] Compilazione comandi per aggiunta pool '{cfg.pool_name}' con network '{cfg.network_cidr}'")
        # 1. Entriamo in Global Config per primo e una sola volta
        pairs: list[CommandPair] = [
            CommandPair(cmd="configure terminal", rollback="exit")
        ]

        # 2. Comandi Global Config (esclusioni IP)
        if cfg.excluded_start:
            end = cfg.excluded_end or cfg.excluded_start
            pairs.append(CommandPair(
                cmd=f"ip dhcp excluded-address {cfg.excluded_start} {end}",
                rollback=f"no ip dhcp excluded-address {cfg.excluded_start} {end}",
            ))

        # 3. Creazione del pool e comandi di sub-configurazione
        pairs += [
            CommandPair(cmd=f"ip dhcp pool {cfg.pool_name}", rollback=f"no ip dhcp pool {cfg.pool_name}"),
            CommandPair(cmd=f"network {cfg.network} {cfg.netmask}", rollback=f"no network {cfg.network} {cfg.netmask}"),
            CommandPair(cmd=f"default-router {cfg.default_router}", rollback=f"no default-router {cfg.default_router}"),
            CommandPair(cmd=f"dns-server {cfg.dns_server}", rollback=f"no dns-server {cfg.dns_server}"),
            CommandPair(cmd=f"lease {cfg.lease_days}", rollback="no lease"),
            # Usciamo dalla modalità DHCP pool
            CommandPair(cmd="exit", rollback=f"ip dhcp pool {cfg.pool_name}"),
            # Torniamo diretti al prompt Privileged EXEC senza doppi exit
            CommandPair(cmd="end", rollback=""),
            # Salviamo
            CommandPair(cmd="write memory", rollback=""),
        ]
        return RouterCommands(pairs=pairs)

    def compile_replace(self, cfg: DhcpPoolConfig) -> RouterCommands:
        pairs = [
            CommandPair(cmd="configure terminal", rollback="exit"),
            CommandPair(cmd=f"no ip dhcp pool {cfg.pool_name}", rollback=f"ip dhcp pool {cfg.pool_name}"),
            CommandPair(cmd="exit", rollback="configure terminal"),
        ]
        add_cmds = self.compile_add(cfg)
        pairs += add_cmds.pairs
        return RouterCommands(pairs=pairs)


# ─────────────────────────────────────────────────────────────────────────────
# 4. ENTRY POINT PUBBLICO E PARSER RESILIENTE AD ALTO LIVELLO
# ─────────────────────────────────────────────────────────────────────────────

_inspector = DhcpStateInspector()
_compiler  = DhcpCommandCompiler()

def diff_dhcp(desired: DhcpPoolConfig, running_config_raw: str) -> DhcpStateDelta:
    return _inspector.diff(desired, running_config_raw)

def compile_dhcp_delta(delta: DhcpStateDelta) -> RouterCommands:
    if delta.action_needed == "CORRECT":
        return RouterCommands(pairs=[])
    if delta.action_needed == "MISSING":
        return _compiler.compile_add(delta.desired)
    if delta.action_needed == "WRONG":
        return _compiler.compile_replace(delta.desired)
    return RouterCommands(pairs=[])

# tools/dhcp_config.py (Sostituisci la funzione in fondo al file)

def parse_dhcp_intents_from_text(extra_params: str) -> list[DhcpPoolConfig]:
    """
    Parser iper-resiliente multi-vendor ad alto livello.
    Supporta la dichiarazione di MULTIPLI pool DHCP estraendo i blocchi.
    """
    pools = []
    
    # Dividiamo il testo in blocchi usando DHCP_POOL_NAME o 'ip dhcp pool' come delimitatore
    # (Manteniamo il delimitatore nel testo grazie alla lookahead assertion (?=...))
    blocks = re.split(r'(?i)(?=DHCP_POOL_NAME|ip\s+dhcp\s+pool)', extra_params)
    
    for block in blocks:
        text = block.replace("\n", " ").replace("|", " ").strip()
        if not text:
            continue
            
        # Rilevamento Nome Pool (Se non c'è il nome, non è un blocco DHCP valido)
        pool_m = re.search(r'(?:DHCP_POOL_NAME|pool|POOL)[:\s]+(\S+)', text, re.IGNORECASE)
        if not pool_m:
            continue
            
        pool_name = pool_m.group(1).strip().rstrip('|,')

        # Rilevamento Subnet
        net_m = re.search(r'(?:DHCP_NETWORK|network|net)[:\s]+([\d\.]+)(?:/(\d{1,2})|\s+([255\d\.]+))?', text, re.IGNORECASE)
        if not net_m:
            continue

        network_addr = net_m.group(1).strip()
        prefix_len = 24

        if net_m.group(2):
            prefix_len = int(net_m.group(2))
        elif net_m.group(3):
            try:
                prefix_len = ipaddress.IPv4Network(f"0.0.0.0/{net_m.group(3).strip()}").prefixlen
            except ValueError:
                pass

        try:
            parsed = ipaddress.IPv4Network(f"{network_addr}/{prefix_len}", strict=False)
            network_addr = str(parsed.network_address)
        except ValueError:
            continue

        # Rilevamento Default Router
        gw_m = re.search(r'(?:DHCP_ROUTER|default-router|gateway|gw|via)[:\s]+([\d\.]+)', text, re.IGNORECASE)
        default_router = gw_m.group(1).strip() if gw_m else str(list(parsed.hosts())[0])

        # Rilevamento DNS
        dns_m = re.search(r'(?:DHCP_DNS|dns-server|dns)[:\s-]+([\d\.]+)', text, re.IGNORECASE)
        dns_server = dns_m.group(1).strip() if dns_m else "8.8.8.8"

        # Rilevamento Lease
        lease_m = re.search(r'\b(?:DHCP_LEASE|lease)[:\s]+(\d+)\b', text, re.IGNORECASE)
        lease_days = int(lease_m.group(1)) if lease_m else 1

        # Rilevamento Indirizzi Esclusi
        excl_start, excl_end = None, None
        excl_m = re.search(r'(?:DHCP_EXCLUDED|excluded)[:\s]+([\d\.]+)\s*([\d\.]+)?', text, re.IGNORECASE)
        if excl_m:
            excl_start = excl_m.group(1).strip()
            if excl_m.group(2):
                excl_end = excl_m.group(2).strip()
        else:
            excl_start = default_router
            excl_end = default_router

        pools.append(DhcpPoolConfig(
            pool_name=pool_name,
            network=network_addr,
            prefix_len=prefix_len,
            default_router=default_router,
            dns_server=dns_server,
            lease_days=lease_days,
            excluded_start=excl_start,
            excluded_end=excl_end
        ))

    return pools
