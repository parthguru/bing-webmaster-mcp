# Bing Webmaster Tools MCP Server

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)

An MCP (Model Context Protocol) server that provides access to Bing Webmaster Tools functionality through Claude and other MCP-compatible AI assistants.

This is a hardened fork of [isiahw1/mcp-server-bing-webmaster](https://github.com/isiahw1/mcp-server-bing-webmaster) (MIT License, copyright (c) 2025 Isiah Wheeler — see [LICENSE](LICENSE)), maintained by Synectus for internal agency use across multiple client Bing Webmaster accounts. All credit for the original implementation and the full BWT API surface goes to the upstream author; this fork adds the production-hardening described below on top of it.

## Changes from upstream

The upstream server runs over stdio only, with all 60 tools always registered against a single global API key and no access controls. This fork adds:

- **Read-only by default** (`BWT_READ_ONLY`, defaults `true`). The 26 tools that mutate state on Bing's side (`submit_url`, `add_site`, `remove_sitemap`, crawl settings, site roles, etc.) simply don't register as MCP tools under the default — an agent can't see or attempt to call a tool it can't use. Set `BWT_READ_ONLY=false` to register all 60.
- **Site allowlist** (`BWT_ALLOWED_SITES`), enforced centrally in the API client before any HTTP call reaches Bing, not per-tool. A `site_url`-shaped argument for a site outside the allowlist raises immediately, on both read and write tools.
- **Streamable HTTP transport** alongside the original stdio, selected by `MCP_TRANSPORT=stdio|http`. HTTP binds `0.0.0.0:$PORT` (default `8080`) and exposes `GET /health` (200, unauthenticated) for hosting platforms.
- **Bearer auth with per-tenant API key routing** for HTTP. A single `MCP_AUTH_TOKEN` covers the simple case (one deployment, one Bing account). `BWT_TENANTS` covers this agency's actual shape — one shared deployment serving multiple client sites, several under different Bing Webmaster accounts — by mapping each client's own bearer token to its own API key and site allowlist. The key is bound to the authenticated connection, not looked up per tool call, because tools like `get_sites` (no site argument) and `add_site` (the site isn't in any map yet — it's being created) have no site to route a lookup on.
- **Production Docker image**: `python:3.13-slim`, uv-based build, non-root user. Replaces the previous `mcp-proxy`/Glama.ai scaffolding, which cloned from GitHub at build time instead of using the local tree.
- Renamed package (`synectus-bing-webmaster-mcp` on PyPI-style naming, `@synectus/bing-webmaster-mcp` for npm), version reset to `0.1.0` to mark this as a new release line separate from upstream's versioning.

None of this changes what the 60 tools do against the Bing Webmaster API — only what's registered, what's reachable, and how the server is deployed.

## Installation

### Prerequisites
- Python 3.10+ ([python.org](https://python.org/downloads/))
- [uv](https://docs.astral.sh/uv/) for local/dev use, or Docker for hosted deployment
- A Bing Webmaster API key ([Settings → API Access](https://www.bing.com/webmasters/configure/apikey)) — one per Bing Webmaster account you intend to expose

### Local development install
```bash
git clone <this-repo-url>
cd bing-webmaster-mcp
uv sync
```
This resolves against the pins in `pyproject.toml` (notably `mcp[cli]>=1.10.0,<2.0.0` — the `mcp` SDK's 2.x line renamed `FastMCP` and removed the module this server is built on, so don't lift that upper bound without adapting the code). No `uv.lock` is committed; `uv sync` resolves fresh each time.

### npm package
This fork is not yet published to npm under `@synectus/bing-webmaster-mcp` — `package.json` and the version-sync tooling are in place, but publishing is a separate decision (the existing `.github/workflows/publish.yml` still targets the old `@isiahw1/...` package name and needs updating before it's used). Until that's resolved, use the local `uv` install above or the Docker image.

## Configuration

Every variable below can be set directly in the environment or via a `.env` file (see `.env.example` for a fully commented copy).

| Variable | Default | Applies to | Purpose |
|---|---|---|---|
| `BING_WEBMASTER_API_KEY` | *(required*)* | both | Bing Webmaster API key. Required for stdio; required for HTTP unless running a pure `BWT_TENANTS`-only deployment with no `MCP_AUTH_TOKEN`. |
| `BWT_READ_ONLY` | `true` | both | `false`/`0`/`no` (case-insensitive) registers all 60 tools including the 26 mutating ones; anything else, including unset, keeps only the 34 read tools. |
| `BWT_ALLOWED_SITES` | *(empty = allow all)* | both | Comma-separated site origins. A `site_url` outside this list is rejected before any Bing API call. |
| `MCP_TRANSPORT` | `stdio` | both | `stdio` or `http`. Anything else fails at startup. |
| `PORT` | `8080` | http | Bound on `0.0.0.0`. |
| `MCP_AUTH_TOKEN` | *(unset)* | http | Single static bearer token, routes to the global `BING_WEBMASTER_API_KEY`/`BWT_ALLOWED_SITES` above. |
| `BWT_TENANTS` | *(unset)* | http | JSON object mapping bearer token → `{"api_key": "...", "allowed_sites": [...]}` for multi-client deployments. See below. |

At least one of `MCP_AUTH_TOKEN` / `BWT_TENANTS` must be set for HTTP transport to start — it fails closed rather than serving unauthenticated.

### Local (stdio)

```bash
export BING_WEBMASTER_API_KEY=your_api_key_here
uv run mcp-server-bing-webmaster
```

Claude Desktop / Claude Code config:
```json
{
  "mcpServers": {
    "bing-webmaster": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/bing-webmaster-mcp", "mcp-server-bing-webmaster"],
      "env": {
        "BING_WEBMASTER_API_KEY": "your_api_key_here"
      }
    }
  }
}
```

### Remote (Streamable HTTP)

Run the server (or the [Docker image](#docker)) with `MCP_TRANSPORT=http`, then point any MCP client that supports remote Streamable HTTP servers with a custom header at it:

```json
{
  "mcpServers": {
    "bing-webmaster": {
      "type": "http",
      "url": "https://your-deployment-host/mcp",
      "headers": {
        "Authorization": "Bearer your_token_here"
      }
    }
  }
}
```

Single-tenant example (one Bing account):
```bash
export BING_WEBMASTER_API_KEY=your_api_key_here
export MCP_TRANSPORT=http
export MCP_AUTH_TOKEN=a_long_random_token
uv run mcp-server-bing-webmaster
```

Multi-tenant example (one deployment, several client sites/accounts — each client gets their own bearer token, mapped to their own Bing API key and allowed sites):
```bash
export MCP_TRANSPORT=http
export BWT_TENANTS='{"tok_client1":{"api_key":"CLIENT1_BING_KEY","allowed_sites":["https://client1.com"]},"tok_client2":{"api_key":"CLIENT2_BING_KEY","allowed_sites":["https://client2.com"]}}'
uv run mcp-server-bing-webmaster
```

Health check (no auth required):
```bash
curl https://your-deployment-host/health
```

### Docker

```bash
docker build -t bing-webmaster-mcp .
docker run -p 8080:8080 \
  -e MCP_AUTH_TOKEN=a_long_random_token \
  -e BING_WEBMASTER_API_KEY=your_api_key_here \
  bing-webmaster-mcp
```
The image defaults `MCP_TRANSPORT=http` and binds `0.0.0.0:8080`. Runs as a non-root user; `GET /health` is wired up as the container `HEALTHCHECK`.

## Available Tools

34 read tools are always registered. The 26 write/mutating tools below are marked **write** and require `BWT_READ_ONLY=false` to appear in `tools/list` at all.

### Site Management
- `get_sites` - List all verified sites in your account
- `add_site` **write** - Add a new site to Bing Webmaster Tools
- `verify_site` **write** - Verify ownership of a site
- `remove_site` **write** - Remove a site from your account
- `get_site_roles` - Get list of users with access to the site
- `add_site_roles` **write** - Delegate site access to another user
- `remove_site_role` **write** - Revoke a user's site access

### Traffic Analysis
- `get_query_stats` - Get search query performance data
- `get_page_stats` - Get page-level traffic statistics
- `get_rank_and_traffic_stats` - Get overall ranking and traffic data
- `get_query_page_stats` - Get detailed traffic statistics for a specific query
- `get_query_page_detail_stats` - Get statistics for specific query-page combinations
- `get_url_traffic_info` - Get traffic information for specific URLs
- `get_children_url_traffic_info` - Get traffic information for child URLs
- `get_page_query_stats` - Get query stats for one page
- `get_query_traffic_stats` - Get traffic-over-time for a query

### Crawling & Indexing
- `get_crawl_stats` - View crawl statistics and bot activity
- `get_crawl_issues` - Get crawl errors and issues
- `get_crawl_settings` - Get crawl settings for a site
- `update_crawl_settings` **write** - Update crawl settings (slow/normal/fast)
- `get_url_info` - Get detailed index information for a specific URL
- `get_children_url_info` - Get information about child URLs under a parent URL
- `fetch_url` **write** - Request Bing crawl a specific URL (consumes quota)
- `get_fetched_urls` - List URLs previously fetched
- `get_fetched_url_details` - Details of a fetched URL

### URL Management
- `submit_url` **write** - Submit a single URL for indexing
- `submit_url_batch` **write** - Submit multiple URLs at once
- `get_url_submission_quota` - Check your URL submission limits

### Content Submission
- `submit_content` **write** - Submit page content directly without crawling
- `get_content_submission_quota` - Get content submission quota information

### Sitemaps & Feeds
- `submit_sitemap` **write** - Submit a new sitemap
- `remove_sitemap` **write** - Remove a sitemap
- `remove_feed` **write** - Remove a feed
- `get_feeds` - Get all RSS/Atom feeds for a site
- `get_feed_details` - Details of one feed

### Keyword Analysis
- `get_keyword_data` - Get detailed data for specific keywords
- `get_related_keywords` - Find related search terms
- `get_keyword_stats` - Get historical statistics for a specific keyword

### Link Analysis
- `get_link_counts` - Get inbound link statistics
- `get_url_links` - Get inbound links for specific site URL (requires link and page parameters)
- `add_connected_page` **write** - Add a page that has a link to your website
- `get_connected_pages` - List pages linking to the site

### Content Blocking
- `get_blocked_urls` - View blocked URLs
- `add_blocked_url` **write** - Block URLs from crawling
- `remove_blocked_url` **write** - Unblock URLs

### Deep Link Management
- `get_deep_link_blocks` - Get list of blocked deep links
- `add_deep_link_block` **write** - Block deep links for specific URL patterns
- `remove_deep_link_block` **write** - Remove a deep link block

### URL Parameters
- `get_query_parameters` - Get URL normalization parameters (may require special permissions)
- `add_query_parameter` **write** - Add URL normalization parameter
- `remove_query_parameter` **write** - Remove a URL normalization parameter
- `enable_disable_query_parameter` **write** - Toggle a query param on/off

### Geographic Settings
- `get_country_region_settings` - Get country/region targeting settings (may require special permissions)
- `add_country_region_settings` **write** - Add country/region targeting settings
- `remove_country_region_settings` **write** - Remove country/region targeting settings

### Page Preview Management
- `add_page_preview_block` **write** - Add a page preview block to prevent rich snippets
- `get_active_page_preview_blocks` - Get list of active page preview blocks
- `remove_page_preview_block` **write** - Remove a page preview block

### Site Migration
- `get_site_moves` - Get history of site moves/migrations
- `submit_site_move` **write** - Submit a site move/migration notification (validates both the old and new site against the allowlist)

## Usage Examples

Once configured, you can use these tools in Claude:

```
"Show me all my verified sites in Bing Webmaster Tools"
"What are the top search queries for example.com?"
"Show me crawl errors for my site"
"What's my daily URL submission quota?"
"Get detailed stats for the query 'best products' on my site"
"Show me traffic info for my top 10 pages"
"Get historical data for the keyword 'seo tools'"
```

Anything that mutates state (`"Submit this URL for indexing"`, `"Add a new site"`, `"Block this URL from crawling"`) requires `BWT_READ_ONLY=false` — under the default, those tools aren't visible to the agent at all.

## Development

```bash
uv sync
uv run pytest tests/ -v
```

The test suite (`tests/test_read_only_gate.py`, `tests/test_site_allowlist.py`, `tests/test_http_transport.py`) covers the read-only gate, the site allowlist (including origin normalization), and the HTTP transport's auth/tenant-routing behavior — including an end-to-end check that a tenant's API key actually reaches `BingWebmasterAPI._make_request` during a real `tools/call`, not just at the auth middleware layer.

## Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

## License

MIT License — see [LICENSE](LICENSE). Original work copyright (c) 2025 Isiah Wheeler.

## Support

For issues with the upstream BWT tool implementations, see the [original repository](https://github.com/isiahw1/mcp-server-bing-webmaster). For issues with the hardening in this fork (read-only gate, allowlist, HTTP transport, tenant routing, Docker), open an issue in this repository.
