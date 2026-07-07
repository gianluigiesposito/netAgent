# nodes/troubleshoot.py
"""
Nodo TROUBLESHOOT v1.

Responsabilità:
  1. Raccogliere uno snapshot live ESCLUSIVAMENTE dei dispositivi
     falliti e dei loro vicini di transito (hop L3 sul path verso
     la destinazione non raggiunta).
  2. Costruire un contesto diagnostico strutturato: stato desiderato
     (dal piano), stato attuale (snapshot Neo4j + running-config live),
     execution log dell'ultimo ciclo.
  3. Inviare il contesto all'LLM chiedendo comandi di fix chirurgici.
  4. Produrre RouterCommands pronti per EXECUTE oppure, se i tentativi
     sono esauriti, produrre un diagnostic_report Markdown e segnalare
     final_status = "TROUBLESHOOT_EXHAUSTED".

Flusso nel grafo:
  VERIFY → TROUBLESHOOT → EXECUTE → VERIFY   (retry 1)
                        → EXECUTE → VERIFY   (retry 2, se ancora fallisce)
                        → DIAGNOSTIC_REPORT  (se troubleshoot_attempt >= MAX)

Limiti:
  MAX_ATTEMPTS = 2  (configurabile via costante).
  Ogni tentativo aumenta troubleshoot_attempt di 1 prima di agire,
  in modo che il router post-VERIFY possa decidere se riprovare o fermarsi.
"""

from __future__ import annotations

import asyncio
import logging
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from core.state import AgentState, RouterCommands, CommandPair
from database.neo4j_queries import TroubleshootRepository
from tools.graph_store import AsyncNetworkGraphStore
from tools.template_engine import parser as output_parser
from llm.async_client import llm_client
from tools.parser import load_inventory
from tools.device_snapshot import live_snapshot_for_diagnostics
from tools.metrics import metrics
from tools.vector_store import LocalVectorStore

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 3

# Blacklist di comandi CLI che l'LLM non può generare per sicurezza
_DANGEROUS_COMMANDS = frozenset([
    "write erase", "erase startup-config", "erase nvram:",
    "reload", "no enable secret", "no enable password",
    "format", "delete flash:", "squeeze",
    "no username", "no aaa",
])

_FLAT_NETWORK_FORBIDDEN_COMMAND_RE = (
    "vlan ",
    "switchport mode",
    "switchport access vlan",
    "switchport trunk",
    "encapsulation dot1q",
)

def _is_safe_command(cmd: str) -> bool:
    """Verifica che un comando CLI non sia nella blacklist di sicurezza."""
    cmd_lower = cmd.strip().lower()
    return not any(cmd_lower.startswith(d) for d in _DANGEROUS_COMMANDS)


def _extract_conceptual_keywords(failed_devices: list[str], execution_log: list[str]) -> str:
    """
    Analizza i log di errore e i dispositivi falliti per estrarre parole chiave concettuali.
    Mappa i log di errore reali a concetti della Knowledge Base (STP, VLAN, DHCP Relay, LACP, OSPF).
    """
    keywords = set()
    log_text = "\n".join(execution_log).lower()

    # Regole di mappatura semantica
    if any(k in log_text for k in ("vlan", "trunk", "access", "allowed vlan", "allowed-vlan", "native vlan", "native-vlan", "native_vlan")):
        keywords.add("vlan")
        keywords.add("trunk")
        keywords.add("switchport")

    if any(k in log_text for k in ("spanning tree", "spanning-tree", "spantree", "portfast", "bpduguard", "bpdu-guard", "stp", "blocking", "blocked", "block", "blk", "listening", "learning")):
        keywords.add("spanning-tree")
        keywords.add("portfast")
        keywords.add("bpduguard")

    if any(k in log_text for k in ("etherchannel", "lacp", "port-channel", "portchannel", "channel-group", "channel group", "bundle", "bundled")):
        keywords.add("etherchannel")
        keywords.add("lacp")
        keywords.add("port-channel")

    if any(k in log_text for k in ("dhcp", "helper", "helper-address", "helper_address", "dhcp relay", "ip dhcp")):
        keywords.add("dhcp")
        keywords.add("helper-address")
        keywords.add("dhcp relay")

    if any(k in log_text for k in ("ospf", "area", "router ospf", "adjacency", "neighbor")):
        keywords.add("ospf")
        keywords.add("routing")
        keywords.add("dynamic routing")

    if any(k in log_text for k in ("route", "gateway", "static route", "next-hop", "ip route", "timeout", "lost", "loss", "ping", "unreachable")):
        keywords.add("static route")
        keywords.add("ip route")
        keywords.add("gateway")

    # Fallback basato sui dispositivi
    if not keywords:
        # Se abbiamo switch o router tra i failed devices, proviamo a includere concetti di base
        if any("sw" in fd.lower() for fd in failed_devices):
            keywords.update(["vlan", "spanning-tree"])
        if any("r" in fd.lower() for fd in failed_devices):
            keywords.update(["static route", "ospf"])

    query = " ".join(sorted(keywords))
    logger.info("[TROUBLESHOOT] Parole chiave estratte dai log per la query KB: '%s'", query)
    return query


