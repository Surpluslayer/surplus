"""
routes/relationships.py : thin re-export shim.

The /api/relationships router (the durable contact spine) now lives in
routes/book.py alongside the /api/book surface — one module for the whole
relationship side. This shim keeps the old import path working (main.py's
include list, tests, and any external callers of
`backend.routes.relationships.<name>`): `router` is the real
relationships_router object, and every other attribute (route handlers,
request models, helpers) delegates to routes/book.py via PEP 562.
"""
from __future__ import annotations

from . import book as _book

router = _book.relationships_router


def __getattr__(name: str):
    return getattr(_book, name)
