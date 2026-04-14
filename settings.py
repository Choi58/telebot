from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path


def load_env_file(path: str | Path = ".env", *, override: bool = False) -> None:
    """Minimal .env loader (no external dependency).

    Supports lines like: KEY=value
    Ignores empty lines and comments (# ...).
    """
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if not key:
            continue
        if override or key not in os.environ:
            os.environ[key] = value


def _getenv_str(name: str, default: str = "") -> str:
    return os.getenv(name, default)


def _getenv_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


@dataclass(frozen=True)
class AppSettings:
    telegram_bot_token: str
    lm_studio_base_url: str
    lm_studio_model: str
    lm_studio_embedding_model: str
    pdf_dir: str


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    load_env_file(".env")
    return AppSettings(
        telegram_bot_token=_getenv_str("TELEGRAM_BOT_TOKEN", ""),
        lm_studio_base_url=_getenv_str("LM_STUDIO_BASE_URL", "http://host.docker.internal:1234/v1"),
        lm_studio_model=_getenv_str("LM_STUDIO_MODEL", "google/gemma-3-4b"),
        lm_studio_embedding_model=_getenv_str("LM_STUDIO_EMBEDDING_MODEL", "nomic-embed-text-v1.5"),
        pdf_dir=_getenv_str("PDF_DIR", "./Papers"),
    )