def _plan_is_flat_network(plan) -> bool:
    """True se il desired state non contiene VLAN custom, trunk/access o subinterface."""
    devices = getattr(plan, "devices", None)
    if not devices:
        return False
    for dev in devices:
        if getattr(dev, "vlans", None):
            return False
        for iface in getattr(dev, "interfaces", []) or []:
            if getattr(iface, "mode", None) == "trunk":
                return False
            if getattr(iface, "trunk_vlans", None):
                return False
            access_vlan = getattr(iface, "access_vlan", None)
            if access_vlan not in (None, 1):
                return False
            if getattr(iface, "vlan_id", None):
                return False
            if "." in getattr(iface, "name", ""):
                return False
    return True


def _is_flat_network_l2_command(cmd: str) -> bool:
    stripped = cmd.strip().lower()
    return any(stripped.startswith(prefix) for prefix in _FLAT_NETWORK_FORBIDDEN_COMMAND_RE)

# I/O database e snapshot di rete delegati rispettivamente a TroubleshootRepository e live_snapshot_for_diagnostics.



# ─────────────────────────────────────────────────────────────────────────────
# Costruzione contesto LLM
# ─────────────────────────────────────────────────────────────────────────────

def _build_desired_state(plan, failed_devices: list[str]) -> str:
    """
    Estrae dal piano solo i RouterIntent/DeviceIntent dei dispositivi falliti e di transito.
    """
    if not plan:
        return "Piano non disponibile."

    lines = ["=== DESIRED STATE (dal piano) ==="]
    if _plan_is_flat_network(plan):
        lines.extend([
            "NETWORK_MODE: FLAT_NO_VLAN",
            "Regola: nessuna VLAN custom, nessun trunk, nessuna subinterface dot1q nel desired state.",
        ])
    if getattr(plan, "devices", None):
        for dev in plan.devices:
            if dev.name in failed_devices:
                lines.append(f"\n[DEVICE: {dev.name}]")
                iface_names = [iface.name for iface in dev.interfaces]
                lines.append(f"Interfaces: {', '.join(iface_names)}")
                lines.append("Intent:")
                if dev.extra_params:
                    for line in dev.extra_params.splitlines():
                        if line.strip():
                            lines.append(f"  {line}")
        return "\n".join(lines)
    elif getattr(plan, "router_plans", None):
        for rp in plan.router_plans:
            if rp.router_name in failed_devices:
                lines.append(f"\n[DEVICE: {rp.router_name}]")
                lines.append(f"Interfaces: {', '.join(rp.interfaces)}")
                lines.append("Intent:")
                if rp.extra_params:
                    for line in rp.extra_params.splitlines():
                        if line.strip():
                            lines.append(f"  {line}")
        return "\n".join(lines)

    return "Piano non disponibile o in formato non riconosciuto."


