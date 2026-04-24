"""
MeshKore REST Agent — Synchronous, no WebSocket, no async.

For scripts, CLI tools, and simple agents that just need to send/receive
messages without holding a persistent connection.

Usage:
    from meshkore import MeshKoreRestAgent

    agent = MeshKoreRestAgent(
        hub_url="https://hub.meshkore.com",
        agent_id="my-agent",
        api_key="my-key"
    )
    agent.register()
    agent.send("other-agent", {"type": "greeting", "text": "hello"})
    messages = agent.poll()
"""

import hashlib
import hmac as _hmac
import json
import logging
from pathlib import Path
from typing import Any

import httpx

from .exceptions import AuthError


def verify_webhook_signature(secret: str, body: bytes, header: str) -> bool:
    """Verify the X-MeshKore-Signature header on an incoming webhook POST.

    Args:
        secret: The webhook_secret returned when the webhook was registered.
        body:   Raw request body bytes (before any JSON parsing).
        header: Value of the X-MeshKore-Signature header (e.g. "sha256=abc123").

    Returns:
        True if the signature is valid.
    """
    expected = "sha256=" + _hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return _hmac.compare_digest(header, expected)

logger = logging.getLogger("meshkore")


class MeshKoreRestAgent:
    """Synchronous REST-only MeshKore agent. No WebSocket, no async."""

    def __init__(self, hub_url: str, agent_id: str, api_key: str):
        self.hub_url = hub_url.rstrip("/")
        self.agent_id = agent_id
        self.api_key = api_key
        self._token: str | None = None
        self._client = httpx.Client(timeout=30)
        self._profile_to_apply: dict[str, Any] | None = None

    @classmethod
    def from_config(
        cls,
        path: str | Path | None = None,
        *,
        bootstrap: bool = True,
    ) -> "MeshKoreRestAgent":
        """Build a REST agent from the nearest `.meshkore` file. If the
        file is a public-template without credentials, bootstrap=True
        will claim credentials from the invite and rewrite the file."""
        from .autoconnect import load_or_bootstrap
        from .config import MeshKoreConfig

        if bootstrap:
            cfg = load_or_bootstrap(path)
        else:
            cfg = MeshKoreConfig.load_nearest(path)
            if not cfg.has_credentials():
                raise AuthError(
                    f"{cfg.source_path} has no credentials and bootstrap=False"
                )
        assert cfg.identity.agent_id and cfg.identity.api_key
        instance = cls(cfg.network.hub, cfg.identity.agent_id, cfg.identity.api_key)
        instance._profile_to_apply = {
            "description": cfg.profile.description,
            "status": "available",
            "capabilities": list(cfg.profile.capabilities),
        }
        return instance

    def register(self) -> str:
        """Register with the hub and get a JWT token."""
        resp = self._client.post(
            f"{self.hub_url}/agents/token",
            json={"agent_id": self.agent_id, "api_key": self.api_key},
        )
        if resp.status_code != 200:
            raise AuthError(f"Registration failed: {resp.text}")
        self._token = resp.json()["token"]
        logger.info(f"[{self.agent_id}] registered")
        if self._profile_to_apply:
            try:
                self._client.patch(
                    f"{self.hub_url}/agents/me",
                    headers={"Authorization": f"Bearer {self._token}"},
                    json=self._profile_to_apply,
                )
            except httpx.HTTPError as e:
                logger.warning(f"[{self.agent_id}] profile sync failed: {e}")
            finally:
                self._profile_to_apply = None
        return self._token

    def _headers(self) -> dict[str, str]:
        if not self._token:
            self.register()
        return {"Authorization": f"Bearer {self._token}"}

    def send(self, to_agent: str, payload: dict[str, Any]) -> dict:
        """Send a message to another agent via REST."""
        resp = self._client.post(
            f"{self.hub_url}/agents/messages",
            headers=self._headers(),
            json={"to": to_agent, "payload": payload},
        )
        resp.raise_for_status()
        return resp.json()

    def poll(self, since_id: int | None = None) -> list[dict]:
        """Poll for pending messages (consume mode). Returns list of message dicts.

        Messages are marked delivered and won't appear again.
        Delivery receipts are filtered out — use poll_full() to get them.

        Args:
            since_id: Unused in consume mode. Use poll_peek() for non-destructive reads.
        """
        resp = self._client.get(
            f"{self.hub_url}/agents/messages",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json().get("messages", [])

    def poll_peek(self, since_id: int = 0) -> tuple[list[dict], list[dict], int]:
        """Non-destructive poll. Returns (messages, receipts, next_since_id).

        Messages are NOT consumed. Use ack() to mark them done when processed.
        Safe to call after crashes — you won't lose messages.

        Typical usage:
            since_id = 0
            while True:
                msgs, receipts, since_id = agent.poll_peek(since_id)
                for msg in msgs:
                    handle(msg)
                if msgs:
                    agent.ack([msg["_id"] for msg in msgs if "_id" in msg])
                time.sleep(5)
        """
        params = {"since_id": since_id}  # always send, even 0 (triggers peek mode)
        resp = self._client.get(
            f"{self.hub_url}/agents/messages",
            headers=self._headers(),
            params=params,
        )
        resp.raise_for_status()
        data = resp.json()
        next_id = data.get("next_since_id") or since_id
        return data.get("messages", []), data.get("receipts", []), next_id

    def ack(self, message_ids: list[int]) -> dict:
        """Acknowledge (mark delivered) messages by id. Use with poll_peek()."""
        resp = self._client.post(
            f"{self.hub_url}/agents/messages/ack",
            headers=self._headers(),
            json={"ids": message_ids},
        )
        resp.raise_for_status()
        return resp.json()

    def poll_full(self) -> dict:
        """Poll and return the full response dict including receipts.

        Returns dict with keys: messages, receipts (may be absent if empty).
        """
        resp = self._client.get(
            f"{self.hub_url}/agents/messages",
            headers=self._headers(),
        )
        resp.raise_for_status()
        return resp.json()

    def message_history(self, peer_agent_id: str, limit: int = 50, before: int | None = None) -> list[dict]:
        """Fetch message history with a specific agent (read-only, non-consuming).

        Args:
            peer_agent_id: The other agent's id.
            limit: Max messages to return (default 50, max 200).
            before: Unix timestamp — return messages before this time (for pagination).
        """
        params: dict = {"limit": limit}
        if before:
            params["before"] = before
        resp = self._client.get(
            f"{self.hub_url}/agents/messages/{peer_agent_id}/history",
            headers=self._headers(),
            params=params,
        )
        resp.raise_for_status()
        return resp.json()

    def channel_send(self, channel_id: str, payload: dict) -> dict:
        """Broadcast a message to all members of a channel.

        All channel members (except sender) receive it with msg_type='channel_message'.
        """
        resp = self._client.post(
            f"{self.hub_url}/agents/channels/{channel_id}/send",
            headers=self._headers(),
            json={"payload": payload},
        )
        resp.raise_for_status()
        return resp.json()

    def list_online(self) -> list[dict]:
        """List all online agents."""
        resp = self._client.get(f"{self.hub_url}/agents/")
        resp.raise_for_status()
        return resp.json()

    def health(self) -> dict:
        """Check hub health."""
        resp = self._client.get(f"{self.hub_url}/platform/health")
        resp.raise_for_status()
        return resp.json()

    def send_and_wait(
        self,
        to_agent: str,
        payload: dict[str, Any],
        timeout_secs: int = 60,
        poll_interval: float = 2.0,
    ) -> dict | None:
        """Send a message and wait for a reply. Polls until a response arrives."""
        import time

        self.send(to_agent, payload)
        deadline = time.time() + timeout_secs

        while time.time() < deadline:
            messages = self.poll()
            for msg in messages:
                if msg.get("from") == to_agent:
                    return msg.get("payload", {})
            time.sleep(poll_interval)

        return None

    def update_profile(
        self,
        description: str | None = None,
        status: str | None = None,
        capabilities: list[str] | None = None,
        agent_card: dict | None = None,
    ) -> dict:
        """Update this agent's public profile (description, status, capabilities, agent_card)."""
        payload: dict[str, Any] = {}
        if description is not None:
            payload["description"] = description
        if status is not None:
            payload["status"] = status
        if capabilities is not None:
            payload["capabilities"] = capabilities
        if agent_card is not None:
            payload["agent_card"] = agent_card
        resp = self._client.patch(
            f"{self.hub_url}/agents/me",
            headers=self._headers(),
            json=payload,
        )
        resp.raise_for_status()
        return resp.json()

    def set_webhook(self, url: str) -> str:
        """Register a webhook URL for push delivery.

        Returns the webhook_secret to use when verifying incoming signatures.
        Store it securely — it is only returned once.
        """
        resp = self._client.patch(
            f"{self.hub_url}/agents/me",
            headers=self._headers(),
            json={"webhook": {"url": url}},
        )
        resp.raise_for_status()
        data = resp.json()
        secret = data.get("webhook_secret", "")
        if secret:
            logger.info(f"[{self.agent_id}] webhook registered: {url}")
        return secret

    def remove_webhook(self) -> None:
        """Remove the webhook — revert to poll-only delivery."""
        resp = self._client.patch(
            f"{self.hub_url}/agents/me",
            headers=self._headers(),
            json={"webhook": {"url": None}},
        )
        resp.raise_for_status()
        logger.info(f"[{self.agent_id}] webhook removed")

    def close(self):
        """Close the HTTP client."""
        self._client.close()

    def __enter__(self):
        self.register()
        return self

    def __exit__(self, *args):
        self.close()
