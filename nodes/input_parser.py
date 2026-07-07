# nodes/input_parser.py
import logging
import re
from pathlib import Path
from core.state import AgentState
from llm.async_client import llm_client

logger = logging.getLogger(__name__)


async def parse_input_node(state: AgentState) -> dict:
    """
    Nodo PARSE_INPUT Polimorfo:
    Valida e normalizza l'ingresso.
    Supporta:
      1. Caricamento file YAML/JSON strutturato (passato come spec_path)
      2. Generazione dello schema strutturato tramite LLM/VLM a partire da task testuale e/o immagine.
    """
    logger.info(">>> PARSE_INPUT <<<")

    user_task = state.get("user_task", "")
    image_path = state.get("image_path")
    spec_path = state.get("spec_path")

    import yaml
    from core.state import NetworkIntentSchema

    # ── CASO 1: IaC Specification Mode (.yaml/.json o file testuale convertibile) ──
    if spec_path and not image_path:
        logger.info(f"[PARSE_INPUT] Rilevato contesto di specifica tecnica. Caricamento: {spec_path}")
        try:
            path = Path(spec_path)
            if not path.exists():
                raise FileNotFoundError(f"Il file di specifica tecnica '{spec_path}' non esiste.")

            content = path.read_text(encoding="utf-8")
            
            # Proviamo a parsarla come YAML/JSON strutturato
            try:
                parsed_data = yaml.safe_load(content)
                if isinstance(parsed_data, dict) and "devices" in parsed_data:
                    intent = NetworkIntentSchema.model_validate(parsed_data)
                    msg = f"PARSE_INPUT: Specifica YAML validata con successo. Dispositivi: {[d.name for d in intent.devices]}"
                    logger.info(msg)
                    return {
                        "intent": intent,
                        "specification_raw": content,
                        "raw_input": f"Task: {user_task}\n\nTechnical Specification Content:\n{content}",
                        "execution_log": [msg]
                    }
            except Exception as yaml_err:
                logger.warning(f"[PARSE_INPUT] Il file non è uno schema YAML diretto. Fallback all'LLM: {yaml_err}")

            # Se non era uno YAML strutturato, usiamo il testo come user_task per l'LLM
            if not user_task:
                user_task = content

        except Exception as e:
            logger.error(f"[PARSE_INPUT] Errore critico durante l'ingestione della specifica: {e}")
            raise e

    # ── CASO 2: Generazione tramite LLM/VLM (da testo o immagine) ──
    if image_path or user_task:
        logger.info("[PARSE_INPUT] Generazione schema strutturato tramite LLM...")
        intent = await llm_client.parse_multimodal_input(
            task_text=user_task,
            image_path=image_path,
        )

        msg = (
            f"PARSE_INPUT: Estratto piano intent con devices={[d.name for d in intent.devices]}"
        )
        logger.info(msg)

        # Salva lo YAML strutturato su disco
        generated_yaml_path = Path("config/generated_intent.yaml")
        generated_yaml_path.parent.mkdir(parents=True, exist_ok=True)
        with open(generated_yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(intent.model_dump(), f, default_flow_style=False)
        logger.info(f"[PARSE_INPUT] Specifica intent salvata su disk come YAML: {generated_yaml_path}")

        return {
            "intent": intent,
            "specification_raw": yaml.dump(intent.model_dump(), default_flow_style=False),
            "raw_input": f"Task: {user_task}\n\nExtracted Intent YAML:\n{yaml.dump(intent.model_dump(), default_flow_style=False)}",
            "execution_log": [msg],
        }

    if state.get("raw_input"):
        logger.info("PARSE_INPUT: Contesto già parzialmente bufferizzato. Skip del nodo.")
        return {}

    raise ValueError("CRITICAL: Configurazione di ingresso assente. Specificare un parametro valido (--spec o --image o user_task).")