def _build_actual_state(snapshots: dict[str, dict]) -> str:
    """
    Formatta gli snapshot live in un blocco leggibile dall'LLM, applicando
    il pruning delle interfacce inattive/shutdown (Strategy A) e convertendo la
    running-config in un formato strutturato YAML compatto (Strategy C).
    """
    lines = ["=== ACTUAL STATE (snapshot live) ==="]
    for device, snap in snapshots.items():
        lines.append(f"\n[DEVICE: {device}]")
        if snap["error"]:
            lines.append(f"  STATUS: UNREACHABLE — {snap['error']}")
            continue
        lines.append("  STATUS: REACHABLE")
        if snap["interfaces"]:
            lines.append("  --- show interface brief ---")
            for iline in snap["interfaces"].splitlines():
                if iline.strip():
                    # Pruning: escludi le interfacce non assegnate e spente
                    iline_lower = iline.lower()
                    if "unassigned" in iline_lower and ("down" in iline_lower or "administratively down" in iline_lower):
                        continue
                    lines.append(f"  {iline}")
        if snap.get("operational_status"):
            lines.append("  --- show operational status ---")
            for oline in snap["operational_status"].splitlines():
                lines.append(f"  {oline}")
        if snap["running_config"]:
            lines.append("  --- running-config (YAML representation) ---")
            try:
                from ciscoconfparse import CiscoConfParse
                import yaml

                parse = CiscoConfParse(snap["running_config"].splitlines(), factory=False)
                dev_dict = {}

                # Hostname
                hn_objs = parse.find_lines(r"^hostname\s+")
                if hn_objs:
                    dev_dict["hostname"] = hn_objs[0].split()[1]

                # Rotte statiche
                route_objs = parse.find_lines(r"^ip route\s+")
                if route_objs:
                    routes = []
                    for r in route_objs:
                        parts = r.split()
                        if len(parts) >= 5:
                            dest_ip = parts[2]
                            dest_mask = parts[3]
                            next_hop = parts[4]
                            try:
                                from ipaddress import IPv4Network
                                prefix = IPv4Network(f"0.0.0.0/{dest_mask}").prefixlen
                                routes.append({"network": f"{dest_ip}/{prefix}", "next_hop": next_hop})
                            except Exception:
                                routes.append({"network": f"{dest_ip} {dest_mask}", "next_hop": next_hop})
                    if routes:
                        dev_dict["static_routes"] = routes

                # Routing Dinamico (OSPF, ecc.)
                router_objs = parse.find_objects(r"^router\s+")
                if router_objs:
                    routers = {}
                    for robj in router_objs:
                        name = robj.text.strip()
                        children = [c.text.strip() for c in robj.children]
                        routers[name] = children
                    dev_dict["dynamic_routing"] = routers

                # DHCP pools e configurazioni
                dhcp_objs = parse.find_objects(r"^ip dhcp\s+")
                if dhcp_objs:
                    dhcp_configs = {}
                    for dobj in dhcp_objs:
                        name = dobj.text.strip()
                        children = [c.text.strip() for c in dobj.children]
                        dhcp_configs[name] = children
                    dev_dict["dhcp_config"] = dhcp_configs

                # VLAN database logico
                vlan_objs = parse.find_objects(r"^vlan\s+")
                if vlan_objs:
                    vlans = {}
                    for vobj in vlan_objs:
                        parts = vobj.text.split()
                        if len(parts) >= 2 and parts[1].isdigit():
                            v_id = int(parts[1])
                            v_name = ""
                            for child in vobj.children:
                                if child.text.strip().lower().startswith("name "):
                                    v_name = child.text.split()[1]
                            vlans[v_id] = v_name
                    if vlans:
                        dev_dict["vlan_database"] = vlans

                # Interfacce (Strategy A + Strategy C - Preservazione di comandi come spanning-tree)
                interfaces = {}
                for obj in parse.find_objects(r"^interface\s+"):
                    iface_name = obj.text.split()[1]
                    if iface_name.lower().startswith("null"):
                        continue

                    has_shutdown = False
                    ip_address = None
                    other_configs = []

                    for child in obj.children:
                        text_stripped = child.text.strip()
                        text_lower = text_stripped.lower()

                        if text_lower.startswith("shutdown"):
                            has_shutdown = True
                        elif text_lower.startswith("ip address"):
                            parts = text_stripped.split()
                            if len(parts) >= 4:
                                ip = parts[2]
                                mask = parts[3]
                                try:
                                    from ipaddress import IPv4Network
                                    prefix = IPv4Network(f"0.0.0.0/{mask}").prefixlen
                                    ip_address = f"{ip}/{prefix}"
                                except Exception:
                                    ip_address = f"{ip} {mask}"
                            else:
                                other_configs.append(text_stripped)
                        else:
                            other_configs.append(text_stripped)

                    # Strategy A: Skip if interface is down and has no configuration
                    if has_shutdown and not (ip_address or other_configs):
                        continue

                    iface_dict = {}
                    if has_shutdown:
                        iface_dict["status"] = "shutdown"
                    if ip_address:
                        iface_dict["ip"] = ip_address
                    if other_configs:
                        iface_dict["config"] = other_configs

                    interfaces[iface_name] = iface_dict

                if interfaces:
                    dev_dict["interfaces"] = interfaces

                # Conversione in YAML serializzato
                yaml_text = yaml.dump(dev_dict, default_flow_style=False, sort_keys=False)
                for yline in yaml_text.splitlines():
                    lines.append(f"  {yline}")

            except Exception as ex:
                # Fallback resiliente in caso di errore inatteso nel parsing CiscoConfParse/YAML
                logger.warning("[TROUBLESHOOT] Errore CiscoConfParse/YAML: %s. Uso fallback regex.", ex)
                relevant_sections = []
                in_section = False
                section_keywords = (
                    "interface ", "ip route", "ip dhcp", "vlan ", "switchport",
                    "router ", "hostname", "ip address",
                )
                for rline in snap["running_config"].splitlines():
                    stripped = rline.strip()
                    if any(stripped.lower().startswith(k) for k in section_keywords):
                        in_section = True
                    elif stripped.startswith("!") or (stripped and not rline.startswith(" ")):
                        in_section = False
                    if in_section or any(stripped.lower().startswith(k) for k in section_keywords):
                        relevant_sections.append(f"  {rline}")
                lines.extend(relevant_sections[:500])
                if len(relevant_sections) > 500:
                    lines.append(f"  ... (troncato, {len(relevant_sections) - 500} righe omesse)")
    return "\n".join(lines)


