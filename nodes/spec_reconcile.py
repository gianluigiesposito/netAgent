# nodes/spec_reconcile.py
"""
Nodo SPEC_RECONCILE.

Responsabilità:
  Dopo un ciclo TROUBLESHOOT → EXECUTE → VERIFY che ha raggiunto SUCCESS,
  aggiorna la specifica YAML originale per riflettere le correzioni applicate.

  In questo modo la spec diventa la fonte di verità dello stato reale della rete
  e il problema non si ripresenterà nei run successivi.

Filosofia di design:
  - Zero side-effect se il troubleshoot non era necessario (troubleshoot_attempt == 0).
  - Il modello LLM riceve solo il delta minimo: spec originale + comandi applicati +
    diagnosi del troubleshooter. Non riscrive l'intera spec.
  - Il merge viene eseguito dal motore Python (merge_specifications), non dall'LLM.
    L'LLM produce solo il frammento parziale (Merge Patch).
  - Device-loss guard: se il patch elimina dispositivi, viene scartato e loggato.
  - Scrittura atomica via file temporaneo. Se qualcosa va storto il file originale
    rimane intatto.
  - Il nodo non blocca mai il flusso: qualsiasi errore viene loggato e il nodo
    ritorna spec_reconcile_status: "SKIPPED" o "FAILED" senza interrompere il grafo.

Quando viene chiamato:
  Il router LangGraph chiama questo nodo solo quando:
    final_status == "SUCCESS" AND troubleshoot_attempt > 0

Flusso nel grafo:
  VERIFY (SUCCESS, troubleshoot_attempt > 0) → SPEC_RECONCILE → END
  VERIFY (SUCCESS, troubleshoot_attempt == 0) → END              (bypass)
  VERIFY (FAILED, esauriti)                  → END              (bypass, spec non toccata)
"""

from __future__ import annotations

import copy
import logging
import re
import shutil
import yaml
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# System prompt dedicato al reconcile (compatto, zero ridondanze)
# ─────────────────────────────────────────────────────────────────────────────

_RECONCILE_SYSTEM_PROMPT = """\
Sei un assistente di rete. Il tuo compito è produrre un frammento YAML parziale \
(Merge Patch) che aggiorni una specifica NetworkIntentSchema per riflettere \
le correzioni applicate durante un troubleshooting riuscito.

VINCOLI ASSOLUTI:
1. Emetti ESCLUSIVAMENTE le chiavi modificate — mai l'intera specifica.
2. Ogni modifica nel frammento deve corrispondere a un comando CLI nel log.
   Non inventare configurazioni non presenti nel log.
3. Non rimuovere dispositivi o interfacce esistenti.
   Usa `delete: true` solo se un elemento è stato esplicitamente rimosso nel log.
4. Se un comando CLI non ha un mapping diretto nello schema YAML
   (es. comandi operativi come `shutdown`/`no shutdown` per LACP),
   omettilo dal frammento — non forzare una mappatura inesistente.

Formato di output obbligatorio — nient'altro oltre a questo:
<<<SPEC_START>>>
devices:
  - name: <NomeDispositivo>
    <solo i campi modificati>
<<<SPEC_END>>>

Dopo il frammento, una riga per ogni modifica effettiva nel formato:
CHANGE: <dispositivo> | <campo_yaml> | <vecchio_valore> → <nuovo_valore> | <motivo>
"""


# ─────────────────────────────────────────────────────────────────────────────
# Costruzione del contesto per l'LLM
# ─────────────────────────────────────────────────────────────────────────────

