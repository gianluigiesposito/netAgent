# tools/template_engine.py
"""
Motore di template per NetAgent v2.1.

Due responsabilità ben separate:

  CliRenderer   — Renderizza template Jinja2 in sequenze di comandi CLI.
                  Input:  nome template + variabili del delta.
                  Output: lista di righe CLI pulite.

  OutputParser  — Parsa l'output testuale dei comandi show.
                  Strategia per vendor:
                    - FRRouting:    TextFSM (formato show interface brief)
                    - Cisco IOS/SW: parser colonnare deterministico
                                    (TextFSM non è robusto su 'administratively down')
                    - VPCS:         TextFSM (formato show ip)
                  Fallback automatico al parser regex legacy di parser.py
                  se TextFSM fallisce per qualsiasi motivo.

Perché TextFSM invece di pyATS/Genie:
  Genie richiede il suo device object per gestire la connessione ed è
  incompatibile con AsyncTelnetConnection/AsyncSSHConnection già esistenti.
  TextFSM opera offline su stringhe già raccolte: integrazione zero-cost,
  dipendenza ~50KB vs ~200MB di pyATS.

Perché Jinja2 invece di f-string in generate.py:
  La sintassi CLI non deve vivere nel codice Python.
  Il flag is_rollback sul template elimina la doppia lista cmd+rollback:
  un solo .j2 per operazione, manutenzione dimezzata.
  Aggiungere un vendor = aggiungere una cartella .j2, zero modifiche qui.
"""

from __future__ import annotations

import io
import logging
import re
from pathlib import Path
from typing import Any

import textfsm
from jinja2 import Environment, FileSystemLoader, StrictUndefined, TemplateNotFound

logger = logging.getLogger(__name__)

_TEMPLATES_ROOT = Path(__file__).parent / "templates"


# ─────────────────────────────────────────────────────────────────────────────
# CliRenderer
# ─────────────────────────────────────────────────────────────────────────────

