"""MeshKore SDK — public API.

The config/CLI surface is importable with zero heavy dependencies so
`python -m meshkore join ...` works in minimal environments. The
WebSocket/async agent and the REST agent are loaded lazily via
`__getattr__`, so `import meshkore; meshkore.MeshKoreAgent` still works
but only pulls `websockets` / `httpx` when actually accessed.
"""

from .autoconnect import bootstrap_from_invite, load_or_bootstrap
from .config import (
    ConfigError,
    IdentityConfig,
    JoinConfig,
    MeshKoreConfig,
    NetworkConfig,
    PolicyConfig,
    ProfileConfig,
    SCHEMA_VERSION,
    ensure_gitignored,
)
from .exceptions import AgentOfflineError, AuthError, MeshKoreError
from .rest_agent import verify_webhook_signature
from .fleet import (
    BroadcastResult,
    FleetClient,
    FleetResponder,
    RequestResult,
    DEFAULT_FEATURES as FLEET_DEFAULT_FEATURES,
    is_fleet_type,
)
from .models import RelayMessage

__all__ = [
    "MeshKoreAgent",
    "MeshKoreRestAgent",
    "MeshKoreConfig",
    "NetworkConfig",
    "IdentityConfig",
    "JoinConfig",
    "ProfileConfig",
    "PolicyConfig",
    "ConfigError",
    "SCHEMA_VERSION",
    "ensure_gitignored",
    "load_or_bootstrap",
    "bootstrap_from_invite",
    "RelayMessage",
    "MeshKoreError",
    "AuthError",
    "AgentOfflineError",
    "FleetClient",
    "FleetResponder",
    "BroadcastResult",
    "RequestResult",
    "FLEET_DEFAULT_FEATURES",
    "is_fleet_type",
    "verify_webhook_signature",
]


def __getattr__(name: str):
    if name == "MeshKoreAgent":
        from .agent import MeshKoreAgent
        return MeshKoreAgent
    if name == "MeshKoreRestAgent":
        from .rest_agent import MeshKoreRestAgent
        return MeshKoreRestAgent
    raise AttributeError(f"module 'meshkore' has no attribute {name!r}")
