"""
MCP Server for Bing Webmaster Tools

An MCP server that provides integration with Bing Webmaster Tools,
enabling site management and analytics through AI assistants.
"""

import contextvars
import json
import logging
import os
from typing import Annotated, Any, Dict, List, Optional
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialize MCP server with capabilities
mcp = FastMCP(
    name="mcp-server-bing-webmaster",
    instructions="Direct access to Bing Webmaster Tools API with OData compatibility",
    host="0.0.0.0",
    port=int(os.getenv("PORT", "8080")),
)


@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> Response:
    return PlainTextResponse("ok", status_code=200)

# Read-only gate: mutating tools are not registered when BWT_READ_ONLY is true (default).
_READ_ONLY_FALSE_TOKENS = {"false", "0", "no"}
READ_ONLY = os.getenv("BWT_READ_ONLY", "true").strip().lower() not in _READ_ONLY_FALSE_TOKENS


def mutating_tool(**kw):
    def deco(fn):
        return fn if READ_ONLY else mcp.tool(**kw)(fn)
    return deco


# API configuration
API_BASE_URL = "https://ssl.bing.com/webmaster/api.svc/json"
API_KEY = os.getenv("BING_WEBMASTER_API_KEY", "")

# Site allowlist: keys that carry a site URL into a request, across every tool
# (grep-verified: siteUrl for 59 of the 60 tools; submit_site_move uses
# oldSiteUrl/newSiteUrl instead since it has no siteUrl of its own). get_sites
# is the only tool with no site parameter, so it never hits this check.
_SITE_PARAM_KEYS = ("siteUrl", "oldSiteUrl", "newSiteUrl")


def _normalize_origin(url: str) -> str:
    """Normalize a URL to a bare origin (scheme+host+port) for comparison.

    BWT site URLs are treated as origins, not paths: every site_url docstring
    in this file describes it as "the URL of the site", and BWT commonly
    returns/expects a trailing slash (e.g. https://example.com/), so naive
    string equality would falsely reject a matching site.
    """
    parsed = urlparse(url)
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"


def _parse_allowed_sites(raw: str) -> frozenset[str]:
    """Parse BWT_ALLOWED_SITES into a normalized origin set. Empty = allow all."""
    origins: set[str] = set()
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        parsed = urlparse(entry)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError(
                f"BWT_ALLOWED_SITES entry {entry!r} is missing a scheme or host; "
                "expected e.g. https://example.com"
            )
        origins.add(_normalize_origin(entry))
    return frozenset(origins)


# Empty/unset = allow all sites (default, matches prior unrestricted behavior).
ALLOWED_SITES = _parse_allowed_sites(os.getenv("BWT_ALLOWED_SITES", ""))


# HTTP transport auth: single static bearer token (backward-compatible,
# routes to the global API_KEY/ALLOWED_SITES) and/or a per-tenant token map
# (routes each token to its own api_key/allowed_sites).
MCP_AUTH_TOKEN = os.getenv("MCP_AUTH_TOKEN", "").strip() or None


def _parse_tenants(raw: str) -> Dict[str, Dict[str, Any]]:
    """Parse BWT_TENANTS into {bearer_token: {"api_key": str, "allowed_sites": frozenset[str]}}.

    BWT_TENANTS is a JSON object: {"<token>": {"api_key": "...", "allowed_sites": ["https://..."]}}.
    An empty/missing allowed_sites list is a config error (fail import), not
    "allow all" — that would silently grant a misconfigured tenant broader
    access than intended.
    """
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"BWT_TENANTS is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("BWT_TENANTS must be a JSON object mapping bearer token to tenant config")

    tenants: Dict[str, Dict[str, Any]] = {}
    for token, entry in data.items():
        if not isinstance(entry, dict):
            raise ValueError(f"BWT_TENANTS entry for token {token!r} must be a JSON object")

        api_key = entry.get("api_key")
        if not isinstance(api_key, str) or not api_key:
            raise ValueError(
                f"BWT_TENANTS entry for token {token!r} is missing a non-empty 'api_key'"
            )

        sites = entry.get("allowed_sites")
        if not isinstance(sites, list) or not sites or not all(isinstance(s, str) for s in sites):
            raise ValueError(
                f"BWT_TENANTS entry for token {token!r} must have a non-empty 'allowed_sites' "
                "list of site URL strings"
            )

        allowed = _parse_allowed_sites(",".join(sites))
        if not allowed:
            raise ValueError(
                f"BWT_TENANTS entry for token {token!r} has no valid entries in 'allowed_sites'"
            )
        tenants[token] = {"api_key": api_key, "allowed_sites": allowed}
    return tenants


TENANTS = _parse_tenants(os.getenv("BWT_TENANTS", ""))

# Carries the per-request (tenant-scoped) API key and site allowlist from the
# HTTP auth middleware into BingWebmasterAPI._make_request. None (the
# default, e.g. on stdio) means "use the global API_KEY/ALLOWED_SITES".
_request_api_key: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "_request_api_key", default=None
)
_request_allowed_sites: contextvars.ContextVar[Optional[frozenset]] = contextvars.ContextVar(
    "_request_allowed_sites", default=None
)


