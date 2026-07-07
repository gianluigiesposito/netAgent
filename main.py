# main.py
"""
NetAgent v2 — Entry point.

Modalità operative:
  REAL_DEVICES=false (default) → flusso GNS3 identico alla v1
  REAL_DEVICES=true            → flusso dispositivi reali:
                                   1. Bootstrap console (Human-in-the-Loop)
                                   2. Flusso principale via SSH

Argomenti CLI:
  --task   "descrizione dell'intento"
  --spec   path del file .txt IaC (mutualmente esclusivo con --image)
  --image  path del diagramma PNG (Vision Mode)
"""

import argparse
import asyncio
import logging
import uuid
import warnings

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

from core.graph import build_graph
from tools.connection import REAL_DEVICES
from tools.metrics import metrics

from dotenv import load_dotenv

# 1. Carica le variabili dal file .env
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logging.getLogger("langgraph.checkpoint").setLevel(logging.ERROR)
logging.getLogger("neo4j").setLevel(logging.WARNING)
logging.getLogger("ciscoconfparse").setLevel(logging.WARNING)
logging.getLogger("tools.device_snapshot").setLevel(logging.DEBUG)
logging.getLogger("tools.graph_store").setLevel(logging.DEBUG)
warnings.filterwarnings("ignore", message=".*Deserializing unregistered type.*")

try:
    from loguru import logger as loguru_logger
    loguru_logger.disable("ciscoconfparse")
except ImportError:
    pass

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap console (solo REAL_DEVICES=true)
# ─────────────────────────────────────────────────────────────────────────────

async def _run_console_bootstrap_phase(spec_path: str | None) -> dict[str, bool]:
    """
    Fase 0 (solo REAL_DEVICES=true):
    Esegue il bootstrap console dei dispositivi Cisco che richiedono
    la configurazione di base prima di essere raggiungibili via SSH.

    Flusso:
      1. Carica la specifica IaC YAML.
      2. Genera i comandi di base config.
      3. Chiama run_console_bootstrap con Human-in-the-Loop.

    Ritorna un dizionario {device_name: bool} con l'esito per device.
    """
    if not spec_path:
        logger.info("[BOOTSTRAP] Nessuna specifica fornita. Fase bootstrap saltata.")
        return {}

    from pathlib import Path
    import yaml
    from core.state import NetworkIntentSchema
    from tools.console_bootstrap import run_console_bootstrap
    from generate.models.deltas import BaseConfig, DeviceDelta
    from nodes.generate import _compile_delta
    from generate.diff.engine import diff_base_config

    path = Path(spec_path)
    if not path.exists():
        logger.error(f"[BOOTSTRAP] Il file di specifica '{spec_path}' non esiste.")
        return {}

    content = path.read_text(encoding="utf-8")
    
    # Tentativo di parsing come YAML
    intent = None
    try:
        parsed_data = yaml.safe_load(content)
        if isinstance(parsed_data, dict) and "devices" in parsed_data:
            intent = NetworkIntentSchema.model_validate(parsed_data)
            logger.info("[BOOTSTRAP] Specifica YAML caricata per il bootstrap.")
    except Exception as e:
        logger.warning(f"[BOOTSTRAP] Fallito parsing YAML: {e}")

    if not intent:
        logger.warning("[BOOTSTRAP] Impossibile caricare l'intento per il bootstrap (solo formato YAML supportato).")
        return {}

    # Carica l'inventario per i parametri di connessione console
    inventory: dict = {}
    try:
        with open("config/devices.yaml") as f:
            inventory = yaml.safe_load(f) or {}
    except Exception as e:
        logger.error("[BOOTSTRAP] Inventario non leggibile: %s", e)
        return {}

    devices_to_bootstrap: list[tuple[str, dict, list[str]]] = []

    for device in intent.devices:
        name = device.name
        cfg  = inventory.get(name, {})
        vendor = (cfg.get("vendor") or "").lower()

        if vendor not in ("cisco_ios", "cisco_switch"):
            continue

        # Verifica se è richiesta configurazione di base
        if not any([device.hostname, device.banner, device.enable_secret, device.domain_name]):
            continue

        access_ports = []
        for iface in device.interfaces:
            if iface.mode == "access":
                access_ports.append(iface.name)

        desired_base = BaseConfig(
            enabled=True,
            hostname=device.hostname or "",
            banner=device.banner,
            enable_secret=device.enable_secret,
            domain_name=device.domain_name,
            access_ports=access_ports,
        )

        # Compila i comandi di base config (running_config vuoto = tutto MISSING)
        delta = DeviceDelta(router_name=name)
        delta.base_config_delta = diff_base_config(desired_base, "", is_switch=(vendor == "cisco_switch"))

        commands = _compile_delta(delta, vendor_type=vendor)
        base_cmds = [p.cmd for p in commands.pairs if p.cmd.strip()]

        if base_cmds:
            devices_to_bootstrap.append((name, cfg, base_cmds))
            logger.info("[BOOTSTRAP] %s: %d comandi di base config preparati.", name, len(base_cmds))

    if not devices_to_bootstrap:
        logger.info("[BOOTSTRAP] Nessun device Cisco richiede il bootstrap console.")
        return {}

    return await run_console_bootstrap(devices_to_bootstrap)


