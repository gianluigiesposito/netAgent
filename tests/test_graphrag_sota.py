# tests/test_graphrag_sota.py
import pytest
import os
import sys
from pathlib import Path

# Setup path for imports
sys.path.append(str(Path(__file__).parent.parent))

from tools.vector_store import LocalVectorStore, cosine_similarity
from nodes.troubleshoot import _extract_conceptual_keywords
from llm.async_client import llm_client


def test_cosine_similarity():
    """Verifica il calcolo matematico della similarità coseno."""
    v1 = [1.0, 0.0, 0.0]
    v2 = [1.0, 0.0, 0.0]
    assert abs(cosine_similarity(v1, v2) - 1.0) < 1e-6

    v3 = [0.0, 1.0, 0.0]
    assert abs(cosine_similarity(v1, v3) - 0.0) < 1e-6

    v4 = [1.0, 1.0, 0.0] # 45 gradi
    assert abs(cosine_similarity(v1, v4) - 0.7071) < 1e-3


def test_extract_conceptual_keywords():
    """Verifica che i log grezzi vengano mappati correttamente in parole chiave concettuali."""
    # Test VLAN log
    log1 = ["ASSURANCE FAILED [SW1]: VLAN 20 (office) non trovata sul dispositivo."]
    keywords1 = _extract_conceptual_keywords(["SW1"], log1)
    assert "vlan" in keywords1
    assert "switchport" in keywords1
    assert "trunk" in keywords1

    # Test STP log
    log2 = ["VERIFY FAILED: Spanning tree state BLOCKED on port Ethernet0/1"]
    keywords2 = _extract_conceptual_keywords(["SW1"], log2)
    assert "spanning-tree" in keywords2
    assert "portfast" in keywords2

    # Test LACP log
    log3 = ["Membro EtherChannel Ethernet0/2 non è in stato bundled (P) ma in stato 'D'"]
    keywords3 = _extract_conceptual_keywords(["SW1"], log3)
    assert "etherchannel" in keywords3
    assert "lacp" in keywords3

    # Test DHCP log
    log4 = ["VERIFY: Client DHCP PC1 non ha ottenuto un IP"]
    keywords4 = _extract_conceptual_keywords(["PC1"], log4)
    assert "dhcp" in keywords4
    assert "helper-address" in keywords4

    # Test Multiple concepts
    log5 = ["VLAN 10 is missing", "STP blocking state on Fa0/1"]
    keywords5 = _extract_conceptual_keywords(["SW1"], log5)
    assert "vlan" in keywords5
    assert "spanning-tree" in keywords5

    # Test ping/timeout/loss log
    log6 = ["🔴 PC2 -> 192.168.10.2 LOST (100%)", "192.168.10.2 icmp_seq=1 timeout"]
    keywords6 = _extract_conceptual_keywords(["SW1"], log6)
    assert "gateway" in keywords6
    assert "static route" in keywords6
    assert "ip route" in keywords6


@pytest.mark.asyncio
async def test_local_vector_store_search():
    """Verifica che il Vector Store locale esegua correttamente la ricerca semantica coseno."""
    # Instanziamo il Vector Store (caricherà kb_index_gemini.json in base al provider attivo)
    store = LocalVectorStore()
    
    # Se il file esiste e ha documenti, testiamo la ricerca
    if store.documents:
        results = await store.search("spanning tree portfast bpduguard", top_k=1)
        assert len(results) > 0
        assert results[0]["score"] > 0.0
        # Dovrebbe abbinarsi al capitolo STP
        assert "spanning-tree" in results[0]["text"].lower() or "stp" in results[0]["text"].lower()
        
        # Test con ricerca OSPF
        results_ospf = await store.search("ospf routing area network", top_k=1)
        assert len(results_ospf) > 0
        assert "ospf" in results_ospf[0]["text"].lower()


def test_async_client_has_get_embedding():
    """Verifica che la classe AsyncLLMClient esponga il metodo get_embedding."""
    assert hasattr(llm_client, "get_embedding")
    assert callable(getattr(llm_client, "get_embedding"))