class BingWebmasterAPI:
    """Client for Bing Webmaster Tools API with OData response handling."""

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = API_BASE_URL
        self._client: Optional[httpx.AsyncClient] = None

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Get or create the persistent HTTP client with connection pooling."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0),
                limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            )
        return self._client

    async def _make_request(
        self,
        endpoint: str,
        method: str = "GET",
        json_data: Optional[Dict[str, Any]] = None,
        params: Optional[Dict[str, Any]] = None,
    ) -> Any:
        """Make a request to the Bing API and handle OData responses."""
        effective_allowed_sites = _request_allowed_sites.get()
        if effective_allowed_sites is None:
            effective_allowed_sites = ALLOWED_SITES

        if effective_allowed_sites:
            for source in (params, json_data):
                if not source:
                    continue
                for key in _SITE_PARAM_KEYS:
                    if key in source:
                        site_value = source[key]
                        if _normalize_origin(site_value) not in effective_allowed_sites:
                            raise ValueError(
                                f"Site {site_value!r} is not in the BWT_ALLOWED_SITES allowlist"
                            )

        client = await self._ensure_client()

        headers = {"Content-Type": "application/json; charset=utf-8"}

        # Build URL with httpx params for proper encoding
        # Set apikey AFTER merging caller params to prevent override
        url = f"{self.base_url}/{endpoint}"
        all_params: Dict[str, Any] = dict(params) if params else {}
        all_params["apikey"] = _request_api_key.get() or self.api_key

        try:
            if method == "GET":
                response = await client.get(url, headers=headers, params=all_params)
            else:
                response = await client.request(
                    method, url, headers=headers, json=json_data, params=all_params
                )

            if response.status_code != 200:
                logger.error("API error %d for %s", response.status_code, endpoint)
                raise Exception(f"API error {response.status_code}: {response.text}")

            data = response.json()

            # Handle OData response format
            if "d" in data:
                return data["d"]
            return data

        except httpx.TimeoutException:
            logger.error("Request timeout for %s", endpoint)
            raise

    def _ensure_type_field(self, data: Any, type_name: str) -> Any:
        """Ensure __type field is present for MCP compatibility."""
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "__type" not in item:
                    item["__type"] = f"{type_name}:#Microsoft.Bing.Webmaster.Api"
        elif isinstance(data, dict) and "__type" not in data:
            data["__type"] = f"{type_name}:#Microsoft.Bing.Webmaster.Api"
        return data


# Create global API instance
api = BingWebmasterAPI(API_KEY)


# Site Management Tools
@mcp.tool(
    name="get_sites",
    description="Retrieve all sites in the user's Bing Webmaster Tools account",
)
async def get_sites() -> List[Dict[str, Any]]:
    """
    Retrieve all sites in the user's Bing Webmaster Tools account.

    Returns:
        List of sites with their details including URL, verification status, etc.
    """
    sites = await api._make_request("GetUserSites")
    return api._ensure_type_field(sites, "Site")


@mutating_tool(name="add_site", description="Add a new site to Bing Webmaster Tools")
async def add_site(site_url: Annotated[str, "The URL of the site to add"]) -> Dict[str, str]:
    """
    Add a new site to Bing Webmaster Tools.

    Args:
        site_url: The URL of the site to add

    Returns:
        Success message
    """
    await api._make_request("AddSite", "POST", {"siteUrl": site_url})
    return {"message": f"Site {site_url} added successfully"}


@mutating_tool(name="verify_site", description="Attempt to verify ownership of a site")
async def verify_site(site_url: Annotated[str, "The URL of the site to verify"]) -> Dict[str, Any]:
    """
    Attempt to verify ownership of a site.

    Args:
        site_url: The URL of the site to verify

    Returns:
        Verification result
    """
    result = await api._make_request("VerifySite", "POST", {"siteUrl": site_url})
    return {"verified": result, "site_url": site_url}


@mutating_tool(name="remove_site", description="Remove a site from Bing Webmaster Tools")
async def remove_site(site_url: Annotated[str, "The URL of the site to remove"]) -> Dict[str, str]:
    """
    Remove a site from Bing Webmaster Tools.

    Args:
        site_url: The URL of the site to remove

    Returns:
        Success message
    """
    await api._make_request("RemoveSite", "POST", {"siteUrl": site_url})
    return {"message": f"Site {site_url} removed successfully"}


# Traffic Analysis Tools
@mcp.tool(
    name="get_query_stats",
    description="Get detailed traffic statistics for top queries.",
)
async def get_query_stats(site_url: Annotated[str, "The URL of the site"]) -> List[Dict[str, Any]]:
    """
    Get detailed traffic statistics for top queries.

    Args:
        site_url: The URL of the site

    Returns:
        List of query statistics with clicks, impressions, CTR, and position
    """
    stats = await api._make_request("GetQueryStats", params={"siteUrl": site_url})
    return api._ensure_type_field(stats, "QueryStats")


@mcp.tool(name="get_page_stats", description="Get traffic statistics for top pages.")
async def get_page_stats(site_url: Annotated[str, "The URL of the site"]) -> List[Dict[str, Any]]:
    """
    Get traffic statistics for top pages.

    Args:
        site_url: The URL of the site

    Returns:
        List of page statistics
    """
    stats = await api._make_request("GetPageStats", params={"siteUrl": site_url})
    return api._ensure_type_field(stats, "PageStats")


@mcp.tool(
    name="get_rank_and_traffic_stats",
    description="Get overall ranking and traffic statistics.",
)
async def get_rank_and_traffic_stats(
    site_url: Annotated[str, "The URL of the site"],
) -> Dict[str, Any]:
    """
    Get overall ranking and traffic statistics.

    Args:
        site_url: The URL of the site

    Returns:
        Overall site statistics
    """
    stats = await api._make_request("GetRankAndTrafficStats", params={"siteUrl": site_url})
    return api._ensure_type_field(stats, "RankAndTrafficStats")


# Crawling Tools
@mcp.tool(name="get_crawl_stats", description="Retrieve crawl statistics for a specific site.")
async def get_crawl_stats(site_url: Annotated[str, "The URL of the site"]) -> List[Dict[str, Any]]:
    """
    Retrieve crawl statistics for a specific site.

    Args:
        site_url: The URL of the site

    Returns:
        List of daily crawl statistics
    """
    stats = await api._make_request("GetCrawlStats", params={"siteUrl": site_url})
    return api._ensure_type_field(stats, "CrawlStats")


@mcp.tool(name="get_crawl_issues", description="Get crawl issues and errors for a site.")
async def get_crawl_issues(site_url: Annotated[str, "The URL of the site"]) -> List[Dict[str, Any]]:
    """
    Get crawl issues and errors for a site.

    Args:
        site_url: The URL of the site

    Returns:
        List of crawl issues
    """
    issues = await api._make_request("GetCrawlIssues", params={"siteUrl": site_url})
    return api._ensure_type_field(issues, "CrawlIssue")