# ─────────────────────────────────────────────────────────────────────────────
# Manutenzione Database Checkpoint
# ─────────────────────────────────────────────────────────────────────────────

async def prune_old_checkpoints(conn: aiosqlite.Connection, keep_last_n: int = 5) -> None:
    """
    Rimuove i vecchi checkpoint e i relativi log di scrittura associati a thread terminati
    per impedire la crescita indefinita del database netagent_state.db.
    Mantiene solo gli ultimi `keep_last_n` thread attivi.
    """
    try:
        # Recupera i thread_id ordinati per l'attività più recente (max rowid)
        query_threads = """
            SELECT thread_id FROM (
                SELECT thread_id, max(rowid) as r FROM checkpoints GROUP BY thread_id ORDER BY r DESC
            ) LIMIT -1 OFFSET ?
        """
        async with conn.execute(query_threads, (keep_last_n,)) as cursor:
            old_threads = [row[0] for row in await cursor.fetchall() if row[0]]
        
        if old_threads:
            logger = logging.getLogger("main")
            logger.info("Database Maintenance: Pruning %d old threads/runs from netagent_state.db...", len(old_threads))
            
            # Eseguiamo le cancellazioni all'interno di una singola transazione protetta
            await conn.execute("BEGIN TRANSACTION")
            # Elimina prima da 'writes' per questioni di integrità referenziale
            for t_id in old_threads:
                await conn.execute("DELETE FROM writes WHERE thread_id = ?", (t_id,))
                await conn.execute("DELETE FROM checkpoints WHERE thread_id = ?", (t_id,))
            await conn.commit()
            
            # Recupera spazio su disco (fuori dal blocco di transazione)
            await conn.execute("VACUUM")
            logger.info("Database Maintenance: Pruning completed. SQLite database vacuumed.")
    except Exception as e:
        logging.getLogger("main").warning("Database Maintenance warning: Failed to prune old checkpoints: %s", e)


# ─────────────────────────────────────────────────────────────────────────────
# Flusso principale
# ─────────────────────────────────────────────────────────────────────────────

