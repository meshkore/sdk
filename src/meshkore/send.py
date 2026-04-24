"""
MeshKore Send — CLI tool to send a message to another agent via REST.

Usage:
    python -m meshkore.send \
        --hub http://localhost:8080 \
        --agent-id agent-a \
        --api-key dev-key-a \
        --to agent-b \
        --message '{"type": "greeting", "text": "hello from agent-a"}'
"""

import argparse
import json
import sys

import httpx


def main():
    parser = argparse.ArgumentParser(description="Send a message to another MeshKore agent")
    parser.add_argument("--hub", required=True, help="Hub URL")
    parser.add_argument("--agent-id", required=True, help="Your agent ID")
    parser.add_argument("--api-key", required=True, help="Your API key")
    parser.add_argument("--to", required=True, help="Target agent ID")
    parser.add_argument("--message", required=True, help="JSON payload to send")
    args = parser.parse_args()

    hub_url = args.hub.rstrip("/")

    # Register to get token
    resp = httpx.post(
        f"{hub_url}/agents/token",
        json={"agent_id": args.agent_id, "api_key": args.api_key},
    )
    if resp.status_code != 200:
        print(f"Registration failed: {resp.text}", file=sys.stderr)
        sys.exit(1)
    token = resp.json()["token"]

    # Parse payload
    try:
        payload = json.loads(args.message)
    except json.JSONDecodeError as e:
        print(f"Invalid JSON payload: {e}", file=sys.stderr)
        sys.exit(1)

    # Send message
    resp = httpx.post(
        f"{hub_url}/agents/messages",
        headers={"Authorization": f"Bearer {token}"},
        json={"to": args.to, "payload": payload},
    )
    if resp.status_code != 200:
        print(f"Send failed: {resp.text}", file=sys.stderr)
        sys.exit(1)

    result = resp.json()
    print(f"Sent to {args.to} (room: {result['room_id']})")


if __name__ == "__main__":
    main()