def _build_execution_log_summary(execution_log: list[str]) -> str:
    """Filtra il log tenendo solo le righe di errore/warning dell'ultimo ciclo."""
    lines = ["=== EXECUTION LOG (ultimo ciclo) ==="]
    error_keywords = ("FAILED", "FATAL", "EXCEPTION", "SAVE_WARN", "CRITICAL", "UNREACHABLE")
    for entry in (execution_log or []):
        if any(k in entry.upper() for k in error_keywords):
            lines.append(f"  {entry}")
    if len(lines) == 1:
        lines.append("  (nessun errore esplicito nel log)")
    return "\n".join(lines)


def _build_flat_network_guard(flat_network: bool) -> str:
    if not flat_network:
        return ""
    return """\
=== FLAT NETWORK GUARDRAIL ===
Il piano desiderato e' una rete piatta senza VLAN custom.
Non generare comandi VLAN/switchport/trunk/native-vlan/subinterface.
Correggi solo IP fisici, default gateway host/switch gestiti, no ip routing sugli switch L2 quando serve, rotte statiche e shutdown/no shutdown.
"""


# ─────────────────────────────────────────────────────────────────────────────
# Prompt LLM per il fix
# ─────────────────────────────────────────────────────────────────────────────

_TROUBLESHOOT_SYSTEM_PROMPT = """\
Sei un esperto di networking (Cisco IOS, FRRouting, VPCS) che analizza un fallimento \
di configurazione automatica e produce comandi CLI di correzione.

LINEE GUIDA DIAGNOSTICHE E PROCEDURE DI FIX OPERATIVI:
1. EtherChannel / LACP Sospeso o Inattivo:
   Se l'output di `show etherchannel summary` indica che le porte fisiche sono sospese (stato 's' o 'suspended') o che il Port-channel è inattivo (SD o D):
   - Assicurati che i parametri switchport delle porte fisiche (ad es. `switchport mode trunk` e `switchport trunk encapsulation dot1q` o le VLAN permesse) coincidano esattamente con quelli del rispettivo Port-channel.
   - Per riattivare/forzare la negoziazione LACP, usa una sequenza combinata di `shutdown` e `no shutdown` sia sull'interfaccia Port-channel logica sia sulle relative interfacce fisiche.
2. Stato di Spanning Tree (STP) bloccante:
   Se un'interfaccia si trova nello stato di blocco STP (BLK/blocking) o manca uno stato di forwarding per una VLAN:
   - Configura `spanning-tree portfast` o `spanning-tree portfast trunk` sulle porte connesse ai PC (access port) per evitare il ritardo di convergenza iniziale di 30 secondi.
   - Controlla ed elimina eventuali Native VLAN mismatch o incompatibilità di link trunking adiacenti.
3. DHCP Relay e connettività Client DHCP:
   Se un PC fallisce la ricezione dell'IP tramite DHCP:
   - Controlla la presenza e il corretto indirizzamento di `ip helper-address` sulle subinterface/interfacce del router che fungono da default gateway.
   - Assicurati che il routing statico sia presente e corretto tra il router e il server DHCP.

Regole:
1. Analizza il gap tra DESIRED STATE e ACTUAL STATE.
2. Produci SOLO i comandi strettamente necessari a colmare il gap.
3. Per ogni dispositivo elenca i comandi in ordine di esecuzione, raggruppati per device.
4. Non ripetere comandi già corretti nell'ACTUAL STATE.
5. DIAGNOSTICA OLISTICA (COERENZA DELLA TOPOLOGIA): Se un host finale (es. PC) non ottiene un IP o fallisce la raggiungibilità, non suggerire solo correzioni locali parziali. Analizza e risolvi l'intera catena di connettività, ma sempre nel RIGIDO RISPETTO DELLA TOPOLOGIA DESIDERATA (DESIRED STATE):
   - Configurazione dell'interfaccia dell'host (IP corretto e default gateway).
   - Configurazione dello switch a cui è collegato: adegua la porta solo se coerente con lo stato desiderato (es. abilita 'spanning-tree portfast' su porte d'accesso, ma NON inventare o configurare VLAN custom se non sono esplicitamente definite nello stato desiderato).
   - Configurazione del router gateway: configura l'IP sull'interfaccia fisica o sulla subinterfaccia seguendo esclusivamente quanto indicato nello stato desiderato (non creare subinterfacce L3 arbitrariamente). Configura 'ip helper-address' solo se il DHCP server è remoto rispetto alla subnet dell'host.
   - Tabelle di routing tra tutti i router coinvolti (routing del traffico e rotte di ritorno).
   Assicurati di includere comandi correttivi per tutti i dispositivi interessati nel medesimo tentativo.
6. SPECULARITÀ E SICUREZZA DEI COMANDI DI ROLLBACK:
   Per ciascun comando 'cmd' suggerito, devi obbligatoriamente definire un comando 'rollback' che sia l'esatto opposto logico sul dispositivo (es. 'switchport access vlan 10' -> 'no switchport access vlan', 'spanning-tree portfast' -> 'no spanning-tree portfast'). Il rollback non deve MAI essere vuoto o approssimativo, altrimenti l'apparato potrebbe rimanere bloccato o isolato.
7. COERENZA RETI PIATTE (NO ALLUCINAZIONI VLAN):
   Se il DESIRED STATE non definisce alcuna VLAN custom (es. la sezione 'vlans' è vuota o assente, e le interfacce non hanno parametri come 'access_vlan' o 'trunk_vlans'), significa che la rete è piatta (Flat L3) e opera interamente sulla VLAN 1 di default.
   In questo scenario è un errore gravissimo (allucinazione) inventare VLAN (come VLAN 10 o 20 basandosi sulle subnet IP 192.168.10.x/192.168.20.x) o creare subinterfacce (es. Ethernet0/0.10). Attieniti strettamente all'uso della VLAN 1 e delle interfacce fisiche.
   Per gli switch L2 in una rete piatta, se risultano irraggiungibili da subnet esterne, la causa risiede esclusivamente nella mancanza del default gateway e nell'abilitazione del routing L3. Risolvi configurando 'ip default-gateway <ip_gateway>' e disabilitando il routing con il comando 'no ip routing'.
9. DIVIETO ASSOLUTO DI RIMOZIONE DI INDIRIZZI IP O CONFIGURAZIONI: Non eliminare o disabilitare MAI interfacce SVI, indirizzi IP o rotte statiche che sono definiti nel DESIRED STATE. Non accorciare la matrice dei ping rimuovendo IP per far passare la validazione. Tutti gli IP della specifica desiderata devono rimanere configurati e raggiungibili.
10. Formato output OBBLIGATORIO — rispondi SOLO con questo JSON, nessun testo fuori:

{
  "analysis": "Breve diagnosi (max 3 frasi)",
  "fixes": [
    {
      "device": "NOME_DEVICE",
      "vendor": "cisco_ios|cisco_switch|frrouting|vpcs",
      "commands": [
        {"cmd": "configure terminal", "rollback": "exit"},
        {"cmd": "interface Ethernet0/1.50", "rollback": "no interface Ethernet0/1.50"},
        ...
      ]
    }
  ]
}

"""