class CliRenderer:
    """
    Renderizza template Jinja2 in liste di comandi CLI.

    Uso:
      renderer = CliRenderer()
      lines = renderer.render("cisco_ios/interface.j2",
                              iface="Ethernet0/0", ip="10.0.0.1",
                              mask="255.255.255.252", is_rollback=False)

    StrictUndefined garantisce che una variabile dimenticata sollevi
    un errore esplicito invece di produrre comandi silenziosamente errati.
    """

    def __init__(self) -> None:
        self._env = Environment(
            loader=FileSystemLoader(str(_TEMPLATES_ROOT / "cli")),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=False,
        )

    def render(self, template_path: str, **ctx: Any) -> list[str]:
        """Renderizza il template e restituisce le righe CLI pulite (no righe vuote)."""
        try:
            tmpl = self._env.get_template(template_path)
        except TemplateNotFound:
            logger.error("[CliRenderer] Template non trovato: %s", template_path)
            return []
        try:
            rendered = tmpl.render(**ctx)
        except Exception as e:
            logger.error("[CliRenderer] Errore rendering '%s': %s", template_path, e)
            return []
        return [
            line.rstrip()
            for line in rendered.splitlines()
            if line.strip() and not line.strip().startswith("{#")
        ]

    def render_with_rollback(
        self, template_path: str, **ctx: Any
    ) -> tuple[list[str], list[str]]:
        """
        Renderizza sia il comando sia il rollback dallo stesso template.
        Ritorna (forward_lines, rollback_lines).
        """
        forward  = self.render(template_path, is_rollback=False, **ctx)
        rollback = self.render(template_path, is_rollback=True,  **ctx)
        return forward, rollback

    def compile_action_plan(
        self,
        action_plan: Any,
        vendor_type: str,
    ) -> RouterCommands:
        """
        Compila un RouterActionPlan (generato dall'LLM) in coppie RouterCommands
        passando per i template Jinja2 invece di comandi hardcodati in Python.
        """
        from core.state import RouterCommands, CommandPair
        import ipaddress

        compiled_pairs: list[CommandPair] = []
        folder = vendor_type

        for action in action_plan.actions:
            act_dict = action if isinstance(action, dict) else action.dict()
            act_type = act_dict.get("action_type", act_dict.get("type", "")).lower()

            if act_type == "configure_interface":
                iface = act_dict.get("iface") or act_dict.get("interface")
                ip = act_dict.get("ip")
                cidr = act_dict.get("cidr", 24)

                if not iface or not ip:
                    logger.warning("[CliRenderer] 'configure_interface' saltata: dati mancanti (iface/ip)")
                    continue

                try:
                    netmask = str(ipaddress.IPv4Network(f"0.0.0.0/{cidr}").netmask)
                except ValueError:
                    netmask = "255.255.255.0"

                if folder == "frrouting":
                    tmpl = "frrouting/interface.j2"
                    ctx = {"iface": iface, "ip": ip, "cidr": cidr}
                else:
                    tmpl = "cisco_ios/interface.j2"
                    ctx = {"iface": iface, "ip": ip, "mask": netmask}

                fwd, rb = self.render_with_rollback(tmpl, **ctx)
                rb_iter = iter(rb)
                pairs = [CommandPair(cmd=cmd, rollback=next(rb_iter, "")) for cmd in fwd]
                compiled_pairs.extend(pairs)

            elif act_type == "add_static_route":
                net = act_dict.get("network")
                cidr = act_dict.get("cidr", 24)
                nh = act_dict.get("next_hop")

                if not net or not nh:
                    logger.warning("[CliRenderer] 'add_static_route' saltata: dati mancanti (network/next_hop)")
                    continue

                try:
                    netmask = str(ipaddress.IPv4Network(f"0.0.0.0/{cidr}").netmask)
                except ValueError:
                    netmask = "255.255.255.0"

                if folder == "frrouting":
                    tmpl = "frrouting/static_route.j2"
                    ctx = {"network": net, "cidr": cidr, "next_hop": nh}
                else:
                    tmpl = "cisco_ios/static_route.j2"
                    ctx = {"network": net, "mask": netmask, "next_hop": nh}

                fwd, rb = self.render_with_rollback(tmpl, **ctx)
                rb_iter = iter(rb)
                pairs = [CommandPair(cmd=cmd, rollback=next(rb_iter, "")) for cmd in fwd]
                compiled_pairs.extend(pairs)

            elif act_type == "configure_dhcp_pool":
                pool_name = act_dict.get("pool_name", "DEFAULT_POOL")
                network = act_dict.get("network")
                prefix_len = act_dict.get("prefix_len", 24)
                default_router = act_dict.get("default_router")
                dns_server = act_dict.get("dns_server", "8.8.8.8")
                lease_days = act_dict.get("lease_days", 1)
                excl_start = act_dict.get("excluded_start", default_router)
                excl_end = act_dict.get("excluded_end", excl_start)

                if not network or not default_router:
                    logger.warning("[CliRenderer] 'configure_dhcp_pool' saltata: dati mancanti (network/default_router)")
                    continue

                try:
                    netmask = str(ipaddress.IPv4Network(f"0.0.0.0/{prefix_len}").netmask)
                except ValueError:
                    netmask = "255.255.255.0"

                tmpl = "frrouting/dhcp_pool.j2"
                ctx = {
                    "pool_name": pool_name,
                    "network": network,
                    "netmask": netmask,
                    "default_router": default_router,
                    "dns_server": dns_server,
                    "lease_days": lease_days,
                    "excluded_start": excl_start,
                    "excluded_end": excl_end,
                }

                fwd, rb = self.render_with_rollback(tmpl, **ctx)
                rb_iter = iter(rb)
                pairs = [CommandPair(cmd=cmd, rollback=next(rb_iter, "")) for cmd in fwd]
                compiled_pairs.extend(pairs)

            elif act_type == "configure_vpcs":
                ip = act_dict.get("ip")
                mask = act_dict.get("mask", "255.255.255.0")
                gateway = act_dict.get("gateway")

                if not ip:
                    logger.warning("[CliRenderer] 'configure_vpcs' saltata: parametro 'ip' mancante")
                    continue

                mode = "dhcp" if ip.lower() == "dhcp" else "static"
                tmpl = "vpcs/host.j2"
                ctx = {"mode": mode, "ip": ip, "mask": mask, "gateway": gateway or ""}

                fwd, rb = self.render_with_rollback(tmpl, **ctx)
                rb_iter = iter(rb)
                pairs = [CommandPair(cmd=cmd, rollback=next(rb_iter, "")) for cmd in fwd]
                compiled_pairs.extend(pairs)

            else:
                logger.warning("[CliRenderer] Azione non riconosciuta: '%s'", act_type)

        return RouterCommands(pairs=compiled_pairs)



# ─────────────────────────────────────────────────────────────────────────────
# OutputParser
# ─────────────────────────────────────────────────────────────────────────────

