# tools/console_bootstrap.py
"""
Bootstrap console per dispositivi reali (REAL_DEVICES=true).

Flusso:
  1. Per ogni dispositivo Cisco nella specifica, apre la console port (Telnet).
  2. Esegue la configurazione di base (hostname, SSH, utenti, ecc.).
  3. Stampa un messaggio e aspetta che l'operatore prema INVIO per confermare
     e passare al dispositivo successivo (Human-in-the-Loop).

Questo modulo è chiamato da main.py prima del flusso principale del grafo,
solo quando REAL_DEVICES=true.

Nota sull'architettura:
  Il bootstrap console è deliberatamente sequenziale e sincrono dal punto
  di vista dell'operatore: un dispositivo alla volta, conferma manuale.
  Non è parallelizzabile per design (sicurezza + cablaggio fisico).
"""

from __future__ import annotations

import asyncio
import logging
import sys
from typing import Callable

from tools.connection import get_console_connection, _cisco_telnet_login, REAL_DEVICE_CMD_DELAY

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Human-in-the-Loop
# ─────────────────────────────────────────────────────────────────────────────

async def _wait_for_operator(message: str) -> None:
    """
    Mostra un messaggio all'operatore e aspetta la conferma da tastiera.
    Funziona sia in ambiente interattivo sia in un event loop asincrono.
    """
    print(f"\n{'='*60}")
    print(f"  [HUMAN IN THE LOOP] {message}")
    print(f"  Premi INVIO per continuare...")
    print(f"{'='*60}")
    # asyncio non può usare input() direttamente senza bloccare il loop
    await asyncio.get_event_loop().run_in_executor(None, sys.stdin.readline)


# ─────────────────────────────────────────────────────────────────────────────
# Esecuzione comandi sulla console
# ─────────────────────────────────────────────────────────────────────────────

async def _send_and_log(conn, cmd: str, device_name: str, delay: float | None = None) -> str:
    """Invia un comando sulla console, logga l'output e rispetta il delay."""
    if not cmd or not cmd.strip():
        return ""
    out = await conn.send_command(cmd)
    logger.info("[%s][CONSOLE] > %s", device_name, cmd)
    if out.strip():
        logger.debug("[%s][CONSOLE] < %s", device_name, out.strip()[:200])
    await asyncio.sleep(delay or REAL_DEVICE_CMD_DELAY)
    return out


async def _bootstrap_cisco_device(
    device_name: str,
    cfg: dict,
    base_commands: list[str],
) -> bool:
    """
    Esegue il bootstrap su un singolo dispositivo Cisco via console.

    base_commands è la lista di comandi CLI già compilata dal nodo GENERATE
    (la stessa che andrebbe via SSH, ma applicata sulla console per la prima volta).

    Ritorna True se il bootstrap è completato con successo.
    """
    logger.info("[%s] Avvio bootstrap console su %s:%s",
                device_name, cfg.get("console_host") or cfg.get("host"), cfg.get("console_port") or cfg.get("port"))

    try:
        async with get_console_connection(cfg) as conn:
            # Gestione login iniziale console (potrebbe essere già in enable)
            await _cisco_telnet_login(conn, cfg)

            # Esecuzione comandi di configurazione base
            for cmd in base_commands:
                out = await _send_and_log(conn, cmd, device_name)

                # Rilevazione errori CLI (stessa logica di execute.py)
                error_kw = ("% invalid", "% error", "% unknown", "% incomplete")
                if any(k in out.lower() for k in error_kw):
                    logger.error("[%s][CONSOLE] Errore su comando '%s': %s", device_name, cmd, out.strip())
                    return False

                # Pausa extra per comandi lenti (crypto key generate rsa)
                if "crypto key" in cmd.lower():
                    logger.info("[%s][CONSOLE] Generazione RSA in corso, attendo 10s...", device_name)
                    await asyncio.sleep(10)

            logger.info("[%s] Bootstrap console completato con successo.", device_name)
            return True

    except Exception as e:
        logger.error("[%s] Errore fatale durante il bootstrap console: %s", device_name, e, exc_info=True)
        return False


# ─────────────────────────────────────────────────────────────────────────────
# Entry point pubblico
# ─────────────────────────────────────────────────────────────────────────────

async def run_console_bootstrap(
    devices: list[tuple[str, dict, list[str]]],
) -> dict[str, bool]:
    """
    Esegue il bootstrap console su tutti i dispositivi reali che lo richiedono,
    uno alla volta, con conferma manuale dell'operatore tra un dispositivo e l'altro.

    Args:
        devices: lista di (device_name, cfg, base_commands)
                 dove base_commands è la lista CLI per la base config.

    Returns:
        dict device_name -> True (successo) | False (fallito)
    """
    if not devices:
        logger.info("[BOOTSTRAP] Nessun dispositivo richiede il bootstrap console.")
        return {}

    results: dict[str, bool] = {}

    print(f"\n{'='*60}")
    print(f"  MODALITÀ DISPOSITIVI REALI — BOOTSTRAP CONSOLE")
    print(f"  Verranno configurati {len(devices)} dispositivi via console port.")
    print(f"  Assicurati che i cavi console siano collegati prima di continuare.")
    print(f"{'='*60}")
    await _wait_for_operator("Pronto per iniziare il bootstrap?")

    for i, (device_name, cfg, base_commands) in enumerate(devices, 1):
        print(f"\n  [{i}/{len(devices)}] Bootstrap: {device_name}")
        print(f"  Console: {cfg.get('console_host') or cfg.get('host')}:{cfg.get('console_port') or cfg.get('port')}")
        print(f"  Comandi da applicare: {len(base_commands)}")
        print()

        await _wait_for_operator(
            f"Connetti il cavo console a {device_name} e verifica che sia acceso."
        )

        success = await _bootstrap_cisco_device(device_name, cfg, base_commands)
        results[device_name] = success

        if success:
            print(f"\n  ✅ {device_name}: Bootstrap completato.")
        else:
            print(f"\n  ❌ {device_name}: Bootstrap FALLITO. Controlla i log per dettagli.")

        if i < len(devices):
            await _wait_for_operator(
                f"Bootstrap di {device_name} {'riuscito' if success else 'fallito'}. "
                f"Pronto per il prossimo dispositivo ({devices[i][0]})?"
            )

    # Riepilogo finale
    ok = [n for n, r in results.items() if r]
    failed = [n for n, r in results.items() if not r]
    print(f"\n{'='*60}")
    print(f"  BOOTSTRAP CONSOLE COMPLETATO")
    print(f"  Successo: {ok}")
    if failed:
        print(f"  Falliti:  {failed}")
    print(f"{'='*60}\n")

    return results