async def run_agent(
    task: str,
    image_path: str | None = None,
    spec_path: str | None = None,
) -> None:
    print("\n=== NETAGENT v2 START ===")
    if REAL_DEVICES:
        print("  Modalità: DISPOSITIVI REALI (SSH + Console Bootstrap)")
    else:
        print("  Modalità: LAB GNS3 (Telnet)")

    # FASE 0: Bootstrap console (solo REAL_DEVICES=true)
    if REAL_DEVICES and spec_path:
        bootstrap_results = await _run_console_bootstrap_phase(spec_path)
        if bootstrap_results:
            failed_bootstrap = [n for n, ok in bootstrap_results.items() if not ok]
            if failed_bootstrap:
                print(f"\n  ⚠️  Bootstrap fallito per: {failed_bootstrap}")
                print("  I device falliti saranno marcati UNREACHABLE nel flusso principale.")

    # FASE 1: Flusso principale LangGraph
    async with aiosqlite.connect("netagent_state.db") as conn:
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA synchronous=NORMAL")

        # Esegue la manutenzione preventiva del database prima di compilare il grafo
        await prune_old_checkpoints(conn, keep_last_n=5)

        checkpointer = AsyncSqliteSaver(conn)
        graph = build_graph().compile(checkpointer=checkpointer)

        thread_id = f"run-{uuid.uuid4().hex[:8]}"
        config = {"configurable": {"thread_id": thread_id}}

        input_state = {
            "user_task":         task,
            "image_path":        image_path,
            "spec_path":         spec_path,
            "specification_raw": "",
            "raw_input":         "",
            "reachability":      {},
            "router_commands":   {},
            "execution_log":     [],
            "final_status":      "UNKNOWN",
        }

        # Inizializzazione metriche
        metrics.start_pipeline()
        metrics.spec_file = spec_path or image_path or "(none)"

        logger.info("Task: '%s' | Thread: %s", task, thread_id)

        async for _ in graph.astream(input_state, config=config, stream_mode="values"):
            pass

        final = await graph.aget_state(config)
        sv    = final.values

        # Raccolta metriche finali dallo state
        plan = sv.get("plan")
        reachability = sv.get("reachability", {})
        if plan and hasattr(plan, "devices"):
            total_devices = len(plan.devices)
            reachable = sum(1 for d in plan.devices if reachability.get(d.name) == "REACHABLE")
            idempotent = 0
            router_cmds = sv.get("executed_commands") or sv.get("router_commands", {})
            for d in plan.devices:
                cmds = router_cmds.get(d.name)
                if cmds and hasattr(cmds, "pairs") and not cmds.pairs:
                    idempotent += 1
            metrics.record_devices(total_devices, reachable, idempotent)

        # Se il troubleshoot è andato a buon fine registriamo il risultato
        ts_attempt = sv.get("troubleshoot_attempt", 0)
        final_status = sv.get("final_status", "UNKNOWN")
        if ts_attempt > 0 and final_status == "SUCCESS":
            metrics.record_troubleshoot(ts_attempt, resolved=True)

        metrics.finalize()

    # ── Report finale ──────────────────────────────────────────────────────
    print("\n── Comandi CLI Applicati ──────────────────────────────────────")
    router_commands = sv.get("executed_commands") or sv.get("router_commands", {})
    if router_commands:
        for device, cmds in router_commands.items():
            print(f"\n  [{device}]")
            if cmds.commands:
                for cmd in cmds.commands:
                    for line in cmd.splitlines():
                        if line.strip():
                            print(f"    > {line}")
            else:
                print("    (Nessuna variazione — Idempotente)")
    else:
        print("  Nessun comando generato.")

    print("\n── Log Workflow ───────────────────────────────────────────────")
    for entry in sv.get("execution_log", []):
        print(f"  {entry}")

    print("\n── Stato Finale ───────────────────────────────────────────────")
    has_failed_execute = any(
        "EXECUTE" in log and "FAILED" in log
        for log in sv.get("execution_log", [])
    )
    status = sv.get("final_status", "UNKNOWN")
    if has_failed_execute:
        status = "FAILED"

    icons = {
        "SUCCESS":              "✅ SUCCESS — Convergenza verificata",
        "FAILED_VERIFICATION":  "❌ FAILED_VERIFICATION — Mismatch post-esecuzione",
        "FAILED":               "⚠️  FAILED — Errore CLI o rollback eseguito",
    }
    print(f"  {icons.get(status, f'🔍 {status}')}")

    # ── Report Metriche ────────────────────────────────────────────────────
    print(metrics.to_markdown())
    try:
        json_path = metrics.save_json()
        print(f"  📊 Report JSON salvato in: {json_path}")
    except Exception as e:
        logger.warning("[METRICS] Impossibile salvare il report JSON: %s", e)
    print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="NetAgent v2")
    parser.add_argument("--task", required=True, help="Intento in linguaggio naturale")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--image", help="Path diagramma di rete (Vision Mode)")
    group.add_argument("--spec",  help="Path specifica IaC .txt")

    args = parser.parse_args()
    asyncio.run(run_agent(task=args.task, image_path=args.image, spec_path=args.spec))
