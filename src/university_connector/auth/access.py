"""Access key guarding the connector's HTTP surface (portal, MCP endpoint, file links).

Once the server is tunneled to the internet, anyone with the URL could otherwise read
coursework or submit assignments, so every request must carry this key.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from university_connector.config import Settings


class AccessKey:
    def __init__(self, settings: Settings):
        self._path = settings.home / "access-key"
        settings.ensure_dirs()
        if self._path.exists():
            self.value = self._path.read_text().strip()
        else:
            self.value = secrets.token_urlsafe(32)
            self._path.write_text(self.value)
            os.chmod(self._path, 0o600)

    def verify(self, candidate: str | None) -> bool:
        return bool(candidate) and hmac.compare_digest(candidate, self.value)

    def sign(self, message: str) -> str:
        return hmac.new(self.value.encode(), message.encode(), hashlib.sha256).hexdigest()

    def verify_signature(self, message: str, signature: str | None) -> bool:
        return bool(signature) and hmac.compare_digest(self.sign(message), signature)

    def rotate(self) -> str:
        self.value = secrets.token_urlsafe(32)
        self._path.write_text(self.value)
        os.chmod(self._path, 0o600)
        return self.value
