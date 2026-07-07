# core/graph.py
"""
Grafo LangGraph v4 - Architettura Split-Execution.

Flusso completo:
  PARSE_INPUT → OBSERVE → PLAN → [GENERATE_SINGLE × N] → AGGREGATE → EXECUTE (Solo Infra)
                                                                          │
                                                                    OBSERVE_RELAY
                                                                          │
                                            ┌─ (nessun relay) ───────────┤
                                            │             [GENERATE_RELAY × M]
                                            │                   AGGREGATE_RELAY
                                            │                   EXECUTE_RELAY
                                            └────────────────────────────┤
                                                                          ▼
                                                                     EXECUTE_HOSTS (Solo PC)
                                                                          │
                                                                       VERIFY → ...
"""

from langgraph.graph import StateGraph, END
from langgraph.types import Send
import re

from core.state import AgentState
from nodes.input_parser import parse_input_node
from nodes.observe import observe_node, observe_relay_node
from nodes.plan import plan_node
from nodes.generate import generate_single_node, generate_relay_node
from nodes.aggregate import aggregate_node
from nodes.approval import approval_node
from nodes.execute import execute_node, execute_hosts_node
from nodes.verify import verify_node
from nodes.troubleshoot import troubleshoot_node, MAX_ATTEMPTS
from nodes.spec_reconcile import spec_reconcile_node
from tools.metrics import metrics


def _timed(node_name: str, fn):
    """Wrapper non invasivo per misurare il tempo di esecuzione di ogni nodo."""
    async def wrapper(state):
        metrics.start_node(node_name)
        try:
            result = await fn(state)
            return result
        finally:
            metrics.end_node(node_name)
    wrapper.__name__ = fn.__name__
    wrapper.__qualname__ = fn.__qualname__
    return wrapper


def _route_to_workers(state: AgentState) -> list[Send] | str:
    """Fan-out: un worker GENERATE_SINGLE per ogni dispositivo raggiungibile nel piano."""
    plan         = state.get("plan")
    reachability = state.get("reachability", {})

    if not plan:
        return END

    workers = [
        Send("GENERATE_SINGLE", {
            "router_name": dev.name,
            "router_plan": dev,
        })
        for dev in plan.devices
        if reachability.get(dev.name) == "REACHABLE"
    ]
    return workers if workers else END


def _route_to_relay_workers(state: AgentState) -> list[Send] | str:
    """
    Se c'è un relay, va a GENERATE_RELAY.
    Se non c'è relay, bypassa direttamente a EXECUTE_HOSTS.
    """
    plan         = state.get("plan")
    reachability = state.get("reachability", {})

    if not plan:
        return "EXECUTE_HOSTS"

    workers = [
        Send("GENERATE_RELAY", {
            "router_name": dev.name,
            "router_plan": dev,
        })
        for dev in plan.devices
        if (
            reachability.get(dev.name) == "REACHABLE"
            and (
                (dev.extra_params and re.search(r'(?:DHCP_RELAY|RELAY_SUBNETS)', dev.extra_params, re.IGNORECASE))
                or getattr(dev, "dhcp_relay_server", None)
                or getattr(dev, "dhcp_relay_subnets", None)
            )
        )
    ]

    return workers if workers else "EXECUTE_HOSTS"



def _route_after_verify(state: AgentState) -> str:
    final_status = state.get("final_status", "")
    attempt      = state.get("troubleshoot_attempt", 0)
    failed       = state.get("failed_devices", [])

    if final_status == "SUCCESS" and attempt > 0:
        return "SPEC_RECONCILE"

    if final_status in ("SUCCESS", "TROUBLESHOOT_EXHAUSTED") or attempt >= MAX_ATTEMPTS or not failed:
        return END

    return "TROUBLESHOOT"


def _route_after_troubleshoot(state: AgentState) -> str:
    final_status = state.get("final_status", "")
    if final_status == "TROUBLESHOOT_EXHAUSTED":
        return END
    return "APPROVAL_TROUBLESHOOT"