# URL Submission Tools
@mutating_tool(name="submit_url", description="Submit a single URL for indexing.")
async def submit_url(
    site_url: Annotated[str, "The URL of the site"],
    url: Annotated[str, "The specific URL to submit"],
) -> Dict[str, str]:
    """
    Submit a single URL for indexing.

    Args:
        site_url: The URL of the site
        url: The specific URL to submit

    Returns:
        Success message
    """
    await api._make_request("SubmitUrl", "POST", {"siteUrl": site_url, "url": url})
    return {"message": f"URL {url} submitted successfully"}


@mutating_tool(name="submit_url_batch", description="Submit multiple URLs for indexing.")
async def submit_url_batch(
    site_url: Annotated[str, "The URL of the site"], urls: List[str]
) -> Dict[str, Any]:
    """
    Submit multiple URLs for indexing.

    Args:
        site_url: The URL of the site
        urls: List of URLs to submit

    Returns:
        Submission result
    """
    result = await api._make_request(
        "SubmitUrlBatch", "POST", {"siteUrl": site_url, "urlList": urls}
    )
    return {"message": f"Submitted {len(urls)} URLs", "result": result}


@mcp.tool(
    name="get_url_submission_quota",
    description="Get information about URL submission quota and usage.",
)
async def get_url_submission_quota(
    site_url: Annotated[str, "The URL of the site"],
) -> Dict[str, Any]:
    """
    Get information about URL submission quota and usage.

    Args:
        site_url: The URL of the site

    Returns:
        Quota information
    """
    quota = await api._make_request("GetUrlSubmissionQuota", params={"siteUrl": site_url})
    return api._ensure_type_field(quota, "UrlSubmissionQuota")


# Sitemap Tools


@mutating_tool(name="submit_sitemap", description="Submit a sitemap to Bing.")
async def submit_sitemap(
    site_url: Annotated[str, "The URL of the site"],
    sitemap_url: Annotated[str, "The URL of the sitemap"],
) -> Dict[str, str]:
    """
    Submit a sitemap to Bing.

    Args:
        site_url: The URL of the site
        sitemap_url: The URL of the sitemap

    Returns:
        Success message
    """
    await api._make_request("SubmitFeed", "POST", {"siteUrl": site_url, "feedUrl": sitemap_url})
    return {"message": f"Sitemap {sitemap_url} submitted successfully"}


@mutating_tool(name="remove_sitemap", description="Remove a sitemap from Bing.")
async def remove_sitemap(
    site_url: Annotated[str, "The URL of the site"],
    sitemap_url: Annotated[str, "The URL of the sitemap to remove"],
) -> Dict[str, str]:
    """
    Remove a sitemap from Bing.

    Args:
        site_url: The URL of the site
        sitemap_url: The URL of the sitemap to remove

    Returns:
        Success message
    """
    await api._make_request("RemoveFeed", "POST", {"siteUrl": site_url, "feedUrl": sitemap_url})
    return {"message": f"Sitemap {sitemap_url} removed successfully"}


# Keyword Analysis Tools
@mcp.tool(
    name="get_keyword_data",
    description="Get detailed data for a specific keyword/query.",
)
async def get_keyword_data(
    site_url: Annotated[str, "The URL of the site"],
    query: Annotated[str, "The keyword/query to analyze"],
) -> Dict[str, Any]:
    """
    Get detailed data for a specific keyword/query.

    Args:
        site_url: The URL of the site
        query: The keyword/query to analyze

    Returns:
        Keyword performance data
    """
    data = await api._make_request("GetKeyword", params={"siteUrl": site_url, "query": query})
    return api._ensure_type_field(data, "KeywordData")


@mcp.tool(name="get_related_keywords", description="Get keywords related to a specific query.")
async def get_related_keywords(
    site_url: Annotated[str, "The URL of the site"],
    query: Annotated[str, "The base keyword/query"],
) -> List[Dict[str, Any]]:
    """
    Get keywords related to a specific query.

    Args:
        site_url: The URL of the site
        query: The base keyword/query

    Returns:
        List of related keywords
    """
    keywords = await api._make_request(
        "GetRelatedKeywords", params={"siteUrl": site_url, "query": query}
    )
    return api._ensure_type_field(keywords, "RelatedKeyword")


# Link Analysis Tools
@mcp.tool(name="get_link_counts", description="Get inbound link counts for a site.")
async def get_link_counts(site_url: Annotated[str, "The URL of the site"]) -> Dict[str, Any]:
    """
    Get inbound link counts for a site.

    Args:
        site_url: The URL of the site

    Returns:
        Link count statistics
    """
    counts = await api._make_request("GetLinkCounts", params={"siteUrl": site_url})
    return api._ensure_type_field(counts, "LinkCounts")


@mcp.tool(name="get_url_links", description="Get inbound links for specific site URL.")
async def get_url_links(
    site_url: Annotated[str, "The URL of the site"],
    link: Annotated[str, "Specific link to retrieve details for"],
    page: Annotated[int, "Page number of results"] = 0,
) -> Dict[str, Any]:
    """
    Get inbound links for specific site URL.

    Args:
        site_url: The URL of the site
        link: Specific link to retrieve details for
        page: Page number of results (default: 0)

    Returns:
        LinkDetails object with inbound link information
    """
    details = await api._make_request(
        "GetUrlLinks", params={"siteUrl": site_url, "link": link, "page": page}
    )
    return api._ensure_type_field(details, "LinkDetails")


# Content Blocking Tools
@mcp.tool(name="get_blocked_urls", description="Get list of blocked URLs for a site.")
async def get_blocked_urls(site_url: Annotated[str, "The URL of the site"]) -> List[Dict[str, Any]]:
    """
    Get list of blocked URLs for a site.

    Args:
        site_url: The URL of the site

    Returns:
        List of blocked URLs
    """
    urls = await api._make_request("GetBlockedUrls", params={"siteUrl": site_url})
    return api._ensure_type_field(urls, "BlockedUrl")


