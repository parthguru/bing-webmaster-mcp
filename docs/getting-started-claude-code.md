# Getting Started with Claude Code

This guide will help you set up the Bing Webmaster Tools MCP server with Claude Code.

## Prerequisites

- Claude Code CLI installed ([Download here](https://claude.ai/code))
- Python 3.10+ ([python.org](https://python.org/downloads/))
- [uv](https://docs.astral.sh/uv/) installed
- Bing Webmaster API key ([Get it here](https://www.bing.com/webmasters))

## Setup Steps

### 1. Install the server locally

```bash
git clone <this-repo-url>
cd bing-webmaster-mcp
uv sync
```

### 2. Add the MCP Server

Choose the option that matches how you're connecting.

#### Option A: Local (stdio)
Add the server with your API key in one command:
```bash
claude mcp add bing-webmaster -e BING_WEBMASTER_API_KEY=your_api_key_here -- uv run --directory /path/to/bing-webmaster-mcp mcp-server-bing-webmaster
```
Replace `/path/to/bing-webmaster-mcp` with the absolute path to your local clone.

#### Option B: Remote (Streamable HTTP)
If you're connecting to a hosted deployment of this server instead of running it locally:
```bash
claude mcp add --transport http bing-webmaster https://your-deployment-host/mcp --header "Authorization: Bearer your_token_here"
```
Replace the URL and token with the values for your deployment (see the [README](../README.md#remote-streamable-http) for how a deployment issues tokens).

### 3. Launch Claude Code

```bash
claude
```

## Verify Installation

Once launched, try these commands to verify the setup:

```
"Show me all my sites in Bing Webmaster Tools"
"What's my URL submission quota?"
```

## Troubleshooting

### Enable Debug Mode
If you're experiencing issues, run Claude Code with debug logging:
```bash
claude --mcp-debug
```

### Common Issues

**"Cannot find MCP server" error:**
- Ensure you've run the `claude mcp add` command
- For local (stdio) setups, check that `uv` is installed: `uv --version`

**"Invalid API key" error:**
- Verify your API key is correct
- Make sure the environment variable is set: `echo $BING_WEBMASTER_API_KEY`

**"spawn ENOENT" error:**
- This usually means `uv` can't be found
- Ensure `uv` is installed and in your PATH

**Asked Claude to submit a URL / add a site / do anything else that changes data, and no matching tool exists:**
- The server registers only its 34 read tools by default (`BWT_READ_ONLY` defaults to `true`). The 26 mutating tools — `submit_url`, `add_site`, `submit_sitemap`, and others — simply aren't in the tool list under this default, so Claude can't see or call them.
- To enable them, add `BWT_READ_ONLY=false` to the server's environment (another `-e` flag on `claude mcp add`, or in your `.env` file), then restart Claude Code.

### Checking Logs
Look for messages like:
```
MCP server "bing-webmaster" Server stderr: Starting Bing Webmaster MCP server...
```

## Advanced Usage

### Using a Specific Git Revision
To pin to a specific commit or tag of this repo instead of tracking your local checkout's current state, check it out before running `uv sync`:
```bash
git checkout <tag-or-commit>
uv sync
```
The `claude mcp add` command above always runs whatever is checked out at `--directory`.

### Restricting to Specific Sites
Add `BWT_ALLOWED_SITES` as another `-e` flag to restrict the server to a comma-separated list of site origins — a `site_url` outside the list is rejected before any Bing API call:
```bash
claude mcp add bing-webmaster \
  -e BING_WEBMASTER_API_KEY=your_api_key_here \
  -e BWT_ALLOWED_SITES=https://example.com,https://example2.com \
  -- uv run --directory /path/to/bing-webmaster-mcp mcp-server-bing-webmaster
```

### Multiple Environment Variables
You can pass multiple environment variables using multiple `-e` flags:
```bash
claude mcp add bing-webmaster \
  -e BING_WEBMASTER_API_KEY=your_api_key_here \
  -e BWT_READ_ONLY=false \
  -- uv run --directory /path/to/bing-webmaster-mcp mcp-server-bing-webmaster
```

## Next Steps

- Explore the [full list of available tools](../README.md#available-tools)
- Check out [usage examples](../README.md#usage-examples)
- Learn about [API quotas and limits](https://www.bing.com/webmaster/help/webmaster-api-limits)

## Support

If you encounter issues:
1. Check the [troubleshooting section](#troubleshooting)
2. Review [GitHub Issues](https://github.com/isiahw1/mcp-server-bing-webmaster/issues) for the upstream implementation, or open an issue in this repository for issues with this fork's hardening
3. Open a new issue with debug logs
