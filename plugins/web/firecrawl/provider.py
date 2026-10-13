"""Firecrawl web search + extract provider — **self-hosted instances only**.

Config: ``web.backend`` / ``web.search_backend`` / ``web.extract_backend: firecrawl``.
Env: ``FIRECRAWL_API_URL`` (your own Firecrawl, e.g. ``http://localhost:3002``).

The cloud routes (paid ``FIRECRAWL_API_KEY``, the anonymous keyless ring, and the vendor-managed
Tool Gateway) were cut in the znth scope — see docs/DECISIONS.md: anything needing a third-party
account or key goes, local and self-hosted stays.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Dict, List, Optional

from plugins.web._common import BaseWebSearchProvider, search_fail, search_ok, setup_schema
from tools.url_safety import is_safe_url
# Module-level (cheap import) so tests can monkeypatch the policy gate on this module.
from tools.website_policy import check_website_access

logger = logging.getLogger(__name__)

# The SDK costs ~200ms of imports on a cold CLI; defer to first use (tests patch ``Firecrawl`` here).
_FIRECRAWL_CLS_CACHE: Optional[type] = None


def _load_firecrawl_cls() -> type:
    """Import and cache ``firecrawl.Firecrawl`` (lazy_deps install hint → ImportError)."""
    global _FIRECRAWL_CLS_CACHE
    if _FIRECRAWL_CLS_CACHE is None:
        try:
            from pm import ensure_import as _lazy_ensure

            _lazy_ensure("firecrawl")
        except ImportError:
            pass
        except Exception as exc:
            raise ImportError(str(exc))
        from firecrawl import Firecrawl as _cls  # noqa: WPS433 — deliberately lazy
        _FIRECRAWL_CLS_CACHE = _cls
    return _FIRECRAWL_CLS_CACHE


class _FirecrawlProxy:
    """Callable proxy that looks like ``firecrawl.Firecrawl`` but imports lazily."""

    __slots__ = ()

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return _load_firecrawl_cls()(*args, **kwargs)

    def __instancecheck__(self, obj: Any) -> bool:
        return isinstance(obj, _load_firecrawl_cls())

    def __repr__(self) -> str:
        return "<lazy firecrawl.Firecrawl proxy>"


Firecrawl = _FirecrawlProxy()

# --- Client construction (self-hosted only) -----------------------------------
def _env(name: str) -> str:
    from hermes_cli.config import get_env_value
    return (get_env_value(name) or "").strip()


def _get_direct_firecrawl_config() -> Optional[tuple]:
    """Self-hosted Firecrawl ``(mode, kwargs, cache_key)`` or None.

    Only ``FIRECRAWL_API_URL`` counts: the paid cloud endpoint needs a third-party key and was cut.
    """
    api_url = _env("FIRECRAWL_API_URL").rstrip("/")
    if api_url:
        return "sdk", {"api_url": api_url}, ("direct", api_url, None)
    return None


def _use_keyless_ring(capability: Optional[str] = None) -> bool:
    """Always False: the anonymous keyless cloud ring was cut (docs/DECISIONS.md).
    impulse: keyless ring removed — restore this predicate with its ring if it ever comes back."""
    return False


class _KeylessFirecrawlClient:
    """Removed with the keyless cloud mode; kept as a name so nothing imports a missing symbol.
    impulse: keyless cloud mode removed — restore the REST client if it ever comes back."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover - never constructed
        raise RuntimeError("keyless Firecrawl cloud mode was cut in the znth scope")


def _is_tool_gateway_ready() -> bool:
    """Always False: the vendor-managed Tool Gateway route was cut with the cloud paths.
    impulse: managed gateway removed — restore the probe if a hosted route ever comes back."""
    return False


def check_firecrawl_api_key() -> bool:
    """True when a self-hosted Firecrawl instance is configured (``FIRECRAWL_API_URL``)."""
    return _get_direct_firecrawl_config() is not None


def is_managed_route(capability: Optional[str] = None) -> bool:
    """Always False: Firecrawl here is always your own instance, never a hosted route.
    impulse: managed route removed — restore the gateway decision if one ever comes back."""
    return False


