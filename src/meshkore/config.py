"""
MeshKore config — parse, validate, persist, protect the `.meshkore` pair.

MeshKore configuration lives in TWO JSON files, following the same pattern
as docker-compose.yml / docker-compose.override.yml, Django settings.py /
local_settings.py, or .env / .env.local:

    .meshkore         — the base config. Committed to the repo. Contains
                        hub URL, invite URL, default profile, policy. NO
                        credentials. Safe to share. Maintained upstream.

    .meshkore.local   — per-user overrides. NEVER committed. Contains the
                        local agent's identity + api_key (obtained via
                        bootstrap from the invite URL) and any personal
                        overrides. Always gitignored automatically.

At load time the SDK:
  1. Locates the nearest `.meshkore` walking upward from cwd.
  2. Looks for `.meshkore.local` in the SAME directory as the base.
  3. Deep-merges them (local wins field by field).
  4. Validates the merged result.

Standalone agents (no shared repo) can instead put everything in a single
`.meshkore` with visibility=private — the SDK still auto-gitignores it.

Schema v1 — see https://hub.meshkore.com/docs/agent/config
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from .exceptions import MeshKoreError

SCHEMA_VERSION = 1
CONFIG_FILENAME = ".meshkore"
CONFIG_LOCAL_FILENAME = ".meshkore.local"
GITIGNORE_FILENAME = ".gitignore"

VISIBILITY_PRIVATE = "private"
VISIBILITY_PROJECT = "project"
VISIBILITY_PUBLIC_TEMPLATE = "public-template"
VALID_VISIBILITY = {VISIBILITY_PRIVATE, VISIBILITY_PROJECT, VISIBILITY_PUBLIC_TEMPLATE}

MODE_PUBLIC = "public"
MODE_PROJECT = "project"
MODE_RESTRICTED = "restricted"
VALID_MODES = {MODE_PUBLIC, MODE_PROJECT, MODE_RESTRICTED}

ACCEPT_ANYONE = "anyone"
ACCEPT_PROJECT = "project"
ACCEPT_ALLOWLIST = "allowlist"
VALID_ACCEPT = {ACCEPT_ANYONE, ACCEPT_PROJECT, ACCEPT_ALLOWLIST}


class ConfigError(MeshKoreError):
    """Raised when the .meshkore file is missing, malformed, or incomplete."""


DEFAULT_HUB = "https://hub.meshkore.com"


@dataclass
class NetworkConfig:
    hub: str = DEFAULT_HUB
    mode: str = MODE_PUBLIC
    project: str | None = None
    docs: str | None = None


@dataclass
class IdentityConfig:
    agent_id: str | None = None
    api_key: str | None = None


@dataclass
class JoinConfig:
    invite: str | None = None


@dataclass
class ProfileConfig:
    description: str = ""
    capabilities: list[str] = field(default_factory=list)
    visible_in_directory: bool = False


@dataclass
class PolicyConfig:
    accept_from: str = ACCEPT_ANYONE
    allowlist: list[str] = field(default_factory=list)
    rate_limit: int = 60


@dataclass
class MeshKoreConfig:
    version: int = SCHEMA_VERSION
    visibility: str = VISIBILITY_PRIVATE
    network: NetworkConfig = field(default_factory=NetworkConfig)
    identity: IdentityConfig = field(default_factory=IdentityConfig)
    join: JoinConfig = field(default_factory=JoinConfig)
    profile: ProfileConfig = field(default_factory=ProfileConfig)
    policy: PolicyConfig = field(default_factory=PolicyConfig)

    # Non-persisted metadata about which files this config came from.
    # base_path is the `.meshkore` that was loaded; local_path is the
    # `.meshkore.local` sibling if one was merged on top.
    source_path: Path | None = field(default=None, repr=False, compare=False)
    base_path: Path | None = field(default=None, repr=False, compare=False)
    local_path: Path | None = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------------
    # Parse / serialize
    # ------------------------------------------------------------------

    @classmethod
    def from_dict(cls, data: dict[str, Any], *, strict: bool = True) -> "MeshKoreConfig":
        """Parse a dict into a MeshKoreConfig.

        strict=True (default) validates the document as a self-contained
        config: network.hub is required and visibility must make sense
        given what's present. Use strict=True for the *merged* result or
        for a standalone `.meshkore` file.

        strict=False is used to parse `.meshkore.local` or any partial
        override document. Missing fields default to placeholders and
        semantic checks are deferred to the merged result.
        """
        if not isinstance(data, dict):
            raise ConfigError("meshkore config must be a JSON object at the top level")

        version = data.get("version", SCHEMA_VERSION)
        if version != SCHEMA_VERSION:
            raise ConfigError(
                f"meshkore config version {version} is not supported by this SDK "
                f"(expected {SCHEMA_VERSION}). Upgrade the SDK or downgrade the file."
            )

        visibility = data.get("visibility", VISIBILITY_PRIVATE if not strict else VISIBILITY_PRIVATE)
        if visibility not in VALID_VISIBILITY:
            raise ConfigError(
                f"visibility must be one of {sorted(VALID_VISIBILITY)}, got {visibility!r}"
            )

        net_raw = data.get("network") or {}
        network = NetworkConfig(
            hub=net_raw.get("hub", DEFAULT_HUB if strict else ""),
            mode=net_raw.get("mode", MODE_PUBLIC),
            project=net_raw.get("project"),
            docs=net_raw.get("docs"),
        )
        if network.mode not in VALID_MODES:
            raise ConfigError(
                f"network.mode must be one of {sorted(VALID_MODES)}, got {network.mode!r}"
            )
        if strict:
            if not network.hub or not isinstance(network.hub, str):
                raise ConfigError("network.hub is required and must be a string URL")
        if isinstance(network.hub, str):
            network.hub = network.hub.rstrip("/")

        id_raw = data.get("identity") or {}
        identity = IdentityConfig(
            agent_id=id_raw.get("agent_id"),
            api_key=id_raw.get("api_key"),
        )

        join_raw = data.get("join") or {}
        join = JoinConfig(invite=join_raw.get("invite"))

        prof_raw = data.get("profile") or {}
        profile = ProfileConfig(
            description=prof_raw.get("description", "") or "",
            capabilities=list(prof_raw.get("capabilities") or []),
            visible_in_directory=bool(prof_raw.get("visible_in_directory", False)),
        )

        pol_raw = data.get("policy") or {}
        policy = PolicyConfig(
            accept_from=pol_raw.get("accept_from", ACCEPT_ANYONE),
            allowlist=list(pol_raw.get("allowlist") or []),
            rate_limit=int(pol_raw.get("rate_limit", 60)),
        )
        if policy.accept_from not in VALID_ACCEPT:
            raise ConfigError(
                f"policy.accept_from must be one of {sorted(VALID_ACCEPT)}, got {policy.accept_from!r}"
            )

        cfg = cls(
            version=version,
            visibility=visibility,
            network=network,
            identity=identity,
            join=join,
            profile=profile,
            policy=policy,
        )
        if strict:
            cfg._validate_semantics()
        return cfg

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "version": self.version,
            "visibility": self.visibility,
            "network": {
                "hub": self.network.hub,
                "mode": self.network.mode,
                "project": self.network.project,
                "docs": self.network.docs,
            },
            "identity": {
                "agent_id": self.identity.agent_id,
                "api_key": self.identity.api_key,
            },
            "join": {"invite": self.join.invite},
            "profile": {
                "description": self.profile.description,
                "capabilities": list(self.profile.capabilities),
                "visible_in_directory": self.profile.visible_in_directory,
            },
            "policy": {
                "accept_from": self.policy.accept_from,
                "allowlist": list(self.policy.allowlist),
                "rate_limit": self.policy.rate_limit,
            },
        }
        # Drop nulls in identity / join for public-template so the file stays clean.
        if self.visibility == VISIBILITY_PUBLIC_TEMPLATE:
            if not d["identity"]["agent_id"] and not d["identity"]["api_key"]:
                d.pop("identity")
        return d

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False) + "\n"

    def _validate_semantics(self) -> None:
        """Enforce visibility-specific requirements."""
        if self.visibility in (VISIBILITY_PRIVATE, VISIBILITY_PROJECT):
            # Must resolve to credentials somehow — either inline or via invite bootstrap.
            has_creds = bool(self.identity.agent_id and self.identity.api_key)
            has_invite = bool(self.join.invite)
            if not has_creds and not has_invite:
                raise ConfigError(
                    f"visibility={self.visibility!r} requires either inline "
                    f"identity.{{agent_id,api_key}} or join.invite for bootstrap"
                )
        elif self.visibility == VISIBILITY_PUBLIC_TEMPLATE:
            if not self.join.invite:
                raise ConfigError(
                    "visibility='public-template' requires join.invite "
                    "(templates bootstrap credentials from the invite URL)"
                )
            if self.identity.api_key:
                raise ConfigError(
                    "visibility='public-template' must NOT contain identity.api_key "
                    "(public templates are committed to git — inline secrets would leak)"
                )

    def has_credentials(self) -> bool:
        return bool(self.identity.agent_id and self.identity.api_key)

    def requires_secret_protection(self) -> bool:
        """True if this file contains secrets and must not be committed to git."""
        return self.visibility in (VISIBILITY_PRIVATE, VISIBILITY_PROJECT) and self.has_credentials()

    # ------------------------------------------------------------------
    # Filesystem I/O
    # ------------------------------------------------------------------

    @staticmethod
    def _read_json(p: Path) -> dict[str, Any]:
        try:
            raw = p.read_text(encoding="utf-8")
        except OSError as e:
            raise ConfigError(f"cannot read {p}: {e}") from e
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ConfigError(f"{p} is not valid JSON: {e}") from e
        if not isinstance(data, dict):
            raise ConfigError(f"{p} must be a JSON object at the top level")
        return data

    @classmethod
    def load(cls, path: str | Path, *, strict: bool = True) -> "MeshKoreConfig":
        """Load a single file as a standalone config (no merging)."""
        p = Path(path)
        if not p.exists():
            raise ConfigError(f"{p} does not exist")
        data = cls._read_json(p)
        cfg = cls.from_dict(data, strict=strict)
        cfg.source_path = p.resolve()
        return cfg

    @classmethod
    def find(cls, start: str | Path | None = None) -> Path | None:
        """Walk up from `start` (default cwd) looking for a .meshkore file.
        Mirrors how git locates .git — the nearest ancestor wins.

        A bare `.meshkore.local` (without a sibling `.meshkore`) also
        counts as a valid match — this supports standalone agents that
        only have a local file.
        """
        start_path = Path(start or os.getcwd()).resolve()
        candidates = [start_path, *start_path.parents]
        for d in candidates:
            base = d / CONFIG_FILENAME
            local = d / CONFIG_LOCAL_FILENAME
            if base.is_file():
                return base
            if local.is_file():
                return local
        return None

    @classmethod
    def find_pair(
        cls, start: str | Path | None = None
    ) -> tuple[Path | None, Path | None]:
        """Return (base_path, local_path) for the nearest meshkore config
        in `start` or any ancestor directory. Either value may be None if
        the corresponding file is missing."""
        start_path = Path(start or os.getcwd()).resolve()
        for d in [start_path, *start_path.parents]:
            base = d / CONFIG_FILENAME
            local = d / CONFIG_LOCAL_FILENAME
            if base.is_file() or local.is_file():
                return (
                    base.resolve() if base.is_file() else None,
                    local.resolve() if local.is_file() else None,
                )
        return (None, None)

    @classmethod
    def load_merged(cls, start: str | Path | None = None) -> "MeshKoreConfig":
        """Load `.meshkore` + `.meshkore.local` (if present) and deep-merge.

        The local file overrides the base field-by-field. The merged
        document is then strictly validated. This is the method the
        SDK uses in normal operation — it handles all three scenarios:
            • base only (public-template in a shared repo, no local yet)
            • local only (standalone agent, no shared base)
            • base + local (the common case after bootstrap)
        """
        base_path, local_path = cls.find_pair(start)
        if base_path is None and local_path is None:
            raise ConfigError(
                f"no {CONFIG_FILENAME} or {CONFIG_LOCAL_FILENAME} found walking up from "
                f"{start or os.getcwd()}. Run `python -m meshkore join <invite-url>` to create one."
            )

        base_raw: dict[str, Any] = {}
        local_raw: dict[str, Any] = {}
        if base_path is not None:
            base_raw = cls._read_json(base_path)
        if local_path is not None:
            local_raw = cls._read_json(local_path)

        merged = _deep_merge(base_raw, local_raw)

        # If a `.meshkore.local` declared visibility, it wins (users
        # typically keep that file as visibility="private"). Otherwise
        # infer: presence of api_key implies private.
        if "visibility" not in local_raw:
            id_section = merged.get("identity") or {}
            if id_section.get("api_key"):
                merged["visibility"] = VISIBILITY_PRIVATE

        cfg = cls.from_dict(merged, strict=True)
        cfg.base_path = base_path
        cfg.local_path = local_path
        # source_path points at whichever file is considered "primary".
        cfg.source_path = base_path or local_path
        return cfg

    # Backward-compatible alias — prior API used load_nearest().
    @classmethod
    def load_nearest(cls, start: str | Path | None = None) -> "MeshKoreConfig":
        return cls.load_merged(start)

    def save(self, path: str | Path | None = None) -> Path:
        """Write this full config to disk at `path` (or source_path).
        Standalone use — for the two-file model prefer save_credentials_local()."""
        target = Path(path) if path is not None else self.source_path
        if target is None:
            raise ConfigError("save() requires a path when source_path is unknown")
        target = target.resolve()
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(self.to_json(), encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, target)
        self.source_path = target
        if target.name == CONFIG_LOCAL_FILENAME:
            self.local_path = target
        elif target.name == CONFIG_FILENAME:
            self.base_path = target
        if self.requires_secret_protection():
            ensure_gitignored(target)
        return target

    def save_credentials_local(self, base_dir: str | Path | None = None) -> Path:
        """Write ONLY the credentials + private overrides to `.meshkore.local`
        next to the base file, leaving the shared `.meshkore` untouched.

        This is what bootstrap and `meshkore join` use when a public template
        is already present — never mutate the upstream file, always produce a
        sibling `.meshkore.local` that the user owns.
        """
        if base_dir is not None:
            target_dir = Path(base_dir)
        elif self.base_path is not None:
            target_dir = self.base_path.parent
        elif self.local_path is not None:
            target_dir = self.local_path.parent
        elif self.source_path is not None:
            target_dir = self.source_path.parent
        else:
            target_dir = Path.cwd()
        target = (target_dir / CONFIG_LOCAL_FILENAME).resolve()

        doc: dict[str, Any] = {
            "version": self.version,
            "visibility": VISIBILITY_PRIVATE,
            "identity": {
                "agent_id": self.identity.agent_id,
                "api_key": self.identity.api_key,
            },
        }
        # If the local differs from the base on network (e.g. a different hub),
        # persist that override. Otherwise omit to keep the local file minimal.
        if self.base_path is None:
            doc["network"] = {
                "hub": self.network.hub,
                "mode": self.network.mode,
                "project": self.network.project,
            }

        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, target)
        self.local_path = target
        ensure_gitignored(target)
        return target


# ----------------------------------------------------------------------
# Deep merge helper (local overrides base, dicts merge, scalars replace)
# ----------------------------------------------------------------------

def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge `override` on top of `base` returning a new dict.

    Dictionaries are merged recursively. Any non-dict value in
    `override` replaces the corresponding value in `base`. Lists are
    replaced wholesale (NOT concatenated) — this matches how Docker
    Compose overrides, Django settings, and .env.local behave and
    avoids surprising accumulation of stale list entries.
    """
    out: dict[str, Any] = dict(base)
    for key, value in override.items():
        if (
            key in out
            and isinstance(out[key], dict)
            and isinstance(value, dict)
        ):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


