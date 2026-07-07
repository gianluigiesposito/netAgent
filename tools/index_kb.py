# tools/index_kb.py
import asyncio
import os
import sys
import logging
from pathlib import Path
import json

# Setup import path for tools and llm modules
sys.path.append(str(Path(__file__).parent.parent))

from llm.async_client import AsyncLLMClient
from dotenv import load_dotenv

load_dotenv()

# Setup logging
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("index_kb")


def chunk_markdown_file(file_path: Path) -> list[dict]:
    """
    Spezza un file markdown in chunk logici basati sulle intestazioni '## '.
    Ritorna una lista di dict: {"text": str, "metadata": dict}
    """
    if not file_path.exists():
        logger.warning("File non trovato per l'indicizzazione: %s", file_path)
        return []

    with open(file_path, "r", encoding="utf-8") as f:
        content = f.read()

    chunks = []
    # Splitta per intestazione '## '
    sections = content.split("\n## ")
    
    # Il primo elemento contiene l'intestazione H1 del file, lo usiamo come contesto generale
    h1_context = sections[0].strip()
    
    for section in sections[1:]:
        lines = section.split("\n")
        title = lines[0].strip()
        body = "\n".join(lines[1:]).strip()
        
        # Uniamo l'intestazione del file, il titolo della sezione e il corpo
        chunk_text = f"{h1_context}\n\n## {title}\n\n{body}"
        
        chunks.append({
            "text": chunk_text,
            "metadata": {
                "source": file_path.name,
                "title": title
            }
        })
        
    return chunks


async def index_for_provider(provider: str, chunks: list[dict]):
    """Genera gli embedding per tutti i chunk e salva l'indice JSON."""
    logger.info("Avvio indicizzazione per il provider: %s", provider)
    
    # Temporaneamente sovrascriviamo le variabili d'ambiente per forzare l'inizializzazione del client
    original_provider = os.getenv("LLM_PROVIDER")
    os.environ["LLM_PROVIDER"] = provider
    
    try:
        client = AsyncLLMClient()
    except Exception as e:
        logger.error("Impossibile inizializzare il client per il provider %s: %s", provider, e)
        if original_provider:
            os.environ["LLM_PROVIDER"] = original_provider
        return

    indexed_docs = []
    for i, chunk in enumerate(chunks):
        text = chunk["text"]
        metadata = chunk["metadata"]
        logger.info("[%s] Generazione embedding per chunk %d/%d: '%s'", provider, i+1, len(chunks), metadata["title"])
        try:
            # Chiamata API per estrarre l'embedding
            vector = await client.get_embedding(text)
            indexed_docs.append({
                "text": text,
                "metadata": metadata,
                "embedding": vector
            })
            # Piccola pausa per evitare rate limit
            await asyncio.sleep(0.5)
        except Exception as e:
            logger.error("[%s] Errore nell'embedding per il chunk '%s': %s", provider, metadata["title"], e)

    # Scrivi l'indice
    config_dir = Path("config")
    config_dir.mkdir(parents=True, exist_ok=True)
    output_path = config_dir / f"kb_index_{provider}.json"
    
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(indexed_docs, f, indent=2, ensure_ascii=False)
        
    logger.info("[%s] Indice salvato con successo in '%s' (%d documenti)", provider, output_path, len(indexed_docs))
    
    # Ripristina provider originale
    if original_provider:
        os.environ["LLM_PROVIDER"] = original_provider


async def main():
    kb_dir = Path("knowledge_base")
    files_to_index = [
        kb_dir / "cisco_ios_troubleshooting.md",
        kb_dir / "frrouting_troubleshooting.md"
    ]

    all_chunks = []
    for file_path in files_to_index:
        logger.info("Lettura e suddivisione di: %s", file_path)
        all_chunks.extend(chunk_markdown_file(file_path))

    if not all_chunks:
        logger.error("Nessun chunk generato. Ricontrolla i file markdown.")
        return

    # Controlliamo quali credenziali sono disponibili in .env
    gemini_ok = bool(os.getenv("GEMINI_API_KEY"))
    github_ok = bool(os.getenv("GITHUB_TOKEN"))

    if not gemini_ok and not github_ok:
        logger.error("Nessuna chiave API rilevata in .env (GEMINI_API_KEY o GITHUB_TOKEN). Impossibile procedere.")
        return

    if gemini_ok:
        await index_for_provider("gemini", all_chunks)
    else:
        logger.warning("GEMINI_API_KEY assente. Indicizzazione per Gemini saltata.")

    if github_ok:
        await index_for_provider("github", all_chunks)
    else:
        logger.warning("GITHUB_TOKEN assente. Indicizzazione per GitHub Models saltata.")


if __name__ == "__main__":
    asyncio.run(main())
