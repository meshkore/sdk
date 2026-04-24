import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

from .exceptions import AgentOfflineError, AuthError
from .models import RelayMessage

logger = logging.getLogger("meshkore")

MessageHandler = Callable[[str, dict[str, Any]], Awaitable[None]]


class MeshKoreAgent:
    """Client SDK for connecting an AI agent to the MeshKore relay hub."""

    def __init__(self, hub_url: str, agent_id: str, api_key: str):
        """
        Args:
            hub_url: Base URL of the relay hub, e.g. "https://hub.meshkore.com"
                     or "http://localhost:8080" for local testing.
            agent_id: Unique identifier for this agent.
            api_key: API key that maps to this agent_id on the hub.
        """
        self.hub_url = hub_url.rstrip("/")
        self.agent_id = agent_id
        self.api_key = api_key

        self._token: str | None = None
        self._ws: ClientConnection | None = None
        self._handlers: list[MessageHandler] = []
        self._rooms: dict[str, str] = {}  # peer_agent_id -> room_id
        self._pending_rooms: dict[str, asyncio.Future[str]] = {}  # peer -> future<room_id>
        self._receive_task: asyncio.Task | None = None
        self._connected = False
        self._profile_to_apply: dict[str, Any] | None = None

    @classmethod
    def from_config(
        cls,
        path: str | Path | None = None,
        *,
        bootstrap: bool = True,
    ) -> "MeshKoreAgent":
        """Build an agent from the nearest `.meshkore` file (walking upward
        from `path` or cwd). If the file is a public-template without
        credentials, bootstrap=True will POST to the invite and rewrite
        the file in place with fresh credentials."""
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
        assert cfg.identity.agent_id and cfg.identity.api_key  # for type checker
        instance = cls(cfg.network.hub, cfg.identity.agent_id, cfg.identity.api_key)
        # Remember the profile so `connect()` can PATCH it to the hub.
        instance._profile_to_apply = {
            "description": cfg.profile.description,
            "status": "available",
            "capabilities": list(cfg.profile.capabilities),
        }
        return instance

    @property
    def http_url(self) -> str:
        return self.hub_url.replace("ws://", "http://").replace("wss://", "https://")

    @property
    def ws_url(self) -> str:
        return self.hub_url.replace("http://", "ws://").replace("https://", "wss://")

    async def connect(self) -> None:
        """Register with the hub and open a WebSocket connection."""
        # Step 1: Register via REST to get JWT
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"{self.http_url}/agents/token",
                json={"agent_id": self.agent_id, "api_key": self.api_key},
            )
            if resp.status_code != 200:
                raise AuthError(f"Registration failed: {resp.text}")
            self._token = resp.json()["token"]

        # Step 2: Open WebSocket
        self._ws = await websockets.connect(f"{self.ws_url}/ws?token={self._token}")
        self._connected = True

        # Step 3: Start receive loop
        self._receive_task = asyncio.create_task(self._receive_loop())
        logger.info(f"[{self.agent_id}] connected to hub")

        # Step 4: Apply profile from config, if any. Best-effort: we don't
        # fail the connection on a profile PATCH error.
        if self._profile_to_apply:
            try:
                async with httpx.AsyncClient() as client:
                    await client.patch(
                        f"{self.http_url}/agents/me",
                        headers={"Authorization": f"Bearer {self._token}"},
                        json=self._profile_to_apply,
                    )
            except httpx.HTTPError as e:
                logger.warning(f"[{self.agent_id}] profile sync failed: {e}")
            finally:
                self._profile_to_apply = None

    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        self._connected = False
        if self._receive_task:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
        logger.info(f"[{self.agent_id}] disconnected")

    def on_message(self, handler: MessageHandler) -> None:
        """Register an async callback for incoming messages.

        The handler receives (from_agent_id: str, payload: dict).
        """
        self._handlers.append(handler)

    async def send(self, to_agent: str, payload: dict[str, Any]) -> None:
        """Send a message to another agent. Auto-creates a room if needed."""
        room_id = await self._ensure_room(to_agent)

        msg = RelayMessage(
            msg_type="send",
            to_agent=to_agent,
            room_id=room_id,
            payload=payload,
        )
        await self._ws_send(msg.to_dict())

    async def list_online(self) -> list[str]:
        """Return list of online agent IDs."""
        async with httpx.AsyncClient() as client:
            headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
            resp = await client.get(f"{self.http_url}/agents/", headers=headers)
            resp.raise_for_status()
            return [a["agent_id"] for a in resp.json()]

    # --- Internal ---

    async def _ensure_room(self, peer: str) -> str:
        """Get or create a room with a peer agent."""
        if peer in self._rooms:
            return self._rooms[peer]

        # Request room creation
        loop = asyncio.get_event_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._pending_rooms[peer] = future

        msg = RelayMessage(msg_type="connect_to", to_agent=peer)
        await self._ws_send(msg.to_dict())

        try:
            room_id = await asyncio.wait_for(future, timeout=10.0)
        except asyncio.TimeoutError:
            self._pending_rooms.pop(peer, None)
            raise AgentOfflineError(f"Timeout waiting for room with '{peer}'")

        return room_id

    async def _ws_send(self, data: dict) -> None:
        if not self._ws:
            raise ConnectionError("not connected")
        await self._ws.send(json.dumps(data))

    async def _receive_loop(self) -> None:
        """Read messages from WebSocket and dispatch."""
        try:
            async for raw in self._ws:
                try:
                    data = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                msg = RelayMessage.from_dict(data)
                await self._dispatch(msg)
        except websockets.ConnectionClosed:
            logger.info(f"[{self.agent_id}] WebSocket closed")
        except asyncio.CancelledError:
            pass

    async def _dispatch(self, msg: RelayMessage) -> None:
        if msg.msg_type == "room_created":
            room_id = msg.room_id
            # Determine the peer (the other agent in the room)
            peer = msg.to_agent if msg.from_agent == self.agent_id else msg.from_agent
            if room_id and peer:
                self._rooms[peer] = room_id
                # Resolve pending room future if exists
                if peer in self._pending_rooms:
                    fut = self._pending_rooms.pop(peer)
                    if not fut.done():
                        fut.set_result(room_id)

        elif msg.msg_type == "message":
            from_agent = msg.from_agent or "unknown"
            payload = msg.payload or {}
            for handler in self._handlers:
                try:
                    await handler(from_agent, payload)
                except Exception as e:
                    logger.error(f"Handler error: {e}")

        elif msg.msg_type == "presence":
            if msg.payload:
                agent_id = msg.payload.get("agent_id", "")
                status = msg.payload.get("status", "")
                logger.debug(f"Presence: {agent_id} is {status}")

        elif msg.msg_type == "error":
            detail = msg.payload.get("detail", "unknown") if msg.payload else "unknown"
            logger.warning(f"Hub error: {detail}")
