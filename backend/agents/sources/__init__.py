"""Prospect source adapters. ALL_ADAPTERS is the registry the prospector fans out."""
from .base import SourceAdapter
from .github import GitHubAdapter
from .x import XAdapter
from .linkedin import LinkedInAdapter
from .scholar import ScholarAdapter

ALL_ADAPTERS: list[SourceAdapter] = [
    GitHubAdapter(),
    XAdapter(),
    LinkedInAdapter(),
    ScholarAdapter(),
]

# LinkedIn is mandatory: it's the only source that resolves a real contact
# (linkedin_url + provider_id). Without it the rest of the pipeline can't send
# anything. The frontend renders the chip as locked-on; the backend enforces it
# in adapters_for() in case a caller bypasses the UI.
MANDATORY_SOURCE_KEY = "linkedin"


def adapters_for(keys) -> list[SourceAdapter]:
    """Filter ALL_ADAPTERS by adapter key, always including LinkedIn.

    Accepts a list/tuple of keys, a CSV string (storage shape), or empty
    (returns the LinkedIn-only fallback). Case-insensitive, dedupes, and
    floats LinkedIn to position 0 so the async fan-out gives it first dibs
    when an upstream rate-limits.
    """
    if not keys:
        wanted: set[str] = set()
    elif isinstance(keys, (list, tuple, set)):
        wanted = {str(k).strip().lower() for k in keys if str(k).strip()}
    else:
        wanted = {k.strip().lower() for k in str(keys).split(",") if k.strip()}
    wanted.add(MANDATORY_SOURCE_KEY)

    out: list[SourceAdapter] = []
    for a in ALL_ADAPTERS:
        if a.key == MANDATORY_SOURCE_KEY and a.key in wanted:
            out.insert(0, a)
        elif a.key in wanted:
            out.append(a)
    return out


__all__ = [
    "SourceAdapter",
    "GitHubAdapter",
    "XAdapter",
    "LinkedInAdapter",
    "ScholarAdapter",
    "ALL_ADAPTERS",
    "MANDATORY_SOURCE_KEY",
    "adapters_for",
]
