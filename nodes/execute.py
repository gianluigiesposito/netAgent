# nodes/execute.py
"""
Nodo EXECUTE v3 - Enterprise Grade con Split-Execution (Infra vs Hosts).

Architettura e Fix:
  - Lock per-sessione (Semaphore) per eliminare race conditions su I/O concorrente.
  - Polimorfismo di salvataggio: delega il salvataggio NVRAM (write memory/save) 
    al driver di connessione tramite conn.save_config(), eliminando I/O bloccante.
  - Error detection chirurgica basata su prefissi nativi dei vendor CLI ('% ').
  - Execution separata in due Nodi LangGraph distinti: execute_node (Infra) ed execute_hosts_node (PC),
    garantendo che i client DHCP vengano attivati SOLO dopo che tutto il network (relay compresi) è pronto.
"""

from __future__ import annotations

import re
import asyncio
import logging
import yaml
from tools.parser import load_inventory, normalize_interface_name
from tools.template_engine import parser as output_parser
from core.state import AgentState, RouterCommands, NetworkIntentSchema
from tools.connection import get_connection
from tools.metrics import metrics

logger = logging.getLogger(__name__)

_MAX_CONCURRENT = 10


def _is_host(router_name: str, cfg: dict) -> bool:
    """Classifica in modo robusto se un dispositivo è un PC/Host."""
    if "pc" in router_name.lower():
        return True
    if cfg and cfg.get("vendor", "").lower() == "vpcs":
        return True
    return False


def _device_family(cfg: dict, router_name: str) -> str:
    vendor    = (cfg.get("vendor") or "").lower()
    conn_type = (cfg.get("connection_type") or "").lower()
    if vendor in ("cisco_ios", "cisco_switch") or "cisco" in conn_type:
        return "cisco"
    if vendor == "vpcs" or "vpcs" in conn_type or "pc" in router_name.lower():
        return "vpcs"
    return "frr"


_SYSLOG_RE = re.compile(r'%\s*[a-z0-9_\-]+-[0-7]-[a-z0-9_\-]+:', re.IGNORECASE)


def _is_cli_error(output: str) -> bool:
    lo = (output or "").lower()
    explicit_errors = (
        "% invalid input",
        "% unknown command",
        "% incomplete command",
        "% ambiguous command",
        "% command rejected",
        "% bad ip address",
        "% bad mask",
        "% too many parameters",
        "% mode not valid",
        "command unknown",
        "error:",
        "malformed command",
    )
    if any(m in lo for m in explicit_errors):
        return True

    # Analizza linea per linea per individuare altri errori ed escludere i syslog
    for line in lo.splitlines():
        line = line.strip()
        if not line:
            continue
        line_lower = line.lower()
        # Se c'è un '%' o la riga inizia con 'error'
        if "%" in line or line_lower.startswith("error"):
            # Se è un messaggio di syslog del tipo %LINK-3-UPDOWN: o %SYS-5-CONFIG_I: lo ignoriamo
            if _SYSLOG_RE.search(line):
                continue
            # Consenti esplicitamente i warning e disclaimer del portfast (non considerati errori)
            if "portfast" in line_lower:
                continue
            # Salta messaggi di stato informativi noti per la generazione chiavi e SSH
            if (
                ("generating" in line_lower and "rsa keys" in line_lower)
                or "key modulus" in line_lower
                or "keys will be" in line_lower
                or "ssh-" in line_lower
                or ("ssh" in line_lower and "enabled" in line_lower)
            ):
                continue
            if any(marker in line_lower for marker in ("[ok]", "note:")):
                continue
            # Cisco CLI error format: '% ' followed by error text (always has a space after %)
            percent_idx = line.find("%")
            if percent_idx != -1 and percent_idx < len(line) - 2:
                after_pct = line[percent_idx + 1]
                # Cisco errors: "% Invalid input" — always '% ' (space then word)
                if after_pct == " ":
                    return True
            if line_lower.startswith("error"):
                return True

    return False