async def _ask_llm_for_fix(
    desired_state: str,
    actual_state: str,
    execution_log_summary: str,
    attempt: int,
    graph_topology: str = "",
    kb_content: str = "",
    flat_network: bool = False,
) -> Optional[dict]:
    """
    Chiama il client LLM con il contesto diagnostico completo.
    Ritorna il JSON parsato o None in caso di errore.
    """
    graph_section = f"\n\n{graph_topology}" if graph_topology else ""
    kb_section = f"\n\n{kb_content}" if kb_content else ""
    flat_section = f"\n\n{_build_flat_network_guard(flat_network)}" if flat_network else ""
    user_prompt = (
        f"TROUBLESHOOTING ATTEMPT #{attempt}\n\n"
        f"{desired_state}\n\n"
        f"{actual_state}{graph_section}{kb_section}{flat_section}\n\n"
        f"{execution_log_summary}\n\n"
        "Analizza il problema e produci i comandi di fix nel formato JSON richiesto."
    )

    try:
        raw = await llm_client.raw_completion(
            system_prompt=_TROUBLESHOOT_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            caller="troubleshoot",
        )
        import json, re
        # Estrai il JSON anche se l'LLM lo avvolge in ```json ... ```
        # Usa ricerca bilanciata per trovare il primo oggetto JSON completo
        depth = 0
        start_idx = None
        for i, ch in enumerate(raw):
            if ch == '{':
                if depth == 0:
                    start_idx = i
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0 and start_idx is not None:
                    try:
                        return json.loads(raw[start_idx:i + 1])
                    except json.JSONDecodeError:
                        continue
        logger.error("[TROUBLESHOOT] LLM non ha restituito JSON valido.")
        return None
    except Exception as e:
        logger.error("[TROUBLESHOOT] Errore chiamata LLM: %s", e, exc_info=True)
        return None


