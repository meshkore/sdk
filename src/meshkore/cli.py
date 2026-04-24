"""
MeshKore CLI — commands around the two-file `.meshkore` / `.meshkore.local`
configuration.

    python -m meshkore join <invite-url> [--agent-id NAME] [--capabilities a,b,c]
        Bootstrap credentials from an invite URL.
        - In a repo that already has a base `.meshkore` public template:
          writes ONLY `.meshkore.local` next to it. The base file is
          untouched so `git pull` stays clean.
        - In an empty directory (no base yet): writes a full `.meshkore`
          with inline credentials, visibility=private.
        Either way the credential file is added to `.gitignore` if inside
        a git repo.

    python -m meshkore status [--path PATH]
        Show a summary of the current merged config (base + local): hub,
        agent_id, visibility, credentials present, gitignore protection.

    python -m meshkore init [--hub URL] [--invite URL]
        Write a blank `.meshkore` public template in the current directory.
        Useful for repo maintainers creating a template for others to
        clone and bootstrap.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .config import (
    CONFIG_FILENAME,
    CONFIG_LOCAL_FILENAME,
    DEFAULT_HUB,
    IdentityConfig,
    JoinConfig,
    MeshKoreConfig,
    NetworkConfig,
    PolicyConfig,
    ProfileConfig,
    ConfigError,
    VISIBILITY_PRIVATE,
    VISIBILITY_PUBLIC_TEMPLATE,
)
from .autoconnect import bootstrap_from_invite
from .exceptions import AuthError, MeshKoreError


def _load_or_none(start: Path) -> MeshKoreConfig | None:
    try:
        return MeshKoreConfig.load_merged(start)
    except ConfigError:
        return None


def cmd_join(args: argparse.Namespace) -> int:
    start_dir = Path(args.path or Path.cwd()).resolve()
    caps = [c.strip() for c in (args.capabilities or "").split(",") if c.strip()]

    existing = _load_or_none(start_dir)

    if existing is not None and existing.has_credentials() and not args.force:
        print(
            f"meshkore join: credentials already present "
            f"(base={existing.base_path}, local={existing.local_path}). "
            f"Use --force to overwrite.",
            file=sys.stderr,
        )
        return 1

    if existing is not None and existing.base_path is not None:
        # There's already a base template (public). Reuse it, override
        # the invite on the CLI if provided, and only write a local.
        cfg = existing
        if args.invite_url:
            cfg.join.invite = args.invite_url
        if not cfg.join.invite:
            print(
                "meshkore join: base .meshkore has no join.invite and none provided on CLI",
                file=sys.stderr,
            )
            return 1
        if args.hub:
            cfg.network.hub = args.hub.rstrip("/")
        if args.description:
            cfg.profile.description = args.description
        if caps:
            cfg.profile.capabilities = caps
    else:
        # No base template on disk → build a standalone skeleton that
        # bootstrap_from_invite will promote to a full private config.
        if not args.invite_url:
            print("meshkore join: invite URL is required", file=sys.stderr)
            return 1
        cfg = MeshKoreConfig(
            visibility=VISIBILITY_PUBLIC_TEMPLATE,
            network=NetworkConfig(
                hub=(args.hub or DEFAULT_HUB).rstrip("/"),
            ),
            identity=IdentityConfig(),
            join=JoinConfig(invite=args.invite_url),
            profile=ProfileConfig(
                description=args.description or "",
                capabilities=caps,
            ),
            policy=PolicyConfig(),
        )
        cfg.source_path = start_dir / CONFIG_FILENAME

    try:
        bootstrap_from_invite(cfg, agent_id=args.agent_id, capabilities=caps)
    except (AuthError, ConfigError, MeshKoreError) as e:
        print(f"meshkore join: {e}", file=sys.stderr)
        return 1

    target = cfg.local_path or cfg.source_path
    print(f"meshkore: joined as {cfg.identity.agent_id}")
    print(f"  hub:         {cfg.network.hub}")
    if cfg.base_path is not None:
        print(f"  base:        {cfg.base_path}  (committed, untouched)")
        print(f"  credentials: {target}  (gitignored)")
    else:
        print(f"  config:      {target}  (visibility=private, gitignored)")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    start = Path(args.path) if args.path else Path.cwd()
    try:
        cfg = MeshKoreConfig.load_merged(start)
    except ConfigError as e:
        print(f"meshkore status: {e}", file=sys.stderr)
        return 1

    protected = "n/a"
    secret_file = cfg.local_path or (cfg.base_path if cfg.has_credentials() else None)
    if secret_file is not None:
        from .config import _find_git_root, GITIGNORE_FILENAME
        root = _find_git_root(secret_file.parent)
        if root is None:
            protected = "no git repo"
        else:
            gi = root / GITIGNORE_FILENAME
            if gi.exists():
                lines = {ln.strip() for ln in gi.read_text().splitlines()}
                entry = secret_file.name
                protected = (
                    "yes"
                    if (entry in lines or f"/{entry}" in lines)
                    else "NO — secrets at risk!"
                )
            else:
                protected = "NO — no .gitignore"

    summary = {
        "base_path": str(cfg.base_path) if cfg.base_path else None,
        "local_path": str(cfg.local_path) if cfg.local_path else None,
        "visibility": cfg.visibility,
        "network": {
            "hub": cfg.network.hub,
            "mode": cfg.network.mode,
            "project": cfg.network.project,
            "docs": cfg.network.docs,
        },
        "agent_id": cfg.identity.agent_id,
        "credentials_present": cfg.has_credentials(),
        "gitignore_protected": protected,
        "profile": {
            "description": cfg.profile.description,
            "capabilities": cfg.profile.capabilities,
            "visible_in_directory": cfg.profile.visible_in_directory,
        },
        "policy": {
            "accept_from": cfg.policy.accept_from,
            "rate_limit": cfg.policy.rate_limit,
        },
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return 0


def cmd_init(args: argparse.Namespace) -> int:
    target = Path(args.path or Path.cwd() / CONFIG_FILENAME).resolve()
    if target.exists() and not args.force:
        print(
            f"meshkore init: {target} already exists (use --force to overwrite)",
            file=sys.stderr,
        )
        return 1

    cfg = MeshKoreConfig(
        visibility=VISIBILITY_PUBLIC_TEMPLATE,
        network=NetworkConfig(hub=(args.hub or DEFAULT_HUB).rstrip("/")),
        join=JoinConfig(
            invite=args.invite or "https://hub.meshkore.com/join/REPLACE_ME"
        ),
        profile=ProfileConfig(description=args.description or "", capabilities=[]),
    )
    cfg.source_path = target
    try:
        cfg.save(target)
    except ConfigError as e:
        print(f"meshkore init: {e}", file=sys.stderr)
        return 1
    print(f"meshkore: wrote public template to {target}")
    print("  visibility=public-template (safe to commit to git)")
    print("  edit join.invite to point at a real invite URL before distributing")
    print(f"  contributors will bootstrap their own {CONFIG_LOCAL_FILENAME} automatically")
    return 0


def _connected_agent_from_config():
    """Load the merged .meshkore config and return a registered REST agent."""
    from .rest_agent import MeshKoreRestAgent

    agent = MeshKoreRestAgent.from_config()
    agent.register()
    return agent


def _print_json(obj: Any) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def cmd_fleet_list(args: argparse.Namespace) -> int:
    from .fleet import FleetClient
    try:
        agent = _connected_agent_from_config()
    except Exception as e:
        print(f"meshkore fleet list: {e}", file=sys.stderr)
        return 1
    fleet = FleetClient(agent)
    agents = fleet.list(capability=args.capability, include_offline=args.all)
    _print_json(agents)
    return 0


def cmd_fleet_ping(args: argparse.Namespace) -> int:
    from .fleet import FleetClient
    try:
        agent = _connected_agent_from_config()
    except Exception as e:
        print(f"meshkore fleet ping: {e}", file=sys.stderr)
        return 1
    fleet = FleetClient(agent)
    result = fleet.ping(capability=args.capability, timeout=args.timeout)
    _print_json(result.to_dict())
    return 0


def cmd_fleet_status(args: argparse.Namespace) -> int:
    from .fleet import FleetClient
    try:
        agent = _connected_agent_from_config()
    except Exception as e:
        print(f"meshkore fleet status: {e}", file=sys.stderr)
        return 1
    fleet = FleetClient(agent)
    result = fleet.status(capability=args.capability, timeout=args.timeout)
    _print_json(result.to_dict())
    return 0


def cmd_fleet_announce(args: argparse.Namespace) -> int:
    from .fleet import FleetClient
    try:
        agent = _connected_agent_from_config()
    except Exception as e:
        print(f"meshkore fleet announce: {e}", file=sys.stderr)
        return 1
    fleet = FleetClient(agent)
    caps = (
        [c.strip() for c in args.capabilities.split(",") if c.strip()]
        if args.capabilities
        else None
    )
    result = fleet.announce(description=args.description, capabilities=caps)
    _print_json(result.to_dict())
    return 0


def cmd_fleet_going_away(args: argparse.Namespace) -> int:
    from .fleet import FleetClient
    try:
        agent = _connected_agent_from_config()
    except Exception as e:
        print(f"meshkore fleet going-away: {e}", file=sys.stderr)
        return 1
    fleet = FleetClient(agent)
    result = fleet.going_away(return_at=args.return_at, reason=args.reason)
    _print_json(result.to_dict())
    return 0


def cmd_fleet_update_request(args: argparse.Namespace) -> int:
    from .fleet import FleetClient
    try:
        agent = _connected_agent_from_config()
    except Exception as e:
        print(f"meshkore fleet update-request: {e}", file=sys.stderr)
        return 1
    fleet = FleetClient(agent)
    result = fleet.update_request(
        target=args.target,
        source=args.source,
        description=args.description,
        capability=args.capability,
        timeout=args.timeout,
    )
    _print_json(result.to_dict())
    return 0


def cmd_fleet_restart(args: argparse.Namespace) -> int:
    from .fleet import FleetClient
    try:
        agent = _connected_agent_from_config()
    except Exception as e:
        print(f"meshkore fleet restart: {e}", file=sys.stderr)
        return 1
    fleet = FleetClient(agent)
    result = fleet.restart(
        reason=args.reason,
        delay_secs=args.delay,
        capability=args.capability,
        timeout=args.timeout,
    )
    _print_json(result.to_dict())
    return 0


def cmd_fleet_broadcast(args: argparse.Namespace) -> int:
    from .fleet import FleetClient
    try:
        agent = _connected_agent_from_config()
    except Exception as e:
        print(f"meshkore fleet broadcast: {e}", file=sys.stderr)
        return 1
    fleet = FleetClient(agent)
    extra_args: dict[str, Any] = {}
    if args.json:
        try:
            extra_args = json.loads(args.json)
        except json.JSONDecodeError as e:
            print(f"meshkore fleet broadcast: invalid --json: {e}", file=sys.stderr)
            return 1
    result = fleet.custom(
        command=args.command,
        args=extra_args,
        capability=args.capability,
        wait_for_replies=args.wait,
        timeout=args.timeout,
    )
    _print_json(result.to_dict())
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m meshkore", description="MeshKore config CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    p_join = sub.add_parser("join", help="Bootstrap credentials from an invite URL")
    p_join.add_argument(
        "invite_url",
        nargs="?",
        help="Invite URL (optional if the base .meshkore already has one)",
    )
    p_join.add_argument("--agent-id", help="Preferred agent ID (default: derived)")
    p_join.add_argument("--capabilities", help="Comma-separated capability list")
    p_join.add_argument("--description", help="Short profile description")
    p_join.add_argument("--hub", help="Override hub URL")
    p_join.add_argument("--path", help="Working directory (default: cwd)")
    p_join.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing .meshkore.local",
    )
    p_join.set_defaults(func=cmd_join)

    p_status = sub.add_parser("status", help="Show current merged config")
    p_status.add_argument("--path", help="Start directory (default: cwd)")
    p_status.set_defaults(func=cmd_status)

    p_init = sub.add_parser(
        "init", help="Write a blank public-template .meshkore in cwd"
    )
    p_init.add_argument("--hub", help="Hub URL")
    p_init.add_argument("--invite", help="Invite URL placeholder")
    p_init.add_argument("--description", help="Profile description")
    p_init.add_argument("--path", help="Target file path")
    p_init.add_argument("--force", action="store_true", help="Overwrite existing")
    p_init.set_defaults(func=cmd_init)

    # ---- fleet subcommands ----
    p_fleet = sub.add_parser("fleet", help="Fleet coordination operations")
    fleet_sub = p_fleet.add_subparsers(dest="fleet_command", required=True)

    f_list = fleet_sub.add_parser("list", help="GET /agents with filters")
    f_list.add_argument("--capability", help="Filter by capability")
    f_list.add_argument("--all", action="store_true", help="Include offline agents")
    f_list.set_defaults(func=cmd_fleet_list)

    f_ping = fleet_sub.add_parser("ping", help="Broadcast fleet.ping, collect pongs")
    f_ping.add_argument("--capability", help="Filter recipients by capability")
    f_ping.add_argument("--timeout", type=float, default=5.0)
    f_ping.set_defaults(func=cmd_fleet_ping)

    f_status = fleet_sub.add_parser("status", help="Ask fleet for full status reports")
    f_status.add_argument("--capability", help="Filter by capability")
    f_status.add_argument("--timeout", type=float, default=10.0)
    f_status.set_defaults(func=cmd_fleet_status)

    f_announce = fleet_sub.add_parser("announce", help="Announce presence to the fleet")
    f_announce.add_argument("--description", help="What this agent does")
    f_announce.add_argument("--capabilities", help="Comma-separated list")
    f_announce.set_defaults(func=cmd_fleet_announce)

    f_going = fleet_sub.add_parser(
        "going-away", help="Tell the fleet you're about to disconnect"
    )
    f_going.add_argument("--return-at", help="ISO-8601 timestamp of expected return")
    f_going.add_argument("--reason", help="Short reason (restart/upgrade/idle/…)")
    f_going.set_defaults(func=cmd_fleet_going_away)

    f_update = fleet_sub.add_parser(
        "update-request", help="Ask the fleet to pull a new version"
    )
    f_update.add_argument("target", help="Ref / version / image to update to")
    f_update.add_argument("--source", default="git", help="git | pypi | npm | docker | custom")
    f_update.add_argument("--description", help="Human reason (shown to receivers)")
    f_update.add_argument("--capability", help="Only ask agents matching this capability")
    f_update.add_argument("--timeout", type=float, default=30.0)
    f_update.set_defaults(func=cmd_fleet_update_request)

    f_restart = fleet_sub.add_parser("restart", help="Ask the fleet to restart")
    f_restart.add_argument("--reason", help="Short reason")
    f_restart.add_argument("--delay", type=int, help="Seconds receivers should wait")
    f_restart.add_argument("--capability", help="Filter recipients")
    f_restart.add_argument("--timeout", type=float, default=10.0)
    f_restart.set_defaults(func=cmd_fleet_restart)

    f_broadcast = fleet_sub.add_parser(
        "broadcast", help="Send a custom fleet.broadcast command"
    )
    f_broadcast.add_argument("command", help="Short verb receivers will dispatch on")
    f_broadcast.add_argument("--json", help="JSON object of extra args")
    f_broadcast.add_argument("--capability", help="Filter recipients")
    f_broadcast.add_argument(
        "--wait",
        action="store_true",
        help="Wait for fleet.broadcast_result replies instead of fire-and-forget",
    )
    f_broadcast.add_argument("--timeout", type=float, default=10.0)
    f_broadcast.set_defaults(func=cmd_fleet_broadcast)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
