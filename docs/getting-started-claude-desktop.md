# Getting Started with Claude Desktop

This guide will help you set up the Bing Webmaster Tools MCP server with Claude Desktop.

## Prerequisites

- Claude Desktop installed ([Download here](https://claude.ai/download))
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

### 2. Open Claude Desktop Settings

1. Launch Claude Desktop
2. Click on `Claude` menu → `Settings`
3. Select `Developer` from the sidebar
4. Click the `Edit Config` button

### 3. Add the MCP Server Configuration

Choose the option that matches how you're connecting.

#### Option A: Local (stdio)
Add the following to your `claude_desktop_config.json` file:
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
Replace `/path/to/bing-webmaster-mcp` with the absolute path to your local clone, and `your_api_key_here` with your actual Bing Webmaster API key.

#### Option B: Remote (Streamable HTTP)
If you're connecting to a hosted deployment of this server instead of running it locally:
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
Replace the URL and token with the values for your deployment (see the [README](../README.md#remote-streamable-http) for how a deployment issues tokens).

### 4. Save and Restart

1. Save the configuration file
2. Completely quit Claude Desktop (Cmd+Q on macOS, Alt+F4 on Windows)
3. Restart Claude Desktop

## Configuration File Locations

- **macOS**: `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows**: `%APPDATA%\Claude\claude_desktop_config.json`
- **Linux**: `~/.config/Claude/claude_desktop_config.json`

## Verify Installation

After restarting, you should see:
- The MCP server icon in the bottom of your chat interface
- "bing-webmaster" listed when you click on the MCP icon

Try these commands to verify:
```
"Show me all my sites in Bing Webmaster Tools"
"What are my crawl statistics?"
```

## Troubleshooting

### "Could not attach to MCP server" Error

1. **Check the logs:**
   - Go to Settings → Developer
   - Click "Open Logs Folder"
   - Look for error messages in the most recent log file

2. **Common causes:**
   - Incorrect API key
   - Missing `uv` installation (for local/stdio setups)
   - Syntax error in the JSON configuration

### "spawn uv ENOENT" Error

This means `uv` is not found. Solutions:
1. Install `uv` following the [official instructions](https://docs.astral.sh/uv/getting-started/installation/)
2. Ensure `uv` is in your system PATH
3. Restart Claude Desktop after installing `uv`

### API Key Issues

1. Verify your API key:
   - Log in to [Bing Webmaster Tools](https://www.bing.com/webmasters)
   - Go to Settings → API Access
   - Regenerate key if needed

2. Check for typos in the configuration
3. Ensure the API key is enclosed in quotes

### Asked Claude to submit a URL / add a site / do anything else that changes data, and nothing happens

The server registers only its 34 read tools by default (`BWT_READ_ONLY` defaults to `true`). The 26 mutating tools — `submit_url`, `add_site`, `submit_sitemap`, and others — simply aren't in the tool list under this default, so Claude can't see or call them.

To enable them, add `"BWT_READ_ONLY": "false"` to the server's `env` block in your config, then restart Claude Desktop:
```json
{
  "mcpServers": {
    "bing-webmaster": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/bing-webmaster-mcp", "mcp-server-bing-webmaster"],
      "env": {
        "BING_WEBMASTER_API_KEY": "your_api_key_here",
        "BWT_READ_ONLY": "false"
      }
    }
  }
}
```

## Advanced Configuration

### Using a Specific Git Revision
To pin to a specific commit or tag of this repo instead of tracking your local checkout's current state, check it out before running `uv sync`:
```bash
git checkout <tag-or-commit>
uv sync
```
The `command`/`args` above always run whatever is checked out at the `--directory` path.

### Restricting to Specific Sites
Add `BWT_ALLOWED_SITES` to the `env` block to restrict the server to a comma-separated list of site origins — a `site_url` outside the list is rejected before any Bing API call:
```json
{
  "mcpServers": {
    "bing-webmaster": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/bing-webmaster-mcp", "mcp-server-bing-webmaster"],
      "env": {
        "BING_WEBMASTER_API_KEY": "your_api_key_here",
        "BWT_ALLOWED_SITES": "https://example.com,https://example2.com"
      }
    }
  }
}
```

### Using Environment Variables
If you prefer not to store your API key in the config, set it in your shell profile:
```bash
export BING_WEBMASTER_API_KEY="your_api_key_here"
```
Note that Claude Desktop launches from your OS's GUI environment rather than a shell, so this only works reliably if your OS is configured to pass shell-exported variables through to GUI apps. If in doubt, use the `env` block in the config as shown above.

### Multiple MCP Servers
You can run multiple MCP servers simultaneously:
```json
{
  "mcpServers": {
    "bing-webmaster": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/bing-webmaster-mcp", "mcp-server-bing-webmaster"],
      "env": {
        "BING_WEBMASTER_API_KEY": "your_api_key_here"
      }
    },
    "another-server": {
      "command": "npx",
      "args": ["another-mcp-server"]
    }
  }
}
```

## Next Steps

- Explore the [full list of available tools](../README.md#available-tools)
- Check out [usage examples](../README.md#usage-examples)
- Learn about [API quotas and limits](https://www.bing.com/webmaster/help/webmaster-api-limits)

## Support

If you encounter issues:
1. Check the [troubleshooting section](#troubleshooting)
2. Review the logs in Settings → Developer → Open Logs Folder
3. Check [GitHub Issues](https://github.com/isiahw1/mcp-server-bing-webmaster/issues) for the upstream implementation, or open an issue in this repository for issues with this fork's hardening
4. Open a new issue with your configuration and error logs