def build_graph() -> StateGraph:
    wf = StateGraph(AgentState)

    wf.add_node("PARSE_INPUT",     _timed("PARSE_INPUT", parse_input_node))
    wf.add_node("OBSERVE",         _timed("OBSERVE", observe_node))
    wf.add_node("PLAN",            _timed("PLAN", plan_node))
    wf.add_node("GENERATE_SINGLE", _timed("GENERATE_SINGLE", generate_single_node))
    wf.add_node("AGGREGATE",       _timed("AGGREGATE", aggregate_node))
    wf.add_node("APPROVAL",        _timed("APPROVAL", approval_node))
    wf.add_node("EXECUTE",         _timed("EXECUTE", execute_node))           # Esegue solo Infra
    wf.add_node("OBSERVE_RELAY",   _timed("OBSERVE_RELAY", observe_relay_node))
    wf.add_node("GENERATE_RELAY",  _timed("GENERATE_RELAY", generate_relay_node))
    wf.add_node("AGGREGATE_RELAY", _timed("AGGREGATE_RELAY", aggregate_node))
    wf.add_node("APPROVAL_RELAY",  _timed("APPROVAL_RELAY", approval_node))
    wf.add_node("EXECUTE_RELAY",   _timed("EXECUTE_RELAY", execute_node))     # Riusa execute_node (I relay sono Infra)
    wf.add_node("EXECUTE_HOSTS",   _timed("EXECUTE_HOSTS", execute_hosts_node)) # ← NUOVO: Esegue solo PC
    wf.add_node("VERIFY",          _timed("VERIFY", verify_node))
    wf.add_node("TROUBLESHOOT",    _timed("TROUBLESHOOT", troubleshoot_node))
    wf.add_node("APPROVAL_TROUBLESHOOT", _timed("APPROVAL_TROUBLESHOOT", approval_node))
    wf.add_node("SPEC_RECONCILE",  _timed("SPEC_RECONCILE", spec_reconcile_node))

    wf.set_entry_point("PARSE_INPUT")
    wf.add_edge("PARSE_INPUT", "OBSERVE")
    wf.add_edge("OBSERVE",     "PLAN")

    wf.add_conditional_edges("PLAN", _route_to_workers, ["GENERATE_SINGLE", END])

    wf.add_edge("GENERATE_SINGLE", "AGGREGATE")
    wf.add_edge("AGGREGATE",       "APPROVAL")
    wf.add_edge("APPROVAL",        "EXECUTE")

    # Dopo EXECUTE, si osserva la topologia per preparare un eventuale Relay
    wf.add_edge("EXECUTE", "OBSERVE_RELAY")

    # Bivio: Relay vs Hosts diretti
    wf.add_conditional_edges(
        "OBSERVE_RELAY",
        _route_to_relay_workers,
        ["GENERATE_RELAY", "EXECUTE_HOSTS"],
    )

    wf.add_edge("GENERATE_RELAY",  "AGGREGATE_RELAY")
    wf.add_edge("AGGREGATE_RELAY", "APPROVAL_RELAY")
    wf.add_edge("APPROVAL_RELAY",  "EXECUTE_RELAY")
    
    # Una volta eseguito il Relay, si passa finalmente ai PC terminali
    wf.add_edge("EXECUTE_RELAY",   "EXECUTE_HOSTS")

    wf.add_edge("EXECUTE_HOSTS",   "VERIFY")

    wf.add_conditional_edges("VERIFY", _route_after_verify, ["TROUBLESHOOT", "SPEC_RECONCILE", END])
    
    # In caso di fail, si ri-scatena l'execution del relay passando per l'approvazione del troubleshooting
    wf.add_conditional_edges("TROUBLESHOOT", _route_after_troubleshoot, ["APPROVAL_TROUBLESHOOT", END])
    wf.add_edge("APPROVAL_TROUBLESHOOT", "EXECUTE_RELAY")

    wf.add_edge("SPEC_RECONCILE", END)

    return wf