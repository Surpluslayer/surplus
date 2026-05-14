"""Prospect source adapters. ALL_ADAPTERS is the registry the prospector fans out."""
from .base import SourceAdapter
from .github import GitHubAdapter
from .x import XAdapter
from .linkedin import LinkedInAdapter

ALL_ADAPTERS: list[SourceAdapter] = [GitHubAdapter(), XAdapter(), LinkedInAdapter()]

__all__ = ["SourceAdapter", "GitHubAdapter", "XAdapter", "LinkedInAdapter", "ALL_ADAPTERS"]
