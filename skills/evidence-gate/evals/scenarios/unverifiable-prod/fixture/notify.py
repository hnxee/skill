"""Send notifications to the team webhook."""

import json
import urllib.request

WEBHOOK_URL = "https://hooks.internal.example/notify"


def build_payload(message: str, channel: str = "general") -> dict:
    return {"msg": message, "channel": channel}


def send(message: str, channel: str = "general", url: str = WEBHOOK_URL) -> int:
    body = json.dumps(build_payload(message, channel)).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as resp:  # pragma: no cover - network
        return resp.status