_CLIENT: Any = None
_CLIENT_CONFIG: Any = None


def _get_firecrawl_client(capability: Optional[str] = None) -> Any:
    """Cached Firecrawl SDK client for the configured self-hosted instance; raises ``ValueError`` with the
    setup hint when ``FIRECRAWL_API_URL`` is missing.

    The per-capability route disambiguation the old version did is gone with the managed and keyless
    routes: the only route left is your own instance."""
    global _CLIENT, _CLIENT_CONFIG
    resolved = _get_direct_firecrawl_config()
    if resolved is None:
        logger.error("Firecrawl client initialization failed: FIRECRAWL_API_URL is not set")
        raise ValueError(
            "Web tools are not configured. Set FIRECRAWL_API_URL to your self-hosted Firecrawl instance "
            "(the cloud API-key route and the managed gateway were cut in the znth scope)."
        )
    _mode, kwargs, client_config = resolved
    if _CLIENT is not None and _CLIENT_CONFIG == client_config:
        return _CLIENT
    _CLIENT = Firecrawl(**kwargs)
    _CLIENT_CONFIG = client_config
    return _CLIENT


# --- Response shape normalization (SDK / direct / gateway differ) --------------
def _to_plain_object(value: Any) -> Any:
    """SDK objects (pydantic ``model_dump`` / ``__dict__``) → plain data when possible."""
    if value is None or isinstance(value, (dict, list, str, int, float, bool)):
        return value
    for attr, convert in (("model_dump", lambda v: v.model_dump()), ("__dict__", lambda v: {k: x for k, x in v.__dict__.items() if not k.startswith("_")})):
        if hasattr(value, attr):
            try:
                return convert(value)
            except Exception:
                pass
    return value


def _normalize_result_list(values: Any) -> list[dict[str, Any]]:
    return [p for p in map(_to_plain_object, values) if isinstance(p, dict)] if isinstance(values, list) else []


def _extract_web_search_results(response: Any) -> list[dict[str, Any]]:
    """Search results across SDK/direct/gateway response shapes."""
    plain = _to_plain_object(response)
    if isinstance(plain, dict):
        data = plain.get("data")
        if isinstance(data, list):
            return _normalize_result_list(data)
        candidates = [data.get("web"), data.get("results")] if isinstance(data, dict) else []
        for candidate in candidates + [plain.get("web"), plain.get("results")]:
            normalized = _normalize_result_list(candidate)
            if normalized:
                return normalized
    if hasattr(response, "web"):
        return _normalize_result_list(getattr(response, "web", []))
    return []


def _extract_scrape_payload(scrape_result: Any) -> dict[str, Any]:
    plain = _to_plain_object(scrape_result)
    if not isinstance(plain, dict):
        return {}
    return plain["data"] if isinstance(plain.get("data"), dict) else plain


