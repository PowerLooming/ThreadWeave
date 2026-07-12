# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (C) 2026 ThreadWeave contributors
"""
Graph Connector for Microsoft 365 Copilot integration.

Syncs ThreadWeave organizational knowledge to Microsoft Graph as
external items, making it searchable in Copilot, Microsoft Search,
and other M365 surfaces.

Quick Start:
    1. Register an Azure AD app with ExternalConnection.ReadWrite.OwnedBy
    2. Set environment variables:
       THREADWEAVE_GRAPH_TENANT_ID=...
       THREADWEAVE_GRAPH_CLIENT_ID=...
       THREADWEAVE_GRAPH_CLIENT_SECRET=...
    3. Run: threadweave graph setup      (register schema)
    4. Run: threadweave graph sync       (full sync)
    5. Run: threadweave graph daemon     (continuous sync)
"""

from threadweave.connectors.graph.connector import (
    ThreadWeaveGraphConnector,
    SyncStats,
)
from threadweave.connectors.graph.sync import SyncEngine, SyncState
from threadweave.connectors.graph.auth import GraphCredentials, GraphAuth
from threadweave.connectors.graph.schema import (
    CONNECTION_ID,
    CONNECTION_NAME,
    map_threadweave_to_graph,
    GraphExternalItem,
    GraphItemProperties,
    GraphAclEntry,
)

__all__ = [
    "ThreadWeaveGraphConnector",
    "SyncEngine",
    "SyncState",
    "SyncStats",
    "GraphCredentials",
    "GraphAuth",
    "CONNECTION_ID",
    "CONNECTION_NAME",
    "map_threadweave_to_graph",
    "GraphExternalItem",
    "GraphItemProperties",
    "GraphAclEntry",
]
