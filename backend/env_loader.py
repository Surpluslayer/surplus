"""Load .env from repo root and/or backend/ before other modules read os.environ."""
from __future__ import annotations

import os
from pathlib import Path

_LOADED = False


def load_env() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    backend_dir = Path(__file__).resolve().parent
    repo_root = backend_dir.parent
    for env_file in (repo_root / ".env", backend_dir / ".env"):
        if not env_file.is_file():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and not os.environ.get(key):
                os.environ[key] = value
