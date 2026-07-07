# tools/vector_store.py
from __future__ import annotations

import json
import os
import logging
from pathlib import Path
from llm.async_client import llm_client

logger = logging.getLogger(__name__)

def cosine_similarity(v1: list[float], v2: list[float]) -> float:
    """Calcola la similarità coseno tra due vettori di float."""
    dot_product = sum(a * b for a, b in zip(v1, v2))
    norm_a = sum(a * a for a in v1) ** 0.5
    norm_b = sum(b * b for b in v2) ** 0.5
    if not norm_a or not norm_b:
        return 0.0
    return dot_product / (norm_a * norm_b)


class LocalVectorStore:
    """
    Vector Store Serverless e Pure Python per la Knowledge Base tecnica di NetAgent.
    Mantiene separati gli indici per Gemini e GitHub/OpenAI per evitare asimmetrie dimensionali.
    """

    def __init__(self, index_dir: str = "config") -> None:
        self.index_dir = Path(index_dir)
        self.provider = os.getenv("LLM_PROVIDER", "github").lower()

        # Determina il file di indice in base al provider per evitare collisioni dimensionali
        if self.provider == "gemini":
            self.index_path = self.index_dir / "kb_index_gemini.json"
        else:
            self.index_path = self.index_dir / "kb_index_github.json"

        self.documents: list[dict] = []
        self.load_index()

    def load_index(self) -> None:
        """Carica l'indice JSON se esiste su disco."""
        if self.index_path.exists():
            try:
                with open(self.index_path, "r", encoding="utf-8") as f:
                    self.documents = json.load(f)
                logger.info(
                    "[Vector Store] Caricato indice '%s' con %d documenti.",
                    self.index_path.name,
                    len(self.documents),
                )
            except Exception as e:
                logger.error(
                    "[Vector Store] Errore nel caricamento dell'indice '%s': %s",
                    self.index_path,
                    e,
                )
                self.documents = []
        else:
            logger.info(
                "[Vector Store] File di indice '%s' non trovato. Inizializzato vuoto.",
                self.index_path.name,
            )
            self.documents = []

    def save_index(self) -> None:
        """Salva l'indice corrente su disco."""
        self.index_dir.mkdir(parents=True, exist_ok=True)
        try:
            with open(self.index_path, "w", encoding="utf-8") as f:
                json.dump(self.documents, f, indent=2, ensure_ascii=False)
            logger.info(
                "[Vector Store] Salvato indice '%s' con %d documenti.",
                self.index_path.name,
                len(self.documents),
            )
        except Exception as e:
            logger.error(
                "[Vector Store] Errore nel salvataggio dell'indice '%s': %s",
                self.index_path,
                e,
            )

    def add_document(self, text: str, metadata: dict, embedding: list[float]) -> None:
        """Aggiunge un documento all'indice in memoria."""
        self.documents.append({
            "text": text,
            "metadata": metadata,
            "embedding": embedding
        })

    async def search(self, query: str, top_k: int = 2) -> list[dict]:
        """
        Interroga il vector store locale calcolando la similarità coseno.
        Genera l'embedding della query al volo.
        """
        if not self.documents:
            logger.warning("[Vector Store] Nessun documento caricato nell'indice. Ricerca interrotta.")
            return []

        try:
            # Genera embedding per la query a runtime usando il client attivo
            query_vector = await llm_client.get_embedding(query)
        except Exception as e:
            logger.error("[Vector Store] Errore nella generazione dell'embedding per la query: %s", e)
            return []

        results = []
        for doc in self.documents:
            doc_vector = doc.get("embedding")
            if not doc_vector:
                continue

            # Controlla asimmetrie dimensionali
            if len(query_vector) != len(doc_vector):
                logger.warning(
                    "[Vector Store] Mismatch dimensionale dei vettori: query=%d, doc=%d. Salto documento.",
                    len(query_vector),
                    len(doc_vector),
                )
                continue

            score = cosine_similarity(query_vector, doc_vector)
            results.append({
                "text": doc.get("text", ""),
                "metadata": doc.get("metadata", {}),
                "score": score
            })

        # Ordina per score discendente (migliore corrispondenza prima)
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]
