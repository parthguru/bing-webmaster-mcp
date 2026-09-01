"""Tests for the BWT_READ_ONLY tool-registration gate.

READ_ONLY is a module-level constant computed at import time, so each test
that needs a different env state must reload the `main` module after setting
the env var via monkeypatch.
"""

import importlib

import main

READ_TOOL_NAMES = {
    "get_sites",
    "get_query_stats",
    "get_page_stats",
    "get_rank_and_traffic_stats",
    "get_crawl_stats",
    "get_crawl_issues",
    "get_url_submission_quota",
    "get_keyword_data",
    "get_related_keywords",
    "get_link_counts",
    "get_url_links",
    "get_blocked_urls",
    "get_query_page_stats",
    "get_query_page_detail_stats",
    "get_url_info",
    "get_keyword_stats",
    "get_deep_link_blocks",
    "get_query_parameters",
    "get_site_roles",
    "get_feeds",
    "get_content_submission_quota",
    "get_url_traffic_info",
    "get_crawl_settings",
    "get_country_region_settings",
    "get_active_page_preview_blocks",
    "get_fetched_urls",
    "get_fetched_url_details",
    "get_connected_pages",
    "get_children_url_info",
    "get_children_url_traffic_info",
    "get_feed_details",
    "get_page_query_stats",
    "get_query_traffic_stats",
    "get_site_moves",
}

MUTATING_TOOL_NAMES = {
    "add_site",
    "verify_site",
    "remove_site",
    "submit_url",
    "submit_url_batch",
    "submit_sitemap",
    "remove_sitemap",
    "add_blocked_url",
    "remove_blocked_url",
    "submit_content",
    "add_connected_page",
    "add_deep_link_block",
    "add_query_parameter",
    "add_site_roles",
    "update_crawl_settings",
    "add_country_region_settings",
    "remove_query_parameter",
    "remove_deep_link_block",
    "add_page_preview_block",
    "remove_page_preview_block",
    "enable_disable_query_parameter",
    "fetch_url",
    "remove_feed",
    "submit_site_move",
    "remove_site_role",
    "remove_country_region_settings",
}


async def _registered_names(mod):
    tools = await mod.mcp.list_tools()
    return {t.name for t in tools}


async def test_default_is_read_only(monkeypatch):
    """No BWT_READ_ONLY set: only the 34 read tools register."""
    monkeypatch.delenv("BWT_READ_ONLY", raising=False)
    mod = importlib.reload(main)

    names = await _registered_names(mod)
    assert len(names) == 34, f"expected 34 registered tools, inspected {sorted(names)}"
    assert "get_sites" in names, f"canary get_sites missing from {sorted(names)}"
    assert "submit_url" not in names, f"canary submit_url unexpectedly present in {sorted(names)}"
    assert names == READ_TOOL_NAMES, f"registered set mismatch: {sorted(names)}"


async def test_garbage_value_stays_read_only(monkeypatch):
    """Values other than the explicit false-tokens keep read-only true (fail-safe)."""
    for garbage in ("1", "yes", "garbage", ""):
        monkeypatch.setenv("BWT_READ_ONLY", garbage)
        mod = importlib.reload(main)
        names = await _registered_names(mod)
        assert len(names) == 34, (
            f"BWT_READ_ONLY={garbage!r} should stay read-only "
            f"(34 tools), inspected {sorted(names)}"
        )


async def test_read_only_false_registers_all_60(monkeypatch):
    """BWT_READ_ONLY=false: all 60 tools register."""
    monkeypatch.setenv("BWT_READ_ONLY", "false")
    mod = importlib.reload(main)

    names = await _registered_names(mod)
    assert len(names) == 60, f"expected 60 registered tools, inspected {sorted(names)}"
    assert "submit_url" in names, f"canary submit_url missing from {sorted(names)}"
    assert names == READ_TOOL_NAMES | MUTATING_TOOL_NAMES, (
        f"registered set mismatch: {sorted(names)}"
    )


async def test_false_tokens_are_case_insensitive(monkeypatch):
    """TRUE / False / 0 / no / FALSE etc. all parse correctly regardless of case."""
    for value in ("false", "FALSE", "False", "0", "no", "NO", "No"):
        monkeypatch.setenv("BWT_READ_ONLY", value)
        mod = importlib.reload(main)
        names = await _registered_names(mod)
        assert len(names) == 60, (
            f"BWT_READ_ONLY={value!r} should disable read-only "
            f"(60 tools), inspected {sorted(names)}"
        )

    for value in ("true", "TRUE", "True", "1", "yes"):
        monkeypatch.setenv("BWT_READ_ONLY", value)
        mod = importlib.reload(main)
        names = await _registered_names(mod)
        assert len(names) == 34, (
            f"BWT_READ_ONLY={value!r} should stay read-only "
            f"(34 tools), inspected {sorted(names)}"
        )