# ----------------------------------------------------------------------
# .gitignore protection
# ----------------------------------------------------------------------

def _find_git_root(start: Path) -> Path | None:
    p = start.resolve()
    for d in [p, *p.parents]:
        if (d / ".git").exists():
            return d
    return None


def ensure_gitignored(config_path: Path) -> bool:
    """Make sure the given meshkore config path is ignored by git.

    Looks for the nearest .git directory. If found, appends the file's
    basename (`.meshkore` or `.meshkore.local`) to the repo's
    `.gitignore` (creating it if needed) unless it's already listed.
    Returns True if the file is now protected, False if there's no git
    repo to protect against.
    """
    config_path = config_path.resolve()
    git_root = _find_git_root(config_path.parent)
    if git_root is None:
        return False

    gitignore = git_root / GITIGNORE_FILENAME
    entry = config_path.name  # `.meshkore.local` or `.meshkore`

    existing_lines: list[str] = []
    if gitignore.exists():
        try:
            existing_lines = gitignore.read_text(encoding="utf-8").splitlines()
        except OSError:
            existing_lines = []
        stripped = {ln.strip() for ln in existing_lines}
        if entry in stripped or f"/{entry}" in stripped:
            return True

    block = [
        "",
        "# MeshKore — contains agent credentials, do not commit",
        entry,
        ""
    ]
    new_content = "\n".join(existing_lines + block).lstrip("\n")
    if not new_content.endswith("\n"):
        new_content += "\n"
    try:
        gitignore.write_text(new_content, encoding="utf-8")
    except OSError as e:
        raise ConfigError(f"failed to update {gitignore}: {e}") from e
    return True
