#!/usr/bin/env python3
# llm/spec_wizard.py
"""
NetAgent Spec Wizard — Generatore interattivo di intenti YAML (NetworkIntentSchema).

Usa un motore LLM Unificato (Gemini o GitHub/OpenAI) come motore 
conversazionale per raccogliere le informazioni topologiche attraverso 
un dialogo guidato e generare la specifica nel formato YAML.

Uso:
    python llm/spec_wizard.py
    python llm/spec_wizard.py --output config/mylab.yaml
    python llm/spec_wizard.py --resume config/partial.yaml   # riprende da spec parziale
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import yaml
import ipaddress
from datetime import datetime
from pathlib import Path

try:
    from dotenv import load_dotenv
    from google import genai
    from google.genai import types
    from openai import OpenAI
    
    from rich.console import Console
    from rich.markdown import Markdown
    from rich.panel import Panel
    from rich.rule import Rule
    from rich.syntax import Syntax
    from rich.theme import Theme
    from prompt_toolkit import prompt as pt_prompt
    from prompt_toolkit.history import InMemoryHistory, FileHistory
    from prompt_toolkit.styles import Style as PtStyle
except ImportError:
    print(
        "\n[ERROR] Dipendenze mancanti. Esegui:\n"
        "  pip install google-genai openai python-dotenv rich prompt_toolkit PyYAML\n"
    )
    sys.exit(1)

# Caricamento variabili d'ambiente (LLM_PROVIDER, GEMINI_API_KEY, ecc.)
load_dotenv()

import time as _time
from tools.metrics import metrics as _metrics

from tools.dhcp_relay import extract_dhcp_relay_params
# ─────────────────────────────────────────────────────────────────────────────
# Configurazione UI
# ─────────────────────────────────────────────────────────────────────────────

THEME = Theme({
    "ai":      "bold cyan",
    "user":    "bold white",
    "spec":    "bold green",
    "warn":    "bold yellow",
    "error":   "bold red",
    "dim":     "dim white",
    "success": "bold green",
})

console = Console(theme=THEME)

PT_STYLE = PtStyle.from_dict({
    "prompt": "ansicyan bold",
})

# VLAN configuration for Spec Wizard (MGMT and Native VLANs)
def get_mgmt_vlan() -> str:
    return os.getenv("NETAGENT_MGMT_VLAN", "99")

def get_native_vlan() -> str:
    return os.getenv("NETAGENT_NATIVE_VLAN", "999")

def get_system_prompt() -> str:
    return get_system_prompt_for_phase(2)

def get_system_prompt_for_phase(phase: int, current_spec: str = "") -> str:
    mgmt_vlan = get_mgmt_vlan()
    native_vlan = get_native_vlan()

    # Rilevamento automatico se l'utente ha configurato una rete piatta (senza VLAN)
    is_flat = True
    if current_spec:
        try:
            parsed = yaml.safe_load(current_spec)
            if isinstance(parsed, dict) and "devices" in parsed:
                for dev in parsed["devices"]:
                    if not isinstance(dev, dict):
                        continue
                    # Se ci sono VLAN definite
                    if dev.get("vlans"):
                        is_flat = False
                        break
                    # Se ci sono interfacce con mode trunk, vlan_id o access_vlan != 1
                    for iface in dev.get("interfaces", []):
                        if not isinstance(iface, dict):
                            continue
                        if iface.get("vlan_id"):
                            is_flat = False
                            break
                        if iface.get("mode") == "trunk":
                            is_flat = False
                            break
                        if iface.get("access_vlan") and iface.get("access_vlan") != 1:
                            is_flat = False
                            break
        except Exception:
            pass

    flat_instructions = ""
    if is_flat:
        flat_instructions = f"""
⚠️ **IMPORTANTE - RETE PIATTA RILEVATA (Nessuna VLAN configurata)**:
L'utente ha scelto di NON utilizzare le VLAN per segmentare la rete (rete piatta di livello 3).
Di conseguenza:
1. NON proporre, consigliare o inserire configurazioni o attributi relativi a VLAN (vlan_id, access_vlan, trunk_vlans, ecc.) nello YAML.
2. NON utilizzare sottointerfacce (es. Ethernet0/0.10) o Router-on-a-Stick (ROAS). Configura gli indirizzi IP direttamente sulle interfacce fisiche padre (es. Ethernet0/0).
3. Le rotte statiche di default degli host e degli switch devono puntare all'indirizzo IP fisico dell'interfaccia del router connessa, NON a SVI o sottointerfacce.
4. Ignora qualsiasi regola o prompt sulla VLAN di management (es. VLAN {mgmt_vlan}). Non configurare interfacce SVI Vlan{mgmt_vlan}.
"""

    # ── SEZIONE 1: VINCOLI ASSOLUTI (mai violabili, precedono qualsiasi altra regola) ──────
    hard_constraints = f"""\
Sei NetAgent Spec Wizard. Compila in italiano una specifica YAML NetworkIntentSchema \
dialogando con l'operatore fase per fase.

## VINCOLI ASSOLUTI
Questi vincoli non possono essere derogati da nessuna richiesta dell'utente:

1. EMISSIONE YAML — Emetti sempre e solo un frammento parziale (Merge Patch) con le sole chiavi modificate.
   MAI riscrivere l'intero file. Formato obbligatorio:
   <<<SPEC_START>>>
   devices:
     - name: R1
       interfaces:
         - name: Ethernet0/0
           ip: 192.168.1.1/24
   <<<SPEC_END>>>

