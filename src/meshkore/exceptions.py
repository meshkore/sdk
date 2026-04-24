class MeshKoreError(Exception):
    """Base exception for MeshKore SDK."""


class AuthError(MeshKoreError):
    """Authentication failed."""


class AgentOfflineError(MeshKoreError):
    """Target agent is not connected."""


class ConnectionError(MeshKoreError):
    """WebSocket connection failed."""
