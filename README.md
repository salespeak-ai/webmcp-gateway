# WebMCP Gateway

**Bridge AI assistants to any website with WebMCP tools — via a headless browser.**

WebMCP Gateway is an [MCP server](https://modelcontextprotocol.io/) that lets Claude, ChatGPT, and other AI assistants interact with websites that expose tools through the [WebMCP standard](https://developer.chrome.com/blog/webmcp-epp). Behind the scenes, a headless Playwright browser navigates to the website, discovers registered tools, and calls them on behalf of the AI.

```
┌──────────────┐      MCP       ┌──────────────────┐   Playwright   ┌──────────────────┐
│  Claude /     │ ◄───────────► │  WebMCP Gateway  │ ◄────────────► │  Any Website     │
│  ChatGPT /    │   (stdio/sse) │  (this server)   │   (headless)   │  with WebMCP     │
│  Any MCP      │               │                  │                │  tools           │
│  Client       │               │                  │                │                  │
└──────────────┘               └──────────────────┘               └──────────────────┘
```

## Why?

The web is full of websites with chat widgets, search tools, booking forms, and knowledge bases. But AI assistants can't use them — they're locked behind HTML and JavaScript.

**WebMCP** is an emerging standard (see [Chrome's WebMCP proposal](https://developer.chrome.com/blog/webmcp-epp)) where websites declare their capabilities via `navigator.modelContext.registerTool()`. This lets AI agents know what a site can do and how to interact with it — without scraping or reverse-engineering.

**WebMCP Gateway** bridges the gap:

- **Websites** register tools using the WebMCP standard (declarative HTML forms or imperative JavaScript)
- **This gateway** discovers those tools via a headless browser and exposes them as MCP tools
- **AI assistants** call the MCP tools, and the gateway executes them on the website

This opens the door to **"ask the website"** use cases:

| Use Case | Example |
|----------|---------|
| Customer support | "What's the refund policy on example-store.com?" |
| Product discovery | "Find flights under $500 on travel-site.com" |
| Knowledge bases | "How do I configure SSO on docs.saas-tool.com?" |
| Booking & forms | "Book a demo on vendor-site.com for next Tuesday" |
| Price comparison | "Compare pricing across site-a.com and site-b.com" |

## Quick Start

### Install

```bash
# Clone and install
git clone https://github.com/salespeak-ai/webmcp-gateway.git
cd webmcp-gateway
pip install -e .

# Install Playwright browsers (one-time)
playwright install chromium
```

### Use with Claude Desktop

Add to your Claude Desktop config (`~/Library/Application Support/Claude/claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "webmcp-gateway": {
      "command": "webmcp-gateway",
      "args": ["--transport", "stdio"]
    }
  }
}
```

Then ask Claude: *"Check if example.com has WebMCP tools and ask them about their pricing"*

### Use with Claude Code

Add to your `.mcp.json`:

```json
{
  "mcpServers": {
    "webmcp-gateway": {
      "command": "webmcp-gateway",
      "args": ["--transport", "stdio"]
    }
  }
}
```

### Run as SSE Server (for remote clients)

```bash
webmcp-gateway --transport sse --port 8808
```

## Tools

The gateway exposes three MCP tools:

### `check_webmcp(url)`

Fast HTTP-based check — no browser needed. Scans HTML for WebMCP signals:
- `<form tool-name="...">` declarative forms
- `navigator.modelContext.registerTool()` in inline scripts
- Generic `navigator.modelContext` references

```
> check_webmcp("https://example.com")
{
  "webmcp_detected": true,
  "provider": "webmcp_declarative",
  "tools": [{"name": "ask_question", "description": "Ask about our products"}]
}
```

### `discover_tools(url)`

Full browser-based discovery. Launches Chromium, navigates to the URL, and intercepts all `registerTool()` calls to get complete tool definitions with input schemas.

```
> discover_tools("https://example.com")
{
  "tools_count": 2,
  "tools": [
    {
      "name": "ask_question",
      "description": "Ask a question",
      "input_schema": {"properties": {"question": {"type": "string"}}}
    },
    {
      "name": "search_products",
      "description": "Search catalog",
      "input_schema": {"properties": {"category": {"type": "string"}, "max_price": {"type": "number"}}}
    }
  ]
}
```

### `call_tool(url, question?, tool_name?, tool_args?)`

Call a WebMCP tool on a website. For simple Q&A tools, just pass `question`. For structured tools, pass `tool_name` and `tool_args` as JSON.

```
> call_tool("https://example.com", question="What's your pricing?")
{
  "success": true,
  "tool_name": "ask_question",
  "answer": "Our pricing starts at $99/mo for Pro..."
}
```

## How WebMCP Works

Websites opt into AI interaction by registering tools via the WebMCP standard:

### Declarative (HTML)

```html
<form tool-name="ask_question" tool-description="Ask about our products">
  <input name="question" type="text" />
</form>
```

### Imperative (JavaScript)

```javascript
navigator.modelContext.registerTool({
  name: "ask_question",
  description: "Ask a question about our products",
  inputSchema: {
    type: "object",
    properties: {
      question: { type: "string", description: "The question to ask" }
    },
    required: ["question"]
  },
  execute: async (args) => {
    const answer = await myAPI.getAnswer(args.question);
    return { content: [{ type: "text", text: answer }] };
  }
});
```

The gateway discovers these tools by injecting an interception script **before** page load that hooks into `navigator.modelContext.registerTool()`, capturing tool definitions and their `execute` callbacks.

### How `execute()` Actually Waits for the Response

The critical detail: **`execute()` returns a Promise that only resolves when the actual answer is ready.** The gateway calls `await tool.execute(args)` and the headless browser waits for this Promise to resolve — whether it takes 1 second or 60.

For a simple API-backed tool, this is straightforward:

```javascript
execute: async ({ question }) => {
  const resp = await fetch('/api/chat', { method: 'POST', body: JSON.stringify({ question }) });
  const data = await resp.json();
  return { content: [{ type: 'text', text: data.answer }] };  // Promise resolves here
}
```

For **iframe-based chat widgets** (the most common real-world pattern), the flow uses postMessage with call correlation:

```
execute() called                                    ┐
  ├─ Generate unique callId                         │  Promise
  ├─ Set up window.message listener                 │  is
  ├─ Set 60s timeout                                │  PENDING
  ├─ postMessage({ question, callId }) → iframe     │
  │                                                 │
  │   iframe processes question via LLM...          │
  │                                                 │
  │   iframe posts back:                            │
  │   { botResponse: "answer", callId }             │
  │                                                 │
  ├─ message listener matches callId                │
  └─ resolve({ content: [{ text: answer }] })       ┘  Promise RESOLVES
```

The gateway's headless browser captures this entire async chain — it doesn't just "send a message", it **waits for the actual AI response** before returning.

See [docs/WEBMCP_PROTOCOL.md](docs/WEBMCP_PROTOCOL.md) for the complete protocol specification with code examples.

## Architecture

```
src/webmcp_gateway/
├── __init__.py      # Package version
├── __main__.py      # python -m webmcp_gateway
├── cli.py           # CLI entry point (stdio/sse transport)
├── server.py        # MCP server with 3 tools (check, discover, call)
├── detect.py        # Fast HTTP-based WebMCP detection (no browser)
└── browser.py       # Playwright-based discovery & tool invocation
```

**Detection chain:**
1. **Fast HTTP scan** (`detect.py`) — regex over HTML, instant, no browser
2. **Browser discovery** (`browser.py`) — Playwright + JS interception, finds dynamic tools
3. **Tool invocation** (`browser.py`) — calls `tool.execute(args)` in the page context

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"
playwright install chromium

# Run tests
pytest

# Lint
ruff check src/ tests/
```

## For Website Owners

Want your site to work with WebMCP Gateway? Add tools to your site:

```html
<script>
// Wait for WebMCP support
const check = setInterval(() => {
  if (!navigator.modelContext) return;
  clearInterval(check);

  navigator.modelContext.registerTool({
    name: "ask_question",
    description: "Ask a question about [Your Company]",
    inputSchema: {
      type: "object",
      properties: {
        question: { type: "string", description: "The question to ask" }
      },
      required: ["question"]
    },
    execute: async ({ question }) => {
      // Connect to your backend / knowledge base / chatbot
      const response = await fetch('/api/chat', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ question })
      });
      const data = await response.json();
      return { content: [{ type: "text", text: data.answer }] };
    }
  });
}, 50);
</script>
```

That's it — any AI assistant using WebMCP Gateway can now interact with your site.

## License

MIT
