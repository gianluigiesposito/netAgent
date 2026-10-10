# netAgent
*Orchestratore di rete multi-vendor a ciclo chiuso basato su LangGraph e GraphRAG. Progetto di tesi, Ingegneria Informatica, Federico II (2026).*

`netAgent` automatizza la configurazione, la verifica e il troubleshooting di topologie di rete simulate in GNS3. Supporta apparati **Cisco IOS** (router e switch L2/L3), **FRRouting** e host **VPCS**.

## Approccio

L'LLM interviene dove serve interpretare: nella stesura della specifica (Spec Wizard) e nell'analisi dei guasti. Le operazioni che modificano i dispositivi (calcolo del delta, generazione della CLI, deploy, verifica e rollback) sono gestite da codice deterministico, con approvazione dell'operatore in modalità human-in-the-loop; la generazione della CLI ricorre all'LLM solo come fallback, quando una configurazione non è coperta dai template. Il risultato è un comportamento riproducibile e misurabile (vedi [Risultati misurati](#risultati-misurati)).

## Funzionalità principali

* **Automazione a ciclo chiuso:** il flusso esecutivo acquisisce lo stato reale dei dispositivi, calcola il delta rispetto alla configurazione richiesta nella specifica (tramite `CiscoConfParse`) e applica le modifiche previa approvazione dell'operatore (HITL). In caso di errore durante il deploy, esegue automaticamente la catena di rollback.
* **Verifica di data plane e control plane:** la fase di verifica esegue ping incrociati sul data plane e analizza lo stato dei protocolli sul control plane (VLAN attive, stati Spanning Tree e binding DHCP reali) per attendere la convergenza della rete.
* **Troubleshooting con GraphRAG:** se i test di verifica falliscono, l'agente interroga **Neo4j** usando una query Cypher `shortestPath` per isolare solo i dispositivi fisici e logici sul percorso minimo del guasto. Integra i dati con un database vettoriale in memoria (`LocalVectorStore`) per estrarre le linee guida tecniche e correggere l'anomalia.
* **Spec Wizard interattivo:** interfaccia CLI guidata a 5 fasi ([spec_wizard.py](llm/spec_wizard.py)) per la stesura assistita dello YAML di specifica. Integra il **parsing automatico dei progetti GNS3** (estrazione diretta dei link fisici e dei nodi dal file `.gns3`) e il bootstrap da immagini tramite modelli multimodali (VLM).

---

## Risultati misurati

Misure dello scenario di laboratorio descritto nella tesi (GNS3, Cisco IOS e FRRouting):

* 239 comandi CLI trasmessi senza interruzioni di sessione né errori di sintassi; 19/19 ping di verifica superati; tempo complessivo 170,1 s.
* Nessuna chiamata all'LLM in pianificazione, deploy e verifica: la specifica era già strutturata e tutta la CLI è stata generata dai template deterministici.
* Con GraphRAG il prompt di troubleshooting si riduce del 14-17% su 8 nodi. Il -90% su 40 nodi è un risultato di **simulazione**, non una misura su una rete reale.

Sono risultati di un prototipo in laboratorio, non di una rete di produzione.

---

## Struttura del progetto

* `main.py`: Entry point dell'applicazione.
* `core/`: Definizione del grafo LangGraph, dello stato e degli schemi Pydantic (`NetworkIntentSchema`).
* `nodes/`: Nodi operativi del grafo (ingestione, planning, diff, deploy, verifica e troubleshooting).
* `generate/`: Motore di diff gerarchico per il confronto delle configurazioni.
* `tools/`: Connessioni Scrapli/Telnet, snapshot dello stato dei dispositivi e store vettoriale locale.
* `database/`: Pattern Repository per l'interfacciamento con Neo4j.
* `tests/`: Suite di test (173 test unitari e di integrazione).
* `config/`: Inventario dei dispositivi (`devices.yaml`) e modelli di default.

---

## Requisiti e avvio rapido

### 1. Dipendenze
Il progetto richiede Python 3.10+. Dipendenze principali:
```bash
pip install langgraph langgraph-checkpoint-sqlite aiosqlite pydantic scrapli telnetlib3 textfsm ciscoconfparse jinja2 neo4j google-genai openai python-dotenv pyyaml tenacity loguru rich prompt_toolkit pytest
```

### 2. Configurazione dell'ambiente
Crea un file `.env` nella root del progetto:
```env
LLM_PROVIDER=gemini # o github
GEMINI_API_KEY=tuo_token_gemini
# GITHUB_TOKEN=tuo_token_github  (se LLM_PROVIDER=github)
NEO4J_URI=bolt://localhost:7687
NEO4J_USER=neo4j
NEO4J_PASSWORD=tua_password
DEPLOY_MODE=human-in-the-loop # o automated
```

### 3. Esecuzione

* **Spec Wizard (creazione della specifica):**
  ```bash
  # Modalità interattiva standard
  python llm/spec_wizard.py -o config/test_intent.yaml

  # Avvio con importazione automatica della topologia fisica da progetto GNS3 (.gns3)
  python llm/spec_wizard.py -o config/test_intent.yaml -g percorso/progetto.gns3

  # Avvio con importazione della topologia fisica da immagine (VLM)
  python llm/spec_wizard.py -o config/test_intent.yaml -i percorso/diagramma.png
  ```

* **Orchestratore:**
  ```bash
  python main.py --task "Configura rete LAN e pool DHCP" --spec config/test_intent.yaml
  ```

* **Test:**
  ```bash
  pytest tests/
  ```

---

## Contatti

Gianluigi Esposito · [LinkedIn](https://www.linkedin.com/in/gianluigi-esposito-a3910a229)

---

## Licenza
Copyright (c) 2026 Gianluigi Esposito. Tutti i diritti riservati.

È vietata qualsiasi copia, modifica o ridistribuzione non autorizzata.
