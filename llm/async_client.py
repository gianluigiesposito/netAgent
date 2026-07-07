# llm/async_client.py
from __future__ import annotations
import logging
import os
import time
from google import genai
from google.genai import types
from openai import AsyncOpenAI

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type
from dotenv import load_dotenv

# Allineamento SOTA: Manteniamo gli schemi strutturati rigidi Pydantic per azzerare le allucinazioni
from core.state import IntentModel, RouterActionPlan, NetworkIntentSchema
# FIX DEFINITIVO PYLANCE: Importiamo le costanti statiche corrette dal file prompts.py
from llm.prompts import MACRO_PLANNER_PROMPT, COMMAND_GENERATOR_PROMPT
from tools.metrics import metrics

load_dotenv()
logger = logging.getLogger(__name__)

# Retry decorator per errori transitori degli LLM (rate limits, 5xx)
_llm_retry = retry(
    retry=retry_if_exception_type((Exception,)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    reraise=True,
)


class AsyncLLMClient:
    """
    Client LLM Asincrono Unificato.
    Supporta nativamente sia Google Gemini sia GitHub Models tramite Structured Outputs.
    """
    def __init__(self) -> None:
        # Leggiamo il provider dal file .env (default a 'github' se non specificato)
        self.provider = os.getenv("LLM_PROVIDER", "github").lower()

        if self.provider == "gemini":
            api_key = os.getenv("GEMINI_API_KEY")
            if not api_key:
                raise ValueError("GEMINI_API_KEY non configurata nel file .env.")
            self._google = genai.Client(api_key=api_key)
            self.model_name = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")
            logger.info("LLM Client inizializzato con: Google Gemini (%s)", self.model_name)

        elif self.provider == "github":
            token = os.getenv("GITHUB_TOKEN")
            if not token:
                raise ValueError("GITHUB_TOKEN non configurata nel file .env.")
            self._openai = AsyncOpenAI(
                base_url="https://models.inference.ai.azure.com",
                api_key=token,
            )
            self.model_name = os.getenv("GITHUB_MODEL", "gpt-4o-mini")
            logger.info("LLM Client inizializzato con: GitHub Models (%s)", self.model_name)

        else:
            raise ValueError(f"Provider LLM non supportato: '{self.provider}'")

    # ── internal call helpers ─────────────────────────────────────────────────

    @_llm_retry
    async def _gemini_structured(self, system: str, user: str, schema, caller: str = "unknown"):
        from google.genai import types
        t0 = time.monotonic()
        resp = await self._google.aio.models.generate_content(
            model=self.model_name,
            contents=user,
            config=types.GenerateContentConfig(
                system_instruction=system,
                temperature=0.0,
                response_mime_type="application/json",
                response_schema=schema,
            ),
        )
        duration = time.monotonic() - t0
        # Estrai token usage dalla risposta Gemini
        input_tokens = 0
        output_tokens = 0
        if hasattr(resp, "usage_metadata") and resp.usage_metadata:
            input_tokens = getattr(resp.usage_metadata, "prompt_token_count", 0) or 0
            output_tokens = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0
        metrics.record_llm_call(caller, self.model_name, "gemini", input_tokens, output_tokens, duration)
        return resp.parsed

    @_llm_retry
    async def _openai_structured(self, system: str, user: str, schema,
                                  image_b64: str = None, caller: str = "unknown"):
        content = [{"type": "text", "text": user}]
        if image_b64:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{image_b64}"},
            })
        t0 = time.monotonic()
        resp = await self._openai.beta.chat.completions.parse(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            response_format=schema,
            temperature=0.0,
        )
        duration = time.monotonic() - t0
        # Estrai token usage dalla risposta OpenAI
        input_tokens = 0
        output_tokens = 0
        if hasattr(resp, "usage") and resp.usage:
            input_tokens = getattr(resp.usage, "prompt_tokens", 0) or 0
            output_tokens = getattr(resp.usage, "completion_tokens", 0) or 0
        metrics.record_llm_call(caller, self.model_name, "github", input_tokens, output_tokens, duration)
        return resp.choices[0].message.parsed

    # ── public API ────────────────────────────────────────────────────────────

    async def generate_plan(
        self, intent_text: str, topology_dump: str, unreachable_routers: list[str], specification_raw: str = ""
    ) -> NetworkIntentSchema:
        """
        Invocazione del nodo PLAN (Macro-Strategia).
        Inietta in sicurezza il target della specifica tecnica per guidare l'LLM
        ed appende i vincoli fisici degli apparati offline direttamente nel prompt utente.
        """
        # Integrazione del contesto di raggiungibilità e i parametri della specifica desiderata
        unreachable_context = f"ATTENTION: The following devices are OFFLINE/UNREACHABLE: {unreachable_routers}. Do not route packets through them!\n\n" if unreachable_routers else ""
        spec_context = f"\n[DESIRED TARGET NETWORK SPECIFICATION]:\n{specification_raw}" if specification_raw else ""
        
        user_prompt = f"{unreachable_context}User Intent: {intent_text}\n{spec_context}\n\nCurrent Real Topology (GraphRAG Source of Truth):\n{topology_dump}"

        if self.provider == "gemini":
            result = await self._gemini_structured(MACRO_PLANNER_PROMPT, user_prompt, NetworkIntentSchema, caller="generate_plan")
        else:
            result = await self._openai_structured(MACRO_PLANNER_PROMPT, user_prompt, NetworkIntentSchema, caller="generate_plan")
        
        print("\n=== RAW LLM RESPONSE (NetworkIntentSchema) ===")
        # Usa il logger integrato
        logger.info("Risposta LLM (Macro-Piano):\n%s", result.model_dump_json(indent=2))
        
        return result
    
    @_llm_retry
    async def raw_completion(self, system_prompt: str, user_prompt: str, caller: str = "raw_completion") -> str:
        """
        Invocazione generica per risposte testuali non legate a schemi Pydantic rigidi.
        Usata dal nodo TROUBLESHOOT per estrarre la logica di auto-remediation.
        """
        if self.provider == "gemini":
            from google.genai import types as gtypes
            t0 = time.monotonic()
            resp = await self._google.aio.models.generate_content(
                model=self.model_name,
                contents=user_prompt,
                config=gtypes.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.0,
                ),
            )
            duration = time.monotonic() - t0
            input_tokens = 0
            output_tokens = 0
            if hasattr(resp, "usage_metadata") and resp.usage_metadata:
                input_tokens = getattr(resp.usage_metadata, "prompt_token_count", 0) or 0
                output_tokens = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0
            metrics.record_llm_call(caller, self.model_name, "gemini", input_tokens, output_tokens, duration)
            return resp.text
            
        # Fallback per GitHub Models / OpenAI
        t0 = time.monotonic()
        resp = await self._openai.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.0,
        )
        duration = time.monotonic() - t0
        input_tokens = 0
        output_tokens = 0
        if hasattr(resp, "usage") and resp.usage:
            input_tokens = getattr(resp.usage, "prompt_tokens", 0) or 0
            output_tokens = getattr(resp.usage, "completion_tokens", 0) or 0
        metrics.record_llm_call(caller, self.model_name, "github", input_tokens, output_tokens, duration)
        return resp.choices[0].message.content

    async def generate_commands(
        self, router_name: str, delta_description: str
    ) -> RouterActionPlan:
        """
        Invocazione del nodo GENERATE (Map-Reduce Fallback).
        Questo metodo si attiva unicamente se l'engine di Diff deterministico locale
        richiede un paracadute semantico per blocchi non identificati dalle Regex.
        """
        user_prompt = f"Device: {router_name}\n\nDelta:\n{delta_description}"

        if self.provider == "gemini":
            return await self._gemini_structured(COMMAND_GENERATOR_PROMPT, user_prompt, RouterActionPlan, caller="generate_commands")
        return await self._openai_structured(COMMAND_GENERATOR_PROMPT, user_prompt, RouterActionPlan, caller="generate_commands")

    async def parse_multimodal_input(
        self, task_text: str, image_path: str | None
    ) -> NetworkIntentSchema:
        """
        Invocazione del nodo iniziale PARSE_INPUT (Vision/Text).
        Estrae e valida l'intento di rete in base al prompt dell'utente e all'immagine.
        """
        system = (
            "You are a deterministic network intent extractor. "
            "Configure all devices (R1, R2, SW1, PC1, PC2, etc.) matching the topology and user instruction. "
            "For each device, specify its name, profile (cisco_ios, cisco_switch, frrouting, vpcs), "
            "interfaces, static routes, DHCP pools, and VLANs as requested. "
            "Ensure you return a valid NetworkIntentSchema structure. No markdown, no filler text."
        )

        if self.provider == "gemini":
            from google.genai import types
            import mimetypes
            from pathlib import Path
            import json
            from core.state import NetworkIntentSchema

            parts = [task_text]
            if image_path:
                data = Path(image_path).read_bytes()
                mime, _ = mimetypes.guess_type(image_path)
                parts.insert(0, types.Part.from_bytes(data=data, mime_type=mime or "image/png"))

            from google.genai import types as gtypes
            t0 = time.monotonic()
            resp = await self._google.aio.models.generate_content(
                model=self.model_name,
                contents=parts,
                config=gtypes.GenerateContentConfig(
                    system_instruction=system,
                    temperature=0.0,
                    response_mime_type="application/json",
                    response_schema=NetworkIntentSchema,
                ),
            )
            duration = time.monotonic() - t0
            input_tokens = 0
            output_tokens = 0
            if hasattr(resp, "usage_metadata") and resp.usage_metadata:
                input_tokens = getattr(resp.usage_metadata, "prompt_token_count", 0) or 0
                output_tokens = getattr(resp.usage_metadata, "candidates_token_count", 0) or 0
            metrics.record_llm_call("parse_multimodal_input", self.model_name, "gemini", input_tokens, output_tokens, duration)
            raw_json = resp.text
            logger.info("Raw JSON output from Gemini: %s", raw_json)
            parsed_json = json.loads(raw_json)
            intent = NetworkIntentSchema.model_validate(parsed_json)
            return intent

        # GitHub / OpenAI path
        import base64
        import json
        from core.state import NetworkIntentSchema
        image_b64 = None
        if image_path:
            with open(image_path, "rb") as f:
                image_b64 = base64.b64encode(f.read()).decode()

        content = [{"type": "text", "text": task_text}]
        if image_b64:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{image_b64}"},
            })
        t0 = time.monotonic()
        resp = await self._openai.beta.chat.completions.parse(
            model=self.model_name,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            response_format=NetworkIntentSchema,
            temperature=0.0,
        )
        duration = time.monotonic() - t0
        input_tokens = 0
        output_tokens = 0
        if hasattr(resp, "usage") and resp.usage:
            input_tokens = getattr(resp.usage, "prompt_tokens", 0) or 0
            output_tokens = getattr(resp.usage, "completion_tokens", 0) or 0
        metrics.record_llm_call("parse_multimodal_input", self.model_name, "github", input_tokens, output_tokens, duration)
        raw_json = resp.choices[0].message.content
        logger.info("Raw JSON output from OpenAI/GitHub: %s", raw_json)
        parsed_json = json.loads(raw_json)
        intent = NetworkIntentSchema.model_validate(parsed_json)
        return intent

    @_llm_retry
    async def get_embedding(self, text: str) -> list[float]:
        """
        Genera l'embedding vettoriale per un testo in ingresso.
        Supporta text-embedding-004 per Gemini e text-embedding-3-small per GitHub Models.
        """
        if self.provider == "gemini":
            resp = await self._google.aio.models.embed_content(
                model="models/gemini-embedding-001",
                contents=text,
            )
            if resp and resp.embeddings:
                return resp.embeddings[0].values
            raise ValueError("[LLM Client] Nessun embedding restituito da Gemini.")

        # GitHub Models / OpenAI path
        resp = await self._openai.embeddings.create(
            model="text-embedding-3-small",
            input=text,
        )
        if resp and resp.data:
            return resp.data[0].embedding
        raise ValueError("[LLM Client] Nessun embedding restituito da GitHub Models.")


# Esportazione Singleton pulita per LangGraph

class _LazyLLMClient:
    """Proxy che inizializza AsyncLLMClient solo al primo accesso."""
    _instance: AsyncLLMClient | None = None

    def __getattr__(self, name):
        if _LazyLLMClient._instance is None:
            _LazyLLMClient._instance = AsyncLLMClient()
        return getattr(_LazyLLMClient._instance, name)


llm_client = _LazyLLMClient()