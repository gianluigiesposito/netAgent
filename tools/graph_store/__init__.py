# tools/graph_store/__init__.py
from neo4j import AsyncGraphDatabase
from .base import GraphStoreBase
from .device_ops import DeviceOpsMixin
from .topology_ops import TopologyOpsMixin

class AsyncNetworkGraphStore(GraphStoreBase, DeviceOpsMixin, TopologyOpsMixin):
    pass

__all__ = ["AsyncNetworkGraphStore", "AsyncGraphDatabase"]