class OutputParser:
    """
    Parsa l'output testuale dei comandi show.

    Strategia per vendor:
      frrouting    → TextFSM  (fallback regex se TextFSM fallisce)
      cisco_ios    → parser colonnare Python (più robusto di TextFSM per
                     'show ip interface brief' con 'administratively down')
      cisco_switch → stesso parser colonnare Python
      vpcs         → TextFSM  (fallback regex se TextFSM fallisce)
    """

    """
    _TEXTFSM_TEMPLATES: dict[str, str] = {
        "frrouting": "frr_show_interface_brief.textfsm",
        "vpcs":      "vpcs_show_ip.textfsm",
    }
    """
    _TEXTFSM_TEMPLATES: dict[str, str] = {
        "vpcs":      "vpcs_show_ip.textfsm",
    }

    # Vendor che usano il parser colonnare Python invece di TextFSM
    _COLUMNAR_VENDORS: frozenset[str] = frozenset(["cisco_ios", "cisco_switch"])

    def __init__(self) -> None:
        self._tmpl_dir = _TEMPLATES_ROOT / "textfsm"

    def parse_with_template(self, raw_output: str, template_filename: str) -> list[dict]:
        """Parsa l'output usando un template TextFSM specifico."""
        fsm = self._load_textfsm(template_filename)
        if fsm is None:
            return []
        try:
            return fsm.ParseTextToDicts(raw_output)
        except Exception as e:
            logger.error("[OutputParser] TextFSM parse error template='%s': %s", template_filename, e)
            return []

    def _load_textfsm(self, filename: str) -> textfsm.TextFSM | None:
        """Carica un template TextFSM. Nessuna cache: TextFSM è stateful."""
        path = self._tmpl_dir / filename
        if not path.exists():
            logger.error("[OutputParser] TextFSM non trovato: %s", path)
            return None
        try:
            return textfsm.TextFSM(io.StringIO(path.read_text()))
        except Exception as e:
            logger.error("[OutputParser] Errore compilazione TextFSM '%s': %s", filename, e)
            return None

    def parse_interfaces(self, raw_output: str, vendor: str) -> list[dict]:
        """
        Parsa 'show interfaces' e restituisce lista di dict:
          [{"name": "eth0", "ip": "1.2.3.4/24", "status": "up"}, ...]
        """
        if vendor in self._COLUMNAR_VENDORS:
            return _legacy_parse(raw_output, vendor)

        tmpl_name = self._TEXTFSM_TEMPLATES.get(vendor)
        if not tmpl_name:
            return _legacy_parse(raw_output, vendor)

        fsm = self._load_textfsm(tmpl_name)
        if fsm is None:
            return _legacy_parse(raw_output, vendor)

        try:
            rows = fsm.ParseTextToDicts(raw_output)
        except Exception as e:
            logger.error("[OutputParser] TextFSM parse error vendor='%s': %s", vendor, e)
            return _legacy_parse(raw_output, vendor)

        results = []
        for row in rows:
            name   = row.get("NAME", "").strip()
            ip_raw = row.get("IP", "").strip()
            cidr   = row.get("CIDR", "").strip()
            status = (row.get("STATUS") or row.get("PROTOCOL") or "up").strip().lower()

            if not name or name.lower().startswith(("lo", "mgmt", "dummy")):
                continue

            # Composizione IP/CIDR per VPCS
            if vendor == "vpcs" and ip_raw and cidr:
                ip = f"{ip_raw}/{cidr}"
            elif not ip_raw or ip_raw.startswith("0.0.0.0"):
                ip = "unassigned"
            else:
                ip = ip_raw

            status = "down" if "down" in status else "up"
            results.append({"name": name, "ip": ip, "status": status})

        if not results and raw_output.strip():
            logger.debug("[OutputParser] TextFSM ha restituito 0 righe per vendor='%s'. Fallback regex.", vendor)
            return _legacy_parse(raw_output, vendor)

        logger.debug("[OutputParser] %s: %d interfacce via TextFSM.", vendor, len(results))
        return results


# ─────────────────────────────────────────────────────────────────────────────
# Fallback legacy (safety net — non rimuovere)
# ─────────────────────────────────────────────────────────────────────────────

def _legacy_parse(raw_output: str, vendor: str) -> list[dict]:
    """Parser regex originale da tools/parser.py."""
    from tools.parser import parse_interfaces
    return parse_interfaces(raw_output, vendor)


# ─────────────────────────────────────────────────────────────────────────────
# Singleton
# ─────────────────────────────────────────────────────────────────────────────

renderer = CliRenderer()
parser   = OutputParser()