def _is_context_drift(output: str) -> bool:
    return "frr:~#" in output or "command unknown" in output.lower()


def _is_exec_prompt(prompt: str) -> bool:
    if not isinstance(prompt, str):
        return False
    p = (prompt or "").strip()
    return bool(re.match(r'^[a-zA-Z0-9_-]+#$', p))


def _is_config_command(cmd: str) -> bool:
    cmd_clean = cmd.strip().lower()
    non_config = (
        "show ", "ping ", "traceroute ", "write ", "copy ", 
        "exit", "end", "configure terminal", "conf t", "terminal "
    )
    return not any(cmd_clean.startswith(prefix) for prefix in non_config)


def _get_expected_context_level(cmd: str) -> str:
    cmd_clean = cmd.strip().lower()
    if cmd_clean in ("configure terminal", "conf t"):
        return "config"
    if cmd_clean.startswith("interface ") or cmd_clean.startswith("int "):
        return "config-if"
    if cmd_clean.startswith("line "):
        return "config-line"
    if cmd_clean.startswith("router "):
        return "config-router"
    if cmd_clean.startswith("vlan ") and not cmd_clean.startswith("vlan database"):
        return "config-vlan"
    if cmd_clean in ("exit", "end"):
        return "exit"
    return "same"


def _get_current_context_level(prompt: str) -> str:
    if not isinstance(prompt, str):
        return "mock"
    p = (prompt or "").strip().lower()
    if "config-if" in p or "config-subif" in p:
        return "config-if"
    if "config-line" in p:
        return "config-line"
    if "config-router" in p:
        return "config-router"
    if "config-vlan" in p:
        return "config-vlan"
    if "config" in p:
        return "config"
    if _is_exec_prompt(prompt) or p.endswith(">"):
        return "exec"
    return "unknown"


async def _ensure_config_mode(conn, family: str, router_name: str) -> bool:
    curr_level = _get_current_context_level(conn.current_prompt)
    if curr_level == "mock":
        return True
    if curr_level == "exec":
        logger.warning("[%s] Context drift: atteso stato config, ma il prompt è in EXEC mode ('%s'). Tento riallineamento con 'configure terminal'...", router_name, conn.current_prompt)
        await conn.send_command("configure terminal")
        await asyncio.sleep(0.3)
        new_level = _get_current_context_level(conn.current_prompt)
        if new_level == "config":
            logger.info("[%s] Riallineamento riuscito. Prompt attuale: '%s'", router_name, conn.current_prompt)
            return True
        else:
            logger.error("[%s] Riallineamento FALLITO. Prompt attuale: '%s'", router_name, conn.current_prompt)
            return False
    return True


async def _recover_cli_context(conn, family: str, cmd: str) -> None:
    if family == "frr":
        # Invia end per uscire da modalità di configurazione vtysh
        await conn.send_command("end")
        await asyncio.sleep(0.2)
        # Se siamo usciti del tutto da vtysh e ci troviamo nella bash shell Linux, rientriamo
        probe = await conn.send_command("")
        if "frr:~#" in probe or "~#" in probe or "~$" in probe:
            await conn.send_command("vtysh")
    elif family == "cisco":
        await conn.send_command("end")
    await asyncio.sleep(0.4)


async def _persist_config(conn, family: str, router_name: str) -> bool:
    if family == "vpcs":
        return True
    saved = await conn.save_config()
    if not saved:
        logger.warning("[%s] save_config: timeout superato o conferma non trovata.", router_name)
    return saved


