"""Tests for the BWT_ALLOWED_SITES site allowlist.

ALLOWED_SITES is a module-level constant computed at import time (same as
READ_ONLY in test_read_only_gate.py), so each test that needs a different
env state must reload the `main` module after setting the env var(s) via
monkeypatch. BWT_READ_ONLY is explicitly set at every reload too, since a
write tool (submit_url) doesn't register at all under the default read-only
gate.
"""

import importlib
from unittest.mock import patch

import pytest

import main


def _reload(monkeypatch, *, allowed_sites: str = "", read_only: str = "true"):
    monkeypatch.setenv("BWT_ALLOWED_SITES", allowed_sites)
    monkeypatch.setenv("BWT_READ_ONLY", read_only)
    return importlib.reload(main)


class _NetworkCalledUnexpectedly(Exception):
    """Sentinel raised by the patched client so tests can prove the HTTP
    layer was (or wasn't) reached, without building a full httpx mock."""


async def test_disallowed_site_rejected_on_read_tool(monkeypatch):
    """get_query_stats with a site outside the allowlist must raise before
    any network call — proven by patching httpx.AsyncClient and asserting
    it was never constructed."""
    mod = _reload(monkeypatch, allowed_sites="https://allowed.com", read_only="true")

    with patch.object(mod, "httpx") as mock_httpx:
        with pytest.raises(ValueError, match="not in the BWT_ALLOWED_SITES allowlist"):
            await mod.get_query_stats(site_url="https://evil.com")
        assert mock_httpx.AsyncClient.call_count == 0, (
            f"expected httpx.AsyncClient never constructed, "
            f"was called {mock_httpx.AsyncClient.call_count} time(s)"
        )


async def test_disallowed_site_rejected_on_write_tool(monkeypatch):
    """submit_url with a site outside the allowlist must raise before any
    network call. Requires BWT_READ_ONLY=false at the same reload as
    BWT_ALLOWED_SITES, since write tools don't register under the default
    read-only gate."""
    mod = _reload(monkeypatch, allowed_sites="https://allowed.com", read_only="false")

    assert "submit_url" in {t.name for t in await mod.mcp.list_tools()}, (
        "submit_url should be registered when BWT_READ_ONLY=false"
    )

    with patch.object(mod, "httpx") as mock_httpx:
        with pytest.raises(ValueError, match="not in the BWT_ALLOWED_SITES allowlist"):
            await mod.submit_url(site_url="https://evil.com", url="https://evil.com/page")
        assert mock_httpx.AsyncClient.call_count == 0, (
            f"expected httpx.AsyncClient never constructed, "
            f"was called {mock_httpx.AsyncClient.call_count} time(s)"
        )


async def test_allowed_site_passes_allowlist_check(monkeypatch):
    """A site in the allowlist (with a trailing slash, to prove origin
    normalization) must clear the allowlist check and reach the HTTP layer.
    We don't need a full round trip — just proof control got past the
    allowlist gate — so _ensure_client is patched to raise a sentinel, and
    we assert that sentinel (not ValueError) comes out."""
    mod = _reload(monkeypatch, allowed_sites="https://allowed.com", read_only="true")

    with patch.object(mod.api, "_ensure_client", side_effect=_NetworkCalledUnexpectedly):
        with pytest.raises(_NetworkCalledUnexpectedly):
            # trailing slash: BWT commonly returns/expects one
            await mod.get_query_stats(site_url="https://allowed.com/")


async def test_empty_allowlist_allows_any_site(monkeypatch):
    """Unset/empty BWT_ALLOWED_SITES must allow any site through the
    allowlist check (default, back-compat behavior)."""
    mod = _reload(monkeypatch, allowed_sites="", read_only="true")

    assert mod.ALLOWED_SITES == frozenset(), (
        f"expected empty ALLOWED_SITES for unset env var, got {mod.ALLOWED_SITES!r}"
    )

    with patch.object(mod.api, "_ensure_client", side_effect=_NetworkCalledUnexpectedly):
        with pytest.raises(_NetworkCalledUnexpectedly):
            await mod.get_query_stats(site_url="https://anything-at-all.com")
