from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path


class Access:
    def __init__(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / "access.json"
        if not self.path.exists():
            self.path.write_text(json.dumps({"code": secrets.token_urlsafe(18)}), encoding="utf-8")
        self.code = json.loads(self.path.read_text(encoding="utf-8"))["code"]
        self.key = secrets.token_bytes(32)
        self.sessions: dict[str, float] = {}
        self.failures: dict[str, list[float]] = {}

    def login(self, code: str, host: str) -> str | None:
        now = time.time()
        previous = [t for t in self.failures.get(host, []) if t > now - 60]
        self.failures[host] = previous
        if len(previous) >= 8:
            return None
        if not hmac.compare_digest(hashlib.sha256(code.encode()).digest(), hashlib.sha256(self.code.encode()).digest()):
            previous.append(now)
            return None
        self.failures.pop(host, None)
        token = secrets.token_urlsafe(32)
        self.sessions[token] = now + 86400
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        expiration = self.sessions.get(token, 0)
        if expiration <= time.time():
            self.sessions.pop(token, None)
            return False
        return True