async def _execute_device(
    router_name: str,
    commands_obj: RouterCommands,
    reachability: dict,
    inventory: dict,
    semaphore: asyncio.Semaphore,
    plan: NetworkIntentSchema | None = None,
) -> tuple[str, bool]:
    if reachability.get(router_name) != "REACHABLE":
        return f"EXECUTE {router_name}: SKIPPED (offline)", True

    cfg = inventory.get(router_name)
    if not cfg:
        return f"EXECUTE {router_name}: FAILED (not in inventory)", False

    pairs = getattr(commands_obj, "pairs", [])
    if not pairs:
        return f"EXECUTE {router_name}: NOOP (idempotent)", True

    family = _device_family(cfg, router_name)

    async with semaphore:
        total_lines = sum(len([l for l in p.cmd.splitlines() if l.strip()]) for p in pairs)
        logger.info("[%s] Connecting to %s:%s — %d commands",
                    router_name, cfg["host"], cfg["port"], total_lines)
        try:
            async with get_connection(cfg) as conn:
                executed_indices: list[int] = []
                line_counter = 0
                interfaces_to_wait = set()
                has_errors = False

                for i, pair in enumerate(pairs):
                    cmd_block = pair.cmd
                    if not cmd_block or not cmd_block.strip():
                        continue

                    cmd_lines = [l.strip() for l in cmd_block.splitlines() if l.strip()]
                    last_cmd_sent = ""
                    current_interface = None

                    for line in cmd_lines:
                        line_counter += 1
                        logger.info("[%s] [%d/%d] %s", router_name, line_counter, total_lines, line)
                        last_cmd_sent = line

                        # Rileva interfaccia corrente nel blocco per no shutdown wait
                        line_stripped = line.strip()
                        if line_stripped.startswith("!sleep ") or line_stripped.startswith("__sleep__ "):
                            parts = line_stripped.split()
                            try:
                                sleep_time = float(parts[1])
                            except (IndexError, ValueError):
                                sleep_time = 2.0
                            logger.info("[%s] Rilevato comando di sleep interno: attesa di %.1f secondi...", router_name, sleep_time)
                            await asyncio.sleep(sleep_time)
                            continue

                        if line_stripped.lower() == "exit" and _is_exec_prompt(conn.current_prompt):
                            logger.info("[%s] Salto 'exit' in EXEC mode per evitare logout.", router_name)
                            continue

                        # Verifica pre-comando per riallineare config mode se in EXEC
                        if _is_config_command(line) and family != "vpcs":
                            success_align = await _ensure_config_mode(conn, family, router_name)
                            if not success_align:
                                logger.error("[%s] Impossibile eseguire '%s': riallineamento config fallito.", router_name, line)
                                has_errors = True
                                continue

                        if line_stripped.lower().startswith("interface "):
                            parts = line_stripped.split()
                            if len(parts) >= 2:
                                current_interface = parts[1]
                        elif line_stripped.lower() == "no shutdown":
                            if current_interface:
                                interfaces_to_wait.add(current_interface)

                        logger.info("[%s] Prompt prima del comando: '%s'", router_name, conn.current_prompt)
                        output = await conn.send_command(line)
                        await asyncio.sleep(0.3)
                        logger.info("[%s] Prompt dopo il comando: '%s'", router_name, conn.current_prompt)

                        # Verifica post-comando per cambi di contesto
                        if family != "vpcs":
                            expected_level = _get_expected_context_level(line)
                            current_level = _get_current_context_level(conn.current_prompt)
                            if current_level != "mock" and expected_level != "same" and expected_level != "exit" and current_level != expected_level:
                                logger.error("[%s] Mismatch di contesto rilevato! Comando '%s' doveva portare a '%s', ma il prompt è '%s' (livello: '%s')",
                                             router_name, line, expected_level, conn.current_prompt, current_level)
                                await _recover_cli_context(conn, family, line)
                                has_errors = True
                                continue

                        if _is_context_drift(output) and family != "vpcs":
                            logger.warning("[%s] Context drift rilevato. Recupero...", router_name)
                            await _recover_cli_context(conn, family, line)
                            has_errors = True
                            continue

                        if _is_cli_error(output):
                            logger.error("[%s] CLI error su '%s' [prompt: '%s']: %s", router_name, line, conn.current_prompt, output.strip())
                            await _recover_cli_context(conn, family, line)
                            has_errors = True
                            continue

                    executed_indices.append(i)

                if has_errors:
                    metrics.record_commands(router_name, total_lines, line_counter, 1)
                    return f"EXECUTE {router_name}: FAILED (CLI errors encountered)", False

                # Attesa convergenza per le interfacce che hanno ricevuto no shutdown
                if interfaces_to_wait and family in ("cisco", "frr"):
                    await _recover_cli_context(conn, family, "")
                    logger.info("[%s] Rilevato 'no shutdown' su: %s. Avvio procedura di attesa convergenza.", router_name, list(interfaces_to_wait))

                    # Determiniamo se il dispositivo è uno switch Cisco (L2) o un router (L3)
                    is_switch = (
                        cfg.get("vendor", "").lower() == "cisco_switch" 
                        or "sw" in router_name.lower()
                    )

                    if is_switch:
                        # Per gli switch L2, verifichiamo lo stato dello Spanning Tree (STP)
                        # sulle interfacce target, attendendo che tutte le VLAN attive siano in Forwarding (FWD).
                        # Usiamo controlli sequenziali a 5, 15 e 30 secondi (max 50s totali).
                        # Filtriamo le interfacce per verificare solo quelle di Livello 2 (fisiche o Port-Channels).
                        # Le interfacce logiche/L3 come le SVI (es: Vlan99) o Loopback non partecipano a Spanning Tree.
                        l2_interfaces = [
                            iface for iface in interfaces_to_wait
                            if not (iface.lower().startswith("vlan") or "." in iface or iface.lower().startswith("loopback"))
                        ]

                        # Se abbiamo un piano intent, arricchiamo l'attesa con le interfacce L2 dichiarate nell'intento per lo switch
                        if plan and hasattr(plan, "devices") and plan.devices:
                            device_plan = next((d for d in plan.devices if d.name == router_name), None)
                            if device_plan and device_plan.interfaces:
                                for iface in device_plan.interfaces:
                                    iface_name = iface.name
                                    if not (iface_name.lower().startswith("vlan") or "." in iface_name or iface_name.lower().startswith("loopback")):
                                        if iface_name not in l2_interfaces:
                                            l2_interfaces.append(iface_name)

                        if not l2_interfaces:
                            logger.info("[%s] Nessuna interfaccia L2 rilevata per il controllo Spanning Tree. Skip attesa STP.", router_name)
                        else:
                            delays = [5.0, 15.0, 30.0]
                            total_wait = 0.0

                            for step, delay in enumerate(delays + [0.0]):
                                all_fwd = True

                                for iface in l2_interfaces:
                                    cmd_stp = f"show spanning-tree interface {iface}"
                                    logger.info("[%s] Verifica STP - Esecuzione comando: '%s'", router_name, cmd_stp)
                                    stp_out = await conn.send_command(cmd_stp)
                                    logger.info("[%s] Output di '%s':\n%s", router_name, cmd_stp, stp_out.strip())

                                    # Parsiamo l'output del comando STP riga per riga per identificare
                                    # lo stato di ciascuna istanza VLAN associata all'interfaccia.
                                    vlan_lines = []
                                    for line in stp_out.splitlines():
                                        line_stripped = line.strip()
                                        if not line_stripped:
                                            continue
                                        parts = line_stripped.split()
                                        # La riga STP per VLAN inizia tipicamente con "VLAN" seguita da cifre (es. VLAN0010)
                                        if len(parts) >= 3 and re.match(r'^VLAN\d+', parts[0], re.IGNORECASE):
                                            vlan_lines.append(parts)

                                    if not vlan_lines:
                                        logger.info("[%s] Interfaccia %s: Spanning Tree non ancora attivo o nessuna VLAN rilevata.", router_name, iface)
                                        all_fwd = False
                                        break

                                    for parts in vlan_lines:
                                        vlan_name = parts[0]
                                        sts = parts[2].upper() # Il terzo campo (indice 2) rappresenta lo stato (Sts)
                                        if sts != "FWD":
                                            logger.info("[%s] Interfaccia %s - %s: stato STP attuale '%s' (atteso FWD).", router_name, iface, vlan_name, sts)
                                            all_fwd = False
                                            break

                                    if not all_fwd:
                                        break

                                if all_fwd:
                                    logger.info("[%s] Spanning Tree in stato Forwarding (FWD) su tutte le porte configurate dopo %.1f secondi.", router_name, total_wait)
                                    break

                                if delay == 0.0:
                                    logger.warning("[%s] Timeout convergenza STP: alcune porte non sono in stato FWD dopo %.1f secondi.", router_name, total_wait)
                                    break

                                logger.info("[%s] Convergenza STP non completata. Attesa di %.1fs (tempo totale trascorso: %.1fs)...", router_name, delay, total_wait)
                                await asyncio.sleep(delay)
                                total_wait += delay
                    else:
                        # Per i router L3 (e.g. R1, FRRouting), attendiamo un tempo fisso di 30 secondi
                        # per consentire al protocollo di linea e ad eventuali protocolli di routing
                        # di convergere stabilmente.
                        logger.info("[%s] Dispositivo L3 rilevato. Attesa fissa di 30 secondi per la convergenza delle porte...", router_name)
                        await asyncio.sleep(30.0)

                saved = await _persist_config(conn, family, router_name)
                if not saved:
                    metrics.record_commands(router_name, total_lines, line_counter, 0)
                    return f"EXECUTE {router_name}: SAVE_WARN (config applied, persist uncertain)", True

                metrics.record_commands(router_name, total_lines, line_counter, 0)
                return f"EXECUTE {router_name}: SUCCESS", True

        except Exception as e:
            logger.exception("[%s] Eccezione fatale: %s", router_name, e)
            metrics.record_commands(router_name, total_lines, line_counter if 'line_counter' in dir() else 0, 1)
            return f"EXECUTE {router_name}: FATAL ({e})", False


