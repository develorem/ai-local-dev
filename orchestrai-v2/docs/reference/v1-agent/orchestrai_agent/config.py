"""Agent runtime configuration."""

import os
import socket


class Config:
    HUB_URL: str = os.environ.get("ORCHESTRAI_HUB_URL", "http://hub:6724").rstrip("/")
    # Operator token, used only to authenticate this worker's registration call
    # (thereafter it uses its per-agent lease token). Must match the hub's.
    OPERATOR_TOKEN: str = os.environ.get("ORCHESTRAI_OPERATOR_TOKEN", "").strip()
    OLLAMA_URL: str = os.environ.get("OLLAMA_URL", "http://ollama:11434").rstrip("/")
    AGENT_NAME: str = os.environ.get("AGENT_NAME", f"agent@{socket.gethostname()}")
    AGENT_HOST: str = os.environ.get("AGENT_HOST", socket.gethostname())
    AGENT_VERSION: str = os.environ.get("AGENT_VERSION", "0.1.0")
    CAPABILITIES: list[str] = [
        c.strip() for c in
        os.environ.get("AGENT_CAPABILITIES",
                       "python,node,git,linux").split(",")
        if c.strip()
    ]
    # Ports the host has mapped into this container. The agent advertises
    # these to the Hub so the UI can render clickable links and the LLM
    # knows which ports it may bind demo / feedback servers to.
    # Bind in-container to 0.0.0.0:<port>; the host port is identity-mapped.
    HTTP_PORTS: list[int] = [
        int(p.strip()) for p in
        os.environ.get("AGENT_HTTP_PORTS", "").split(",")
        if p.strip().isdigit()
    ]
    HTTP_BIND_HOST: str = os.environ.get("AGENT_HTTP_BIND_HOST", "0.0.0.0")
    POLL_IDLE_SEC: float = float(os.environ.get("AGENT_POLL_IDLE_SEC", "5"))
    HEARTBEAT_DEFAULT_SEC: int = int(os.environ.get("AGENT_HEARTBEAT_SEC", "10"))
    LLM_TIMEOUT_SEC: int = int(os.environ.get("LLM_TIMEOUT_SEC", "300"))
    DEFAULT_MODEL: str = os.environ.get("MODEL_PRIMARY", "qwen2.5-coder:14b")
    DEFAULT_NUM_CTX: int = int(os.environ.get("INFERENCE_NUM_CTX", "16384"))


config = Config()