@mutating_tool(name="add_blocked_url", description="Block a URL or directory from being crawled.")
async def add_blocked_url(
    site_url: Annotated[str, "The URL of the site"],
    url: Annotated[str, "The URL or directory to block"],
    block_type: Annotated[str, "Type of block (Page or Directory)"] = "Directory",
) -> Dict[str, str]:
    """
    Block a URL or directory from being crawled.

    Args:
        site_url: The URL of the site
        url: The URL or directory to block
        block_type: Type of block ("Page" or "Directory")

    Returns:
        Success message
    """
    await api._make_request(
        "AddBlockedUrl",
        "POST",
        {"siteUrl": site_url, "blockedUrl": url, "blockType": block_type},
    )
    return {"message": f"URL {url} blocked successfully"}


@mutating_tool(name="remove_blocked_url", description="Remove a URL from the blocked list.")
async def remove_blocked_url(
    site_url: Annotated[str, "The URL of the site"],
    url: Annotated[str, "The blocked URL to remove"],
) -> Dict[str, str]:
    """
    Remove a URL from the blocked list.

    Args:
        site_url: The URL of the site
        url: The blocked URL to remove

    Returns:
        Success message
    """
    await api._make_request("RemoveBlockedUrl", "POST", {"siteUrl": site_url, "blockedUrl": url})
    return {"message": f"URL {url} unblocked successfully"}


# Advanced Query and Page Statistics
@mcp.tool(
    name="get_query_page_stats",
    description="Get detailed traffic statistics for a specific query.",
)
async def get_query_page_stats(
    site_url: Annotated[str, "The URL of the site"],
    query: Annotated[str, "The search query to analyze"],
) -> List[Dict[str, Any]]:
    """
    Get detailed traffic statistics for a specific query.

    Args:
        site_url: The URL of the site
        query: The search query to analyze

    Returns:
        List of page statistics for the given query
    """
    stats = await api._make_request(
        "GetQueryPageStats", params={"siteUrl": site_url, "query": query}
    )
    return api._ensure_type_field(stats, "QueryPageStats")


@mcp.tool(
    name="get_query_page_detail_stats",
    description="Get detailed statistics for a specific query and page combination.",
)
async def get_query_page_detail_stats(
    site_url: Annotated[str, "The URL of the site"],
    query: Annotated[str, "The search query"],
    page: Annotated[str, "The specific page URL"],
) -> Dict[str, Any]:
    """
    Get detailed statistics for a specific query and page combination.

    Args:
        site_url: The URL of the site
        query: The search query
        page: The specific page URL

    Returns:
        Detailed statistics for the query-page combination
    """
    stats = await api._make_request(
        "GetQueryPageDetailStats",
        params={"siteUrl": site_url, "query": query, "page": page},
    )
    return api._ensure_type_field(stats, "DetailedQueryStats")


# URL Information and Analysis
@mcp.tool(
    name="get_url_info",
    description="Get detailed index information for a specific URL.",
)
async def get_url_info(
    site_url: Annotated[str, "The URL of the site"],
    url: Annotated[str, "The specific URL to check"],
) -> Dict[str, Any]:
    """
    Get detailed index information for a specific URL.

    Args:
        site_url: The URL of the site
        url: The specific URL to check

    Returns:
        Detailed information about the URL's index status
    """
    info = await api._make_request("GetUrlInfo", params={"siteUrl": site_url, "url": url})
    return api._ensure_type_field(info, "UrlInfo")


# Content Submission
@mutating_tool(
    name="submit_content",
    description="Submit page content directly to Bing without crawling.",
)
async def submit_content(
    site_url: Annotated[str, "The URL of the site"],
    url: Annotated[str, "The URL of the content"],
    content: Annotated[str, "The HTML content to submit"],
    content_type: Annotated[str, "MIME type of the content"] = "text/html",
    content_length: Annotated[int, "Length of the content in bytes"] = -1,
) -> Dict[str, str]:
    """
    Submit page content directly to Bing without crawling.

    Args:
        site_url: The URL of the site
        url: The URL of the content
        content: The HTML content to submit
        content_type: MIME type of the content (default: text/html)
        content_length: Length of the content in bytes (default: auto-calculated)

    Returns:
        Success message
    """
    if content_length == -1:
        content_length = len(content.encode("utf-8"))

    await api._make_request(
        "SubmitContent",
        "POST",
        {
            "siteUrl": site_url,
            "url": url,
            "content": content,
            "contentType": content_type,
            "contentLength": content_length,
        },
    )
    return {"message": f"Content for {url} submitted successfully"}


# Keyword Analysis
@mcp.tool(
    name="get_keyword_stats",
    description="Get historical statistics for a specific keyword.",
)
async def get_keyword_stats(
    site_url: Annotated[str, "The URL of the site"],
    query: Annotated[str, "The keyword/query to analyze"],
    country: Annotated[str, "Country code (e.g., 'US', 'GB')"] = "",
    language: Annotated[str, "Language code (e.g., 'en', 'fr')"] = "",
) -> Dict[str, Any]:
    """
    Get historical statistics for a specific keyword.

    Args:
        site_url: The URL of the site
        query: The keyword/query to analyze
        country: Country code (optional)
        language: Language code (optional)

    Returns:
        Historical keyword statistics
    """
    req_params: Dict[str, Any] = {"siteUrl": site_url, "query": query}
    if country:
        req_params["country"] = country
    if language:
        req_params["language"] = language

    stats = await api._make_request("GetKeywordStats", params=req_params)
    return api._ensure_type_field(stats, "KeywordStats")


# Connected Pages Management
@mutating_tool(name="add_connected_page", description="Add a page that has a link to your website.")
async def add_connected_page(
    site_url: Annotated[str, "The URL of your site"],
    connected_url: Annotated[str, "The URL of the page linking to your site"],
) -> Dict[str, str]:
    """
    Add a page that has a link to your website.

    Args:
        site_url: The URL of your site
        connected_url: The URL of the page linking to your site

    Returns:
        Success message
    """
    await api._make_request(
        "AddConnectedPage",
        "POST",
        {"siteUrl": site_url, "connectedPageUrl": connected_url},
    )
    return {"message": f"Connected page {connected_url} added successfully"}


