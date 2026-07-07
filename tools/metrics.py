# tools/metrics.py
"""
Modulo centralizzato per la raccolta di metriche quantitative durante l'esecuzione
del pipeline NetAgent V2.

Metriche raccolte:
  - Timing per nodo LangGraph (durata di ogni fase)
  - Token LLM (input/output per ogni chiamata, modello, caller)
  - Comandi CLI (generati, eseguiti, falliti, per dispositivo)
  - Esito della verifica (ping matrix, assurance, troubleshooting)
  - Metadati (specifica, modello LLM, timestamp)

Utilizzo:
  from tools.metrics import metrics
  metrics.start_node("OBSERVE")
  ...
  metrics.end_node("OBSERVE")
"""

from __future__ import annotations

import json
import time
import os
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class LLMCallMetric:
    """Singola chiamata LLM con dettagli di consumo."""
    caller: str              # Chi ha chiamato (es. "generate_plan", "troubleshoot")
    model: str               # Modello usato (es. "gemini-3.1-flash-lite")
    provider: str            # Provider (es. "gemini", "github")
    input_tokens: int        # Token in input
    output_tokens: int       # Token in output
    duration_s: float        # Durata della chiamata in secondi
    timestamp: str           # ISO timestamp


class MetricsCollector:
    """
    Singleton per la raccolta metriche durante l'esecuzione del pipeline.
    Thread-safe per le operazioni di base (append su liste, assegnamenti atomici).
    """

    _instance: Optional[MetricsCollector] = None

    def __new__(cls) -> MetricsCollector:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self) -> None:
        if self._initialized:
            return
        self._initialized = True
        self.reset()

    def reset(self) -> None:
        """Resetta tutte le metriche per una nuova esecuzione."""
        # Timing
        self.start_time: Optional[datetime] = None
        self.end_time: Optional[datetime] = None
        self._node_start_times: dict[str, float] = {}
        self.node_timings: dict[str, float] = {}

        # LLM
        self.llm_calls: list[LLMCallMetric] = []

        # Comandi CLI
        self.commands_generated: int = 0
        self.commands_executed: int = 0
        self.commands_failed: int = 0
        self.commands_per_device: dict[str, dict[str, int]] = {}

        # Verifica
        self.deploy_success_first_try: Optional[bool] = None
        self.troubleshoot_iterations: int = 0
        self.troubleshoot_resolved: Optional[bool] = None
        self.ping_matrix_total: int = 0
        self.ping_matrix_passed: int = 0
        self.control_plane_errors: int = 0

        # Dispositivi
        self.devices_total: int = 0
        self.devices_reachable: int = 0
        self.devices_idempotent: int = 0

        # Metadati
        self.spec_file: str = ""
        self.llm_provider: str = ""
        self.llm_model: str = ""

    # ── Timing ────────────────────────────────────────────────────────────────

    def start_pipeline(self) -> None:
        """Marca l'inizio dell'esecuzione del pipeline."""
        self.reset()
        self.start_time = datetime.now(timezone.utc)

    def finalize(self) -> None:
        """Marca la fine dell'esecuzione del pipeline."""
        self.end_time = datetime.now(timezone.utc)

    def start_node(self, name: str) -> None:
        """Registra l'inizio di un nodo LangGraph."""
        self._node_start_times[name] = time.monotonic()

    def end_node(self, name: str) -> None:
        """Registra la fine di un nodo LangGraph e calcola la durata."""
        start = self._node_start_times.pop(name, None)
        if start is not None:
            elapsed = time.monotonic() - start
            # Se il nodo viene invocato più volte (es. fan-out GENERATE_SINGLE),
            # accumula il tempo (rappresenta il tempo totale speso in quel tipo di nodo)
            self.node_timings[name] = self.node_timings.get(name, 0.0) + elapsed

    # ── LLM ───────────────────────────────────────────────────────────────────

    def record_llm_call(
        self,
        caller: str,
        model: str,
        provider: str,
        input_tokens: int,
        output_tokens: int,
        duration_s: float,
    ) -> None:
        """Registra una singola chiamata LLM con i dettagli di consumo token."""
        self.llm_calls.append(LLMCallMetric(
            caller=caller,
            model=model,
            provider=provider,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            duration_s=round(duration_s, 3),
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
        # Aggiorna i metadati globali del provider/modello
        if not self.llm_provider:
            self.llm_provider = provider
        if not self.llm_model:
            self.llm_model = model

    # ── Comandi CLI ───────────────────────────────────────────────────────────

    def record_commands(
        self,
        device: str,
        generated: int,
        executed: int,
        failed: int,
    ) -> None:
        """Registra i conteggi CLI per un singolo dispositivo."""
        self.commands_generated += generated
        self.commands_executed += executed
        self.commands_failed += failed
        self.commands_per_device[device] = {
            "generated": generated,
            "executed": executed,
            "failed": failed,
        }

    # ── Verifica ──────────────────────────────────────────────────────────────

    def record_verification(
        self,
        ping_total: int,
        ping_passed: int,
        control_plane_errors: int,
        is_first_try: bool,
        is_success: bool,
    ) -> None:
        """Registra i risultati della fase VERIFY."""
        self.ping_matrix_total = ping_total
        self.ping_matrix_passed = ping_passed
        self.control_plane_errors = control_plane_errors
        if is_first_try and is_success:
            self.deploy_success_first_try = True
        elif is_first_try and not is_success:
            self.deploy_success_first_try = False

    def record_troubleshoot(self, iteration: int, resolved: bool) -> None:
        """Registra un'iterazione di troubleshooting."""
        self.troubleshoot_iterations = iteration
        self.troubleshoot_resolved = resolved

    # ── Dispositivi ───────────────────────────────────────────────────────────

    def record_devices(
        self, total: int, reachable: int, idempotent: int
    ) -> None:
        """Registra il conteggio dei dispositivi."""
        self.devices_total = total
        self.devices_reachable = reachable
        self.devices_idempotent = idempotent

    # ── Proprietà calcolate ───────────────────────────────────────────────────

    @property
    def total_duration_s(self) -> float:
        if self.start_time and self.end_time:
            return (self.end_time - self.start_time).total_seconds()
        return 0.0

    @property
    def total_input_tokens(self) -> int:
        return sum(c.input_tokens for c in self.llm_calls)

    @property
    def total_output_tokens(self) -> int:
        return sum(c.output_tokens for c in self.llm_calls)

    @property
    def total_llm_duration_s(self) -> float:
        return sum(c.duration_s for c in self.llm_calls)

    @property
    def ping_success_rate(self) -> float:
        if self.ping_matrix_total == 0:
            return 0.0
        return (self.ping_matrix_passed / self.ping_matrix_total) * 100.0

    # ── Export ─────────────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Serializza tutte le metriche in un dizionario."""
        return {
            "spec_file": self.spec_file,
            "llm_provider": self.llm_provider,
            "llm_model": self.llm_model,
            "start_time": self.start_time.isoformat() if self.start_time else None,
            "end_time": self.end_time.isoformat() if self.end_time else None,
            "total_duration_s": round(self.total_duration_s, 2),
            "node_timings": {k: round(v, 2) for k, v in self.node_timings.items()},
            "llm_calls": [
                {
                    "caller": c.caller,
                    "model": c.model,
                    "provider": c.provider,
                    "input_tokens": c.input_tokens,
                    "output_tokens": c.output_tokens,
                    "duration_s": c.duration_s,
                    "timestamp": c.timestamp,
                }
                for c in self.llm_calls
            ],
            "llm_totals": {
                "total_calls": len(self.llm_calls),
                "total_input_tokens": self.total_input_tokens,
                "total_output_tokens": self.total_output_tokens,
                "total_tokens": self.total_input_tokens + self.total_output_tokens,
                "total_llm_duration_s": round(self.total_llm_duration_s, 2),
            },
            "commands": {
                "total_generated": self.commands_generated,
                "total_executed": self.commands_executed,
                "total_failed": self.commands_failed,
                "per_device": self.commands_per_device,
            },
            "verification": {
                "success_first_try": self.deploy_success_first_try,
                "troubleshoot_iterations": self.troubleshoot_iterations,
                "troubleshoot_resolved": self.troubleshoot_resolved,
                "ping_total": self.ping_matrix_total,
                "ping_passed": self.ping_matrix_passed,
                "ping_success_rate": round(self.ping_success_rate, 1),
                "control_plane_errors": self.control_plane_errors,
            },
            "devices": {
                "total": self.devices_total,
                "reachable": self.devices_reachable,
                "idempotent": self.devices_idempotent,
            },
        }

    def save_json(self, output_dir: str = "metrics") -> str:
        """Salva le metriche in un file JSON nella directory specificata."""
        Path(output_dir).mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filepath = os.path.join(output_dir, f"run_{ts}.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        logger.info("[METRICS] Report salvato in: %s", filepath)
        return filepath

    def to_markdown(self) -> str:
        """Genera un report Markdown leggibile per il terminale."""
        lines = [
            "",
            "══════════════════════════════════════════════════════════════",
            "                    METRICHE DI ESECUZIONE                   ",
            "══════════════════════════════════════════════════════════════",
            "",
            f"  Specifica:    {self.spec_file or '(nessuna)'}",
            f"  Modello LLM:  {self.llm_model or '(nessuna chiamata LLM)'}",
            f"  Provider:     {self.llm_provider or '-'}",
            f"  Durata:       {self.total_duration_s:.1f}s",
            "",
        ]

        # Timing per nodo
        lines.append("── Timing per Nodo ────────────────────────────────────────")
        if self.node_timings:
            max_name_len = max(len(n) for n in self.node_timings)
            total_node_time = 0.0
            for name, elapsed in self.node_timings.items():
                lines.append(f"  {name:<{max_name_len}}  {elapsed:>6.1f}s")
                total_node_time += elapsed
            lines.append(f"  {'':─<{max_name_len}}  ─────")
            lines.append(f"  {'Totale nodi':<{max_name_len}}  {total_node_time:>6.1f}s")
        else:
            lines.append("  (nessun dato)")
        lines.append("")

        # Token LLM
        lines.append("── Consumo Token LLM ──────────────────────────────────────")
        lines.append(f"  Chiamate totali:   {len(self.llm_calls)}")
        lines.append(f"  Token input:       {self.total_input_tokens}")
        lines.append(f"  Token output:      {self.total_output_tokens}")
        lines.append(f"  Token totali:      {self.total_input_tokens + self.total_output_tokens}")
        lines.append(f"  Tempo LLM totale:  {self.total_llm_duration_s:.1f}s")
        if self.llm_calls:
            lines.append("")
            lines.append("  Dettaglio chiamate:")
            for i, c in enumerate(self.llm_calls, 1):
                lines.append(
                    f"    [{i}] {c.caller:<25} "
                    f"in={c.input_tokens:<6} out={c.output_tokens:<6} "
                    f"({c.duration_s:.1f}s)"
                )
        elif self.total_input_tokens == 0:
            lines.append("  (Nessuna chiamata LLM: specifica YAML + compilatori Jinja2)")
        lines.append("")

        # Comandi CLI
        lines.append("── Comandi CLI ────────────────────────────────────────────")
        lines.append(f"  Comandi generati:  {self.commands_generated}")
        lines.append(f"  Comandi eseguiti:  {self.commands_executed}")
        lines.append(f"  Comandi falliti:   {self.commands_failed}")
        if self.commands_per_device:
            dev_summary = " ".join(
                f"{dev}({info['generated']})"
                for dev, info in sorted(self.commands_per_device.items())
            )
            lines.append(f"  Per dispositivo:   {dev_summary}")
        lines.append("")

        # Dispositivi
        lines.append("── Dispositivi ────────────────────────────────────────────")
        lines.append(f"  Totali:            {self.devices_total}")
        lines.append(f"  Raggiungibili:     {self.devices_reachable}")
        lines.append(f"  Idempotenti:       {self.devices_idempotent}")
        lines.append("")

        # Esito
        lines.append("── Esito ──────────────────────────────────────────────────")
        first_try = "✅ Sì" if self.deploy_success_first_try else ("❌ No" if self.deploy_success_first_try is False else "—")
        lines.append(f"  Deploy al primo tentativo:  {first_try}")
        lines.append(f"  Cicli troubleshooting:      {self.troubleshoot_iterations}")
        if self.troubleshoot_resolved is not None:
            ts_resolved = "✅ Sì" if self.troubleshoot_resolved else "❌ No"
            lines.append(f"  Troubleshoot risolto:       {ts_resolved}")
        lines.append(
            f"  Matrice ping:               "
            f"{self.ping_matrix_passed}/{self.ping_matrix_total} "
            f"({self.ping_success_rate:.0f}%)"
        )
        lines.append(f"  Errori Control Plane:       {self.control_plane_errors}")
        lines.append("")
        lines.append("══════════════════════════════════════════════════════════════")
        lines.append("")

        return "\n".join(lines)


# ── Singleton globale ─────────────────────────────────────────────────────────
metrics = MetricsCollector()
