"""Auto-load .env at import time so every module sees ANTHROPIC_API_KEY etc.

Empty-or-missing process env values are filled from .env. Non-empty process
env values are preserved (so CI / cron jobs can still override).
"""
import os
from pathlib import Path

_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            _k = _k.strip()
            _v = _v.strip().strip('"').strip("'")
            # Only fill if the process env value is unset OR empty
            if not os.environ.get(_k):
                os.environ[_k] = _v