# Deep Link Management
@mcp.tool(name="get_deep_link_blocks", description="Get list of blocked deep links.")
async def get_deep_link_blocks(
    site_url: Annotated[str, "The URL of the site"],
) -> List[Dict[str, Any]]:
    """
    Get list of blocked deep links.

    Args:
        site_url: The URL of the site

    Returns:
        List of blocked deep links
    """
    blocks = await api._make_request("GetDeepLinkBlocks", params={"siteUrl": site_url})
    return api._ensure_type_field(blocks, "DeepLinkBlock")


@mutating_tool(
    name="add_deep_link_block",
    description="Block deep links for specific URL patterns.",
)
async def add_deep_link_block(
    site_url: Annotated[str, "The URL of the site"],
    url_pattern: Annotated[str, "URL pattern to block"],
    block_type: Annotated[str, "Type of block"],
    reason: Annotated[str, "Reason for blocking"],
) -> Dict[str, str]:
    """
    Block deep links for specific URL patterns.

    Args:
        site_url: The URL of the site
        url_pattern: URL pattern to block
        block_type: Type of block
        reason: Reason for blocking

    Returns:
        Success message
    """
    await api._make_request(
        "AddDeepLinkBlock",
        "POST",
        {
            "siteUrl": site_url,
            "urlPattern": url_pattern,
            "blockType": block_type,
            "reason": reason,
        },
    )
    return {"message": f"Deep link block for {url_pattern} added successfully"}


# URL Query Parameters
@mcp.tool(
    name="get_query_parameters",
    description="Get URL normalization parameters. Note: May require special permissions.",
)
async def get_query_parameters(
    site_url: Annotated[str, "The URL of the site"],
) -> List[Dict[str, Any]]:
    """
    Get URL normalization parameters.

    Args:
        site_url: The URL of the site

    Returns:
        List of query parameters used for URL normalization
    """
    params = await api._make_request("GetQueryParameters", params={"siteUrl": site_url})
    return api._ensure_type_field(params, "QueryParameter")


@mutating_tool(name="add_query_parameter", description="Add URL normalization parameter.")
async def add_query_parameter(
    site_url: Annotated[str, "The URL of the site"],
    parameter: Annotated[str, "The query parameter to normalize"],
) -> Dict[str, str]:
    """
    Add URL normalization parameter.

    Args:
        site_url: The URL of the site
        parameter: The query parameter to normalize

    Returns:
        Success message
    """
    await api._make_request(
        "AddQueryParameter", "POST", {"siteUrl": site_url, "parameter": parameter}
    )
    return {"message": f"Query parameter {parameter} added successfully"}


# Site Roles Management
@mcp.tool(name="get_site_roles", description="Get list of users with access to the site.")
async def get_site_roles(site_url: Annotated[str, "The URL of the site"]) -> List[Dict[str, Any]]:
    """
    Get list of users with access to the site.

    Args:
        site_url: The URL of the site

    Returns:
        List of users and their roles
    """
    roles = await api._make_request("GetSiteRoles", params={"siteUrl": site_url})
    return api._ensure_type_field(roles, "SiteRoles")


@mutating_tool(name="add_site_roles", description="Delegate site access to another user.")
async def add_site_roles(
    site_url: Annotated[str, "The URL of the site"],
    user_email: Annotated[str, "Email of the user to grant access"],
    auth_token: Annotated[str, "Authentication token"],
    role_type: Annotated[str, "Type of role to grant"],
    is_explicit: Annotated[bool, "Whether the role is explicit"] = True,
    should_notify: Annotated[bool, "Whether to notify the user"] = True,
) -> Dict[str, str]:
    """
    Delegate site access to another user.

    Args:
        site_url: The URL of the site
        user_email: Email of the user to grant access
        auth_token: Authentication token
        role_type: Type of role to grant
        is_explicit: Whether the role is explicit
        should_notify: Whether to notify the user

    Returns:
        Success message
    """
    await api._make_request(
        "AddSiteRoles",
        "POST",
        {
            "siteUrl": site_url,
            "userEmail": user_email,
            "authToken": auth_token,
            "roleType": role_type,
            "isExplicit": is_explicit,
            "shouldNotify": should_notify,
        },
    )
    return {"message": f"Access granted to {user_email} successfully"}


# Feed/Sitemap Management Enhancement
@mcp.tool(name="get_feeds", description="Get all RSS/Atom feeds for a site.")
async def get_feeds(site_url: Annotated[str, "The URL of the site"]) -> List[Dict[str, Any]]:
    """
    Get all RSS/Atom feeds for a site.

    Args:
        site_url: The URL of the site

    Returns:
        List of feeds
    """
    feeds = await api._make_request("GetFeeds", params={"siteUrl": site_url})
    return api._ensure_type_field(feeds, "Feed")


# Content Submission Quota
@mcp.tool(
    name="get_content_submission_quota",
    description="Get content submission quota information.",
)
async def get_content_submission_quota(
    site_url: Annotated[str, "The URL of the site"],
) -> Dict[str, Any]:
    """
    Get content submission quota information.

    Args:
        site_url: The URL of the site

    Returns:
        Content submission quota details
    """
    quota = await api._make_request("GetContentSubmissionQuota", params={"siteUrl": site_url})
    return api._ensure_type_field(quota, "ContentSubmissionQuota")


# Traffic Information
@mcp.tool(
    name="get_url_traffic_info",
    description="Get traffic information for specific URLs.",
)
async def get_url_traffic_info(
    site_url: Annotated[str, "The URL of the site"], urls: List[str]
) -> List[Dict[str, Any]]:
    """
    Get traffic information for specific URLs.

    Args:
        site_url: The URL of the site
        urls: List of URLs to get traffic info for

    Returns:
        Traffic information for each URL
    """
    traffic_info = await api._make_request(
        "GetUrlTrafficInfo", "POST", {"siteUrl": site_url, "urls": urls}
    )
    return api._ensure_type_field(traffic_info, "UrlTrafficInfo")


# Crawl Settings Management
@mcp.tool(name="get_crawl_settings", description="Get crawl settings for a site.")
async def get_crawl_settings(site_url: Annotated[str, "The URL of the site"]) -> Dict[str, Any]:
    """
    Get crawl settings for a site.

    Args:
        site_url: The URL of the site

    Returns:
        Crawl settings configuration
    """
    settings = await api._make_request("GetCrawlSettings", params={"siteUrl": site_url})
    return api._ensure_type_field(settings, "CrawlSettings")


