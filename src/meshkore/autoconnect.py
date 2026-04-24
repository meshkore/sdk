"""
MeshKore autoconnect — turn a `.meshkore` pair into a connected agent.

The MeshKore config is split across two files (see config.py for the
rationale):

    .meshkore         public base, committed to the repo, no secrets
    .meshkore.local   per-user override with credentials, gitignored

Startup flow:
    1. load_merged()  → walk up, load both files if present, deep-merge.
    2. If the merged config already has credentials → done, return.
    3. If there's a base public-template with an invite URL but no local
       credentials, POST to the invite, then write ONLY the credentials
       to a sibling `.meshkore.local`. The upstream `.meshkore` is never
       touched, so `git pull` of the repo stays clean.

Bootstrap also auto-adds `.meshkore.local` to `.gitignore` if the file
is inside a git repo. Standalone agents (no shared base file) fall
back to a single `.meshkore` with inline credentials, still gitignored.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import httpx

from .config import (
    CONFIG_FILENAME,
    CONFIG_LOCAL_FILENAME,
    DEFAULT_HUB,
    MeshKoreConfig,
    ConfigError,
    NetworkConfig,
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC_TEMPLATE,
    ensure_gitignored,
)
from .exceptions import AuthError, MeshKoreError

logger = logging.getLogger("meshkore")


DEFAULT_AGENT_ID_ENV = "MESHKORE_AGENT_ID"


def _suggest_agent_id(cfg: MeshKoreConfig) -> str:
    """Pick an agent_id when bootstrapping a public template.

    Priority:
      1. MESHKORE_AGENT_ID env var (explicit).
      2. An agent_id already present in the config (even in a template).
      3. A name derived from the project directory + short random suffix.
    """
    env_name = os.environ.get(DEFAULT_AGENT_ID_ENV)
    if env_name:
        return env_name
    if cfg.identity.agent_id:
        return cfg.identity.agent_id
    import secrets

    anchor = cfg.base_path or cfg.local_path or cfg.source_path
    base = anchor.parent.name if anchor else "agent"
    base = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in base).strip("-") or "agent"
    return f"{base}-{secrets.token_hex(3)}"


def bootstrap_from_invite(
    cfg: MeshKoreConfig,
    *,
    agent_id: str | None = None,
    capabilities: list[str] | None = None,
    http_client: httpx.Client | None = None,
) -> MeshKoreConfig:
    """Claim credentials from `cfg.join.invite` and persist them locally.

    Two write targets depending on what was loaded:
      • If a base `.meshkore` exists → write a sibling `.meshkore.local`
        with just the credentials. Upstream file stays untouched.
      • If there's no base at all (pure standalone run) → write a full
        `.meshkore` with inline credentials, visibility=private.

    Either way, the target file is chmod 0600 and added to `.gitignore`
    automatically when inside a git repo.
    """
    if not cfg.join.invite:
        raise ConfigError("cannot bootstrap: config has no join.invite URL")

    chosen_id = agent_id or _suggest_agent_id(cfg)
    caps = capabilities if capabilities is not None else list(cfg.profile.capabilities)

    close_client = False
    if http_client is None:
        http_client = httpx.Client(timeout=20)
        close_client = True

    try:
        resp = http_client.post(
            cfg.join.invite,
            json={"agent_id": chosen_id, "capabilities": caps},
        )
    except httpx.HTTPError as e:
        raise AuthError(f"failed to reach invite URL {cfg.join.invite}: {e}") from e
    finally:
        if close_client:
            http_client.close()

    if resp.status_code != 200:
        raise AuthError(
            f"invite rejected ({resp.status_code}): {resp.text.strip()[:200]}"
        )

    try:
        data = resp.json()
    except ValueError as e:
        raise AuthError(f"invite response was not JSON: {e}") from e

    api_key = data.get("api_key")
    returned_id = data.get("agent_id") or chosen_id
    hub_from_resp = data.get("hub_url")
    if not api_key:
        raise AuthError("invite response missing api_key")

    cfg.identity.agent_id = returned_id
    cfg.identity.api_key = api_key
    if hub_from_resp:
        cfg.network.hub = hub_from_resp.rstrip("/")

    if cfg.base_path is not None:
        # Two-file model: leave the upstream template alone, write
        # credentials to the sibling `.meshkore.local`.
        target = cfg.save_credentials_local()
        cfg.visibility = VISIBILITY_PRIVATE
        logger.info(
            "meshkore: bootstrapped %s, wrote credentials to %s (base template untouched)",
            returned_id,
            target,
        )
    else:
        # Standalone: no shared base. Write a full private `.meshkore`.
        anchor_dir = (
            cfg.local_path.parent if cfg.local_path is not None else Path.cwd()
        )
        target = (anchor_dir / CONFIG_FILENAME).resolve()
        cfg.visibility = VISIBILITY_PRIVATE
        cfg.source_path = target
        cfg.base_path = target
        cfg.save(target)
        logger.info(
            "meshkore: bootstrapped %s, wrote standalone config to %s",
            returned_id,
            target,
        )

    return cfg


def load_or_bootstrap(
    start: str | Path | None = None,
    *,
    agent_id: str | None = None,
    capabilities: list[str] | None = None,
) -> MeshKoreConfig:
    """Return a merged config with usable credentials, bootstrapping if needed.

    Handles all three startup scenarios:
      1. `.meshkore` + `.meshkore.local` already present (common case
         after first bootstrap) → merge and return.
      2. Only `.meshkore.local` exists (standalone agent) → return.
      3. Only `.meshkore` exists as a public-template and no `.meshkore.local`
         yet (fresh clone of a shared repo) → POST to the invite URL
         declared in the base file, write credentials into a new
         `.meshkore.local`, re-merge, return.
    """
    cfg = MeshKoreConfig.load_merged(start)
    if cfg.has_credentials():
        # Defensive: if the merged config somehow has a tracked local file
        # (or even a private `.meshkore` that got committed by accident),
        # make sure the appropriate file is listed in .gitignore.
        if cfg.requires_secret_protection():
            target = cfg.local_path or cfg.source_path
            if target is not None:
                ensure_gitignored(target)
        return cfg

    # No credentials yet. We can only recover if we have an invite URL.
    if not cfg.join.invite:
        raise ConfigError(
            f"loaded meshkore config has no credentials and no join.invite URL "
            f"(base={cfg.base_path}, local={cfg.local_path}). Run "
            f"`python -m meshkore join <invite-url>` to fix."
        )
    return bootstrap_from_invite(cfg, agent_id=agent_id, capabilities=capabilities)