def _build_applied_commands_summary(state: dict) -> str:
    """
    Estrae dal log di esecuzione i comandi CLI effettivamente applicati con successo,
    raggruppati per dispositivo.

    Legge execution_log cercando pattern come:
      "EXECUTE <device>: SUCCESS"
    e accoppia ogni SUCCESS con i comandi del router_commands corrispondente.
    """
    lines = ["=== COMANDI CLI APPLICATI (troubleshoot riuscito) ==="]

    # executed_commands contiene {device: RouterCommands} dei comandi dell'ultimo ciclo
    executed_commands: dict = state.get("executed_commands") or {}

    # Includi anche l'analisi dell'LLM del troubleshooter (breve diagnosi)
    execution_log: list[str] = state.get("execution_log") or []
    diagnosis_lines = [
        l for l in execution_log
        if l.startswith("TROUBLESHOOT attempt") and "—" in l
    ]
    if diagnosis_lines:
        lines.append("\nDiagnosi del troubleshooter:")
        for d in diagnosis_lines:
            lines.append(f"  {d}")

    if not executed_commands:
        # Fallback: estrai i comandi direttamente dall'execution_log
        lines.append("\nComandi estratti dal log:")
        for entry in execution_log:
            if "EXECUTE" in entry and "SUCCESS" in entry:
                lines.append(f"  {entry}")
        return "\n".join(lines)

    lines.append("")
    for device, router_cmds in executed_commands.items():
        pairs = getattr(router_cmds, "pairs", [])
        if not pairs:
            continue
        lines.append(f"  [{device}]")
        for pair in pairs:
            cmd = getattr(pair, "cmd", "").strip()
            if cmd and cmd not in ("configure terminal", "exit", "end", "write memory"):
                lines.append(f"    > {cmd}")

    return "\n".join(lines)


def _build_reconcile_prompt(spec_content: str, state: dict) -> str:
    """
    Costruisce il messaggio utente per l'LLM di reconcile.
    Mantiene il contesto al minimo: spec + comandi applicati.
    Non passa running-config né snapshot live — non servono per il reconcile.
    """
    applied = _build_applied_commands_summary(state)

    return (
        f"STATO_SPEC_ORIGINALE:\n"
        f"<<<CURRENT_SPEC_START>>>\n{spec_content}\n<<<CURRENT_SPEC_END>>>\n\n"
        f"{applied}\n\n"
        "Produci il frammento YAML parziale (Merge Patch) che aggiorna la specifica "
        "per renderla coerente con lo stato reale della rete dopo il troubleshoot."
    )


# ─────────────────────────────────────────────────────────────────────────────
# Estrazione e merge del patch
# ─────────────────────────────────────────────────────────────────────────────

def _extract_patch(text: str) -> Optional[str]:
    """
    Estrae il frammento YAML in modo estremamente robusto:
    1. Cerca i tag <<<SPEC_START>>> e <<<SPEC_END>>> (case-insensitive).
    2. Rimuove eventuali block marker markdown (es. ```yaml ... ```) presenti all'interno.
    3. Se non trova i tag, cerca un blocco di codice markdown generico ```yaml o ```.
    4. Se ancora non lo trova, tenta di ritornare il testo intero se sembra YAML.
    """
    if not text:
        return None

    # 1. Ricerca tag principali (case-insensitive)
    m = re.search(r'(?i)<<<SPEC_START>>>\s*(.*?)\s*<<<SPEC_END>>>', text, re.DOTALL)
    patch_text = m.group(1).strip() if m else None

    # 2. Se non trova i tag, cerca blocchi markdown ```yaml ... ```
    if not patch_text:
        m_md = re.search(r'```(?:yaml)?\s*(.*?)\s*```', text, re.DOTALL | re.IGNORECASE)
        if m_md:
            patch_text = m_md.group(1).strip()

    # 3. Fallback: usa l'intero testo se contiene devices
    if not patch_text:
        if "devices:" in text:
            patch_text = text.strip()
        else:
            return None

    # Pulizia di markdown residui dentro il blocco estratto
    if patch_text.startswith("```"):
        lines = patch_text.splitlines()
        if lines and "```" in lines[0]:
            lines = lines[1:]
        if lines and "```" in lines[-1]:
            lines = lines[:-1]
        patch_text = "\n".join(lines).strip()

    return patch_text