def _fix_to_router_commands(fix_data: dict, flat_network: bool = False) -> dict[str, RouterCommands]:
    """
    Converte la risposta JSON dell'LLM in {device_name: RouterCommands}.
    """
    result: dict[str, RouterCommands] = {}
    for fix in fix_data.get("fixes", []):
        device = fix.get("device", "")
        if not device:
            continue
        pairs = []
        for c in fix.get("commands", []):
            cmd_text = c.get("cmd", "").strip()
            if not cmd_text:
                continue
            if not _is_safe_command(cmd_text):
                logger.warning(
                    "[TROUBLESHOOT] ⛔ Comando bloccato dalla blacklist di sicurezza: '%s' su %s",
                    cmd_text, device,
                )
                continue
            if flat_network and _is_flat_network_l2_command(cmd_text):
                logger.warning(
                    "[TROUBLESHOOT] Comando L2/VLAN scartato in rete piatta: '%s' su %s",
                    cmd_text, device,
                )
                continue
            pairs.append(CommandPair(
                cmd=cmd_text,
                rollback=c.get("rollback", ""),
            ))
        if pairs:
            result[device] = RouterCommands(pairs=pairs)
            logger.info(
                "[TROUBLESHOOT] Fix per %s: %d comandi.", device, len(pairs)
            )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Report diagnostico finale
# ─────────────────────────────────────────────────────────────────────────────

