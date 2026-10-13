"""Stub for the one-shot keyless-ring rescue.

The ring it rescued through (exa / parallel / firecrawl / keenable anonymous public free tiers) was
cut together with its vendors — see docs/DECISIONS.md: anything needing a third-party account or key
goes, local and self-hosted stays. What remains are the seams the dispatcher imports, kept so
``tools/web_tools.py`` calls them unconditionally without branching on whether a rescue exists.

impulse: keyless rescue removed — restore this module and its providers to bring the ring back.
"""

import logging

logger = logging.getLogger("tools.web_tools")


def _managed_search_fallback(provider, original_error: str, query: str, limit: int):
    """No managed vendor to fall back to: None leaves the original error standing."""
    return None


def _rescue_eligible(provider) -> bool:
    """Never: there is no ring left to rescue through."""
    return False


def _rescue_search(provider_name: str, original_error: str, query: str, limit: int) -> dict:
    """Unreachable while ``_rescue_eligible`` is False; kept for the dispatcher's call shape."""
    return {"success": False, "error": original_error or "search failed"}


def _policy_blocked_result(result: dict) -> bool:
    """True for a website-policy refusal — intentional, never rescued."""
    error = str(result.get("error") or "").lower()
    return bool(result.get("blocked_by_policy")) or "blocked by website policy" in error


def _rescue_extract(provider_name: str, urls: list, results: list) -> list:
    """Unreachable while the ring is gone; kept for the dispatcher's call shape."""
    return results
