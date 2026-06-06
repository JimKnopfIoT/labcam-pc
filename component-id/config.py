"""Configuration: minimal .env loader (stdlib) + getters.

The API key (ANTHROPIC_API_KEY) is the runtime credential for the service.
.env is gitignored -- see .env.example for a template.
"""

from __future__ import annotations

import os

_ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")


def load_env(path: str = _ENV_PATH) -> None:
    """Reads KEY=VALUE lines from .env into the environment (without overwriting existing values)."""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip().strip('"').strip("'"))


load_env()


def get_api_key() -> str | None:
    return os.environ.get("ANTHROPIC_API_KEY") or None


def get_model() -> str:
    # Vision-capable default model; overridable via .env (COMPONENT_ID_MODEL).
    return os.environ.get("COMPONENT_ID_MODEL", "claude-sonnet-4-6")
