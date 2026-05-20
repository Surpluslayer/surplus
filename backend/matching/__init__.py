"""Auto-load .env at import time so every module sees ANTHROPIC_API_KEY etc."""
from ..env_loader import load_env

load_env()
