from dataclasses import dataclass, field
from typing import Any


@dataclass
class RelayMessage:
    msg_type: str
    ts: float = 0
    from_agent: str | None = None
    to_agent: str | None = None
    room_id: str | None = None
    payload: dict[str, Any] | None = None

    def to_dict(self) -> dict:
        import time
        d: dict[str, Any] = {"msg_type": self.msg_type, "ts": int(time.time())}
        if self.from_agent is not None:
            d["from"] = self.from_agent
        if self.to_agent is not None:
            d["to"] = self.to_agent
        if self.room_id is not None:
            d["room_id"] = self.room_id
        if self.payload is not None:
            d["payload"] = self.payload
        return d

    @classmethod
    def from_dict(cls, data: dict) -> "RelayMessage":
        return cls(
            msg_type=data.get("msg_type", ""),
            ts=data.get("ts", 0),
            from_agent=data.get("from"),
            to_agent=data.get("to"),
            room_id=data.get("room_id"),
            payload=data.get("payload"),
        )