def _build_diagnostic_report(
    failed_devices: list[str],
    desired_state: str,
    actual_state: str,
    execution_log: list[str],
    attempts: int,
    last_analysis: str,
) -> str:
    """
    Produce un report Markdown strutturato per l'operatore.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines = [
        "# NetAgent — Diagnostic Report",
        f"**Timestamp:** {ts}",
        f"**Tentativi di fix:** {attempts}/{MAX_ATTEMPTS}",
        f"**Dispositivi coinvolti:** {', '.join(failed_devices)}",
        "",
        "## Diagnosi LLM",
        last_analysis or "Non disponibile.",
        "",
        "## Stato Desiderato",
        "```",
        desired_state,
        "```",
        "",
        "## Stato Attuale (ultimo snapshot)",
        "```",
        actual_state,
        "```",
        "",
        "## Execution Log (errori)",
        "```",
        _build_execution_log_summary(execution_log),
        "```",
        "",
        "## Azioni Suggerite per l'Operatore",
        "1. Verificare fisicamente la connettività dei dispositivi elencati.",
        "2. Controllare il running-config manualmente via console.",
        "3. Verificare eventuali conflitti di VLAN o subinterface.",
        "4. Se il problema persiste, eseguire un rollback manuale e "
           "rieseguire NetAgent con una specifica semplificata.",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────────────────────────────────────
# Nodo LangGraph
# ─────────────────────────────────────────────────────────────────────────────

async def _execute_rollback(state: AgentState, failed_devices: list[str], inventory: dict) -> list[str]:
    from nodes.verify import _rollback_device
    log_lines = []
    intent = state.get("intent")
    rollback_scope = "all"
    if intent and hasattr(intent, "rollback_scope"):
        rollback_scope = intent.rollback_scope

    executed_commands = state.get("executed_commands") or {}
    log_lines.append(f"[ROLLBACK] Avvio rollback condizionale (scope={rollback_scope})...")
    
    if rollback_scope == "device-only":
        for dev in failed_devices:
            if dev in executed_commands:
                ok = await _rollback_device(dev, executed_commands[dev], inventory)
                log_lines.append(f"[ROLLBACK] Rollback {dev}: {'SUCCESS' if ok else 'FAILED'}")
    else: # "all"
        for dev in reversed(list(executed_commands.keys())):
            ok = await _rollback_device(dev, executed_commands[dev], inventory)
            log_lines.append(f"[ROLLBACK] Rollback {dev}: {'SUCCESS' if ok else 'FAILED'}")
            
    return log_lines


async def troubleshoot_node(state: AgentState) -> dict:
    logger.info(">>> TROUBLESHOOT <<<")

    failed_devices: list[str] = state.get("failed_devices", [])
    attempt: int = state.get("troubleshoot_attempt", 0) + 1
    plan = state.get("plan")
    execution_log: list[str] = state.get("execution_log", [])

    logger.info(
        "[TROUBLESHOOT] Tentativo %d/%d — dispositivi falliti: %s",
        attempt, MAX_ATTEMPTS, failed_devices,
    )

    if not failed_devices:
        logger.warning("[TROUBLESHOOT] Nessun dispositivo fallito segnalato. Uscita.")
        return {
            "troubleshoot_attempt": attempt,
            "router_commands": {},
            "execution_log": ["TROUBLESHOOT: nessun dispositivo fallito, skip."],
        }

    # 1. Trova dispositivi di transito via GraphRAG + topologia
    async with AsyncNetworkGraphStore() as store:
        repo = TroubleshootRepository(store._driver)
        scope = await repo.collect_transit_devices(failed_devices)

        # 3b. Topologia multi-layer dal grafo (L1 cavi + L2 VLAN + L3 routing).
        # Aggiunge contesto strutturale che lo snapshot live non fornisce.
        try:
            graph_topology = await repo.collect_graph_topology(scope)
        except Exception as _e:
            graph_topology = f"(topologia grafo non disponibile: {_e})"

    # 2. Snapshot live parallelo (solo scope)
    inventory = load_inventory()
    tasks = {
        name: live_snapshot_for_diagnostics(name, inventory.get(name, {}))
        for name in scope
        if name in inventory
    }
    snapshots_list = await asyncio.gather(*tasks.values(), return_exceptions=True)
    snapshots: dict[str, dict] = {}
    for name, result in zip(tasks.keys(), snapshots_list):
        if isinstance(result, Exception):
            snapshots[name] = {"running_config": "", "interfaces": "", "error": str(result)}
        else:
            snapshots[name] = result

    # 3. Contesto strutturato
    desired_state        = _build_desired_state(plan, scope)
    actual_state         = _build_actual_state(snapshots)
    execution_log_summary = _build_execution_log_summary(execution_log)
    flat_network = _plan_is_flat_network(plan)

    # 3.5 Estrazione concetti KB con Vector Store (GraphRAG Fase 2)
    kb_content = ""
    try:
        vector_store = LocalVectorStore()
        query_keywords = _extract_conceptual_keywords(failed_devices, execution_log)
        if query_keywords:
            kb_results = await vector_store.search(query_keywords, top_k=2)
            if kb_results:
                kb_lines = ["=== KNOWLEDGE BASE REFERENCE ==="]
                for idx, res in enumerate(kb_results):
                    kb_lines.append(f"\n[Riferimento #{idx+1}: {res['metadata']['title']} (Score: {res['score']:.2f})]")
                    kb_lines.append(res['text'])
                kb_content = "\n".join(kb_lines)
    except Exception as e:
        logger.error("[TROUBLESHOOT] Errore nel recupero della Knowledge Base: %s", e)

    # 4. LLM fix
    fix_data = await _ask_llm_for_fix(
        desired_state, actual_state, execution_log_summary, attempt,
        graph_topology=graph_topology,
        kb_content=kb_content,
        flat_network=flat_network,
    )

    last_analysis = fix_data.get("analysis", "") if fix_data else ""

    # 5a. Tentativi esauriti → diagnostic report
    if attempt >= MAX_ATTEMPTS and (not fix_data or not fix_data.get("fixes")):
        logger.warning("[TROUBLESHOOT] Tentativi esauriti. Produco diagnostic report.")
        report = _build_diagnostic_report(
            failed_devices=failed_devices,
            desired_state=desired_state,
            actual_state=actual_state,
            execution_log=execution_log,
            attempts=attempt,
            last_analysis=last_analysis,
        )
        rollback_logs = await _execute_rollback(state, failed_devices, inventory)
        metrics.record_troubleshoot(attempt, resolved=False)
        return {
            "troubleshoot_attempt": attempt,
            "diagnostic_report": report,
            "final_status": "TROUBLESHOOT_EXHAUSTED",
            "router_commands": {},
            "execution_log": [
                f"TROUBLESHOOT attempt {attempt}: tentativi esauriti. Report generato."
            ] + rollback_logs,
        }

    # 5b. Fix disponibile → prepara RouterCommands per EXECUTE
    if not fix_data or not fix_data.get("fixes"):
        logger.warning("[TROUBLESHOOT] LLM non ha prodotto fix. Produco diagnostic report.")
        report = _build_diagnostic_report(
            failed_devices=failed_devices,
            desired_state=desired_state,
            actual_state=actual_state,
            execution_log=execution_log,
            attempts=attempt,
            last_analysis=last_analysis,
        )
        rollback_logs = await _execute_rollback(state, failed_devices, inventory)
        metrics.record_troubleshoot(attempt, resolved=False)
        return {
            "troubleshoot_attempt": attempt,
            "diagnostic_report": report,
            "final_status": "TROUBLESHOOT_EXHAUSTED",
            "router_commands": {},
            "execution_log": [
                f"TROUBLESHOOT attempt {attempt}: LLM non ha prodotto fix. Report generato."
            ] + rollback_logs,
        }

    fix_commands = _fix_to_router_commands(fix_data, flat_network=flat_network)
    logger.info(
        "[TROUBLESHOOT] Fix generato per %d dispositivi: %s",
        len(fix_commands), list(fix_commands.keys()),
    )

    return {
        "troubleshoot_attempt": attempt,
        "router_commands": fix_commands,
        "execution_log": [
            f"TROUBLESHOOT attempt {attempt}: fix per {list(fix_commands.keys())} — {last_analysis}"
        ],
    }