@mutating_tool(name="update_crawl_settings", description="Update crawl settings for a site.")
async def update_crawl_settings(
    site_url: Annotated[str, "The URL of the site"],
    crawl_rate: Annotated[str, "Crawl rate setting"] = "Normal",
) -> Dict[str, str]:
    """
    Update crawl settings for a site.

    Args:
        site_url: The URL of the site
        crawl_rate: Crawl rate setting (Slow, Normal, Fast)

    Returns:
        Success message
    """
    await api._make_request(
        "SaveCrawlSettings", "POST", {"siteUrl": site_url, "crawlRate": crawl_rate}
    )
    return {"message": "Crawl settings updated successfully"}


# Country/Region Settings
@mcp.tool(
    name="get_country_region_settings",
    description="Get country/region targeting settings. Note: May require special permissions.",
)
async def get_country_region_settings(
    site_url: Annotated[str, "The URL of the site"],
) -> List[Dict[str, Any]]:
    """
    Get country/region targeting settings.

    Args:
        site_url: The URL of the site

    Returns:
        List of country/region settings
    """
    settings = await api._make_request("GetCountryRegionSettings", params={"siteUrl": site_url})
    return api._ensure_type_field(settings, "CountryRegionSettings")


@mutating_tool(
    name="add_country_region_settings",
    description="Add country/region targeting settings.",
)
async def add_country_region_settings(
    site_url: Annotated[str, "The URL of the site"],
    country_code: Annotated[str, "ISO country code"],
    region_code: Annotated[str, "Region code"] = "",
) -> Dict[str, str]:
    """
    Add country/region targeting settings.

    Args:
        site_url: The URL of the site
        country_code: ISO country code (e.g., 'US', 'GB')
        region_code: Region code (optional)

    Returns:
        Success message
    """
    await api._make_request(
        "AddCountryRegionSettings",
        "POST",
        {
            "siteUrl": site_url,
            "settings": {"countryCode": country_code, "regionCode": region_code},
        },
    )
    return {"message": "Country/region settings added successfully"}


# Remove Methods
@mutating_tool(name="remove_query_parameter", description="Remove a URL normalization parameter.")
async def remove_query_parameter(
    site_url: Annotated[str, "The URL of the site"],
    parameter: Annotated[str, "The query parameter to remove"],
) -> Dict[str, str]:
    """
    Remove a URL normalization parameter.

    Args:
        site_url: The URL of the site
        parameter: The query parameter to remove

    Returns:
        Success message
    """
    await api._make_request(
        "RemoveQueryParameter",
        "POST",
        {"siteUrl": site_url, "parameter": parameter},
    )
    return {"message": f"Query parameter {parameter} removed successfully"}


@mutating_tool(name="remove_deep_link_block", description="Remove a deep link block.")
async def remove_deep_link_block(
    site_url: Annotated[str, "The URL of the site"],
    url_pattern: Annotated[str, "URL pattern to unblock"],
) -> Dict[str, str]:
    """
    Remove a deep link block.

    Args:
        site_url: The URL of the site
        url_pattern: URL pattern to unblock

    Returns:
        Success message
    """
    await api._make_request(
        "RemoveDeepLinkBlock",
        "POST",
        {"siteUrl": site_url, "urlPattern": url_pattern},
    )
    return {"message": f"Deep link block for {url_pattern} removed successfully"}


# Page Preview Block Management
@mutating_tool(
    name="add_page_preview_block",
    description="Add a page preview block to prevent rich snippets.",
)
async def add_page_preview_block(
    site_url: Annotated[str, "The URL of the site"],
    block_url: Annotated[str, "URL or pattern to block"],
    block_type: Annotated[str, "Type of block"] = "Page",
) -> Dict[str, str]:
    """
    Add a page preview block to prevent rich snippets.

    Args:
        site_url: The URL of the site
        block_url: URL or pattern to block
        block_type: Type of block (default: Page)

    Returns:
        Success message
    """
    await api._make_request(
        "AddPagePreviewBlock",
        "POST",
        {"siteUrl": site_url, "blockUrl": block_url, "blockType": block_type},
    )
    return {"message": f"Page preview block for {block_url} added successfully"}


@mcp.tool(
    name="get_active_page_preview_blocks",
    description="Get list of active page preview blocks.",
)
async def get_active_page_preview_blocks(
    site_url: Annotated[str, "The URL of the site"],
) -> List[Dict[str, Any]]:
    """
    Get list of active page preview blocks.

    Args:
        site_url: The URL of the site

    Returns:
        List of active page preview blocks
    """
    blocks = await api._make_request("GetActivePagePreviewBlocks", params={"siteUrl": site_url})
    return api._ensure_type_field(blocks, "PagePreviewBlock")


@mutating_tool(name="remove_page_preview_block", description="Remove a page preview block.")
async def remove_page_preview_block(
    site_url: Annotated[str, "The URL of the site"],
    block_url: Annotated[str, "URL pattern to unblock"],
) -> Dict[str, str]:
    """
    Remove a page preview block.

    Args:
        site_url: The URL of the site
        block_url: URL pattern to unblock

    Returns:
        Success message
    """
    await api._make_request(
        "RemovePagePreviewBlock",
        "POST",
        {"siteUrl": site_url, "blockUrl": block_url},
    )
    return {"message": f"Page preview block for {block_url} removed successfully"}


# Query Parameter Management Enhancement
@mutating_tool(
    name="enable_disable_query_parameter",
    description="Enable or disable a URL query parameter.",
)
async def enable_disable_query_parameter(
    site_url: Annotated[str, "The URL of the site"],
    parameter: Annotated[str, "The query parameter"],
    enabled: Annotated[bool, "Whether to enable or disable"],
) -> Dict[str, str]:
    """
    Enable or disable a URL query parameter.

    Args:
        site_url: The URL of the site
        parameter: The query parameter
        enabled: Whether to enable (True) or disable (False)

    Returns:
        Success message
    """
    await api._make_request(
        "EnableDisableQueryParameter",
        "POST",
        {"siteUrl": site_url, "parameter": parameter, "enabled": enabled},
    )
    status = "enabled" if enabled else "disabled"
    return {"message": f"Query parameter {parameter} {status} successfully"}


