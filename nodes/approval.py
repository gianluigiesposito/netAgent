# nodes/approval.py
"""
Nodo APPROVAL — Sicurezza Operativa (HITL).
Genera un report testuale in stile "terraform plan" e sospende l'esecuzione
in attesa di approvazione umana (y/N) se DEPLOY_MODE=human-in-the-loop.
"""

import logging
import os
import sys
import select
from core.state import AgentState

logger = logging.getLogger(__name__)


async def approval_node(state: AgentState) -> dict:
    logger.info(">>> APPROVAL NODE <<<")

    router_commands = state.get("router_commands", {})
    if not router_commands:
        logger.info("[APPROVAL] Nessun comando da applicare.")
        return {
            "execution_log": ["APPROVAL: Nessun comando da applicare. Approvazione automatica."]
        }

    attempt = state.get("troubleshoot_attempt", 0)

    # 1. Generazione e stampa del report in stile "terraform plan"
    print("\n" + "=" * 72)
    if attempt > 0:
        print(f"               NETAGENT TROUBLESHOOT PLAN (ATTEMPT #{attempt})")
    else:
        print("                       NETAGENT DEPLOY PLAN")
    print("=" * 72)
    if attempt > 0:
        print("The following corrective changes will be applied to the network:")
    else:
        print("The following changes will be applied to the network:")

    total_added = 0
    total_removed = 0

    for device, router_cmds in router_commands.items():
        if not router_cmds.pairs:
            continue
        print(f"\n  [{device}]")
        for pair in router_cmds.pairs:
            for line in pair.cmd.splitlines():
                line_stripped = line.strip()
                if not line_stripped:
                    continue
                if line_stripped.lower().startswith("no ") or line_stripped.lower().startswith("no\t"):
                    # Rosso per rimozione
                    print(f"    \033[91m- {line_stripped}\033[0m")
                    total_removed += 1
                else:
                    # Verde per aggiunta
                    print(f"    \033[92m+ {line_stripped}\033[0m")
                    total_added += 1

    print("\n" + "=" * 72)
    print(f"Plan: {total_added} to add, {total_removed} to remove.")
    print("=" * 72 + "\n")

    # 2. Controllo modalità di deploy
    deploy_mode = os.getenv("DEPLOY_MODE", "automated").lower()
    if deploy_mode != "human-in-the-loop":
        logger.info(f"[APPROVAL] DEPLOY_MODE='{deploy_mode}', approvazione automatica.")
        return {
            "execution_log": ["APPROVAL: Approvazione automatica (modalità non interattiva)."]
        }

    # 3. Controllo presenza TTY interattivo
    if not sys.stdin.isatty():
        logger.error("[APPROVAL] Rilevato ambiente non interattivo (assenza TTY) in modalità human-in-the-loop!")
        raise RuntimeError(
            "CRITICAL CONFORMITY ERROR: DEPLOY_MODE is set to 'human-in-the-loop' but no interactive TTY "
            "was detected. Aborting execution to prevent unauthorized or silent deployment."
        )

    # 4. Prompt interattivo con timeout di 5 minuti (300 secondi)
    print("\033[93mApprovare le modifiche configurative sopra elencate? (y/N): \033[0m", end="", flush=True)
    
    # Usiamo select per attendere l'input con un timeout
    rlist, _, _ = select.select([sys.stdin], [], [], 300)
    if not rlist:
        print("\n\033[91m[APPROVAL] Timeout: Nessuna risposta ricevuta entro 5 minuti. Not Approved.\033[0m")
        raise RuntimeError("CRITICAL: Timeout waiting for operator approval in human-in-the-loop mode.")

    response = sys.stdin.readline().strip().lower()

    if response in ("y", "yes"):
        print("\033[92mModifiche approvate dall'operatore. Procedo con il deploy...\033[0m\n")
        return {
            "execution_log": ["APPROVAL: Modifiche approvate dall'operatore."]
        }
    else:
        if attempt == 0 and os.getenv("TEST_TROUBLESHOOT", "false").lower() == "true":
            print("\033[93m[TEST_TROUBLESHOOT] Modifiche respinte dall'operatore. Procedo simulando il fallimento della configurazione...\033[0m\n")
            return {
                "execution_log": ["APPROVAL: Modifiche respinte (TEST_TROUBLESHOOT attivo, simulazione fallimento)."],
                "test_troubleshoot_skip_execute": True
            }
        print("\033[91mModifiche NON approvate. Interrompo il deploy in modo sicuro.\033[0m\n")
        raise RuntimeError("CRITICAL: Deployment aborted by operator decision.")
