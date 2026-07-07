# nodes/aggregate.py
"""
Nodo AGGREGATE — Fan-in per i worker GENERATE_SINGLE.

Nella v1 ogni worker GENERATE_SINGLE aveva un edge diretto verso EXECUTE,
causando N invocazioni di EXECUTE (una per worker). Questo nodo raccoglie
tutti i router_commands accumulati nel reducer e passa il controllo
a EXECUTE una sola volta.

Il nodo è volutamente banale: i comandi sono già nel reducer dello stato
grazie a update_commands in core/state.py. Qui ci limitiamo a loggare
e a restituire uno stato pulito senza modifiche.
"""

import logging
from core.state import AgentState

logger = logging.getLogger(__name__)


async def aggregate_node(state: AgentState) -> dict:
    router_commands = state.get("router_commands", {})
    total_pairs = sum(len(rc.pairs) for rc in router_commands.values())
    total_lines = sum(
        len([line for pair in rc.pairs for line in pair.cmd.splitlines() if line.strip()])
        for rc in router_commands.values()
    )

    logger.info(
        ">>> AGGREGATE: %d dispositivi, %d coppie comandi (%d righe CLI totali) pronte per EXECUTE <<<",
        len(router_commands),
        total_pairs,
        total_lines,
    )

    # Nessuna modifica allo stato: il reducer update_commands ha già aggregato tutto.
    return {}