# URL Fetching Tools
@mutating_tool(name="fetch_url", description="Request Bing to fetch/crawl a specific URL.")
async def fetch_url(
    site_url: Annotated[str, "The URL of the site"],
    url: Annotated[str, "The specific URL to fetch"],
) -> Dict[str, str]:
    """
    Request Bing to fetch/crawl a specific URL.

    Args:
        site_url: The URL of the site
        url: The specific URL to fetch

    Returns:
        Success message
    """
    await api._make_request("FetchUrl", "POST", {"siteUrl": site_url, "url": url})
    return {"message": f"Fetch request for {url} submitted successfully"}


@mcp.tool(name="get_fetched_urls", description="Get list of URLs that have been fetched.")
async def get_fetched_urls(site_url: Annotated[str, "The URL of the site"]) -> List[Dict[str, Any]]:
    """
    Get list of URLs that have been fetched.

    Args:
        site_url: The URL of the site

    Returns:
        List of fetched URLs
    """
    urls = await api._make_request("GetFetchedUrls", params={"siteUrl": site_url})
    return api._ensure_type_field(urls, "FetchedUrl")


@mcp.tool(
    name="get_fetched_url_details",
    description="Get detailed information about a fetched URL.",
)
async def get_fetched_url_details(
    site_url: Annotated[str, "The URL of the site"],
    url: Annotated[str, "The fetched URL to get details for"],
) -> Dict[str, Any]:
    """
    Get detailed information about a fetched URL.

    Args:
        site_url: The URL of the site
        url: The fetched URL to get details for

    Returns:
        Detailed information about the fetched URL
    """
    details = await api._make_request(
        "GetFetchedUrlDetails", params={"siteUrl": site_url, "url": url}
    )
    return api._ensure_type_field(details, "FetchedUrlDetails")


# Connected Pages Enhancement
@mcp.tool(
    name="get_connected_pages",
    description="Get list of connected pages that link to your site.",
)
async def get_connected_pages(
    site_url: Annotated[str, "The URL of the site"],
) -> List[Dict[str, Any]]:
    """
    Get list of connected pages that link to your site.

    Args:
        site_url: The URL of the site

    Returns:
        List of connected pages
    """
    pages = await api._make_request("GetConnectedPages", params={"siteUrl": site_url})
    return api._ensure_type_field(pages, "ConnectedPage")


# Children URL Information
@mcp.tool(
    name="get_children_url_info",
    description="Get information about child URLs under a parent URL.",
)
async def get_children_url_info(
    site_url: Annotated[str, "The URL of the site"],
    parent_url: Annotated[str, "The parent URL"],
) -> List[Dict[str, Any]]:
    """
    Get information about child URLs under a parent URL.

    Args:
        site_url: The URL of the site
        parent_url: The parent URL

    Returns:
        List of child URL information
    """
    children = await api._make_request(
        "GetChildrenUrlInfo", params={"siteUrl": site_url, "parentUrl": parent_url}
    )
    return api._ensure_type_field(children, "ChildUrlInfo")


@mcp.tool(
    name="get_children_url_traffic_info",
    description="Get traffic information for child URLs.",
)
async def get_children_url_traffic_info(
    site_url: Annotated[str, "The URL of the site"],
    parent_url: Annotated[str, "The parent URL"],
    limit: Annotated[int, "Maximum number of results"] = 100,
) -> List[Dict[str, Any]]:
    """
    Get traffic information for child URLs.

    Args:
        site_url: The URL of the site
        parent_url: The parent URL
        limit: Maximum number of results (default: 100)

    Returns:
        Traffic information for child URLs
    """
    traffic = await api._make_request(
        "GetChildrenUrlTrafficInfo",
        "POST",
        {"siteUrl": site_url, "parentUrl": parent_url, "limit": limit},
    )
    return api._ensure_type_field(traffic, "ChildUrlTrafficInfo")


# Feed Management Enhancement
@mcp.tool(
    name="get_feed_details",
    description="Get detailed information about a specific feed.",
)
async def get_feed_details(
    site_url: Annotated[str, "The URL of the site"],
    feed_url: Annotated[str, "The URL of the feed"],
) -> Dict[str, Any]:
    """
    Get detailed information about a specific feed.

    Args:
        site_url: The URL of the site
        feed_url: The URL of the feed

    Returns:
        Detailed feed information
    """
    details = await api._make_request(
        "GetFeedDetails", params={"siteUrl": site_url, "feedUrl": feed_url}
    )
    return api._ensure_type_field(details, "FeedDetails")


@mutating_tool(name="remove_feed", description="Remove a feed from Bing Webmaster Tools.")
async def remove_feed(
    site_url: Annotated[str, "The URL of the site"],
    feed_url: Annotated[str, "The URL of the feed to remove"],
) -> Dict[str, str]:
    """
    Remove a feed from Bing Webmaster Tools.

    Args:
        site_url: The URL of the site
        feed_url: The URL of the feed to remove

    Returns:
        Success message
    """
    await api._make_request("RemoveFeed", "POST", {"siteUrl": site_url, "feedUrl": feed_url})
    return {"message": f"Feed {feed_url} removed successfully"}


# Additional Statistics
@mcp.tool(name="get_page_query_stats", description="Get query statistics for a specific page.")
async def get_page_query_stats(
    site_url: Annotated[str, "The URL of the site"],
    page: Annotated[str, "The specific page URL"],
) -> List[Dict[str, Any]]:
    """
    Get query statistics for a specific page.

    Args:
        site_url: The URL of the site
        page: The specific page URL

    Returns:
        List of query statistics for the page
    """
    stats = await api._make_request("GetPageQueryStats", params={"siteUrl": site_url, "page": page})
    return api._ensure_type_field(stats, "PageQueryStats")