# ─────────────────────────────────────────────────────────────────────────────
# NODO 1: Esegue SOLO l'Infrastruttura L2/L3
# ─────────────────────────────────────────────────────────────────────────────
async def execute_node(state: AgentState) -> dict:
    logger.info(">>> EXECUTE (INFRA) <<<")

    router_commands = state.get("router_commands", {})
    reachability    = state.get("reachability", {})
    inventory       = load_inventory()
    semaphore       = asyncio.Semaphore(_MAX_CONCURRENT)

    # Estrae solo i dispositivi di infrastruttura
    infra = {n: c for n, c in router_commands.items() if not _is_host(n, inventory.get(n, {}))}

    log: list[str] = []
    success = True
    
    # Clona il dizionario dei comandi per consumare solo quelli eseguiti
    cleared_cmds = router_commands.copy()
    executed_commands = (state.get("executed_commands") or {}).copy()

    if state.get("test_troubleshoot_skip_execute"):
        logger.info("[EXECUTE] TEST_TROUBLESHOOT active and approval rejected. Skipping core infra execution to simulate failure.")
        for name, cmds in infra.items():
            if cmds.pairs:
                log.append(f"EXECUTE {name}: SKIPPED (TEST_TROUBLESHOOT active)")
                cleared_cmds[name] = RouterCommands(pairs=[])
                executed_commands[name] = cmds
            else:
                log.append(f"EXECUTE {name}: NOOP (idempotent)")
                cleared_cmds[name] = RouterCommands(pairs=[])
    elif infra:
        logger.info("[EXECUTE] Fase 1: configurazione infrastruttura core...")
        tasks = []
        task_names = []
        
        for name, cmds in infra.items():
            if not cmds.pairs:
                log.append(f"EXECUTE {name}: NOOP (idempotent)")
                cleared_cmds[name] = RouterCommands(pairs=[])
            else:
                tasks.append(_execute_device(name, cmds, reachability, inventory, semaphore, plan=state.get("plan")))
                task_names.append(name)
                executed_commands[name] = cmds
        
        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for name, res in zip(task_names, results):
                cleared_cmds[name] = RouterCommands(pairs=[])  # Consuma il comando eseguito
                if isinstance(res, Exception):
                    log.append(f"EXECUTE {name}: CRITICAL EXCEPTION — {res}")
                    success = False
                else:
                    msg, ok = res
                    log.append(msg)
                    if not ok:
                        success = False

    status = "SUCCESS" if success else "FAILED"
    
    return {
        "execution_log": log, 
        "final_status": status,
        "router_commands": cleared_cmds,
        "executed_commands": executed_commands
    }