2. DELETE — Per rimuovere un elemento da una LISTA (come un dispositivo in `devices`, un'interfaccia in `interfaces`, una rotta in `static_routes`, o un pool in `dhcp_pools`), imposta `delete: true` all'interno di quell'oggetto.
   Per rimuovere o svuotare una mappa/dizionario (es. `vlans`) o annullare un valore singolo (es. `banner`), impostalo a `null` o ad un oggetto vuoto (es. `vlans: null` o `vlans: {{}}`). NON inserire MAI `delete: true` come chiave all'interno del dizionario `vlans`, poiché lo schema richiede solo numeri interi come chiavi VLAN.
   Omettere semplicemente un elemento di una lista NON lo rimuove: il sistema di merge lo conserverà comunque.

3. PHASE_COMPLETE — Emettilo SOLO dopo esplicita conferma dell'utente. Mai autonomamente.
   Formato: PHASE_COMPLETE su riga singola, poi il frammento YAML della fase.

4. INVENTARE DATI — Non inventare mai indirizzi IP, nomi di dispositivi o topologie.
   Se un dato essenziale manca, fai una domanda di chiarimento.

5. INTEGRITÀ TOPOLOGIA — Non rimuovere mai dispositivi esistenti di tua iniziativa.

6. SCHEMA — Il YAML deve rispettare sempre NetworkIntentSchema (definito sotto).

## GERARCHIA DI PRIORITÀ (in caso di conflitto tra regole)
1. Vincoli assoluti (sezione sopra)
2. Validità dello schema YAML
3. Richiesta esplicita dell'utente
4. Sicurezza e best practice di rete

Esempio: se l'utente chiede esplicitamente di usare Vlan1 per il management, rispetta la sua
scelta (priorità 3) anche se la best practice dice VLAN dedicata (priorità 4). Ma non inventare
un IP se non lo fornisce (priorità 1).
"""

    # ── SEZIONE 2: SCHEMA YAML (fonte di verità unica per la struttura) ──────────────────
    schema_section = f"""\
## SCHEMA YAML — NetworkIntentSchema

```
devices:
  - name: str              # obbligatorio, univoco
    profile: str           # obbligatorio: cisco_ios | cisco_switch | frrouting | vpcs
    interfaces:
      - name: str          # obbligatorio (es. Ethernet0/0, eth0, Ethernet0/0.10)
        ip: str            # CIDR o "dhcp". NO campo 'gateway' qui.
        vlan_id: int       # tag dot1q su sub-interfaccia ROAS
        mode: str          # access | trunk
        access_vlan: int   # solo se mode: access
        trunk_vlans: [int] # lista interi es. [10,20,99]. NO stringhe, NO allowed_vlans
        native_vlan: int   # solo se mode: trunk
        channel_group: int
        channel_mode: str  # active | passive | on
    static_routes:
      - network: str       # CIDR es. 0.0.0.0/0
        next_hop: str
    dhcp_pools:
      - name: str          # obbligatorio
        network: str       # obbligatorio, CIDR
        gateway: str       # obbligatorio
        dns: str           # default: 8.8.8.8
        lease: int         # default: 1
    vlans:
      <int>: str           # es. 10: "Clienti"
    dhcp_relay_server: str
    dhcp_relay_subnets: [str]
    hostname: str
    banner: str
    enable_secret: str
    domain_name: str
links:
  - endpoints: [str, str]  # es. ["PC1:eth0", "SW1:Ethernet0/1"]
```

### Campi obbligatori per procedere alla fase successiva
| Oggetto      | Obbligatori              | Default impliciti       | Opzionali (non bloccare) |
|--------------|--------------------------|-------------------------|--------------------------|
| Device       | name, profile            | —                       | hostname, banner, ecc.   |
| Interface    | name                     | —                       | ip, mode, vlan_id        |
| Link         | endpoints (2 endpoint)   | —                       | —                        |
| DHCP pool    | name, network, gateway   | dns=8.8.8.8, lease=1    | —                        |
| Static route | network, next_hop        | —                       | —                        |

Non bloccare la conversazione per campi opzionali o con default. Applica il default e vai avanti.
"""

    # ── SEZIONE 3: REGOLE TECNICHE DI RETE (fonte di verità unica per ogni concetto) ─────
    network_rules = f"""\
## REGOLE TECNICHE

**Profili dispositivo**
- Router (L3): `profile: cisco_ios`
- Switch (L2, VLAN/trunk): `profile: cisco_switch` — MAI `cisco_ios` per uno switch L2.
- Host finale: `profile: vpcs`
- Router Linux/FRR: `profile: frrouting`

⚠️ **RILEVAMENTO L3 E TRACCIAMENTO DOMINI DI BROADCAST (SUBNET IP)**:
1. Fidati SEMPRE del `profile` e NON del nome del dispositivo per determinarne la natura (L2 o L3). Qualsiasi dispositivo con profile `cisco_ios` o `frrouting` è un ROUTER (dispositivo di livello 3), anche se il suo nome contiene "IOU", "SW", "Switch" o simili.
2. Gli Switch L2 (`profile: cisco_switch`) sono trasparenti al livello 3: NON interrompono e NON creano confini per i domini di broadcast. Il dominio di broadcast attraversa liberamente lo switch L2.
3. Un dominio di broadcast (subnet IP) si estende attraverso tutti i link fisici e gli switch L2 fino a quando non si scontra con un'interfaccia L3 (interfaccia di un router o di un host `vpcs`).
4. Di conseguenza:
   - PC1 <-> Switch L2 <-> Router1 rappresenta un unico e solo dominio di broadcast (una sola subnet IP). Non creare subnet separate per la tratta PC-Switch e la tratta Switch-Router!
   - Router1 <-> Switch L2 <-> Router2 rappresenta un unico dominio di broadcast di transito (una sola subnet IP).
   - In una topologia come `PC1 <-> Switch1 <-> Router1 <-> Router2 <-> Switch2 <-> PC2` dove Router1 e Router2 sono router L3 e Switch1 e Switch2 sono switch L2, vi sono ESATTAMENTE 3 subnet distinte:
     - Subnet 1: PC1 - Switch1 - Router1 (porta e0)
     - Subnet 2: Router1 (porta e1) - Router2 (porta e1)
     - Subnet 3: Router2 (porta e0) - Switch2 - PC2

**Default gateway di ogni host**
Ogni host (vpcs o device con un solo uplink) DEVE avere una static_route verso `0.0.0.0/0`
puntata al gateway della propria VLAN/subnet, anche se usa DHCP.
MAI campo `gateway` sotto `interfaces` — solo in `static_routes`.
  static_routes:
    - network: 0.0.0.0/0
      next_hop: 192.168.10.1   # IP gateway della VLAN dell'host

**Default gateway degli switch**
Se lo switch (`profile: cisco_switch`) ha un indirizzo IP di management configurato (es. su Vlan1 o su una VLAN di management dedicata), deve avere anche una `static_route` `0.0.0.0/0` per essere raggiungibile da remoto.
Se la rete è piatta e lo switch non ha alcun IP di management configurato (agisce come switch L2 trasparente non gestito), NON richiedere né proporre alcun indirizzo IP o rotta statica per lo switch.

⚠️ **FLESSIBILITÀ DELLE RETI PIATTE E SWITCH L2**:
- Gli switch L2 (`profile: cisco_switch`) possono essere utilizzati in una rete piatta (senza VLAN custom) per collegare PC e router nello stesso dominio di broadcast. In questo caso, lavorano interamente sulla VLAN 1 di default.
- In una rete piatta, NON è necessario definire alcuna VLAN nella sezione `vlans` dello switch, e NON è necessario assegnare IP di management o rotte statiche agli switch L2.
- È perfettamente valido avere switch L2 senza alcuna configurazione di VLAN o IP. Non dire mai all'utente che gli switch L2 non possono funzionare in una rete piatta o che richiedono implicitamente VLAN!

**VPCS/host (profile: vpcs)** → MAI attributi switchport (mode, access_vlan, trunk_vlans, native_vlan).

**Router-on-a-Stick (ROAS) — coerenza obbligatoria dei nomi**
Le sub-interfacce DEVONO ereditare ESATTAMENTE il prefisso dell'interfaccia fisica padre.
Se la fisica è `e0/0` → le sub sono `e0/0.10`, `e0/0.20` (NON `Ethernet0/0.10`).
Se la fisica è `Ethernet0/1` → le sub sono `Ethernet0/1.10`, `Ethernet0/1.20` (NON `e0/1.10`).
Mescolare forme abbreviate e forme estese è un errore grave che rompe l'automazione.
Nei `links` si collega SOLO l'interfaccia fisica padre, mai le sub-interfacce.

**Native VLAN {native_vlan}** → Black-hole L2 per sicurezza (prevenzione VLAN hopping/double-tagging). Regole di sicurezza:
- Deve essere creata come VLAN locale sullo switch (es. sotto la sezione `vlans: {native_vlan}: "BlackHole_Native"`).
- Non deve avere alcun IP, nessuna sub-interfaccia router, nessuna interfaccia logica SVI L3 attiva o configurata.
- **Best Practice Suprema**: Deve essere ESCLUSA dalla lista delle VLAN permesse sui trunk (`trunk_vlans`). La lista `trunk_vlans` deve contenere solo le VLAN di transito e NON includere `{native_vlan}`.

**MGMT VLAN {mgmt_vlan} — piano di indirizzamento fisso**
Schema da rispettare nella subnet di management:
  - .1  → gateway sul router (sub-interfaccia ROAS o SVI)
  - .2  → IP management del primo switch
  - .3+ → IP management di switch aggiuntivi
  - .5+ → host di management (PC gestione, jump host, ecc.)
Non assegnare .2 a un host se nella stessa subnet esiste uno switch gestito.
Non confondere MGMT VLAN con native VLAN. Eccezione: rete piatta → accetta Vlan1 se richiesto.

**Porte access** → Solo `access_vlan`. MAI `native_vlan` o `trunk_vlans`.

**Porte trunk** → `trunk_vlans` come lista di interi (escludendo sempre `{native_vlan}`), `native_vlan: {native_vlan}`.

**EtherChannel** → La config switchport delle porte fisiche deve coincidere con il Port-channel logico.
"""
    # ── SEZIONE 4: PREFERENZE DI COMPORTAMENTO (derogabili dall'utente o dal contesto) ───
    behavior_prefs = """\
## PREFERENZE DI COMPORTAMENTO

- Raccogli 2-4 informazioni strettamente correlate nello stesso turno (es. nome + profilo + ruolo).
  Non fare domande su dati non correlati nella stessa risposta.
- Quando mancano dati essenziali, fai una domanda diretta. Quando hai certezza, afferma.
- Proponi valori di default ragionevoli per i campi opzionali senza chiedere conferma.
- Segnala configurazioni non sicure (es. native VLAN 1) ma procedi se l'utente conferma la scelta.
- Rimani negli obiettivi della fase attiva. Non anticipare configurazioni di fasi successive.
"""

    # ── SEZIONE 5: OBIETTIVI DI FASE (scope limitato, nessuna ridondanza con le sezioni sopra) ─
    phase_objectives = {
        1: """\
## FASE 1 — TOPOLOGIA FISICA
Obiettivo: nome, profilo e interfacce fisiche di ogni nodo + sezione `links` completa.
- Raccogli nome, profilo e interfacce in un unico turno per dispositivo.
- Ogni interfaccia usata in un link deve essere presente anche sotto `interfaces` del device.
- Non aggiungere IP, VLAN, DHCP o credenziali (conservali se già presenti nello YAML).
""",
        2: f"""\
## FASE 2 — SWITCHING L2 E VLAN
Obiettivo: VLAN, modalità porte (access/trunk), EtherChannel.
- Se l'utente vuole rete piatta su VLAN 1, procedi senza configurare VLAN custom.
- Raccogli ID e nome delle VLAN in un unico turno, poi configura le porte.
- Non aggiungere IP o DHCP (conservali se già presenti).
""",
        3: """\
## FASE 3 — INDIRIZZAMENTO IP, DHCP E RELAY
Obiettivo: IP su tutte le interfacce attive, pool DHCP, relay, rotte di default.
- **Algoritmo di Tracciamento Subnet**: Analizza la topologia dei collegamenti (`links`) partendo dal profilo (`profile`) di ciascun dispositivo per mappare le subnet IP (domini di broadcast).
  - Gli switch L2 (`profile: cisco_switch`) non hanno indirizzi IP sulle interfacce fisiche e non dividono le subnet.
  - Una subnet si estende dall'interfaccia di un host (PC) o router, attraversa gli switch L2, e si ferma all'interfaccia del router successivo.
  - Per ciascuna subnet identificata, associa tutti i dispositivi e le relative porte fisiche che vi fanno parte (es. PC1 e Router1 sono nella stessa subnet insieme allo switch L2 che li collega).
  - Conta e individua con precisione il numero totale di segmenti IP indipendenti (es. in `lab3` di base ci sono esattamente 3 subnet distinte: PC1-IOU1-FRR8.2.2-1, transito FRR8.2.2-1-IOU3, e IOU3-IOU2-PC2. Non considerare mai i link Switch-PC e Switch-Router come subnet separate!).
- Raccogli IP e piano di indirizzamento in un turno prima di emettere il patch.
- Per i pool DHCP applica dns=8.8.8.8 e lease=1 come default senza chiedere.
- Configura rotte di ritorno sul server DHCP verso tutte le subnet client.
- CHECKLIST obbligatoria a fine fase — verifica che siano presenti:
  □ static_route 0.0.0.0/0 su ogni host (vpcs), anche se usa DHCP
  □ static_route 0.0.0.0/0 su ogni switch gestito (solo se ha un IP di management) verso il gateway della VLAN management
  □ Tutti i pool DHCP con gateway corretto
  □ Sub-interfacce ROAS con nome coerente con l'interfaccia fisica padre
  □ Nessun host con IP .2 nella subnet management se esiste uno switch con quell'IP
""",
        4: f"""\
## FASE 4 — MANAGEMENT E SICUREZZA
Obiettivo: hostname, banner, domain_name, enable_secret su ogni apparato.
- Per `enable_secret`, imposta sempre come valore la stringa di placeholder `"env:NETAGENT_DEV_PASSWORD_DEFAULT"` (o `"env:NETAGENT_DEV_ENABLE_PASSWORD_[DEVICE]"` se specifico) per consentire la risoluzione dinamica dal file `.env` ed evitare credenziali in chiaro nello YAML.
- Usa placeholder (admin/cisco) per gli altri dati comuni. Informa l'utente che le credenziali reali si configurano nel file `.env` locale tramite NETAGENT_DEV_PASSWORD_*.
- IP management switch su VLAN {get_mgmt_vlan()} (richiesto solo per switch gestiti; in una rete piatta senza VLAN o IP di management, ignora gli switch).
- Raccogli hostname e domain_name di tutti gli apparati in un unico turno.
""",
        5: """\
## FASE 5 — VALIDAZIONE FINALE
Obiettivo: correggere tutti gli errori/avvisi del validatore, poi emettere PHASE_COMPLETE.
- Analizza gli errori per path specifico (es. devices[0].interfaces[1].ip).
- Emetti un patch mirato per ogni correzione. Non riscrivere l'intero file.
- Quando lo YAML è privo di errori critici, emetti PHASE_COMPLETE.
"""
    }

    return (
        hard_constraints
        + schema_section
        + network_rules
        + flat_instructions
        + behavior_prefs
        + phase_objectives.get(phase, phase_objectives[5])
    )


def _sanitize_secrets(text: str) -> str:
    """Maschera le informazioni sensibili come password, enable secret e chiavi private da una stringa."""
    if not isinstance(text, str):
        return text
    
    # 1. Mask dictionary-like key-value pairs first (JSON, YAML, Python dicts)
    text = re.sub(
        r'([\'"]?(?:enable_secret|password|key-string)[\'"]?\s*:\s*)(?![\'"]?\*+[\'"]?)[\'"]?[^\'"\n,]+[\'"]?',
        r'\1"********"',
        text,
        flags=re.IGNORECASE
    )
    
    # 2. Mask inline CLI secrets (avoiding colons or already masked ones)
    text = re.sub(
        r'\b(enable\s+secret\s+)(?![\'":\*]+)\S+',
        r'\1********',
        text,
        flags=re.IGNORECASE
    )
    text = re.sub(
        r'\b(password\s+)(?![\'":\*]+)\S+',
        r'\1********',
        text,
        flags=re.IGNORECASE
    )
    text = re.sub(
        r'\b(key-string\s+)(?![\'":\*]+)\S+',
        r'\1********',
        text,
        flags=re.IGNORECASE
    )
    return text


def _sanitize_session_data(data: dict) -> dict:
    """Pulisce ricorsivamente un dizionario per nascondere credenziali o dati sensibili nei log."""
    sanitized = {}
    for k, v in data.items():
        if isinstance(v, str):
            sanitized[k] = _sanitize_secrets(v)
        elif isinstance(v, list):
            sanitized[k] = []
            for item in v:
                if isinstance(item, dict):
                    sanitized_item = {}
                    for ik, iv in item.items():
                        if isinstance(iv, str):
                            sanitized_item[ik] = _sanitize_secrets(iv)
                        else:
                            sanitized_item[ik] = iv
                    sanitized[k].append(sanitized_item)
                else:
                    sanitized[k].append(_sanitize_secrets(item) if isinstance(item, str) else item)
        elif isinstance(v, dict):
            sanitized[k] = _sanitize_session_data(v)
        else:
            sanitized[k] = v
    return sanitized


def _prune_history_specs(history: list[dict]) -> list[dict]:
    """
    Rimpiazza le specifiche YAML intermedie nei messaggi passati della cronologia
    con un segnaposto leggero, mantenendo solo l'ultima versione per risparmiare token.
    """
    if not history:
        return []
    
    pruned = []
    # Trova l'indice dell'ultimo messaggio contenente <<<SPEC_START>>>
    last_spec_idx = -1
    for idx, msg in enumerate(history):
        if "<<<SPEC_START>>>" in msg.get("content", ""):
            last_spec_idx = idx

    for idx, msg in enumerate(history):
        content = msg.get("content", "")
        # Se il messaggio contiene la specifica e non è l'ultimo blocco di specifica, lo tronchiamo
        if "<<<SPEC_START>>>" in content and idx != last_spec_idx:
            content = re.sub(
                r"<<<SPEC_START>>>.*?<<<SPEC_END>>>",
                "<<<SPEC_START>>>\n# [Specifica YAML precedente omessa per risparmio token]\n<<<SPEC_END>>>",
                content,
                flags=re.DOTALL
            )
        pruned.append({"role": msg.get("role", "user"), "content": content})
    return pruned


# ─────────────────────────────────────────────────────────────────────────────
# Client Unificato Sincrono
# ─────────────────────────────────────────────────────────────────────────────

class SyncLLMClient:
    """
    Client LLM Sincrono per Spec Wizard.
    Gestisce il fallback logico tra Gemini e GitHub Models (OpenAI).
    """
    def __init__(self) -> None:
        self.provider = os.getenv("LLM_PROVIDER", "github").lower()


        # Initialize Google GenAI client if GEMINI_API_KEY is present
        api_key = os.getenv("GEMINI_API_KEY")
        if api_key:
            self._google = genai.Client(api_key=api_key)
            self.gemini_model = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
        else:
            self._google = None
            self.gemini_model = None

        # Initialize OpenAI/GitHub client if GITHUB_TOKEN is present
        token = os.getenv("GITHUB_TOKEN")
        if token:
            self._openai = OpenAI(
                base_url="https://models.inference.ai.azure.com",
                api_key=token,
            )
            self.github_model = os.getenv("GITHUB_MODEL", "gpt-4o-mini")
        else:
            self._openai = None
            self.github_model = None

        # Validate that the selected provider is initialized
        if self.provider == "gemini":
            if not self._google:
                console.print("[error]GEMINI_API_KEY non trovata nell'ambiente ma provider impostato su gemini.[/error]")
                sys.exit(1)
            self.model_name = self.gemini_model
        elif self.provider == "github":
            if not self._openai:
                console.print("[error]GITHUB_TOKEN non trovata nell'ambiente ma provider impostato su github.[/error]")
                sys.exit(1)
            self.model_name = self.github_model
        else:
            console.print(f"[error]Provider LLM non supportato: '{self.provider}'[/error]")
            sys.exit(1)

    def bootstrap_from_image(self, image_path: Path, reference_spec: str = "", feedback: str = "") -> str:
        """Usa il VLM per estrarre la topologia iniziale da un'immagine, opzionalmente ereditando parametri da una specifica di riferimento."""
        ref_context = ""
        if reference_spec:
            ref_context = (
                "\n\nTi viene fornita anche la seguente specifica di riferimento parziale o file IaC compact:\n"
                "<<<REFERENCE_START>>>\n"
                f"{reference_spec}\n"
                "<<<REFERENCE_END>>>\n"
                "Usa questa specifica per ereditare profili, nomi di dispositivi, ed eventuali indirizzi IP/DHCP/VLAN se definiti, "
                "e uniscili con i collegamenti fisici estratti dall'immagine. Non perdere nessun dispositivo specificato nel riferimento!"
            )

        system_prompt = (
            "Sei un assistente esperto di rete. Analizza lo schema della topologia fornito nell'immagine "
            "ed estrai l'elenco dei dispositivi fisici e le loro interconnessioni (links) fisiche.\n\n"
            "Regole di generazione dello YAML (NetworkIntentSchema):\n"
            "1. Ogni dispositivo presente nell'immagine o nella specifica di riferimento "
            "deve essere elencato INDIVIDUALMENTE nella sezione 'devices'. NON raggruppare i dispositivi (es. non scrivere "
            "'name: PC1, PC2, PC3'). Ciascuno deve avere il proprio nome e profilo ('cisco_ios', 'cisco_switch', 'vpcs', 'frrouting').\n"
            "2. Sotto la sezione 'interfaces' di ciascun dispositivo, dichiara esclusivamente le sue interfacce fisiche necessarie "
            "viste nell'immagine (es. 'Ethernet0/0', 'eth0'). Ciascuna interfaccia deve essere strutturata come un dizionario con la chiave 'name' (es. `- name: eth0`). "
            "Non impostare alcun indirizzo IP o DHCP o altre configurazioni L2/L3 in questa fase (Fase 1), a meno che non siano ereditate direttamente dalla specifica di riferimento. "
            "Se non specificato diversamente nella specifica di riferimento, non mettere 'ip: dhcp' o indirizzi IP nei PC o router.\n"
            "3. Tutti i collegamenti fisici visibili nel diagramma devono essere estratti e inseriti in una sezione 'links' a livello principale dello YAML. "
            "Ogni collegamento deve essere una voce nella lista con la chiave 'endpoints', che contiene due stringhe formattate come 'NomeDispositivo:NomeInterfaccia' "
            "(es. `- endpoints: ['Switch1:Ethernet0/2', 'PC1:eth0']`).\n"
            "4. Usa nomi di interfaccia standardizzati (es. 'Ethernet0/0', 'Ethernet0/1' per apparati Cisco, 'eth0', 'eth1' per host VPCS e router FRRouting).\n"
            "5. Se viene fornita una specifica di riferimento (IaC compact o parziale), preserva e integra tutti i dettagli dei dispositivi (come 'profile', 'static_routes', 'dhcp_pools', 'vlans', 'hostname', 'banner', 'enable_secret', 'domain_name', 'extra_params', ecc.) "
            "con la topologia fisica estratta dall'immagine. Unisci le interfacce fisiche estratte dall'immagine con quelle della specifica di riferimento (es. se la specifica di riferimento dichiara un'interfaccia con IP, e l'immagine mostra la sua connessione fisica, unisci queste informazioni mantenendo l'IP e la porta corretti).\n"
            "6. Formatta l'output includendo i marker:\n"
            "<<<SPEC_START>>>\n"
            "... contenuto YAML ...\n"
            "<<<SPEC_END>>>\n"
            "7. Ogni interfaccia fisica di un dispositivo può essere utilizzata in al massimo un solo link nella sezione 'links'. Se assegni la stessa porta fisica a collegamenti molteplici, si verificherà un errore. Assicurati che non ci siano porte duplicate nei link. Se necessario, assegna porte diverse (ad es. Ethernet0/2, Ethernet0/3 ecc.) per le varie connessioni dello stesso dispositivo."
        ) + ref_context
        
        user_message = "Analizza l'immagine ed estrai la topologia di rete."
        if feedback:
            user_message += f"\n\nNOTA: Correggi i seguenti errori di validazione riscontrati nello YAML precedente:\n{feedback}"
        
        max_retries = 3
        current_provider = self.provider
        providers_to_try = [current_provider]
        other_provider = "github" if current_provider == "gemini" else "gemini"
        
        if other_provider == "github" and self._openai:
            providers_to_try.append("github")
        elif other_provider == "gemini" and self._google:
            providers_to_try.append("gemini")
            
        last_exception = None
        
        for prov in providers_to_try:
            prov_model = self.gemini_model if prov == "gemini" else self.github_model
            for attempt in range(1, max_retries + 1):
                try:
                    if prov == "gemini":
                        import mimetypes
                        data = image_path.read_bytes()
                        mime, _ = mimetypes.guess_type(str(image_path))
                        image_part = types.Part.from_bytes(data=data, mime_type=mime or "image/png")
                        
                        _t0 = _time.monotonic()
                        response = self._google.models.generate_content(
                            model=prov_model,
                            contents=[image_part, user_message],
                            config=types.GenerateContentConfig(
                                system_instruction=system_prompt,
                                temperature=0.0
                            )
                        )
                        _dur = _time.monotonic() - _t0
                        _in_tok = 0
                        _out_tok = 0
                        if hasattr(response, "usage_metadata") and response.usage_metadata:
                            _in_tok = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
                            _out_tok = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
                        _metrics.record_llm_call("spec_wizard.bootstrap_from_image", prov_model, "gemini", _in_tok, _out_tok, _dur)
                        return response.text
                    else:
                        import base64
                        with open(image_path, "rb") as f:
                            image_b64 = base64.b64encode(f.read()).decode()
                        
                        content = [
                            {"type": "text", "text": user_message},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                            }
                        ]
                        
                        _t0 = _time.monotonic()
                        response = self._openai.chat.completions.create(
                            model=prov_model,
                            messages=[
                                {"role": "system", "content": system_prompt},
                                {"role": "user", "content": content},
                            ],
                            temperature=0.0,
                        )
                        _dur = _time.monotonic() - _t0
                        _in_tok = 0
                        _out_tok = 0
                        if hasattr(response, "usage") and response.usage:
                            _in_tok = getattr(response.usage, "prompt_tokens", 0) or 0
                            _out_tok = getattr(response.usage, "completion_tokens", 0) or 0
                        _metrics.record_llm_call("spec_wizard.bootstrap_from_image", prov_model, "github", _in_tok, _out_tok, _dur)
                        return response.choices[0].message.content
                except Exception as e:
                    last_exception = e
                    console.print(f"[yellow]Tentativo VLM {attempt}/{max_retries} fallito con il provider {prov} ({prov_model}): {e}[/yellow]")
                    if attempt < max_retries:
                        import time
                        sleep_time = 2 ** attempt
                        console.print(f"[dim]Attesa di {sleep_time} secondi prima di riprovare...[/dim]")
                        time.sleep(sleep_time)
            
            console.print(f"[orange3]Tutti i tentativi VLM con il provider {prov} sono falliti.[/orange3]")
            
        # Fallback JSON serialization for debugging
        fallback_session = _sanitize_session_data({
            "timestamp": datetime.now().isoformat(),
            "image_path": str(image_path),
            "reference_spec": reference_spec,
            "feedback": feedback,
            "error": str(last_exception)
        })
        
        fallback_path = Path("config") / f".wizard_error_vlm_fallback_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        fallback_path.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            with open(fallback_path, "w", encoding="utf-8") as f:
                json.dump(fallback_session, f, indent=4, ensure_ascii=False)
            console.print(f"[red]Errore VLM persistente. Lo stato attuale è stato salvato in: {fallback_path}[/red]")
        except Exception as save_err:
            console.print(f"[red]Impossibile salvare lo stato della sessione: {save_err}[/red]")
            
        raise last_exception

    def parse_text_spec_to_yaml(self, text_spec: str) -> str:
        """Usa l'LLM per convertire una specifica descrittiva testuale in formato YAML conforme a NetworkIntentSchema."""
        system_prompt = (
            "Sei un assistente esperto di rete. Converti la specifica descrittiva fornita in un file YAML conforme a NetworkIntentSchema.\n\n"
            "Regole di generazione dello YAML:\n"
            "1. Ogni dispositivo definito nel testo deve essere elencato sotto la sezione 'devices'.\n"
            "2. Estrai tutte le interfacce fisiche, gli indirizzi IP/CIDR, le rotte statiche, i pool DHCP, le VLAN e le credenziali specificate nel testo per ciascun dispositivo.\n"
            "3. Rispetta scrupolosamente lo schema NetworkIntentSchema (ad es. 'vlans' deve essere una mappa id -> name, 'interfaces' deve essere una lista di oggetti con 'name', 'ip', ecc.).\n"
            "4. Formatta l'output includendo i marker:\n"
            "<<<SPEC_START>>>\n"
            "... contenuto YAML ...\n"
            "<<<SPEC_END>>>"
        )
        
        history = []
        return self.chat(history, text_spec, system_prompt)

    def chat(self, history: list[dict], user_message: str, system_prompt: str | None = None, image_path: Path | None = None, current_spec: str | None = None) -> str:
        """Invia un messaggio, aggiorna la cronologia universale e restituisce la risposta.
        
        La specifica corrente viene iniettata UNA SOLA VOLTA come contesto nel messaggio utente
        (non nel system prompt, per evitare doppio costo token). La history viene potata per
        rimuovere le specifiche YAML intermedie obsolete.
        """
        if system_prompt is None:
            system_prompt = get_system_prompt()

        # Costruiamo il messaggio utente finale: se abbiamo la spec corrente, la antepoiamo
        # come contesto compatto. Questo sostituisce completamente la vecchia iniezione nel
        # system_prompt (che causava il doppio costo token: spec nel SP + spec nella history).
        if current_spec:
            effective_user_message = (
                f"STATO_SPEC_CORRENTE:\n<<<CURRENT_SPEC_START>>>\n{current_spec}\n<<<CURRENT_SPEC_END>>>\n\n"
                f"{user_message}"
            )
        else:
            effective_user_message = user_message

        pruned_history = _prune_history_specs(history)
            
        max_retries = 3
        current_provider = self.provider
        providers_to_try = [current_provider]
        other_provider = "github" if current_provider == "gemini" else "gemini"
        
        if other_provider == "github" and self._openai:
            providers_to_try.append("github")
        elif other_provider == "gemini" and self._google:
            providers_to_try.append("gemini")
            
        last_exception = None
        
        for prov in providers_to_try:
            prov_model = self.gemini_model if prov == "gemini" else self.github_model
            for attempt in range(1, max_retries + 1):
                try:
                    if prov == "gemini":
                        contents = []
                        for msg in pruned_history:
                            role = "user" if msg["role"] == "user" else "model"
                            contents.append({"role": role, "parts": [{"text": msg["content"]}]})
                        
                        if image_path:
                            import mimetypes
                            data = image_path.read_bytes()
                            mime, _ = mimetypes.guess_type(str(image_path))
                            image_part = types.Part.from_bytes(data=data, mime_type=mime or "image/png")
                            contents.append({"role": "user", "parts": [image_part, {"text": effective_user_message}]})
                        else:
                            contents.append({"role": "user", "parts": [{"text": effective_user_message}]})
 
                        _t0 = _time.monotonic()
                        response = self._google.models.generate_content(
                             model=prov_model,
                             contents=contents,
                             config=types.GenerateContentConfig(
                                 system_instruction=system_prompt,
                                 temperature=0.0
                             )
                        )
                        _dur = _time.monotonic() - _t0
                        _in_tok = 0
                        _out_tok = 0
                        if hasattr(response, "usage_metadata") and response.usage_metadata:
                            _in_tok = getattr(response.usage_metadata, "prompt_token_count", 0) or 0
                            _out_tok = getattr(response.usage_metadata, "candidates_token_count", 0) or 0
                        _metrics.record_llm_call("spec_wizard.chat", prov_model, "gemini", _in_tok, _out_tok, _dur)
                        assistant_text = response.text
                    else:
                        messages = [{"role": "system", "content": system_prompt}]
                        messages.extend(pruned_history)
                        
                        if image_path:
                            import base64
                            with open(image_path, "rb") as f:
                                image_b64 = base64.b64encode(f.read()).decode()
                            
                            content = [
                                {"type": "text", "text": effective_user_message},
                                {
                                    "type": "image_url",
                                    "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                                }
                            ]
                            messages.append({"role": "user", "content": content})
                        else:
                            messages.append({"role": "user", "content": effective_user_message})
 
                        _t0 = _time.monotonic()
                        response = self._openai.chat.completions.create(
                            model=prov_model,
                            messages=messages,
                            temperature=0.0,
                        )
                        _dur = _time.monotonic() - _t0
                        _in_tok = 0
                        _out_tok = 0
                        if hasattr(response, "usage") and response.usage:
                            _in_tok = getattr(response.usage, "prompt_tokens", 0) or 0
                            _out_tok = getattr(response.usage, "completion_tokens", 0) or 0
                        _metrics.record_llm_call("spec_wizard.chat", prov_model, "github", _in_tok, _out_tok, _dur)
                        assistant_text = response.choices[0].message.content
                        
                    # Successfully got response
                    history_user_msg = user_message
                    if image_path:
                        history_user_msg = f"[Immagine caricata: {image_path.name}] {user_message}"
                    
                    history.append({"role": "user", "content": history_user_msg})
                    history.append({"role": "assistant", "content": assistant_text})
                    return assistant_text
                except Exception as e:
                    last_exception = e
                    console.print(f"[yellow]Tentativo {attempt}/{max_retries} fallito con il provider {prov} ({prov_model}): {e}[/yellow]")
                    if attempt < max_retries:
                        import time
                        sleep_time = 2 ** attempt
                        console.print(f"[dim]Attesa di {sleep_time} secondi prima di riprovare...[/dim]")
                        time.sleep(sleep_time)
            
            console.print(f"[orange3]Tutti i tentativi con il provider {prov} sono falliti.[/orange3]")
            
        # Fallback JSON serialization for debugging
        fallback_session = _sanitize_session_data({
            "timestamp": datetime.now().isoformat(),
            "system_prompt": system_prompt,
            "history": history,
            "failed_user_message": user_message,
            "error": str(last_exception)
        })
        
        fallback_path = Path("config") / f".wizard_error_session_fallback_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        fallback_path.parent.mkdir(parents=True, exist_ok=True)
        
        try:
            with open(fallback_path, "w", encoding="utf-8") as f:
                json.dump(fallback_session, f, indent=4, ensure_ascii=False)
            console.print(f"[red]Errore persistente nell'LLM. Lo stato attuale della sessione è stato salvato in: {fallback_path}[/red]")
        except Exception as save_err:
            console.print(f"[red]Impossibile salvare lo stato della sessione: {save_err}[/red]")
            
        raise last_exception



# ─────────────────────────────────────────────────────────────────────────────
# Estrazione spec dal messaggio LLM
# ─────────────────────────────────────────────────────────────────────────────

def _extract_spec(text: str) -> str | None:
    """Estrae il contenuto tra <<<SPEC_START>>> e <<<SPEC_END>>>."""
    m = re.search(r'<<<SPEC_START>>>\s*(.*?)\s*<<<SPEC_END>>>', text, re.DOTALL)
    return m.group(1).strip() if m else None


def _is_spec_ready(text: str) -> bool:
    return "SPEC_READY" in text


def _validate_topology_and_get_models(spec_text: str) -> tuple[list[str], dict | None, NetworkIntentSchema | None]:
    """Valuta la topologia fisica interna e restituisce eventuali errori insieme al dizionario parsed e all'oggetto Pydantic intent, se validi."""
    errors = []
    try:
        parsed_data = yaml.safe_load(spec_text)
        if not isinstance(parsed_data, dict):
            errors.append("La specifica deve essere un dizionario YAML valido.")
            return errors, None, None
    except Exception as e:
        errors.append(f"Errore di sintassi YAML: {e}")
        return errors, None, None

    from core.state import NetworkIntentSchema
    try:
        intent = NetworkIntentSchema.model_validate(parsed_data)
    except Exception as e:
        from pydantic import ValidationError
        if isinstance(e, ValidationError):
            for err in e.errors():
                loc = " -> ".join(str(x) for x in err["loc"])
                errors.append(f"Validazione fallita in '{loc}': {err['msg']} (valore fornito: {err.get('input')})")
        else:
            errors.append(f"Errore di validazione dello schema: {e}")
        return errors, parsed_data, None

    # VALIDAZIONE LINKS
    links_data = parsed_data.get("links", [])
    if isinstance(links_data, list):
        endpoint_count = {}
        for link_idx, link in enumerate(links_data):
            if not isinstance(link, dict) or "endpoints" not in link:
                errors.append(f"Il link alla posizione {link_idx} nella sezione 'links' è malformato.")
                continue
            endpoints = link.get("endpoints")
            if not isinstance(endpoints, list) or len(endpoints) != 2:
                errors.append(f"Il link alla posizione {link_idx} deve contenere esattamente due endpoints.")
                continue
            
            for ep in endpoints:
                if not isinstance(ep, str) or ":" not in ep:
                    errors.append(f"L'endpoint '{ep}' nel link {link_idx} è malformato. Deve essere nel formato 'Dispositivo:Interfaccia'.")
                    continue
                
                # Check uniqueness of physical port connection
                endpoint_count[ep] = endpoint_count.get(ep, 0) + 1
                
                dev_name, iface_name = ep.split(":", 1)
                
                # Check if device exists in spec
                device_intent = next((d for d in intent.devices if d.name == dev_name), None)
                if not device_intent:
                    errors.append(f"Il link alla posizione {link_idx} fa riferimento al dispositivo '{dev_name}' che non esiste nella sezione 'devices'.")
                else:
                    # Check if interface exists under the device
                    iface_exists = any(i.name == iface_name for i in device_intent.interfaces)
                    if not iface_exists:
                        errors.append(f"Il dispositivo '{dev_name}' è connesso su '{iface_name}' nel link {link_idx}, ma questa interfaccia non è dichiarata nella sua lista 'interfaces'.")
        
        # Add warnings for duplicate interface usage
        for ep, count in endpoint_count.items():
            if count > 1:
                dev_name, iface_name = ep.split(":", 1)
                errors.append(f"L'interfaccia '{iface_name}' del dispositivo '{dev_name}' è utilizzata in {count} collegamenti diversi nella sezione 'links'. Ciascuna porta fisica può avere al massimo una connesione.")
    return errors, parsed_data, intent


def validate_topology(spec_text: str) -> list[str]:
    """Valuta la topologia fisica (dispositivi, interfacce e links) per rilevare errori strutturali e porte duplicate."""
    errors, _, _ = _validate_topology_and_get_models(spec_text)
    return errors


def validate_spec_content(spec_text: str, phase: int | None = None) -> tuple[list[str], list[str]]:
    """Valuta la specifica YAML per rilevare misconfigurazioni L2/L3 e rischi di sicurezza basandosi sulla fase corrente.
    Ritorna una tupla (errors, warnings):
    - errors: errori critici che bloccano la validazione o l'esecuzione.
    - warnings: raccomandazioni e avvisi semantici non bloccanti.
    """
    errors, parsed_data, intent = _validate_topology_and_get_models(spec_text)
    warnings = []
    if errors or parsed_data is None or intent is None:
        return errors, warnings

    # Check L2 interface consistency in parsed_data
    if phase is None or phase >= 2:
        if isinstance(parsed_data, dict):
            devices_list = parsed_data.get("devices", [])
            if isinstance(devices_list, dict):
                normalized_devs = []
                for name, data in devices_list.items():
                    if isinstance(data, dict):
                        d = data.copy()
                        d["name"] = name
                        normalized_devs.append(d)
                    elif isinstance(data, str):
                        normalized_devs.append({"name": name, "profile": data})
                    else:
                        normalized_devs.append({"name": name})
                devices_list = normalized_devs

            if isinstance(devices_list, list):
                for dev in devices_list:
                    if not isinstance(dev, dict):
                        continue
                    dev_name = dev.get("name")
                    profile = dev.get("profile", "")
                    interfaces = dev.get("interfaces", [])
                    if not isinstance(interfaces, list):
                        continue
                    
                    is_vpcs = (profile == "vpcs" or (isinstance(dev_name, str) and "pc" in dev_name.lower()))
                    
                    for iface in interfaces:
                        if not isinstance(iface, dict):
                            continue
                        iface_name = iface.get("name")
                        
                        if is_vpcs:
                            l2_keys = ["mode", "access_vlan", "trunk_vlans", "native_vlan"]
                            found_keys = [k for k in l2_keys if k in iface]
                            if found_keys:
                                errors.append(
                                    f"Dispositivo {dev_name} (VPCS/Host): L'interfaccia '{iface_name}' ha configurato parametri L2 switchport non validi: {', '.join(found_keys)}. I PC non supportano VLAN native, trunk o access."
                                )
                        else:
                            mode = iface.get("mode")
                            if mode == "access":
                                if "native_vlan" in iface:
                                    errors.append(
                                        f"Dispositivo {dev_name}: L'interfaccia '{iface_name}' è in modalità 'access' ma ha configurato 'native_vlan'. La vlan nativa è consentita solo in modalità trunk."
                                    )
                                if "trunk_vlans" in iface:
                                    errors.append(
                                        f"Dispositivo {dev_name}: L'interfaccia '{iface_name}' è in modalità 'access' ma ha configurato 'trunk_vlans'. Le trunk_vlans sono consentite solo sui trunk."
                                    )
                            elif mode == "trunk":
                                if "access_vlan" in iface:
                                    errors.append(
                                        f"Dispositivo {dev_name}: L'interfaccia '{iface_name}' è in modalità 'trunk' ma ha configurato 'access_vlan'. La access_vlan è consentita solo in modalità access."
                                    )
                                native_vlan = iface.get("native_vlan")
                                if native_vlan is None:
                                    native_vlan = 1
                                trunk_vlans = iface.get("trunk_vlans") or []
                                if native_vlan != 1:
                                    if native_vlan in trunk_vlans:
                                        warnings.append(
                                            f"Dispositivo {dev_name}: L'interfaccia trunk '{iface_name}' ha la Native VLAN {native_vlan} inclusa nella lista delle VLAN consentite ('trunk_vlans'). Per best practice di sicurezza, la Native VLAN dummy deve essere esclusa dai trunk consentiti per bloccare il tagging esplicito."
                                        )
                                    dev_vlans = dev.get("vlans") or {}
                                    vlans_keys = []
                                    if isinstance(dev_vlans, dict):
                                        vlans_keys = list(dev_vlans.keys())
                                    elif isinstance(dev_vlans, list):
                                        vlans_keys = dev_vlans
                                    vlans_keys_ints = []
                                    for vk in vlans_keys:
                                        try:
                                            vlans_keys_ints.append(int(vk))
                                        except (ValueError, TypeError):
                                            pass
                                    if dev_vlans and int(native_vlan) not in vlans_keys_ints:
                                        warnings.append(
                                            f"Dispositivo {dev_name}: La Native VLAN {native_vlan} è utilizzata sull'interfaccia '{iface_name}' ma non è dichiarata nel database VLAN ('vlans') del dispositivo. Deve essere creata localmente."
                                        )

                            # Verifica SVI attiva su Native VLAN dummy per gli switch
                            if iface_name:
                                m_vlan = re.match(r'^vlan(\d+)$', iface_name, re.IGNORECASE)
                                if m_vlan and iface.get("ip"):
                                    v_id = int(m_vlan.group(1))
                                    is_native = False
                                    for other_iface in interfaces:
                                        if isinstance(other_iface, dict) and other_iface.get("mode") == "trunk":
                                            if other_iface.get("native_vlan") == v_id:
                                                is_native = True
                                                break
                                    if is_native:
                                        warnings.append(
                                            f"Dispositivo {dev_name}: L'interfaccia SVI '{iface_name}' corrisponde alla Native VLAN {v_id}. La Native VLAN dummy non deve avere interfacce logiche attive o IP associati."
                                        )

    # Controlli logici aggiuntivi
    dhcp_subnets = []
    dhcp_servers = {}
    for device in intent.devices:
        for pool in device.dhcp_pools:
            try:
                dhcp_subnets.append(ipaddress.IPv4Network(pool.network, strict=False))
                dhcp_servers[pool.network] = device.name
            except Exception:
                pass

    for device in intent.devices:
        profile = device.profile
        
        # Check vpcs interface exists (L3 IP/GW checks)
        if phase is None or phase >= 3:
            if profile == "vpcs" or "pc" in device.name.lower():
                if not device.interfaces:
                    errors.append(
                        f"Dispositivo {device.name} (VPCS/Host): La lista 'interfaces' è vuota o assente. "
                        "Ogni host deve avere configurata almeno un'interfaccia (es. 'e0') con IP 'dhcp' o statico."
                    )
                else:
                    for iface in device.interfaces:
                        if not iface.ip:
                            errors.append(
                                f"Dispositivo {device.name} (VPCS/Host): L'interfaccia '{iface.name}' non ha specificato l'IP. "
                                f"Dichiarare 'ip: dhcp' o un IP statico/CIDR (es. '192.168.10.10/24')."
                            )
                        elif iface.ip.lower() != "dhcp":
                            try:
                                pc_ip_iface = ipaddress.IPv4Interface(iface.ip)
                                gw_val = None
                                if device.static_routes:
                                    gw_val = device.static_routes[0].next_hop
                                if not gw_val and device.extra_params:
                                    m_gw = re.search(r'(?:gateway|default-gateway|gw)[:\s]+([\d.]+)', device.extra_params, re.IGNORECASE)
                                    if m_gw:
                                        gw_val = m_gw.group(1)

                                if gw_val:
                                    gw_ip = ipaddress.IPv4Address(gw_val)
                                    if gw_ip not in pc_ip_iface.network:
                                        warnings.append(
                                            f"Dispositivo {device.name} (Host statico): L'indirizzo IP del gateway '{gw_val}' "
                                            f"non appartiene alla subnet '{pc_ip_iface.network}' impostata sull'interfaccia '{iface.name}'."
                                        )
                                    else:
                                        # Check if gateway IP exists on some router interface
                                        gw_found = False
                                        for r_dev in intent.devices:
                                            if r_dev.profile in ("cisco_ios", "frrouting"):
                                                for r_iface in r_dev.interfaces:
                                                    if r_iface.ip and r_iface.ip.lower() != "dhcp":
                                                        try:
                                                            r_ip = ipaddress.IPv4Interface(r_iface.ip).ip
                                                            if r_ip == gw_ip:
                                                                gw_found = True
                                                                break
                                                        except Exception:
                                                            pass
                                                if gw_found:
                                                    break
                                        if not gw_found:
                                            warnings.append(
                                                f"Dispositivo {device.name} (Host statico): Il default gateway '{gw_val}' "
                                                "non corrisponde a nessun indirizzo IP configurato sulle interfacce dei router."
                                            )
                                else:
                                    warnings.append(
                                        f"Dispositivo {device.name} (Host statico): L'interfaccia '{iface.name}' ha un IP statico "
                                        "ma non è configurato alcun default gateway (static_route o gateway in extra_params)."
                                    )
                            except Exception:
                                pass
            elif profile == "cisco_switch":
                mgmt_ifaces = []
                for iface in device.interfaces:
                    if iface.ip and iface.ip.lower() != "dhcp":
                        is_svi = re.match(r"^vlan\d+$", iface.name, re.IGNORECASE)
                        if is_svi:
                            try:
                                mgmt_ifaces.append((iface.name, ipaddress.IPv4Interface(iface.ip)))
                            except Exception:
                                pass

                if mgmt_ifaces:
                    default_routes = [
                        route for route in device.static_routes
                        if route.network in ("0.0.0.0/0", "0.0.0.0")
                    ]
                    if not default_routes:
                        warnings.append(
                            f"Dispositivo {device.name} (Switch L2): ha un IP di management ma non ha una default route. "
                            "Aggiungere static_routes: [{network: 0.0.0.0/0, next_hop: <gateway della subnet di management>}]."
                        )
                    else:
                        gw_val = default_routes[0].next_hop
                        try:
                            gw_ip = ipaddress.IPv4Address(gw_val)
                            if not any(gw_ip in mgmt_iface.network for _, mgmt_iface in mgmt_ifaces):
                                warnings.append(
                                    f"Dispositivo {device.name} (Switch L2): il default gateway '{gw_val}' "
                                    "non appartiene a nessuna subnet delle SVI di management configurate."
                                )
                        except Exception:
                            warnings.append(
                                f"Dispositivo {device.name} (Switch L2): default gateway non valido: '{gw_val}'."
                            )

        # Check DHCP relay extra_params (L3 checks)
        if phase is None or phase >= 3:
            if profile in ("cisco_ios", "frrouting"):
                for iface in device.interfaces:
                    if iface.ip and "/" in iface.ip:
                        try:
                            iface_ip = ipaddress.IPv4Interface(iface.ip)
                            for subnet in dhcp_subnets:
                                if iface_ip.ip in subnet and device.name != dhcp_servers.get(str(subnet)):
                                    relay_subnets, server = extract_dhcp_relay_params(
                                        device.extra_params,
                                        getattr(device, "dhcp_relay_server", None),
                                        getattr(device, "dhcp_relay_subnets", None)
                                    )
                                    # Check if any of the relayed subnets cover this subnet
                                    has_subnet_relay = False
                                    for s in relay_subnets:
                                        try:
                                            if ipaddress.IPv4Network(s, strict=False) == subnet:
                                                has_subnet_relay = True
                                                break
                                        except ValueError:
                                            pass
                                    if not has_subnet_relay or not server:
                                        warnings.append(
                                            f"Dispositivo {device.name}: è il gateway della subnet DHCP {subnet} "
                                            "ma non ha configurato i parametri DHCP Relay. "
                                            "Definisci 'dhcp_relay_server' e 'dhcp_relay_subnets' nella specifica."
                                        )
                        except Exception:
                            pass

        # EtherChannel logical and physical symmetry check (L2 checks)
        if phase is None or phase >= 2:
            etherchannel_configs = {}
            for iface in device.interfaces:
                if iface.name.lower().startswith("port-channel"):
                    # Extract the port-channel ID from its name (e.g. "Port-channel1" -> 1)
                    m = re.search(r'\d+', iface.name)
                    if m:
                        pc_id = int(m.group(0))
                        etherchannel_configs[pc_id] = {
                            "mode": iface.mode,
                            "access_vlan": iface.access_vlan,
                            "trunk_vlans": iface.trunk_vlans or [],
                            "native_vlan": iface.native_vlan or 1,
                        }

            for iface in device.interfaces:
                if iface.channel_group is not None:
                    pc_id = iface.channel_group
                    pc_cfg = etherchannel_configs.get(pc_id)
                    if pc_cfg:
                        # Verify that physical port switchport config matches the port-channel switchport config
                        physical_trunk_vlans = iface.trunk_vlans or []
                        physical_native_vlan = iface.native_vlan or 1
                        if (
                            iface.mode != pc_cfg["mode"] or
                            iface.access_vlan != pc_cfg["access_vlan"] or
                            sorted(physical_trunk_vlans) != sorted(pc_cfg["trunk_vlans"]) or
                            physical_native_vlan != pc_cfg["native_vlan"]
                        ):
                            warnings.append(
                                f"Dispositivo {device.name}: L'interfaccia fisica '{iface.name}' fa parte del Port-channel{pc_id} "
                                "ma la sua configurazione switchport non coincide perfettamente con quella logica. "
                                "Le porte fisiche devono avere le stesse direttive di trunk/access del Port-channel per evitare la sospensione di LACP."
                            )
                    else:
                        # Port-channel configuration is completely missing!
                        warnings.append(
                            f"Dispositivo {device.name}: L'interfaccia fisica '{iface.name}' fa parte del Port-channel{pc_id} "
                            f"ma l'interfaccia logica 'Port-channel{pc_id}' non è configurata nella lista delle interfacce."
                        )
        
        # Management IP on VLAN 1 check (L2/Mgmt checks)
        if phase is None or phase >= 2:
            has_custom_vlans = False
            for dev in intent.devices:
                if dev.vlans:
                    if any(v_id != 1 for v_id in dev.vlans.keys()):
                        has_custom_vlans = True
                        break
            
            for iface in device.interfaces:
                if iface.name.lower() == "vlan1" and iface.ip and iface.ip.lower() != "dhcp":
                    if has_custom_vlans:
                        warnings.append(
                            f"Dispositivo {device.name}: Rilevato IP di management su Vlan1 ({iface.ip}). "
                            f"È una vulnerabilità di sicurezza; usa una VLAN dedicata (es. VLAN {get_mgmt_vlan()})."
                        )


        # DHCP server static routes return check (L3 checks)
        if phase is None or phase >= 3:
            if device.dhcp_pools:
                connected_subnets = []
                for iface in device.interfaces:
                    if iface.ip and iface.ip.lower() != "dhcp":
                        try:
                            connected_subnets.append(ipaddress.IPv4Interface(iface.ip).network)
                        except Exception:
                            pass
                for pool in device.dhcp_pools:
                    try:
                        pool_net = ipaddress.IPv4Network(pool.network, strict=False)
                        is_reachable = False
                        for conn_net in connected_subnets:
                            if pool_net.subnet_of(conn_net) or conn_net.subnet_of(pool_net):
                                is_reachable = True
                                break
                        if not is_reachable:
                            for route in device.static_routes:
                                route_net = ipaddress.IPv4Network(route.network, strict=False)
                                if pool_net.subnet_of(route_net):
                                    is_reachable = True
                                    break
                        if not is_reachable:
                            warnings.append(
                                f"Dispositivo {device.name}: Gestisce il pool DHCP '{pool.name}' per la subnet {pool.network} "
                                "ma non ha alcuna interfaccia connessa o rotta statica (STATIC_ROUTE) per quella subnet. "
                                "Aggiungi una rotta statica di ritorno."
                            )
                    except Exception:
                        pass

    # Router-on-a-Stick uplink trunk validation check (L2/L3 checks - run in Phase 3 as it checks subinterface L3 termination)
    if phase is None or phase >= 3:
        router_vlans = {}  # router_name -> set of VLAN IDs
        for device in intent.devices:
            if device.profile in ("cisco_ios", "frrouting"):
                vlans = set()
                for iface in device.interfaces:
                    if "." in iface.name:
                        vlan = iface.vlan_id
                        if vlan is None:
                            try:
                                vlan = int(iface.name.split(".", 1)[1])
                            except ValueError:
                                pass
                        if vlan is not None:
                            vlans.add(vlan)
                if vlans:
                    router_vlans[device.name] = vlans

        if router_vlans:
            for router_name, vlans in router_vlans.items():
                switch_trunk_vlans = set()
                for device in intent.devices:
                    if device.name != router_name and device.profile in ("cisco_switch", "cisco_ios"):
                        for iface in device.interfaces:
                            if not iface.name.lower().startswith("port-channel") and iface.mode == "trunk":
                                if iface.trunk_vlans:
                                    for v in iface.trunk_vlans:
                                        switch_trunk_vlans.add(v)
                
                missing_vlans = vlans - switch_trunk_vlans
                if missing_vlans:
                    warnings.append(
                        f"Rilevato router '{router_name}' in modalità Router-on-a-Stick (subinterfacce per VLAN {list(vlans)}), "
                        f"ma nessuno switch ha una interfaccia fisica di uplink configurata come trunk per le VLAN {list(missing_vlans)}. "
                        "Assicurati che lo switch connesso al router abbia la porta di uplink configurata in 'mode: trunk' con le VLAN corrispondenti."
                    )

    # Prevenzione terminazione Native VLAN dummy sul router (L2/L3 checks - run in Phase 3 as it checks subinterface IP configuration)
    if phase is None or phase >= 3:
        switch_native_vlans = set()
        for device in intent.devices:
            if device.profile in ("cisco_switch", "cisco_ios"):
                for iface in device.interfaces:
                    if iface.mode == "trunk" and iface.native_vlan:
                        switch_native_vlans.add(iface.native_vlan)

        if switch_native_vlans:
            for device in intent.devices:
                if device.profile in ("cisco_ios", "frrouting"):
                    for iface in device.interfaces:
                        vlan = iface.vlan_id
                        if vlan is None and "." in iface.name:
                            try:
                                vlan = int(iface.name.split(".", 1)[1])
                            except ValueError:
                                pass
                        if vlan in switch_native_vlans and iface.ip:
                            warnings.append(
                                f"Dispositivo {device.name}: L'interfaccia '{iface.name}' fa parte della Native VLAN {vlan} "
                                "configurata sui trunk degli switch, ma ha un indirizzo IP configurato sul router. "
                                "La Native VLAN dummy a livello 2 non deve essere configurata come subinterfaccia o avere un IP sul router."
                            )

    return errors, warnings


from core.utils import (
    deep_merge_dicts as merge_dicts_recursively,
    validate_no_device_loss as _validate_no_device_loss,
    merge_specifications,
)


# ─────────────────────────────────────────────────────────────────────────────
# Rendering risposte AI
# ─────────────────────────────────────────────────────────────────────────────

def _render_ai_response(text: str, last_spec_content: str | None = None) -> None:
    """Renderizza la risposta dell'AI con blocchi Markdown e Syntax Highlight."""
    spec = _extract_spec(text)

    if spec:
        before = text[:text.find("<<<SPEC_START>>>")].strip()
        if before and "SPEC_READY" not in before:
            console.print(Panel(Markdown(before), border_style="cyan", title="[ai]NetAgent Wizard[/ai]"))

        # Eseguiamo il merge logico per mostrare il diff corretto ed evidenziare la specifica finale
        merged_spec = merge_specifications(last_spec_content or "", spec)

        if last_spec_content and last_spec_content.strip() != merged_spec.strip():
            import difflib
            old_lines = last_spec_content.splitlines(keepends=True)
            new_lines = merged_spec.splitlines(keepends=True)
            diff = list(difflib.unified_diff(old_lines, new_lines, fromfile="Spec Precedente", tofile="Nuova Spec", n=3))
            
            if diff:
                console.print()
                console.print(Rule("[spec]Differenze Rilevate (Diff)[/spec]", style="yellow"))
                for line in diff:
                    clean_line = line.rstrip("\r\n")
                    if clean_line.startswith("+") and not clean_line.startswith("+++"):
                        console.print(f"[success]{clean_line}[/success]")
                    elif clean_line.startswith("-") and not clean_line.startswith("---"):
                        console.print(f"[error]{clean_line}[/error]")
                    elif clean_line.startswith("@@"):
                        console.print(f"[cyan]{clean_line}[/cyan]")
                    else:
                        console.print(f"[dim]{clean_line}[/dim]")
                console.print(Rule(style="yellow"))

        console.print()
        console.print(Rule("[spec]Specifica Corrente (YAML)[/spec]", style="green"))
        console.print(Syntax(merged_spec, "yaml", theme="monokai", line_numbers=True))
        console.print(Rule(style="green"))
    else:
        clean = text.replace("SPEC_READY", "").strip()
        if clean:
            console.print(Panel(Markdown(clean), border_style="cyan", title="[ai]NetAgent Wizard[/ai]"))


# ─────────────────────────────────────────────────────────────────────────────
# Importazione Topologia da Fonti Esterne (GNS3, Netbox)
# ─────────────────────────────────────────────────────────────────────────────

def parse_gns3_project(gns3_path: Path) -> str:
    """
    Parsa un file di progetto GNS3 (.gns3 JSON) ed estrae la topologia fisica 
    in un formato YAML conforme a NetworkIntentSchema.
    """
    # ─────────────────────────────────────────────────────────────────────────
    # NOTA DI ESTENSIBILITÀ (Netbox e altre sorgenti future):
    # In futuro, questo caricatore può essere astratto definendo un protocollo o 
    # classe base (es. TopologyLoader) e aggiungendo un caricatore per Netbox:
    #
    # class NetboxTopologyLoader(TopologyLoader):
    #     def load(self, url: str, token: str) -> dict:
    #          # Interroga le API di Netbox per estrarre dispositivi,
    #          # le loro interfacce e i collegamenti (cables) fisici.
    #          ...
    # ─────────────────────────────────────────────────────────────────────────
    
    try:
        with open(gns3_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        raise ValueError(f"Impossibile leggere o decodificare il file GNS3: {e}")
        
    topology = data.get("topology", {})
    nodes = topology.get("nodes", [])
    links = topology.get("links", [])
    
    # Costruiamo una mappa node_id -> node per una rapida risoluzione dei link
    node_map = {n["node_id"]: n for n in nodes if "node_id" in n}
    
    devices = []
    
    # 1. Analisi dei nodi
    for node in nodes:
        node_name = node.get("name")
        if not node_name:
            continue
            
        node_type = node.get("node_type", "").lower()
        symbol = node.get("symbol", "").lower()
        props = node.get("properties", {})
        path = (props.get("path") or props.get("image") or "").lower()
        name_lower = node_name.lower()
        
        if node_type == "vpcs" or "pc" in name_lower or "host" in name_lower or "vpcs" in symbol:
            profile = "vpcs"
        elif "frr" in name_lower or "frrouting" in name_lower or "frr" in path:
            profile = "frrouting"
        elif "router" in symbol or re.search(r'\br\d+\b|^r\d+|r-\d+', name_lower) or "router" in name_lower or node_type == "dynamips" or "l3" in path:
            profile = "cisco_ios"
        elif "switch" in symbol or "sw" in name_lower or "switch" in name_lower or node_type == "ethernet_switch" or "l2" in path:
            profile = "cisco_switch"
        elif node_type == "iou" or "iou" in name_lower:
            if "l2" in path or "switch" in path:
                profile = "cisco_switch"
            else:
                profile = "cisco_ios"
        else:
            profile = "cisco_ios"
            
        # Prepariamo la struttura iniziale del dispositivo
        # Le interfacce fisiche saranno aggiunte analizzando i link
        devices.append({
            "name": node_name,
            "profile": profile,
            "interfaces": [],
            "static_routes": [],
            "dhcp_pools": [],
            "vlans": {}
        })
        
    # Helper per trovare il dispositivo in lista
    def find_device(name):
        for d in devices:
            if d["name"] == name:
                return d
        return None

    # Mappa per tracciare le porte per ciascun dispositivo ed evitare duplicati
    device_ports = {d["name"]: set() for d in devices}
    links_yaml = []
    
    # 2. Analisi dei link
    for link in links:
        link_nodes = link.get("nodes", [])
        if len(link_nodes) != 2:
            continue
            
        endpoints = []
        for ln in link_nodes:
            nid = ln.get("node_id")
            node = node_map.get(nid)
            if not node:
                continue
                
            dev_name = node.get("name")
            dev = find_device(dev_name)
            if not dev:
                continue
                
            adapter = ln.get("adapter_number", 0)
            port = ln.get("port_number", 0)
            label_text = ln.get("label", {}).get("text", "") if ln.get("label") else ""
            
            # Determinazione del nome dell'interfaccia
            if label_text:
                iface_name = label_text
            else:
                # Fallback intelligenti basati sul tipo di dispositivo
                if dev["profile"] == "vpcs":
                    iface_name = f"eth{port}"
                elif dev["profile"] == "cisco_switch" and node.get("node_type") == "ethernet_switch":
                    iface_name = f"Ethernet{port}"
                else:
                    iface_name = f"Ethernet{adapter}/{port}"
                    
            endpoints.append(f"{dev_name}:{iface_name}")
            
            # Aggiungiamo l'interfaccia alla lista del dispositivo se non è già presente
            if iface_name not in device_ports[dev_name]:
                device_ports[dev_name].add(iface_name)
                # Struttura dell'interfaccia conforme a NetworkIntentSchema (name e native_vlan default)
                if dev["profile"] == "vpcs":
                    dev["interfaces"].append({
                        "name": iface_name
                    })
                else:
                    dev["interfaces"].append({
                        "name": iface_name,
                        "native_vlan": 1
                    })
                
        if len(endpoints) == 2:
            links_yaml.append({
                "endpoints": endpoints
            })
            
    # Ordiniamo le interfacce per ciascun dispositivo in modo che l'output sia pulito
    for dev in devices:
        dev["interfaces"].sort(key=lambda x: x["name"])
        
    # Costruiamo lo YAML finale conforme allo schema
    output_data = {
        "devices": devices,
        "links": links_yaml,
        "rollback_scope": "all"
    }
    
    return yaml.dump(output_data, default_flow_style=False, sort_keys=False)


# ─────────────────────────────────────────────────────────────────────────────
# Salvataggio spec e Sessione
# ─────────────────────────────────────────────────────────────────────────────

def _save_spec(spec_content: str, output_path: Path) -> None:
    if output_path.suffix.lower() == ".txt":
        output_path = output_path.with_suffix(".yaml")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    
    # Atomic write via temp file to avoid corruption on crash
    import shutil, tempfile
    try:
        parsed_data = yaml.safe_load(spec_content)
        if isinstance(parsed_data, dict):
            from core.state import NetworkIntentSchema
            intent = NetworkIntentSchema.model_validate(parsed_data)
            spec_content = yaml.dump(intent.model_dump(exclude_none=True), default_flow_style=False, sort_keys=False)
    except Exception:
        pass

    tmp_path = output_path.with_suffix(".yaml.tmp")
    tmp_path.write_text(spec_content, encoding="utf-8")
    shutil.move(str(tmp_path), str(output_path))
    console.print(f"\n[success]✓ Specifica YAML salvata in: {output_path}[/success]")


def _save_session(history: list[dict], session_path: Path, phase: int = 1) -> None:
    """Salva la cronologia della sessione in JSON per eventuale ripresa."""
    data = {
        "phase": phase,
        "history": history
    }
    session_path.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def _load_partial_spec(path: Path) -> str:
    """Carica una spec parziale esistente per riprendere la sessione."""
    if path.exists():
        return path.read_text(encoding="utf-8")
    return ""


# ─────────────────────────────────────────────────────────────────────────────
# Loop principale
# ─────────────────────────────────────────────────────────────────────────────

def _run_wizard(output_path: Path, resume_path: Path | None = None, image_path: Path | None = None, fast_instruction: str | None = None, gns3_path: Path | None = None) -> None:
    if output_path.suffix.lower() == ".txt":
        output_path = output_path.with_suffix(".yaml")

    import atexit
    _metrics.start_pipeline()
    _metrics.spec_file = str(output_path)

    def _print_wizard_metrics():
        _metrics.finalize()
        console.print()
        console.print(_metrics.to_markdown())
        try:
            json_path = _metrics.save_json()
            console.print(f"[dim]Metriche sessione salvate in: {json_path}[/dim]")
        except Exception as e:
            logger.error("Errore durante il salvataggio delle metriche: %s", e)

    atexit.register(_print_wizard_metrics)

    client = SyncLLMClient()
    history: list[dict] = []
    pt_history = FileHistory(os.path.expanduser("~/.netagent_wizard_history"))
    spec_content: str | None = None
    last_spec_content: str | None = None
    phase = 1
    response: str = ""
    last_write_time = os.path.getmtime(output_path) if output_path.exists() else None

    # Verifica e ripresa automatica della sessione salvata
    session_file = output_path.parent / f".wizard_session_{output_path.stem}.json"
    if session_file.exists():
        try:
            with open(session_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            
            has_history = False
            if isinstance(data, list) and len(data) > 0:
                has_history = True
            elif isinstance(data, dict) and len(data.get("history", [])) > 0:
                has_history = True
                
            if has_history:
                try:
                    scelta_resume = pt_prompt(
                        f"Trovata sessione precedente salvata per '{output_path.name}'. Desideri riprenderla? (s/n) > ",
                        style=PT_STYLE
                    ).strip().lower()
                except (KeyboardInterrupt, EOFError):
                    console.print("\n[dim]Uscita.[/dim]")
                    sys.exit(0)
                
                if scelta_resume in ("s", "si", "yes", "y"):
                    if isinstance(data, dict):
                        history = data.get("history", [])
                        phase = data.get("phase", 1)
                    elif isinstance(data, list):
                        history = data
                        # Rileva la fase dai messaggi della cronologia
                        phase = 1
                        for msg in reversed(history):
                            content = msg.get("content", "")
                            m = re.search(r'FASE\s+(\d+)', content, re.IGNORECASE)
                            if m:
                                phase = int(m.group(1))
                                break
                    
                    if output_path.exists():
                        spec_content = output_path.read_text(encoding="utf-8")
                        last_spec_content = spec_content
                    console.print(f"[success]Sessione ripresa con successo alla Fase {phase}.[/success]\n")
                else:
                    console.print("[dim]Avvio di una nuova sessione.[/dim]\n")
        except Exception as e:
            console.print(f"[warn]Impossibile caricare la sessione precedente: {e}[/warn]\n")

    # GNS3 Bootstrap (Opzionale)
    if gns3_path:
        if not gns3_path.exists():
            console.print(f"[error]File GNS3 non trovato: {gns3_path}[/error]")
            sys.exit(1)
        console.print(f"[dim]Importazione topologia dal file GNS3: {gns3_path}...[/dim]")
        try:
            parsed_topology = parse_gns3_project(gns3_path)
            spec_content = parsed_topology
            last_spec_content = parsed_topology
            phase = 2  # Salta direttamente alla Fase 2
            console.print("[success]✓ Topologia GNS3 importata correttamente! Fase 1 saltata.[/success]\n")
        except Exception as e:
            console.print(f"[error]Errore durante l'importazione GNS3: {e}[/error]")
            sys.exit(1)

    # Load reference spec if resume_path is provided
    reference_spec = ""
    if resume_path:
        reference_spec = _load_partial_spec(resume_path)

    # ── Modalità Veloce (--fast) ──────────────────────────────────────────────
    if fast_instruction is not None:
        current_spec = reference_spec or ""
        if not current_spec and output_path.exists():
            current_spec = output_path.read_text(encoding="utf-8")
        
        if not current_spec.strip():
            console.print("[error]Errore: la modalità veloce (--fast) richiede una specifica di base da cui partire. Specifica un file con --resume o assicurati che il file di --output esista.[/error]")
            sys.exit(1)
            
        instruction = fast_instruction
        if not instruction:
            try:
                instruction = pt_prompt(
                    "Quale modifica desideri applicare alla specifica? > ",
                    style=PT_STYLE
                ).strip()
            except (KeyboardInterrupt, EOFError):
                console.print("\n[dim]Uscita.[/dim]")
                sys.exit(0)
            if not instruction:
                console.print("[error]Nessuna istruzione fornita. Uscita.[/error]")
                sys.exit(0)
                
        system_prompt = (
            "Sei un assistente esperto di reti. Modifica la specifica YAML NetworkIntentSchema fornita nel contesto "
            "applicando SOLO la modifica richiesta dall'utente.\n"
            "Regole:\n"
            "1. Emetti ESCLUSIVAMENTE un frammento YAML parziale con le sole chiavi modificate (YAML Merge Patch).\n"
            "2. Non rimuovere, aggiungere o cambiare nulla oltre a quanto esplicitamente richiesto.\n"
            "3. Per rimuovere un elemento usa `delete: true` sul nodo specifico.\n"
            "4. Racchiudi il frammento tra <<<SPEC_START>>> e <<<SPEC_END>>>."
        )
        
        user_msg = f"Istruzione di modifica: {instruction}"
        
        with console.status("[dim]Elaborazione modifica veloce tramite LLM...[/dim]", spinner="dots"):
            try:
                response = client.chat([], user_msg, system_prompt, current_spec=current_spec)
            except Exception as e:
                console.print(f"[error]Errore di connessione LLM: {e}[/error]")
                sys.exit(1)
                
        extracted = _extract_spec(response)
        if not extracted:
            console.print("[error]Errore: L'LLM non ha restituito una specifica valida nei marcatori <<<SPEC_START>>>/<<<SPEC_END>>>.[/error]")
            console.print(response)
            sys.exit(1)
            
        extracted = merge_specifications(current_spec, extracted)
            
        # Validazione strutturale e semantica
        is_valid_yaml = False
        try:
            parsed = yaml.safe_load(extracted)
            if isinstance(parsed, dict) and "devices" in parsed:
                from core.state import NetworkIntentSchema
                NetworkIntentSchema.model_validate(parsed)
                is_valid_yaml = True
        except Exception as e:
            console.print(f"[error]Errore di validazione dello schema YAML: {e}[/error]")
            
        errors, warnings = validate_spec_content(extracted)
        if errors or warnings:
            console.print()
            if errors:
                console.print(Panel(
                    "\n".join(f"[error]• {e}[/error]" for e in errors),
                    border_style="red",
                    title="[error]Errori di Validazione Critici (Bloccanti)[/error]"
                ))
            if warnings:
                console.print(Panel(
                    "\n".join(f"[warn]• {w}[/warn]" for w in warnings),
                    border_style="yellow",
                    title="[warn]Avvisi di Validazione (Sicurezza / Configurazione)[/warn]"
                ))
            if errors:
                console.print("[error]Rilevati errori critici nella specifica. Modifica annullata.[/error]")
                sys.exit(1)
            
        import difflib
        diff_lines = list(difflib.unified_diff(
            current_spec.splitlines(keepends=True),
            extracted.splitlines(keepends=True),
            fromfile="Specifica Corrente",
            tofile="Specifica Modificata",
            n=3
        ))
        
        if not diff_lines:
            console.print("[success]Nessuna modifica rilevata rispetto alla specifica corrente.[/success]")
            sys.exit(0)
            
        console.print(Rule("[spec]Modifiche Proposte (Diff)[/spec]", style="cyan"))
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
        
        try:
            scelta = pt_prompt(
                "Confermi l'applicazione di queste modifiche? (s/n) > ",
                style=PT_STYLE
            ).strip().lower()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Annullato.[/dim]")
            sys.exit(0)
            
        if scelta in ("s", "si", "yes", "y"):
            output_path.parent.mkdir(parents=True, exist_ok=True)
            _save_spec(extracted, output_path)
            console.print(f"[success]✓ Specifica salvata con successo in: {output_path}[/success]")
        else:
            console.print("[error]Modifica annullata dall'utente.[/error]")
            
        return

    console.print()
    console.print(Panel(
        f"[bold cyan]NetAgent Spec Wizard (Multi-Phase)[/bold cyan]\n"
        f"[dim]Provider Attivo: {client.provider.upper()} ({client.model_name})[/dim]\n\n"
        "[dim]Comandi speciali:[/dim]\n"
        "[dim]  • 'mostra spec' — mostra la bozza corrente dello YAML[/dim]\n"
        "[dim]  • 'mostra fase' — mostra la fase corrente del wizard[/dim]\n"
        "[dim]  • 'indietro'    — torna alla fase precedente del wizard[/dim]\n"
        "[dim]  • 'reset'       — ricomincia da capo (Fase 1)[/dim]\n"
        "[dim]  • 'salva'       — salva la specifica corrente[/dim]\n"
        "[dim]  • '/image <path> [prompt]' — invia un'immagine con un prompt associato[/dim]\n"
        "[dim]  • Ctrl+C        — esci[/dim]",
        border_style="cyan",
        title="v4.0 Phase-based",
    ))
    console.print()

    # Image Bootstrap

    if image_path:
        if not image_path.exists():
            console.print(f"[error]File immagine non trovato: {image_path}[/error]")
            sys.exit(1)
        console.print(f"[dim]Analisi dell'immagine della topologia in corso: {image_path}...[/dim]")
        
        feedback = ""
        attempts = 0
        max_attempts = 3
        bootstrap_res = ""
        
        while attempts < max_attempts:
            with console.status(f"[dim]Lettura e analisi VLM (tentativo {attempts+1}/{max_attempts})...[/dim]", spinner="dots"):
                bootstrap_res = client.bootstrap_from_image(image_path, reference_spec, feedback)
            extracted = _extract_spec(bootstrap_res)
            if not extracted:
                console.print("[warn]Impossibile estrarre lo YAML dall'immagine.[/warn]\n")
                break
                
            extracted = merge_specifications(reference_spec or "", extracted)
                
            warnings = validate_topology(extracted)
            if not warnings:
                spec_content = extracted
                last_spec_content = extracted
                console.print("[success]✓ Topologia iniziale estratta dall'immagine con successo ed è valida![/success]\n")
                _render_ai_response(bootstrap_res, None)
                break
            else:
                console.print(f"[warn]Rilevati {len(warnings)} avvisi di topologia nella specifica estratta dal VLM:[/warn]")
                for w in warnings:
                    console.print(f"[warn] • {w}[/warn]")
                
                feedback = "\n".join(f"- {w}" for w in warnings)
                
                # Calcola suggerimenti dinamici per le porte duplicate
                try:
                    parsed_data = yaml.safe_load(extracted)
                    if isinstance(parsed_data, dict):
                        devices_data = parsed_data.get("devices", [])
                        links_data = parsed_data.get("links", [])
                        
                        dev_interfaces = {}
                        for dev in devices_data:
                            if isinstance(dev, dict) and "name" in dev:
                                ifaces = dev.get("interfaces", [])
                                if isinstance(ifaces, list):
                                    dev_interfaces[dev["name"]] = [
                                        i["name"] for i in ifaces 
                                        if isinstance(i, dict) and "name" in i
                                    ]
                        
                        endpoint_count = {}
                        if isinstance(links_data, list):
                            for link in links_data:
                                if isinstance(link, dict) and "endpoints" in link:
                                    endpoints = link.get("endpoints")
                                    if isinstance(endpoints, list):
                                        for ep in endpoints:
                                            if isinstance(ep, str) and ":" in ep:
                                                endpoint_count[ep] = endpoint_count.get(ep, 0) + 1
                        
                        duplicate_suggestions = []
                        for ep, count in endpoint_count.items():
                            if count > 1:
                                dev_name, iface_name = ep.split(":", 1)
                                all_ifaces = dev_interfaces.get(dev_name, [])
                                
                                used_for_this_dev = set()
                                for u_ep in endpoint_count:
                                    if u_ep.startswith(f"{dev_name}:"):
                                        used_for_this_dev.add(u_ep.split(":", 1)[1])
                                
                                unused_ifaces = [i for i in all_ifaces if i not in used_for_this_dev]
                                if unused_ifaces:
                                    duplicate_suggestions.append(
                                        f"Il dispositivo '{dev_name}' ha l'interfaccia '{iface_name}' duplicata (usata {count} volte). "
                                        f"Interfacce libere/disponibili su '{dev_name}': {unused_ifaces}. "
                                        f"Sostituisci una delle connessioni duplicate in 'links' usando una di queste porte libere."
                                    )
                                else:
                                    duplicate_suggestions.append(
                                        f"Il dispositivo '{dev_name}' ha l'interfaccia '{iface_name}' duplicata (usata {count} volte), "
                                        f"ma non ci sono altre interfacce dichiarate nella sua lista 'interfaces'. "
                                        f"Aggiungi una nuova interfaccia (es. Ethernet0/2, Ethernet0/3, ecc.) alla sezione 'interfaces' di '{dev_name}' e usala in uno dei link per risolvere il duplicato."
                                    )
                        
                        if duplicate_suggestions:
                            feedback += "\n\nSuggerimenti per la risoluzione delle porte duplicate:\n" + "\n".join(f"- {s}" for s in duplicate_suggestions)
                except Exception:
                    pass
                
                attempts += 1
                if attempts >= max_attempts:
                    spec_content = extracted
                    last_spec_content = extracted
                    console.print("[error]✕ Impossibile risolvere tutti gli avvisi topologici dopo 3 tentativi di VLM. Si procederà con l'ultimo YAML estratto.[/error]\n")
                    _render_ai_response(bootstrap_res, None)
    # Resume
    elif resume_path:
        if reference_spec:
            is_valid_yaml = False
            try:
                parsed = yaml.safe_load(reference_spec)
                if isinstance(parsed, dict) and "devices" in parsed:
                    is_valid_yaml = True
            except Exception:
                pass

            if not is_valid_yaml:
                console.print("[dim]La specifica fornita non è in formato YAML diretto. Conversione del formato compresso in corso...[/dim]")
                with console.status("[dim]Conversione formato tramite LLM...[/dim]", spinner="dots"):
                    parsed_response = client.parse_text_spec_to_yaml(reference_spec)
                extracted = _extract_spec(parsed_response)
                if extracted:
                    reference_spec = extracted
                else:
                    reference_spec = parsed_response

            last_spec_content = reference_spec
            spec_content = reference_spec
            console.print(f"[dim]Ripresa sessione da specifica esistente: {resume_path}[/dim]\n")
        else:
            console.print(f"[error]File da riprendere non trovato o vuoto: {resume_path}[/error]")

    # Init phase 1
    system_prompt = get_system_prompt_for_phase(phase, current_spec=spec_content or "")
    if not history:
        if spec_content:
            init_msg = (
                f"Siamo nella FASE {phase}. Analizza lo YAML corrente nel contesto e procedi "
                f"chiedendo informazioni mancanti, oppure emetti PHASE_COMPLETE se i requisiti della FASE {phase} sono già soddisfatti."
            )
        else:
            init_msg = "Inizia il dialogo per raccogliere i nodi e i link fisici della topologia."

        with console.status(f"[dim]Inizializzazione FASE {phase}...[/dim]", spinner="dots"):
            response = client.chat(history, init_msg, system_prompt, current_spec=spec_content)

        _render_ai_response(response, last_spec_content)
        extracted = _extract_spec(response)
        if extracted:
            known_init: set[str] = set()
            if spec_content:
                try:
                    _pi = yaml.safe_load(spec_content)
                    if isinstance(_pi, dict):
                        known_init = {d["name"] for d in _pi.get("devices", []) if isinstance(d, dict) and "name" in d}
                except Exception:
                    pass
            candidate_init = merge_specifications(spec_content or "", extracted)
            if not _validate_no_device_loss(known_init, candidate_init, extracted):
                spec_content = candidate_init
                last_spec_content = spec_content
        console.print()
    else:
        # Session was resumed, render the last assistant response to restore context
        last_assistant = next((m for m in reversed(history) if m.get("role") == "assistant"), None)
        if last_assistant:
            _render_ai_response(last_assistant.get("content", ""), last_spec_content)
            console.print()

    # CLI loop
    last_write_time = os.path.getmtime(output_path) if output_path.exists() else None
    while True:
        # Check for external modifications on disk
        if output_path.exists():
            try:
                current_mtime = os.path.getmtime(output_path)
                if last_write_time is not None and current_mtime > last_write_time + 0.1: # 100ms tolerance
                    console.print(f"\n[warn]Rilevata modifica manuale su disco di: {output_path.name}. Integrazione in corso...[/warn]")
                    disk_spec = output_path.read_text(encoding="utf-8")
                    spec_content = merge_specifications(spec_content, disk_spec)
                    last_spec_content = spec_content
                    last_write_time = current_mtime
                    # Feed the updated spec back to LLM context via current_spec param
                    feedback_msg = "L'operatore ha modificato manualmente la specifica YAML su disco. Procedi basandoti sul nuovo stato nel contesto."
                    with console.status("[dim]Aggiornamento contesto LLM con le modifiche manuali...[/dim]", spinner="dots"):
                        response = client.chat(history, feedback_msg, system_prompt, current_spec=spec_content)
                    _render_ai_response(response, last_spec_content)
                    console.print()
                    new_extracted = _extract_spec(response)
                    if new_extracted:
                        known_ext: set[str] = set()
                        if spec_content:
                            try:
                                _pe = yaml.safe_load(spec_content)
                                if isinstance(_pe, dict):
                                    known_ext = {d["name"] for d in _pe.get("devices", []) if isinstance(d, dict) and "name" in d}
                            except Exception:
                                pass
                        candidate_ext = merge_specifications(spec_content or "", new_extracted)
                        if not _validate_no_device_loss(known_ext, candidate_ext, new_extracted):
                            spec_content = candidate_ext
                            last_spec_content = spec_content
            except Exception:
                pass

        # 1. Process Phase Complete check on the current 'response'
        if "PHASE_COMPLETE" in response:
            if spec_content:
                errors, warnings = validate_spec_content(spec_content, phase)
            else:
                errors = ["Nessuna specifica YAML trovata."]
                warnings = []

            # Mostra gli avvisi semantici non bloccanti
            if warnings:
                console.print()
                console.print(Panel(
                    "\n".join(f"[warn]• {w}[/warn]" for w in warnings),
                    border_style="yellow",
                    title="[warn]Avvisi di Validazione rilevati (Non Bloccanti)[/warn]"
                ))

            # Solo gli errori critici sospendono il completamento della fase
            if errors:
                console.print()
                console.print(Panel(
                    "\n".join(f"[error]• {e}[/error]" for e in errors),
                    border_style="red",
                    title="[error]Errori Critici di Validazione rilevati nella specifica[/error]"
                ))
                feedback_msg = (
                    f"Il completamento della FASE {phase} è sospeso a causa dei seguenti errori di validazione:\n"
                    + "\n".join(f"- {e}" for e in errors)
                    + "\nCorreggi la specifica emettendo un frammento YAML parziale (Merge Patch) con PHASE_COMPLETE."
                )
                with console.status("[dim]Invio errori di validazione all'LLM...[/dim]", spinner="dots"):
                    response = client.chat(history, feedback_msg, system_prompt, current_spec=spec_content)
                _render_ai_response(response, last_spec_content)
                console.print()
                new_extracted = _extract_spec(response)
                if new_extracted:
                    known_names_pc: set[str] = set()
                    if spec_content:
                        try:
                            _po = yaml.safe_load(spec_content)
                            if isinstance(_po, dict):
                                known_names_pc = {d["name"] for d in _po.get("devices", []) if isinstance(d, dict) and "name" in d}
                        except Exception:
                            pass
                    candidate_pc = merge_specifications(spec_content or "", new_extracted)
                    if not _validate_no_device_loss(known_names_pc, candidate_pc, new_extracted):
                        spec_content = candidate_pc
                        last_spec_content = spec_content
                continue

            # Se ci sono solo avvisi di validazione, chiedi all'utente se intende procedere o correggerli
            if warnings:
                try:
                    scelta_warn = pt_prompt(
                        "Ci sono avvisi di validazione. Desideri procedere alla fase successiva o correggerli adesso? (avanti/correggi) > ",
                        style=PT_STYLE
                    ).strip().lower()
                except (KeyboardInterrupt, EOFError):
                    console.print("\n[dim]Uscita.[/dim]")
                    session_file = output_path.parent / f".wizard_session_{output_path.stem}.json"
                    _save_session(history, session_file, phase)
                    console.print(f"[dim]Sessione salvata in {session_file}[/dim]")
                    break

                if scelta_warn in ("correggi", "c", "no", "n"):
                    feedback_msg = (
                        f"Il completamento della FASE {phase} è sospeso per correggere i seguenti avvisi di validazione:\n"
                        + "\n".join(f"- {w}" for w in warnings)
                        + "\nCorreggi la specifica emettendo un frammento YAML parziale (Merge Patch) con PHASE_COMPLETE."
                    )
                    with console.status("[dim]Invio avvisi di validazione all'LLM...[/dim]", spinner="dots"):
                        response = client.chat(history, feedback_msg, system_prompt, current_spec=spec_content)
                    _render_ai_response(response, last_spec_content)
                    console.print()
                    new_extracted = _extract_spec(response)
                    if new_extracted:
                        known_names_w: set[str] = set()
                        if spec_content:
                            try:
                                _pw = yaml.safe_load(spec_content)
                                if isinstance(_pw, dict):
                                    known_names_w = {d["name"] for d in _pw.get("devices", []) if isinstance(d, dict) and "name" in d}
                            except Exception:
                                pass
                        candidate_w = merge_specifications(spec_content or "", new_extracted)
                        if not _validate_no_device_loss(known_names_w, candidate_w, new_extracted):
                            spec_content = candidate_w
                            last_spec_content = spec_content
                    continue

            console.print(Panel(f"[success]★ FASE {phase} COMPLETATA CON SUCCESSO! ★[/success]", border_style="green"))
            
            if spec_content:
                _save_spec(spec_content, output_path)
                last_write_time = os.path.getmtime(output_path)

            phase += 1
            if phase > 5:
                console.print(Panel(
                    "[success]✓ COMPILAZIONE E VALIDAZIONE FINALE COMPLETATA CON SUCCESSO![/success]\n"
                    f"La specifica finale è stata salvata in: [bold]{output_path}[/bold]\n"
                    "Puoi uscire dal wizard.",
                    border_style="green",
                    title="[success]Wizard Completato[/success]"
                ))
                break
            
            # Reset history to keep context clean in the new phase
            history.clear()
            system_prompt = get_system_prompt_for_phase(phase, current_spec=spec_content or "")
            init_msg = (
                f"Fase precedente completata. Ora inizia la FASE {phase}: "
                f"analizza lo YAML corrente nel contesto e guida l'utente verso gli obiettivi della FASE {phase}."
            )
            with console.status(f"[dim]Avvio FASE {phase}...[/dim]", spinner="dots"):
                response = client.chat(history, init_msg, system_prompt, current_spec=spec_content)
            _render_ai_response(response, last_spec_content)
            console.print()
            new_extracted = _extract_spec(response)
            if new_extracted:
                known_names_tr: set[str] = set()
                if spec_content:
                    try:
                        _ptr = yaml.safe_load(spec_content)
                        if isinstance(_ptr, dict):
                            known_names_tr = {d["name"] for d in _ptr.get("devices", []) if isinstance(d, dict) and "name" in d}
                    except Exception:
                        pass
                candidate_tr = merge_specifications(spec_content or "", new_extracted)
                if not _validate_no_device_loss(known_names_tr, candidate_tr, new_extracted):
                    spec_content = candidate_tr
                    last_spec_content = spec_content
            continue

        # 2. Get user input
        try:
            user_input = pt_prompt(
                f"[Fase {phase}] Tu > ",
                history=pt_history,
                style=PT_STYLE,
            ).strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Uscita.[/dim]")
            session_file = output_path.parent / f".wizard_session_{output_path.stem}.json"
            _save_session(history, session_file, phase)
            console.print(f"[dim]Sessione salvata in {session_file}[/dim]")
            break

        if not user_input:
            continue

        input_lower = user_input.lower()

        if input_lower in ("salva", "save"):
            if spec_content:
                old_spec = ""
                if output_path.exists():
                    old_spec = output_path.read_text(encoding="utf-8")
                
                merged_spec = merge_specifications(old_spec, spec_content)
                errors, warnings = validate_spec_content(merged_spec, phase)
                
                if errors:
                    console.print()
                    console.print(Panel(
                        "\n".join(f"[error]• {e}[/error]" for e in errors),
                        border_style="red",
                        title="[error]Errori Critici Rilevati (Salvataggio Sconsigliato)[/error]"
                    ))
                    scelta = pt_prompt(
                        "Rilevati errori critici nella topologia. Salvare comunque la specifica nel file? (s/n) > ",
                        style=PT_STYLE
                    ).strip().lower()
                    if scelta not in ("s", "si", "yes", "y"):
                        console.print("[error]Salvataggio annullato.[/error]")
                        continue
                
                if warnings:
                    console.print()
                    console.print(Panel(
                        "\n".join(f"[warn]• {w}[/warn]" for w in warnings),
                        border_style="yellow",
                        title="[warn]Avvisi di Validazione (Sicurezza / Configurazione)[/warn]"
                    ))
                
                _save_spec(merged_spec, output_path)
                last_write_time = os.path.getmtime(output_path)
            else:
                console.print("[warn]Nessuna specifica disponibile. Continua il dialogo.[/warn]")
            response = ""
            continue

        if input_lower == "mostra spec":
            if spec_content:
                console.print(Rule("[spec]Specifica Corrente[/spec]", style="green"))
                console.print(Syntax(spec_content, "yaml", theme="monokai", line_numbers=True))
                console.print(Rule(style="green"))
            else:
                console.print("[warn]Nessuna specifica generata finora.[/warn]")
            response = ""
            continue

        if input_lower == "mostra fase":
            console.print(f"[success]Fase corrente: {phase}[/success]")
            response = ""
            continue

        if input_lower == "reset":
            history.clear()
            spec_content = None
            last_spec_content = None
            phase = 1
            system_prompt = get_system_prompt_for_phase(phase, current_spec=spec_content or "")
            console.print("[warn]Sessione azzerata. Ricomincio dalla Fase 1.[/warn]\n")
            with console.status("[dim]Inizializzazione Fase 1...[/dim]", spinner="dots"):
                response = client.chat(history, "Inizia il dialogo da capo per raccogliere i nodi e i link fisici della topologia.", system_prompt, current_spec=spec_content)
            _render_ai_response(response, last_spec_content)
            extracted = _extract_spec(response)
            if extracted:
                spec_content = merge_specifications("", extracted)
                last_spec_content = spec_content
            console.print()
            continue

        if input_lower in ("indietro", "back"):
            if phase > 1:
                phase -= 1
                history.clear()
                system_prompt = get_system_prompt_for_phase(phase, current_spec=spec_content or "")
                init_msg = (
                    f"L'operatore ha deciso di tornare alla fase precedente. Siamo ora alla FASE {phase}: "
                    f"analizza lo YAML corrente nel contesto e guida l'utente a raffinare o modificare gli obiettivi della FASE {phase}."
                )
                console.print(f"[warn]Torno alla FASE {phase}. Riavvio del contesto...[/warn]\n")
                with console.status(f"[dim]Avvio FASE {phase}...[/dim]", spinner="dots"):
                    response = client.chat(history, init_msg, system_prompt, current_spec=spec_content)
                _render_ai_response(response, last_spec_content)
                console.print()
                new_extracted = _extract_spec(response)
                if new_extracted:
                    known_names_tr: set[str] = set()
                    if spec_content:
                        try:
                            _ptr = yaml.safe_load(spec_content)
                            if isinstance(_ptr, dict):
                                known_names_tr = {d["name"] for d in _ptr.get("devices", []) if isinstance(d, dict) and "name" in d}
                        except Exception:
                            pass
                    candidate_tr = merge_specifications(spec_content or "", new_extracted)
                    if not _validate_no_device_loss(known_names_tr, candidate_tr, new_extracted):
                        spec_content = candidate_tr
                        last_spec_content = spec_content
            else:
                console.print("[warn]Siamo già alla FASE 1, impossibile andare indietro.[/warn]")
            response = ""
            continue

        # Normal dialogue message
        system_prompt = get_system_prompt_for_phase(phase, current_spec=spec_content or "")
        image_to_send = None
        if user_input.startswith("/image "):
            parts = user_input.split(" ", 2)
            img_str = parts[1].strip()
            prompt_str = parts[2].strip() if len(parts) > 2 else "Analizza questa immagine."
            
            img_path = Path(img_str)
            if not img_path.exists():
                console.print(f"[error]File immagine non trovato: {img_path}[/error]\n")
                continue
                
            image_to_send = img_path
            user_input = prompt_str
            console.print(f"[dim]Caricamento e invio di {img_path} all'LLM...[/dim]")

        with console.status(f"[dim]Elaborazione Fase {phase}...[/dim]", spinner="dots"):
            response = client.chat(history, user_input, system_prompt, image_path=image_to_send, current_spec=spec_content)

        _render_ai_response(response, last_spec_content)
        console.print()

        extracted = _extract_spec(response)
        if extracted:
            # Guard: track known device names BEFORE merge to detect silent deletions
            known_names: set[str] = set()
            if spec_content:
                try:
                    parsed_old = yaml.safe_load(spec_content)
                    if isinstance(parsed_old, dict):
                        known_names = {d["name"] for d in parsed_old.get("devices", []) if isinstance(d, dict) and "name" in d}
                except Exception:
                    pass

            candidate = merge_specifications(spec_content or "", extracted)
            missing = _validate_no_device_loss(known_names, candidate, extracted)

            if missing:
                # LLM dropped devices — reject the patch, do NOT update spec_content
                console.print()
                console.print(Panel(
                    f"[error]Il modello ha omesso {len(missing)} dispositivo/i: {', '.join(missing)}.\n"
                    "Il frammento YAML è stato SCARTATO per proteggere l'integrità della topologia.\n"
                    "Continua il dialogo: il modello si correggerà al prossimo turno.[/error]",
                    border_style="red",
                    title="[error]Guardia Integrità Topologia — Patch Rifiutata[/error]"
                ))
                # Do not update spec_content — it remains the last good state
            else:
                spec_content = candidate
                last_spec_content = spec_content

        if spec_content:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(spec_content, encoding="utf-8")
            last_write_time = os.path.getmtime(output_path)

        session_file = output_path.parent / f".wizard_session_{output_path.stem}.json"
        _save_session(history, session_file, phase)


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="NetAgent Spec Wizard — generatore interattivo di specifiche YAML",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Esempi:
  python llm/spec_wizard.py
  python llm/spec_wizard.py --output config/lab5.yaml
  python llm/spec_wizard.py --resume config/lab5_partial.yaml --output config/lab5.yaml
  python llm/spec_wizard.py --image images/topology.png --output config/lab5.yaml
  python llm/spec_wizard.py --gns3 mylab.gns3 --output config/lab5.yaml
        """,
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        default=Path("config") / f"spec_{datetime.now().strftime('%Y%m%d_%H%M%S')}.yaml",
        help="Percorso del file di output (default: config/spec_<timestamp>.yaml)",
    )
    parser.add_argument(
        "--resume", "-r",
        type=Path,
        default=None,
        help="Spec parziale da cui riprendere il dialogo",
    )
    parser.add_argument(
        "--image", "-i",
        type=Path,
        default=None,
        help="Percorso dell'immagine della topologia per il bootstrap visivo",
    )
    parser.add_argument(
        "--gns3", "-g",
        type=Path,
        default=None,
        help="Percorso del file di progetto .gns3 per importare automaticamente la topologia fisica",
    )
    parser.add_argument(
        "--fast", "-f",
        type=str,
        nargs="?",
        const="",
        default=None,
        help="Esegue una modifica mirata in un unico passaggio (single-turn).",
    )
    args = parser.parse_args()

    _run_wizard(
        output_path=args.output,
        resume_path=args.resume,
        image_path=args.image,
        fast_instruction=args.fast,
        gns3_path=args.gns3
    )

if __name__ == "__main__":
    main()
