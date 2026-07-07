import pytest
from unittest.mock import MagicMock
from tools.graph_store import AsyncNetworkGraphStore

@pytest.fixture
def mock_graph_store():
    # Inizializziamo il GraphStore mockando la connessione a Neo4j
    store = AsyncNetworkGraphStore.__new__(AsyncNetworkGraphStore)
    
    # Mock dell'inventario per simulare i tre profili/vendor richiesti
    store._inventory = {
        "PC1": {"profile": "vpcs", "vendor": "vpcs"},
        "R1": {"profile": "cisco_ios", "vendor": "cisco"},
        "LNX-ROUTER": {"profile": "frrouting", "vendor": "linux"}
    }
    
    return store

def test_vpcs_port_normalization(mock_graph_store):
    # Test caso VPCS (verifica il mapping forzato a eth0)
    assert mock_graph_store._normalize_port("eth0", "PC1") == "eth0"
    assert mock_graph_store._normalize_port("e0", "PC1") == "eth0"
    assert mock_graph_store._normalize_port("ethernet0", "PC1") == "eth0"
    # Edge case: sottointerfaccia potenziale
    assert mock_graph_store._normalize_port("eth0.10", "PC1") == "eth0.10"

def test_frrouting_port_preservation(mock_graph_store):
    # Test caso Linux/FRRouting (l'interfaccia OS-level non deve cambiare)
    assert mock_graph_store._normalize_port("eth0", "LNX-ROUTER") == "eth0"
    assert mock_graph_store._normalize_port("enp0s3", "LNX-ROUTER") == "enp0s3"

def test_cisco_port_normalization(mock_graph_store):
    # Test caso Cisco (deve usare le regole standard di normalize_interface_name)
    assert mock_graph_store._normalize_port("Et0/0", "R1") == "Ethernet0/0"
    assert mock_graph_store._normalize_port("Gi0/1", "R1") == "GigabitEthernet0/1"
    assert mock_graph_store._normalize_port("Po1", "R1") == "Port-channel1"