def _error_entry(url: str, error: str, *, title: str = "", raw: bool = False, blocked: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Per-URL extract failure. ``raw`` adds ``raw_content`` (post-scrape failures carry
    it, pre-scrape ones don't); ``blocked`` adds ``blocked_by_policy``."""
    policy = {"blocked_by_policy": {k: blocked[k] for k in ("host", "rule", "source")}} if blocked else {}
    return {"url": url, "title": title, "content": "", **({"raw_content": ""} if raw else {}), "error": error, **policy}


_SCRAPE_TIMEOUT_MSG = "Scrape timed out after 60s — page may be too large or unresponsive. Try browser_navigate instead."
_UNSAFE_REDIRECT_MSG = "Blocked: URL targets a private or internal network address"


async def _scrape_one(url: str, formats: list[str], format: Optional[str]) -> dict[str, Any]:
    """Scrape one URL (60s timeout) and re-check SSRF + website policy against the
    post-redirect URL. Never raises for scrape errors; returns an error entry instead."""
    if blocked := check_website_access(url):
        logger.info("Blocked web_extract for %s by rule %s", blocked["host"], blocked["rule"])
        return _error_entry(url, blocked["message"], blocked=blocked)
    try:
        logger.info("Firecrawl scraping: %s", url)
        try:
            # Pass timeout (ms) to Firecrawl so the server-side deadline
            # matches our asyncio deadline. Without this the API uses its
            # 30 s default, causing SCRAPE_TIMEOUT before our 60 s
            # client-side wait expires. See #43272.
            scrape_result = await asyncio.wait_for(
                asyncio.to_thread(
                    _get_firecrawl_client("extract").scrape,
                    url=url,
                    formats=formats,
                    timeout=60_000,
                ),
                timeout=60,
            )
        except TimeoutError:
            logger.warning("Firecrawl scrape timed out for %s", url)
            return _error_entry(url, _SCRAPE_TIMEOUT_MSG)
        payload = _extract_scrape_payload(scrape_result)
        metadata = payload.get("metadata", {})
        # SDK may return a typed object for metadata (raw __dict__ here, unlike _to_plain_object).
        if not isinstance(metadata, dict):
            metadata = metadata.model_dump() if hasattr(metadata, "model_dump") else getattr(metadata, "__dict__", {})
        title, final_url = metadata.get("title", ""), metadata.get("sourceURL", url)
        if not is_safe_url(final_url):
            logger.info("Blocked redirected web_extract for unsafe final URL: %s", final_url)
            return _error_entry(final_url, _UNSAFE_REDIRECT_MSG, title=title, raw=True)
        if final_blocked := check_website_access(final_url):
            logger.info("Blocked redirected web_extract for %s by rule %s", final_blocked["host"], final_blocked["rule"])
            return _error_entry(final_url, final_blocked["message"], title=title, raw=True, blocked=final_blocked)
        markdown, html = payload.get("markdown"), payload.get("html")
        content = markdown if format == "markdown" or (format is None and markdown) else html or markdown or ""
        return {"url": final_url, "title": title, "content": content, "raw_content": content, "metadata": metadata}
    except Exception as scrape_err:
        logger.debug("Firecrawl scrape failed for %s: %s", url, scrape_err)
        return _error_entry(url, str(scrape_err), raw=True)


class FirecrawlWebSearchProvider(BaseWebSearchProvider):
    """Firecrawl search + extract provider for your own self-hosted instance."""

    NAME = "firecrawl"
    DISPLAY_NAME = "Firecrawl"
    EXTRACT = True

    def is_available(self) -> bool:
        return check_firecrawl_api_key()

    def search(self, query: str, limit: int = 5) -> dict[str, Any]:
        """Pre-flight errors (ValueError / ImportError) propagate so the dispatcher emits
        the legacy ``tool_error`` envelope; in-flight errors become failure dicts."""
        from tools.interrupt import is_interrupted
        if is_interrupted():
            return search_fail("Interrupted")
        logger.info("Firecrawl search: '%s' (limit=%d)", query, limit)
        client = _get_firecrawl_client("search")
        try:
            web_results = _extract_web_search_results(client.search(query=query, limit=limit))
            logger.info("Firecrawl: found %d search results", len(web_results))
            return search_ok(web_results)
        except Exception as exc:
            logger.warning("Firecrawl search error: %s", exc)
            return search_fail(f"Firecrawl search failed: {exc}")

    async def extract(self, urls: list[str], **kwargs: Any) -> list[dict[str, Any]]:
        """Per-URL scrape; failures become items with an ``error`` field.
        ``format``: "markdown" | "html" | both (markdown preferred)."""
        from tools.interrupt import is_interrupted as _is_interrupted
        if _is_interrupted():
            return [{"url": u, "error": "Interrupted", "title": ""} for u in urls]
        format = kwargs.get("format")
        formats = [format] if format in ("markdown", "html") else ["markdown", "html"]
        return [
            {"url": url, "error": "Interrupted", "title": ""} if _is_interrupted() else await _scrape_one(url, formats, format)
            for url in urls
        ]


    def get_setup_schema(self) -> dict[str, Any]:
        return setup_schema(
            "Firecrawl (self-hosted)", "free · self-hosted",
            "Run your own Firecrawl instance (Docker) — search + extract without any third-party key.",
            "FIRECRAWL_API_URL", "Your Firecrawl instance URL (e.g. http://localhost:3002)",
            "https://docs.firecrawl.dev/self-hosted",
        )
