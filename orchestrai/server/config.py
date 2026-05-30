"""Runtime configuration loaded from env vars with sensible defaults."""

import os
from pathlib import Path


class Config:
    DATA_DIR: Path = Path(os.environ.get("DATA_DIR", "./data"))
    DB_PATH: Path = DATA_DIR / "orchestrai.db"
    OLLAMA_URL: str = os.environ.get("OLLAMA_URL", "http://localhost:11434")
    HUB_HOST: str = os.environ.get("HUB_HOST", "0.0.0.0")
    HUB_PORT: int = int(os.environ.get("HUB_PORT", "6724"))
    # Operator (admin) token: the human credential for the UI + all admin REST.
    # When set, auth is enforced on every route except health/webhooks/UI assets.
    # When empty, auth is DISABLED (localhost-dev convenience) — set this before
    # exposing the hub to any untrusted network.
    OPERATOR_TOKEN: str = os.environ.get("ORCHESTRAI_OPERATOR_TOKEN", "").strip()
    MASTER_KEY_PATH: str = os.environ.get("MASTER_KEY_PATH", "")
    REAPER_INTERVAL_SEC: int = int(os.environ.get("REAPER_INTERVAL_SEC", "15"))
    AGENT_LEASE_TIMEOUT_SEC: int = int(os.environ.get("AGENT_LEASE_TIMEOUT_SEC", "30"))
    # How long a 'lost'/'released' agent row is kept before the reaper prunes it.
    # Every agent boot registers a fresh row, so without pruning they pile up.
    AGENT_RETENTION_SEC: int = int(os.environ.get("AGENT_RETENTION_SEC", "600"))
    AGENT_HEARTBEAT_INTERVAL_SEC: int = int(os.environ.get("AGENT_HEARTBEAT_INTERVAL_SEC", "10"))
    VERSION: str = "0.1.0"

    @classmethod
    def ensure_dirs(cls) -> None:
        cls.DATA_DIR.mkdir(parents=True, exist_ok=True)


config = Config()
