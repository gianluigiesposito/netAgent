# netAgent
*Orchestratore di rete multi-vendor a ciclo chiuso basato su LangGraph e GraphRAG.*

`netAgent` è un framework per l'automazione, la verifica e il troubleshooting automatico di topologie di rete simulate in ambiente GNS3. Il sistema supporta apparati **Cisco IOS** (router e switch L2/L3), **FRRouting** e host **VPCS**.

---

## Funzionalità Principali

* **Closed-Loop Automation (Ciclo Chiuso):** Il flusso esecutivo acquisisce lo stato reale dei dispositivi, calcola il delta di configurazione rispetto all'intento desiderato (tramite `CiscoConfParse`) e applica le modifiche previa approvazione dell'operatore (HITL). In caso di errore durante il deploy, esegue automaticamente la catena di rollback.
* **Network Assurance & Control Plane Verification:** La fase di verifica esegue ping incrociati sul data plane ed analizza lo stato dei protocolli sul control plane (VLAN attive, stati Spanning Tree e binding DHCP reali) per attendere la convergenza della rete.
* **Closed-loop Troubleshooting (GraphRAG):** Se i test di verifica falliscono, l'agente interroga **Neo4j** usando una query Cypher `shortestPath` per isolare solo i dispositivi fisici e logici sul percorso minimo del guasto. Integra i dati con un database vettoriale in memoria (`LocalVectorStore`) per estrarre le linee guida tecniche e correggere l'anomalia.
* **Spec Wizard Interattivo:** Interfaccia CLI guidata a 5 fasi ([spec_wizard.py](llm/spec_wizard.py)) per la creazione formale dello YAML di specifica, con supporto per il bootstrap a partire da file di progetto `.gns3` o immagini (VLM).

---

## Struttura del Progetto

* `main.py` : Entry point dell'applicazione.
* `core/` : Definizione del grafo LangGraph, dello stato e degli schemi Pydantic (`NetworkIntentSchema`).
* `nodes/` : Nodi operativi del grafo (ingestione, planning, diff, deploy, verifica e troubleshooting).
* `generate/` : Motore di diff gerarchico per il confronto delle configurazioni.
* `tools/` : Connessioni Scrapli/Telnet, snapshot dello stato dei dispositivi e store vettoriale locale.
* `database/` : Pattern Repository per l'interfacciamento con Neo4j.
* `tests/` : Suite di test (173 test unitari e di integrazione).
* `config/` : Inventario dei dispositivi (`devices.yaml`) e modelli di default.

---

## Requisiti e Avvio Rapido

### 1. Dipendenze
Il progetto richiede Python 3.10+ ed i pacchetti specificati nei requisiti (tra cui `langgraph`, `scrapli`, `ciscoconfparse`, `neo4j` e `pydantic`).

### 2. Configurazione d'Ambiente
Crea un file `.env` nella root del progetto:
```env
LLM_PROVIDER=gemini # o github
GEMINI_API_KEY=tuo_token_gemini
NEO4J_URI=bolt://localhost:7687
NEO4J_USERNAME=neo4j
NEO4J_PASSWORD=tua_password
DEPLOY_MODE=human-in-the-loop # o automated
```

### 3. Esecuzione

* **Avvio dello Spec Wizard (Creazione Specifica):**
  ```bash
  python llm/spec_wizard.py -o config/test_intent.yaml
  ```

* **Avvio dell'Orchestratore Backend:**
  ```bash
  python main.py --task "Configura rete LAN e pool DHCP" --spec config/test_intent.yaml
  ```

* **Esecuzione dei Test:**
  ```bash
  pytest tests/
  ```

---

## Licenza
Copyright (c) 2026 Gianluigi Esposito. Tutti i diritti riservati.

Il codice è pubblicato su questo repository pubblico al solo scopo di consentire la consultazione e la valutazione accademica da parte della commissione d'esame e del corpo docente dell'Università degli Studi di Napoli Federico II. È vietata qualsiasi copia, modifica o ridistribuzione non autorizzata.