@mcp.tool(
    name="get_query_traffic_stats",
    description="Get traffic statistics for queries over time.",
)
async def get_query_traffic_stats(
    site_url: Annotated[str, "The URL of the site"],
    query: Annotated[str, "The search query"],
    period: Annotated[str, "Time period (e.g., '7d', '30d')"] = "30d",
) -> Dict[str, Any]:
    """
    Get traffic statistics for queries over time.

    Args:
        site_url: The URL of the site
        query: The search query
        period: Time period (default: 30d)

    Returns:
        Traffic statistics for the query
    """
    stats = await api._make_request(
        "GetQueryTrafficStats",
        params={"siteUrl": site_url, "query": query, "period": period},
    )
    return api._ensure_type_field(stats, "QueryTrafficStats")


# Site Move Management
@mcp.tool(name="get_site_moves", description="Get history of site moves/migrations.")
async def get_site_moves(site_url: Annotated[str, "The URL of the site"]) -> List[Dict[str, Any]]:
    """
    Get history of site moves/migrations.

    Args:
        site_url: The URL of the site

    Returns:
        List of site moves
    """
    moves = await api._make_request("GetSiteMoves", params={"siteUrl": site_url})
    return api._ensure_type_field(moves, "SiteMove")


@mutating_tool(name="submit_site_move", description="Submit a site move/migration notification.")
async def submit_site_move(
    old_site_url: Annotated[str, "The old site URL"],
    new_site_url: Annotated[str, "The new site URL"],
    move_type: Annotated[str, "Type of move (e.g., 'Domain', 'Subdomain')"] = "Domain",
) -> Dict[str, str]:
    """
    Submit a site move/migration notification.

    Args:
        old_site_url: The old site URL
        new_site_url: The new site URL
        move_type: Type of move (default: Domain)

    Returns:
        Success message
    """
    await api._make_request(
        "SubmitSiteMove",
        "POST",
        {
            "oldSiteUrl": old_site_url,
            "newSiteUrl": new_site_url,
            "moveType": move_type,
        },
    )
    return {"message": f"Site move from {old_site_url} to {new_site_url} submitted"}


# Site Role Management Enhancement
@mutating_tool(name="remove_site_role", description="Remove a user's access to a site.")
async def remove_site_role(
    site_url: Annotated[str, "The URL of the site"],
    user_email: Annotated[str, "Email of the user to remove"],
) -> Dict[str, str]:
    """
    Remove a user's access to a site.

    Args:
        site_url: The URL of the site
        user_email: Email of the user to remove

    Returns:
        Success message
    """
    await api._make_request(
        "RemoveSiteRole", "POST", {"siteUrl": site_url, "userEmail": user_email}
    )
    return {"message": f"Access removed for {user_email}"}


# Country/Region Settings Enhancement
@mutating_tool(
    name="remove_country_region_settings",
    description="Remove country/region targeting settings.",
)
async def remove_country_region_settings(
    site_url: Annotated[str, "The URL of the site"],
    country_code: Annotated[str, "ISO country code to remove"],
) -> Dict[str, str]:
    """
    Remove country/region targeting settings.

    Args:
        site_url: The URL of the site
        country_code: ISO country code to remove

    Returns:
        Success message
    """
    await api._make_request(
        "RemoveCountryRegionSettings",
        "POST",
        {"siteUrl": site_url, "countryCode": country_code},
    )
    return {"message": f"Country settings for {country_code} removed successfully"}


class BearerTenantMiddleware(BaseHTTPMiddleware):
    """Bearer-token auth for the HTTP transport, with per-tenant API key/allowlist routing.

    - /health is exempt (no auth required).
    - A token matching a BWT_TENANTS entry gets that tenant's api_key/allowed_sites
      for the duration of the request (via contextvars).
    - Otherwise, a token matching MCP_AUTH_TOKEN falls through to the global
      API_KEY/ALLOWED_SITES (unscoped admin-style token).
    - Anything else is rejected with 401.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/health":
            return await call_next(request)

        auth_header = request.headers.get("authorization", "")
        token = auth_header[7:] if auth_header.lower().startswith("bearer ") else None

        tenant = TENANTS.get(token) if token else None
        if tenant:
            key_tok = _request_api_key.set(tenant["api_key"])
            sites_tok = _request_allowed_sites.set(tenant["allowed_sites"])
            try:
                return await call_next(request)
            finally:
                _request_api_key.reset(key_tok)
                _request_allowed_sites.reset(sites_tok)

        if token and MCP_AUTH_TOKEN and token == MCP_AUTH_TOKEN:
            return await call_next(request)

        return PlainTextResponse("Unauthorized", status_code=401)


def _require_http_auth_configured() -> None:
    """HTTP transport must never come up unauthenticated."""
    if not MCP_AUTH_TOKEN and not TENANTS:
        raise ValueError(
            "HTTP transport requires MCP_AUTH_TOKEN and/or BWT_TENANTS to be configured"
        )


def run_http() -> None:
    """Run the MCP server over Streamable HTTP with bearer auth."""
    _require_http_auth_configured()

    import uvicorn

    http_app = mcp.streamable_http_app()
    http_app.add_middleware(BearerTenantMiddleware)
    config = uvicorn.Config(
        http_app,
        host=mcp.settings.host,
        port=mcp.settings.port,
        log_level=mcp.settings.log_level.lower(),
    )
    uvicorn.Server(config).run()


def app() -> None:
    """MCP server entrypoint."""
    transport = os.getenv("MCP_TRANSPORT", "stdio").strip().lower()
    # A pure-tenants HTTP deployment (BWT_TENANTS set, no MCP_AUTH_TOKEN) never
    # falls through to the global API_KEY, so it doesn't need one configured.
    pure_tenant_http = transport == "http" and TENANTS and not MCP_AUTH_TOKEN
    if not API_KEY and not pure_tenant_http:
        raise ValueError("BING_WEBMASTER_API_KEY environment variable is required")
    if transport == "stdio":
        logger.info("Starting Bing Webmaster MCP server (stdio)")
        mcp.run(transport="stdio")
    elif transport == "http":
        logger.info("Starting Bing Webmaster MCP server (http)")
        run_http()
    else:
        raise ValueError(f"Invalid MCP_TRANSPORT {transport!r}; expected 'stdio' or 'http'")


if __name__ == "__main__":
    app()
