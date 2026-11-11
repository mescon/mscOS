"""Password + session cookies for the panel's system actions.

One shared household password, stored as an scrypt hash. Sessions are random tokens;
only their SHA-256 is kept on disk, so a copy of the state file cannot be replayed.

Reset (forgotten password), as root on the device:
    python3 /usr/lib/mscos/panel/backend/auth.py reset && systemctl restart mscos-panel
The panel then offers to create a new password.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
import time

SESSION_DAYS = 90
N, R, P = 2 ** 14, 8, 1  # scrypt: ~16 MB, a few hundred ms on the A20


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Auth:
    def __init__(self, path: str, on_write=None):
        self.path = path
        self.on_write = on_write  # e.g. persist() through overlayroot
        self.data: dict = {"password": None, "sessions": {}}
        self.failures: list[float] = []
        try:
            with open(path) as f:
                self.data.update(json.load(f))
        except (OSError, ValueError):
            pass

    # ------------------------------------------------------------- storage
    def _save(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(self.data, f)
        os.replace(tmp, self.path)
        if self.on_write:
            self.on_write(self.path)

    # ------------------------------------------------------------- password
    @property
    def configured(self) -> bool:
        return bool(self.data.get("password"))

    def set_password(self, pw: str):
        salt = secrets.token_bytes(16)
        h = hashlib.scrypt(pw.encode(), salt=salt, n=N, r=R, p=P, maxmem=64 * 2 ** 20)
        self.data["password"] = {"salt": salt.hex(), "hash": h.hex(), "n": N, "r": R, "p": P}
        self.data["sessions"] = {}  # a new password signs everyone out
        self._save()

    def verify(self, pw: str) -> bool:
        rec = self.data.get("password")
        if not rec:
            return False
        h = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(rec["salt"]), n=rec["n"], r=rec["r"],
                           p=rec["p"], maxmem=64 * 2 ** 20)
        return hmac.compare_digest(h.hex(), rec["hash"])

    def throttled(self) -> bool:
        """More than 10 wrong passwords in 10 minutes locks login for the rest of that window."""
        now = time.time()
        self.failures = [t for t in self.failures if now - t < 600]
        return len(self.failures) >= 10

    def failed(self):
        self.failures.append(time.time())

    # ------------------------------------------------------------- sessions
    def new_session(self) -> str:
        token = secrets.token_urlsafe(32)
        now = time.time()
        self.data["sessions"] = {k: v for k, v in self.data["sessions"].items() if v > now}
        self.data["sessions"][_sha(token)] = now + SESSION_DAYS * 86400
        self._save()
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        exp = self.data["sessions"].get(_sha(token))
        return bool(exp and exp > time.time())

    def revoke(self, token: str | None):
        if token and self.data["sessions"].pop(_sha(token), None):
            self._save()


if __name__ == "__main__" and sys.argv[1:] == ["reset"]:
    path = os.path.join(os.environ.get("MSCOS_STATE", "/var/lib/mscos"), "auth.json")
    a = Auth(path)
    a.data = {"password": None, "sessions": {}}
    a._save()
    print("Lösenordet är borttaget. Skapa ett nytt i panelen under System.")
