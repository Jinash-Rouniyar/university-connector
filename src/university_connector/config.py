from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787

# Hostname patterns for common tunnels, so MCP requests forwarded through them
# pass FastMCP's Host header guard without extra configuration.
TUNNEL_HOST_PATTERNS = (
    "*.trycloudflare.com",
    "*.ngrok-free.app",
    "*.ngrok-free.dev",
    "*.ngrok.app",
    "*.ngrok.io",
)


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    home: Path = field(
        default_factory=lambda: Path(
            os.environ.get("UC_HOME", Path.home() / ".university-connector")
        ).expanduser()
    )
    host: str = field(default_factory=lambda: os.environ.get("UC_HOST", DEFAULT_HOST))
    port: int = field(default_factory=lambda: int(os.environ.get("UC_PORT", DEFAULT_PORT)))
    public_url: str | None = field(default_factory=lambda: os.environ.get("UC_PUBLIC_URL"))
    extra_allowed_hosts: list[str] = field(
        default_factory=lambda: [
            h.strip() for h in os.environ.get("UC_ALLOWED_HOSTS", "").split(",") if h.strip()
        ]
    )
    keepalive_minutes: float = field(
        default_factory=lambda: float(os.environ.get("UC_KEEPALIVE_MINUTES", "10"))
    )
    login_timeout_seconds: int = field(
        default_factory=lambda: int(os.environ.get("UC_LOGIN_TIMEOUT_SECONDS", "300"))
    )
    refresh_timeout_seconds: int = field(
        default_factory=lambda: int(os.environ.get("UC_REFRESH_TIMEOUT_SECONDS", "45"))
    )
    # "chrome" uses the locally installed Google Chrome instead of bundled Chromium,
    # which some SSO/MFA providers trust more.
    browser_channel: str | None = field(
        default_factory=lambda: os.environ.get("UC_BROWSER_CHANNEL") or None
    )
    use_keyring: bool = field(default_factory=lambda: _env_bool("UC_USE_KEYRING", True))
    inline_download_limit_bytes: int = 5 * 1024 * 1024

    @property
    def base_url(self) -> str:
        """URL that links handed to agents and phones should use."""
        if self.public_url:
            return self.public_url.rstrip("/")
        return f"http://{self.host}:{self.port}"

    @property
    def mcp_url(self) -> str:
        return f"{self.base_url}/mcp"

    @property
    def allowed_hosts(self) -> list[str]:
        hosts = [*TUNNEL_HOST_PATTERNS, *self.extra_allowed_hosts]
        if self.public_url:
            public_host = urlsplit(self.public_url).hostname
            if public_host:
                hosts.append(public_host)
        return hosts

    @property
    def universities_home(self) -> Path:
        return self.home / "universities"

    @property
    def downloads_dir(self) -> Path:
        return self.home / "downloads"

    @property
    def submissions_log(self) -> Path:
        return self.home / "submissions.jsonl"

    def ensure_dirs(self) -> None:
        for path in (self.home, self.universities_home, self.downloads_dir):
            path.mkdir(parents=True, exist_ok=True)
        os.chmod(self.home, 0o700)