# ─────────────────────────────────────────────────────────────────────────────
# NODO 2: Esegue SOLO i VPCS / Host terminali
# ─────────────────────────────────────────────────────────────────────────────
async def execute_hosts_node(state: AgentState) -> dict:
    logger.info(">>> EXECUTE_HOSTS <<<")

    router_commands = state.get("router_commands", {})
    reachability    = state.get("reachability", {})
    inventory       = load_inventory()
    semaphore       = asyncio.Semaphore(_MAX_CONCURRENT)

    # Estrae solo i dispositivi terminali
    hosts = {n: c for n, c in router_commands.items() if _is_host(n, inventory.get(n, {}))}

    log: list[str] = []
    cleared_cmds = router_commands.copy()
    executed_commands = (state.get("executed_commands") or {}).copy()
    
    # Leggiamo lo status dell'esecuzione infrastrutturale appena completata
    success = state.get("final_status", "SUCCESS") == "SUCCESS"

    if state.get("test_troubleshoot_skip_execute"):
        logger.info("[EXECUTE_HOSTS] TEST_TROUBLESHOOT active. Skipping hosts execution to simulate failure.")
        for name, cmds in hosts.items():
            if cmds.pairs:
                log.append(f"EXECUTE {name}: SKIPPED (TEST_TROUBLESHOOT active)")
                cleared_cmds[name] = RouterCommands(pairs=[])
                executed_commands[name] = cmds
            else:
                log.append(f"EXECUTE {name}: NOOP (idempotent)")
                cleared_cmds[name] = RouterCommands(pairs=[])
    elif success and hosts:
        hosts_to_execute = {}
        for name, cmds in hosts.items():
            if not cmds.pairs:
                log.append(f"EXECUTE {name}: NOOP (idempotent)")
                cleared_cmds[name] = RouterCommands(pairs=[])
            else:
                hosts_to_execute[name] = cmds

        if hosts_to_execute:
            logger.info("[EXECUTE_HOSTS] Attesa 3s per convergenza L2 (STP) e DHCP Relay...")
            await asyncio.sleep(3)
            logger.info("[EXECUTE_HOSTS] Fase 2: attivazione client terminali...")
            
            # Backup per eventuale rollback
            for name, cmds in hosts_to_execute.items():
                executed_commands[name] = cmds

            async def _execute_with_delay(idx, name, cmds):
                delay = idx * 0.2
                if delay > 0:
                    await asyncio.sleep(delay)
                return await _execute_device(name, cmds, reachability, inventory, semaphore, plan=state.get("plan"))

            tasks = [
                _execute_with_delay(i, name, cmds)
                for i, (name, cmds) in enumerate(hosts_to_execute.items())
            ]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for name, res in zip(hosts_to_execute.keys(), results):
                cleared_cmds[name] = RouterCommands(pairs=[])  # Consuma il comando eseguito
                if isinstance(res, Exception):
                    log.append(f"EXECUTE {name}: EXCEPTION — {res}")
                    success = False
                else:
                    msg, ok = res
                    log.append(msg)
                    if not ok:
                        success = False
    elif not success and hosts:
        logger.warning("[EXECUTE_HOSTS] Skip esecuzione host a causa di errori nell'infrastruttura.")
        for name in hosts.keys():
            log.append(f"EXECUTE {name}: SKIPPED (Infra failed)")
            cleared_cmds[name] = RouterCommands(pairs=[])  # Consuma per evitare loop infiniti in caso di Troubleshoot

    status = "SUCCESS" if success else "FAILED"
    logger.info(">>> EXECUTE_HOSTS done: %s <<<", status)
    
    return {
        "execution_log": log, 
        "final_status": status,
        "router_commands": cleared_cmds,
        "executed_commands": executed_commands
    }