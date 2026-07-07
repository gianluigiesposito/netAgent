# tools/ping_parser.py
import re
import logging
from typing import NamedTuple

logger = logging.getLogger(__name__)

# =====================================================================
# CONTRATTO DATI UNIFICATO (Scope Globale)
# =====================================================================
class PingResult(NamedTuple):
    success: bool
    packets_sent: int
    packets_received: int
    loss_pct: float


# Dizionario di espressioni regolari per mantenere la piena generalità sui vendor
_PATTERNS = {
    "vpcs": {
        "sent":     re.compile(r"(\d+) packets? transmitted"),
        "received": re.compile(r"(\d+) packets? received"),
        "loss":     re.compile(r"(\d+(?:\.\d+)?)% packet loss"),
    },
    "linux_host": {
        "sent":     re.compile(r"(\d+) packets? transmitted"),
        "received": re.compile(r"(\d+) received"),
        "loss":     re.compile(r"(\d+(?:\.\d+)?)% packet loss"),
    },
    "windows": {
        "sent":     re.compile(r"Sent = (\d+)"),
        "received": re.compile(r"Received = (\d+)"),
        "loss":     re.compile(r"\((\d+)% loss\)"),
    },
}


def parse_ping_result(output: str, vendor: str = "vpcs") -> PingResult:
    """
    Parser deterministico strutturato per la validazione ICMP.
    Intercetta gli errori locali della CLI quando le interfacce non hanno IP
    e processa l'output dei pacchetti trasmessi/ricevuti.
    """
    if not output:
        return PingResult(success=False, packets_sent=5, packets_received=0, loss_pct=100.0)

    lo = output.lower()
    
    # ── SHIELD RIGIDO SOTA ANTI-FALSO POSITIVO ──
    # Se l'host non ha un indirizzo IP valido o la rotta locale è assente,
    # intercettiamo l'errore a schermo del terminale e forziamo il fallimento atomico.
    if "no ip address" in lo or "not configured" in lo or "is open" in lo or "invalid" in lo:
        logger.warning("[Ping Parser] Rilevato errore locale di configurazione o interfaccia priva di IP.")
        return PingResult(success=False, packets_sent=5, packets_received=0, loss_pct=100.0)
    
    # Estrazione basata sui pattern predefiniti del vendor richiesto
    p = _PATTERNS.get(vendor, _PATTERNS["linux_host"])
    s = p["sent"].search(output)
    r = p["received"].search(output)
    l = p["loss"].search(output)

    if s and r and l:
        sent = int(s.group(1))
        recv = int(r.group(1))
        loss = float(l.group(1))
        logger.debug("[Ping Parser] Match Regex: %d sent, %d received, %f%% loss", sent, recv, loss)
        return PingResult(success=recv > 0 and loss < 100.0, packets_sent=sent,
                          packets_received=recv, loss_pct=loss)

    # ── HEURISTIC FALLBACK PATH ──
    # Se il buffer Telnet taglia o tronca le righe conclusive di sommario, 
    # calcoliamo il successo contando le singole righe di echo-reply "bytes from"
    replies = re.findall(r"\b\d+\s+bytes\s+from\s+", output, re.IGNORECASE)
    if replies:
        n = len(replies)
        logger.debug("[Ping Parser] Sommario assente ma rilevati %d pacchetti ICMP echo-reply.", n)
        return PingResult(success=True, packets_sent=n, packets_received=n, loss_pct=0.0)

    # Nessun match e nessuna riga utile trovata: test fallito al 100%
    return PingResult(success=False, packets_sent=5, packets_received=0, loss_pct=100.0)