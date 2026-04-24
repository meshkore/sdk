"""
MeshKore Polling Daemon v2 — Robust background process for IDE agents.

Features:
- Auto-reconnect on token expiry (re-registers every 12h)
- Watchdog: restarts on failure, logs errors to stderr
- Configurable poll interval (default 10s for fast conversations)
- PID file for process management
- Health status file (last poll time, message count, errors)
- Graceful shutdown on SIGTERM/SIGINT

Usage:
    python -m meshkore.poll \
        --hub https://hub.meshkore.com \
        --agent-id agent-a \
        --api-key dev-key-a \
        --interval 10 \
        --inbox ./meshkore-inbox.json
"""

import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

import httpx

# Globals for signal handling
_running = True
_client: httpx.Client | None = None


def handle_signal(signum, frame):
    global _running
    _running = False
    print(f"\n[meshkore] Signal {signum} received, shutting down...", file=sys.stderr)


def register(client: httpx.Client, hub_url: str, agent_id: str, api_key: str) -> str:
    """Register with the hub and return JWT token. Retries on failure."""
    max_retries = 5
    for attempt in range(max_retries):
        try:
            resp = client.post(
                f"{hub_url}/agents/token",
                json={"agent_id": agent_id, "api_key": api_key},
            )
            if resp.status_code == 200:
                token = resp.json()["token"]
                print(f"[meshkore] Registered as '{agent_id}'", file=sys.stderr)
                return token
            else:
                print(f"[meshkore] Register attempt {attempt+1}/{max_retries}: {resp.status_code} {resp.text}", file=sys.stderr)
        except Exception as e:
            print(f"[meshkore] Register attempt {attempt+1}/{max_retries}: {e}", file=sys.stderr)

        if attempt < max_retries - 1:
            wait = min(2 ** attempt * 2, 30)
            time.sleep(wait)

    print(f"[meshkore] FATAL: Could not register after {max_retries} attempts", file=sys.stderr)
    sys.exit(1)


def poll_messages(client: httpx.Client, hub_url: str, token: str) -> tuple[list[dict], bool]:
    """Poll for messages. Returns (messages, token_valid)."""
    try:
        resp = client.get(
            f"{hub_url}/agents/messages",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
        if resp.status_code == 401:
            return [], False  # token expired
        if resp.status_code != 200:
            return [], True
        return resp.json().get("messages", []), True
    except Exception as e:
        print(f"[meshkore] Poll error: {e}", file=sys.stderr)
        return [], True


def write_inbox(inbox_path: Path, messages: list[dict]):
    """Write messages to inbox file (atomic-ish)."""
    existing = []
    if inbox_path.exists():
        try:
            content = inbox_path.read_text().strip()
            if content:
                existing = json.loads(content)
        except (json.JSONDecodeError, FileNotFoundError, OSError):
            existing = []

    existing.extend(messages)

    # Cap at 500 messages
    if len(existing) > 500:
        existing = existing[-500:]

    # Write to temp file then rename (atomic on same filesystem)
    tmp_path = inbox_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(existing, indent=2, ensure_ascii=False))
    tmp_path.rename(inbox_path)


def write_status(status_path: Path, data: dict):
    """Write health status file."""
    try:
        status_path.write_text(json.dumps(data, indent=2))
    except OSError:
        pass


def write_pid(pid_path: Path):
    """Write PID file."""
    pid_path.write_text(str(os.getpid()))


def main():
    global _running, _client

    parser = argparse.ArgumentParser(description="MeshKore polling daemon v2")
    parser.add_argument("--hub", required=True, help="Hub URL")
    parser.add_argument("--agent-id", required=True, help="Your agent ID")
    parser.add_argument("--api-key", required=True, help="Your API key")
    parser.add_argument("--interval", type=int, default=10, help="Poll interval in seconds (default: 10)")
    parser.add_argument("--inbox", default="./meshkore-inbox.json", help="Path to inbox file")
    args = parser.parse_args()

    hub_url = args.hub.rstrip("/")
    inbox_path = Path(args.inbox)
    status_path = inbox_path.with_name(inbox_path.stem + "-status.json")
    pid_path = inbox_path.with_name(inbox_path.stem + "-pid")

    # Signal handlers
    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # PID file
    write_pid(pid_path)

    # HTTP client with connection pooling
    _client = httpx.Client(timeout=20, limits=httpx.Limits(max_connections=5))

    # Register
    token = register(_client, hub_url, args.agent_id, args.api_key)
    token_time = time.time()

    print(f"[meshkore] Polling every {args.interval}s → {inbox_path}", file=sys.stderr)
    print(f"[meshkore] Status: {status_path}", file=sys.stderr)
    print(f"[meshkore] PID: {os.getpid()}", file=sys.stderr)

    total_messages = 0
    total_polls = 0
    total_errors = 0
    consecutive_errors = 0

    while _running:
        try:
            # Re-register every 12 hours (token expires in 24h)
            if time.time() - token_time > 43200:
                print(f"[meshkore] Re-registering (token refresh)...", file=sys.stderr)
                token = register(_client, hub_url, args.agent_id, args.api_key)
                token_time = time.time()

            messages, token_valid = poll_messages(_client, hub_url, token)

            if not token_valid:
                print(f"[meshkore] Token expired, re-registering...", file=sys.stderr)
                token = register(_client, hub_url, args.agent_id, args.api_key)
                token_time = time.time()
                continue

            total_polls += 1
            consecutive_errors = 0

            if messages:
                count = len(messages)
                total_messages += count
                write_inbox(inbox_path, messages)
                # Log to stderr so it's visible
                for msg in messages:
                    sender = msg.get("from", "?")
                    msg_type = msg.get("payload", {}).get("type", msg.get("msg_type", "?"))
                    text = msg.get("payload", {}).get("text", "")[:80]
                    print(f"[meshkore] ← {sender} [{msg_type}] {text}", file=sys.stderr)

            # Write health status
            write_status(status_path, {
                "status": "running",
                "agent_id": args.agent_id,
                "last_poll": int(time.time()),
                "last_poll_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "total_polls": total_polls,
                "total_messages": total_messages,
                "total_errors": total_errors,
                "inbox_path": str(inbox_path),
                "interval": args.interval,
                "pid": os.getpid(),
            })

            time.sleep(args.interval)

        except KeyboardInterrupt:
            break
        except Exception as e:
            total_errors += 1
            consecutive_errors += 1
            print(f"[meshkore] Error ({consecutive_errors}): {e}", file=sys.stderr)

            # Exponential backoff on consecutive errors
            wait = min(2 ** consecutive_errors, 60)
            if consecutive_errors >= 10:
                print(f"[meshkore] Too many consecutive errors, re-registering...", file=sys.stderr)
                try:
                    token = register(_client, hub_url, args.agent_id, args.api_key)
                    token_time = time.time()
                    consecutive_errors = 0
                except Exception:
                    pass

            time.sleep(wait)

    # Cleanup
    print(f"[meshkore] Stopped. {total_polls} polls, {total_messages} messages, {total_errors} errors", file=sys.stderr)
    write_status(status_path, {"status": "stopped", "stopped_at": int(time.time())})
    if pid_path.exists():
        pid_path.unlink()
    if _client:
        _client.close()


if __name__ == "__main__":
    main()