from core.utils import (
    deep_merge_dicts as _deep_merge_dicts,
    merge_list_by_key as _merge_list_by_key,
    merge_specifications as _apply_patch,
    validate_no_device_loss as _validate_no_device_loss,
)


# ─────────────────────────────────────────────────────────────────────────────
# Scrittura atomica
# ─────────────────────────────────────────────────────────────────────────────

def _atomic_write(path: Path, content: str) -> None:
    """Scrive su file via temp + move atomico. Se fallisce, il file originale è intatto."""
    tmp = path.with_suffix(".yaml.reconcile_tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        shutil.move(str(tmp), str(path))
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Nodo LangGraph
# ─────────────────────────────────────────────────────────────────────────────

async def spec_reconcile_node(state: dict) -> dict:
    """
    Nodo LangGraph: riconcilia la specifica YAML dopo un troubleshoot riuscito.

    Input dallo state:
      - spec_path (str | Path): percorso del file YAML da aggiornare
      - troubleshoot_attempt (int): numero di tentativi di troubleshoot effettuati
      - executed_commands (dict[str, RouterCommands]): comandi applicati nell'ultimo ciclo
      - execution_log (list[str]): log completo del ciclo
      - final_status (str): deve essere "SUCCESS" per procedere

    Output nel state:
      - spec_reconcile_status: "SUCCESS" | "SKIPPED" | "FAILED"
      - spec_reconcile_changes: lista di stringhe CHANGE: dal modello
      - execution_log: aggiornato con le righe di reconcile
    """
    logger.info(">>> SPEC_RECONCILE <<<")

    # Clone state deep to prevent mutating any nested objects in-place
    state_copy = copy.deepcopy(state)

    # ── Pre-condizioni ────────────────────────────────────────────────────────

    final_status = state_copy.get("final_status", "")
    troubleshoot_attempt = state_copy.get("troubleshoot_attempt", 0)
    spec_path_raw = state_copy.get("spec_path")
    execution_log: list[str] = list(state_copy.get("execution_log") or [])

    if final_status != "SUCCESS":
        logger.info("[SPEC_RECONCILE] final_status=%s — skip (solo SUCCESS attiva il reconcile).", final_status)
        return {
            "spec_reconcile_status": "SKIPPED",
            "spec_reconcile_changes": [],
            "execution_log": execution_log + ["SPEC_RECONCILE: skip (final_status != SUCCESS)"],
        }

    if troubleshoot_attempt == 0:
        logger.info("[SPEC_RECONCILE] troubleshoot_attempt=0 — skip (nessuna correzione applicata).")
        return {
            "spec_reconcile_status": "SKIPPED",
            "spec_reconcile_changes": [],
            "execution_log": execution_log + ["SPEC_RECONCILE: skip (nessun troubleshoot effettuato)"],
        }

    if not spec_path_raw:
        logger.warning("[SPEC_RECONCILE] spec_path non presente nello state. Skip.")
        return {
            "spec_reconcile_status": "SKIPPED",
            "spec_reconcile_changes": [],
            "execution_log": execution_log + ["SPEC_RECONCILE: skip (spec_path assente)"],
        }

    spec_path = Path(spec_path_raw)
    if not spec_path.exists():
        logger.error("[SPEC_RECONCILE] File spec non trovato: %s", spec_path)
        return {
            "spec_reconcile_status": "FAILED",
            "spec_reconcile_changes": [],
            "execution_log": execution_log + [f"SPEC_RECONCILE: FAILED (file non trovato: {spec_path})"],
        }

    # ── Lettura spec originale ────────────────────────────────────────────────

    try:
        current_spec = spec_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error("[SPEC_RECONCILE] Impossibile leggere la spec: %s", e)
        return {
            "spec_reconcile_status": "FAILED",
            "spec_reconcile_changes": [],
            "execution_log": execution_log + [f"SPEC_RECONCILE: FAILED (lettura spec: {e})"],
        }

    # Estrai i device noti PRIMA del merge (per il device-loss guard)
    try:
        parsed_before = yaml.safe_load(current_spec) or {}
        known_names: set[str] = {
            d["name"] for d in parsed_before.get("devices", [])
            if isinstance(d, dict) and "name" in d
        }
    except Exception:
        known_names = set()

    logger.info("[SPEC_RECONCILE] Dispositivi noti: %s", sorted(known_names))

    # ── Chiamata LLM ─────────────────────────────────────────────────────────

    user_prompt = _build_reconcile_prompt(current_spec, state_copy)

    try:
        from llm.async_client import llm_client
        raw_response = await llm_client.raw_completion(
            system_prompt=_RECONCILE_SYSTEM_PROMPT,
            user_prompt=user_prompt,
            caller="spec_reconcile",
        )
    except Exception as e:
        logger.error("[SPEC_RECONCILE] Errore chiamata LLM: %s", e, exc_info=True)
        return {
            "spec_reconcile_status": "FAILED",
            "spec_reconcile_changes": [],
            "execution_log": execution_log + [f"SPEC_RECONCILE: FAILED (LLM error: {e})"],
        }

    # ── Estrazione patch ──────────────────────────────────────────────────────

    patch = _extract_patch(raw_response)
    if not patch:
        logger.warning("[SPEC_RECONCILE] LLM non ha prodotto un frammento YAML valido.")
        # Questo non è un errore fatale: la spec rimane invariata
        return {
            "spec_reconcile_status": "FAILED",
            "spec_reconcile_changes": [],
            "execution_log": execution_log + ["SPEC_RECONCILE: FAILED (nessun patch YAML estratto dalla risposta LLM)"],
        }

    # ── Applicazione merge ────────────────────────────────────────────────────

    try:
        reconciled = _apply_patch(current_spec, patch)
    except Exception as e:
        logger.error("[SPEC_RECONCILE] Errore durante il merge: %s", e)
        return {
            "spec_reconcile_status": "FAILED",
            "spec_reconcile_changes": [],
            "execution_log": execution_log + [f"SPEC_RECONCILE: FAILED (merge error: {e})"],
        }

    # ── Device-loss guard ─────────────────────────────────────────────────────

    missing = _validate_no_device_loss(known_names, reconciled)
    if missing:
        logger.error(
            "[SPEC_RECONCILE] Patch rifiutato: il merge eliminerebbe i dispositivi %s.", missing
        )
        return {
            "spec_reconcile_status": "FAILED",
            "spec_reconcile_changes": [],
            "execution_log": execution_log + [
                f"SPEC_RECONCILE: FAILED (device-loss guard: {missing} sarebbero stati eliminati — patch scartato)"
            ],
        }

    # ── Calcolo Diff e Approvazione dell'operatore ──────────────────────────

    import difflib
    from rich.console import Console
    from rich.rule import Rule
    from rich.panel import Panel
    from prompt_toolkit import PromptSession
    from prompt_toolkit.styles import Style as PtStyle

    # Verifica se ci sono modifiche logiche (ignorando formattazione/indentazione)
    try:
        parsed_old = yaml.safe_load(current_spec) or {}
        parsed_new = yaml.safe_load(reconciled) or {}
        has_logical_changes = (parsed_old != parsed_new)
    except Exception as e:
        logger.warning("[SPEC_RECONCILE] Fallito parsing YAML per confronto: %s. Procedo con diff testuale.", e)
        has_logical_changes = True

    if not has_logical_changes:
        logger.info("[SPEC_RECONCILE] Nessuna modifica logica rilevata. Skip prompt e scrittura.")
        return {
            "spec_reconcile_status": "SUCCESS",
            "spec_reconcile_changes": [],
            "execution_log": execution_log + [
                f"SPEC_RECONCILE: SUCCESS — Specifica {spec_path.name} già allineata (nessuna modifica necessaria)"
            ],
        }

    # Normalizza l'originale con lo stesso dumper per evitare rumore di formattazione nel diff
    try:
        normalized_old = yaml.dump(parsed_old, default_flow_style=False, sort_keys=False, allow_unicode=True)
    except Exception:
        normalized_old = current_spec

    old_lines = normalized_old.splitlines(keepends=True)
    new_lines = reconciled.splitlines(keepends=True)
    diff_lines = list(difflib.unified_diff(
        old_lines,
        new_lines,
        fromfile=f"Specifica Corrente ({spec_path.name})",
        tofile=f"Specifica Riconciliata ({spec_path.name})",
        n=3
    ))

    if diff_lines:
        console = Console()
        console.print()
        console.print(Panel(
            "[bold yellow]⚠️  PROPOSTA DI RICONCILIAZIONE DELLA SPECIFICA YAML ⚠️[/bold yellow]\n\n"
            f"Il troubleshooting è andato a buon fine. Viene proposta la seguente modifica al file [cyan]{spec_path.name}[/cyan] "
            "per allinearlo allo stato reale della rete.",
            border_style="yellow"
        ))
        
        console.print(Rule("[bold cyan]Modifiche Proposte (Diff)[/bold cyan]", style="cyan"))
        for line in diff_lines:
            if line.startswith("+") and not line.startswith("+++"):
                console.print(f"[green]{line.rstrip()}[/green]")
            elif line.startswith("-") and not line.startswith("---"):
                console.print(f"[red]{line.rstrip()}[/red]")
            elif line.startswith("@@"):
                console.print(f"[cyan]{line.rstrip()}[/cyan]")
            else:
                console.print(line.rstrip())
        console.print(Rule(style="cyan"))

        pt_style = PtStyle.from_dict({
            "prompt": "ansicyan bold",
        })
        try:
            session = PromptSession(style=pt_style)
            scelta = await session.prompt_async(
                "Applica questa modifica alla specifica YAML? (s/n) > "
            )
            scelta = scelta.strip().lower()
        except (KeyboardInterrupt, EOFError):
            scelta = "n"
        except Exception as e:
            logger.warning("[SPEC_RECONCILE] Eccezione durante il prompt: %s. Default a 'n'.", e)
            scelta = "n"

        if scelta not in ("s", "si", "y", "yes"):
            logger.warning("[SPEC_RECONCILE] Modifica della specifica RIFIUTATA dall'operatore.")
            return {
                "spec_reconcile_status": "SKIPPED",
                "spec_reconcile_changes": [],
                "execution_log": execution_log + [
                    f"SPEC_RECONCILE: SKIPPED (modifica rifiutata dall'operatore per {spec_path.name})"
                ],
            }

    # ── Scrittura atomica ─────────────────────────────────────────────────────

    try:
        _atomic_write(spec_path, reconciled)
    except Exception as e:
        logger.error("[SPEC_RECONCILE] Scrittura atomica fallita: %s", e)
        return {
            "spec_reconcile_status": "FAILED",
            "spec_reconcile_changes": [],
            "execution_log": execution_log + [f"SPEC_RECONCILE: FAILED (scrittura: {e})"],
        }

    # ── Estrai le righe CHANGE per il log ────────────────────────────────────

    changes = [l.strip() for l in raw_response.splitlines() if l.strip().startswith("CHANGE:")]
    logger.info("[SPEC_RECONCILE] Spec aggiornata: %d modifica/e applicata/e.", len(changes))
    for c in changes:
        logger.info("  %s", c)

    log_entry = (
        f"SPEC_RECONCILE: SUCCESS — {len(changes)} modifica/e applicata/e a {spec_path.name}"
        + (f": {'; '.join(c[8:] for c in changes)}" if changes else "")
    )

    return {
        "spec_reconcile_status": "SUCCESS",
        "spec_reconcile_changes": changes,
        "execution_log": execution_log + [log_entry],
    }
