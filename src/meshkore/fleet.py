"""
MeshKore Fleet — coordination protocol client.

Reference implementation of the fleet.* message conventions documented
at https://hub.meshkore.com/docs/agent/fleet. Any agent can
participate in a fleet by exchanging messages with `payload.type`
starting with `fleet.` — this module just makes it ergonomic.

Two classes:

    FleetClient     Sender side. Wraps a MeshKoreRestAgent to list peers,
                    broadcast messages, ping-and-gather, request code
                    updates, announce presence.

    FleetResponder  Receiver side. Stateless helper that looks at an
                    incoming message and returns the correct fleet.*
                    auto-reply (pong, status, …) when appropriate.

The module does NOT execute update_request / restart side-effects —
those are opt-in per agent and implemented by the agent's author.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .rest_agent import MeshKoreRestAgent

# ──────────────────────────────────────────────
# Payload type constants
# ──────────────────────────────────────────────

FLEET_PING = "fleet.ping"
FLEET_PONG = "fleet.pong"
FLEET_STATUS_REQUEST = "fleet.status_request"
FLEET_STATUS = "fleet.status"
FLEET_ANNOUNCE = "fleet.announce"
FLEET_GOING_AWAY = "fleet.going_away"
FLEET_RETURNED = "fleet.returned"
FLEET_UPDATE_REQUEST = "fleet.update_request"
FLEET_UPDATE_ACK = "fleet.update_ack"
FLEET_UPDATE_RESULT = "fleet.update_result"
FLEET_RESTART = "fleet.restart"
FLEET_BROADCAST = "fleet.broadcast"
FLEET_BROADCAST_RESULT = "fleet.broadcast_result"

DEFAULT_FEATURES = ["ping", "status", "announce", "going_away", "returned"]


def _now() -> int:
    return int(time.time())


def is_fleet_type(payload_type: str | None) -> bool:
    """True if the given payload.type is a fleet.* op."""
    return bool(payload_type) and payload_type.startswith("fleet.")


# ──────────────────────────────────────────────
# FleetClient — sender side
# ──────────────────────────────────────────────

@dataclass
class BroadcastResult:
    """Outcome of a fan-out to many agents."""
    targets: list[str] = field(default_factory=list)
    sent: list[str] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "targets": self.targets,
            "sent": self.sent,
            "failed": self.failed,
        }


@dataclass
class RequestResult:
    """Outcome of a request-reply fan-out."""
    correlation_id: str
    sent_to: list[str] = field(default_factory=list)
    replies: dict[str, dict[str, Any]] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    failed_sends: list[dict[str, str]] = field(default_factory=list)
    leftover_messages: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "correlation_id": self.correlation_id,
            "sent_to": self.sent_to,
            "responded": list(self.replies.keys()),
            "replies": self.replies,
            "missing": self.missing,
            "failed_sends": self.failed_sends,
            "leftover_count": len(self.leftover_messages),
        }


class FleetClient:
    """Sender side of the fleet.* protocol.

    Wraps an authenticated MeshKoreRestAgent. The agent must have been
    registered (agent.register() or agent is used inside a `with`
    context) before calling any FleetClient method that hits the hub.
    """

    def __init__(self, agent: "MeshKoreRestAgent"):
        self.agent = agent

    # ---- discovery ----

    def list(
        self,
        *,
        capability: str | None = None,
        query: str | None = None,
        include_offline: bool = False,
    ) -> list[dict[str, Any]]:
        """GET /agents with optional filters. Returns raw agent dicts."""
        params: dict[str, str] = {}
        if capability:
            params["capability"] = capability
        if query:
            params["q"] = query
        if include_offline:
            params["all"] = "true"
        resp = self.agent._client.get(f"{self.agent.hub_url}/agents/", params=params)
        resp.raise_for_status()
        return resp.json()

    # ---- low-level broadcast ----

    def broadcast(
        self,
        payload: dict[str, Any],
        *,
        capability: str | None = None,
        include_self: bool = False,
        include_offline: bool = False,
    ) -> BroadcastResult:
        """Fire-and-forget the same payload to every matching agent.
        Returns a BroadcastResult (no waiting for replies)."""
        targets = [
            a["agent_id"] for a in self.list(capability=capability, include_offline=include_offline)
        ]
        if not include_self:
            targets = [t for t in targets if t != self.agent.agent_id]

        result = BroadcastResult(targets=targets)
        for tid in targets:
            try:
                self.agent.send(tid, payload)
                result.sent.append(tid)
            except Exception as e:  # network error, 429, etc.
                result.failed.append({"agent_id": tid, "error": str(e)})
        return result

    def request(
        self,
        payload: dict[str, Any],
        *,
        reply_type: str,
        correlation_field: str,
        capability: str | None = None,
        timeout: float = 10.0,
        poll_interval: float = 0.5,
    ) -> RequestResult:
        """Broadcast a payload that carries a correlation ID and collect
        replies of `reply_type` matching that ID within `timeout`
        seconds. Messages that arrive and do NOT match end up in
        `leftover_messages` so the caller can still handle them."""
        correlation_id = payload.get(correlation_field) or uuid.uuid4().hex
        payload[correlation_field] = correlation_id

        bcast = self.broadcast(payload, capability=capability)
        result = RequestResult(
            correlation_id=correlation_id,
            sent_to=list(bcast.sent),
            failed_sends=list(bcast.failed),
        )

        deadline = time.time() + timeout
        while time.time() < deadline and len(result.replies) < len(result.sent_to):
            msgs = self.agent.poll()
            for m in msgs:
                p = m.get("payload") or {}
                if (
                    p.get("type") == reply_type
                    and p.get(correlation_field) == correlation_id
                ):
                    sender = m.get("from", "")
                    if sender not in result.replies:
                        result.replies[sender] = p
                else:
                    result.leftover_messages.append(m)
            if len(result.replies) >= len(result.sent_to):
                break
            time.sleep(poll_interval)

        result.missing = [t for t in result.sent_to if t not in result.replies]
        return result

    # ---- high-level ops ----

    def ping(
        self,
        *,
        capability: str | None = None,
        timeout: float = 5.0,
    ) -> RequestResult:
        """Broadcast fleet.ping and collect fleet.pong within timeout."""
        payload = {
            "type": FLEET_PING,
            "ping_id": uuid.uuid4().hex,
            "from": self.agent.agent_id,
            "ts": _now(),
        }
        return self.request(
            payload,
            reply_type=FLEET_PONG,
            correlation_field="ping_id",
            capability=capability,
            timeout=timeout,
        )

    def status(
        self,
        *,
        capability: str | None = None,
        timeout: float = 5.0,
    ) -> RequestResult:
        """Ask every matching agent for a fleet.status report."""
        payload = {
            "type": FLEET_STATUS_REQUEST,
            "request_id": uuid.uuid4().hex,
            "from": self.agent.agent_id,
        }
        return self.request(
            payload,
            reply_type=FLEET_STATUS,
            correlation_field="request_id",
            capability=capability,
            timeout=timeout,
        )

    def announce(
        self,
        *,
        description: str | None = None,
        capabilities: list[str] | None = None,
        features: list[str] | None = None,
    ) -> BroadcastResult:
        """Broadcast fleet.announce — "I'm here, this is what I do"."""
        payload = {
            "type": FLEET_ANNOUNCE,
            "agent_id": self.agent.agent_id,
            "description": description,
            "capabilities": capabilities or [],
            "fleet_features": features or DEFAULT_FEATURES,
            "ts": _now(),
        }
        return self.broadcast(payload)

    def going_away(
        self,
        *,
        return_at: str | None = None,
        reason: str | None = None,
    ) -> BroadcastResult:
        """Broadcast fleet.going_away — "I'm about to disconnect"."""
        payload = {
            "type": FLEET_GOING_AWAY,
            "agent_id": self.agent.agent_id,
            "return_at": return_at,
            "reason": reason,
            "ts": _now(),
        }
        return self.broadcast(payload)

    def returned(
        self,
        *,
        away_for_secs: int | None = None,
        capabilities: list[str] | None = None,
    ) -> BroadcastResult:
        """Broadcast fleet.returned — "I'm back online"."""
        payload = {
            "type": FLEET_RETURNED,
            "agent_id": self.agent.agent_id,
            "away_for_secs": away_for_secs,
            "capabilities": capabilities or [],
            "ts": _now(),
        }
        return self.broadcast(payload)

    def update_request(
        self,
        target: str,
        *,
        source: str = "git",
        description: str | None = None,
        capability: str | None = None,
        timeout: float = 30.0,
        deadline_ts: int | None = None,
    ) -> RequestResult:
        """Ask every matching agent to pull a new version of their code.
        Collects fleet.update_ack responses (NOT update_result — that
        typically arrives much later; use a separate poll for it)."""
        payload = {
            "type": FLEET_UPDATE_REQUEST,
            "update_id": uuid.uuid4().hex,
            "source": source,
            "target": target,
            "description": description or "",
            "requested_by": self.agent.agent_id,
            "deadline_ts": deadline_ts,
            "ts": _now(),
        }
        return self.request(
            payload,
            reply_type=FLEET_UPDATE_ACK,
            correlation_field="update_id",
            capability=capability,
            timeout=timeout,
        )

    def restart(
        self,
        *,
        reason: str | None = None,
        delay_secs: int | None = None,
        capability: str | None = None,
        timeout: float = 10.0,
    ) -> RequestResult:
        """Broadcast fleet.restart. Collects fleet.going_away as
        acknowledgement (agents should send going_away before restarting)."""
        payload = {
            "type": FLEET_RESTART,
            "restart_id": uuid.uuid4().hex,
            "reason": reason,
            "delay_secs": delay_secs,
            "requested_by": self.agent.agent_id,
            "ts": _now(),
        }
        return self.request(
            payload,
            reply_type=FLEET_GOING_AWAY,
            correlation_field="restart_id",
            capability=capability,
            timeout=timeout,
        )

    def custom(
        self,
        command: str,
        *,
        args: dict[str, Any] | None = None,
        capability: str | None = None,
        wait_for_replies: bool = False,
        timeout: float = 10.0,
    ) -> BroadcastResult | RequestResult:
        """Send a fleet.broadcast with an arbitrary command. Receivers
        may ignore it or answer with fleet.broadcast_result."""
        payload: dict[str, Any] = {
            "type": FLEET_BROADCAST,
            "broadcast_id": uuid.uuid4().hex,
            "command": command,
            "args": args or {},
            "requested_by": self.agent.agent_id,
            "ts": _now(),
        }
        if wait_for_replies:
            return self.request(
                payload,
                reply_type=FLEET_BROADCAST_RESULT,
                correlation_field="broadcast_id",
                capability=capability,
                timeout=timeout,
            )
        return self.broadcast(payload, capability=capability)


# ──────────────────────────────────────────────
# FleetResponder — receiver side
# ──────────────────────────────────────────────


class FleetResponder:
    """Stateless handler that produces auto-replies to safe fleet ops.

    Safe defaults:
        fleet.ping            → fleet.pong (always)
        fleet.status_request  → fleet.status (always)

    Everything else (update_request, restart, custom broadcast) is
    ignored unless the agent author explicitly wires it up. This is
    intentional: the responder cannot side-effect the agent's process.

    Typical usage inside a REST poll loop:

        fleet = FleetResponder(agent, capabilities=["coding"])
        for msg in agent.poll():
            reply = fleet.reply_for(msg)
            if reply:
                agent.send(msg["from"], reply)
    """

    def __init__(
        self,
        agent: "MeshKoreRestAgent",
        *,
        description: str | None = None,
        capabilities: list[str] | None = None,
        features: list[str] | None = None,
        project: str | None = None,
        version: str | None = None,
        started_at: int | None = None,
    ):
        self.agent = agent
        self.description = description or ""
        self.capabilities = list(capabilities or [])
        self.features = list(features or DEFAULT_FEATURES)
        self.project = project
        self.version = version
        self.started_at = started_at or _now()

    def reply_for(self, message: dict[str, Any]) -> dict[str, Any] | None:
        """Return the payload of the auto-reply for this incoming
        message, or None if no automatic response applies.

        The caller is responsible for POSTing the reply via
        `agent.send(message["from"], reply)`.
        """
        payload = message.get("payload") or {}
        ptype = payload.get("type")

        if ptype == FLEET_PING:
            return {
                "type": FLEET_PONG,
                "ping_id": payload.get("ping_id"),
                "agent_id": self.agent.agent_id,
                "ts": _now(),
                "status": "available",
                "fleet_features": self.features,
            }

        if ptype == FLEET_STATUS_REQUEST:
            return {
                "type": FLEET_STATUS,
                "request_id": payload.get("request_id"),
                "agent_id": self.agent.agent_id,
                "description": self.description,
                "capabilities": self.capabilities,
                "status": "available",
                "version": self.version,
                "uptime_secs": max(0, _now() - self.started_at),
                "fleet_features": self.features,
                "project": self.project,
                "extra": {},
            }

        # fleet.announce / going_away / returned: no reply expected.
        # fleet.update_request / restart / broadcast: opt-in, never auto.
        return None
