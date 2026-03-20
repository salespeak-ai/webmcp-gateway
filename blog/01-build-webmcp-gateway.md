# Build a WebMCP Gateway: Let AI Assistants Talk to Any Website

*How we built an open-source MCP server that bridges Claude and ChatGPT to websites using a headless browser*

---

Every website has knowledge locked behind its UI — pricing pages, knowledge bases, chat widgets, booking forms. AI assistants like Claude and ChatGPT can't access any of it. They can't click buttons, fill forms, or wait for a chat widget to respond.

**WebMCP** changes that. It's a [new browser standard](https://developer.chrome.com/blog/webmcp-epp) where websites declare what AI agents can do via `navigator.modelContext.registerTool()`. Think of it as an API that lives in the browser — no backend integration needed.

But there's a gap: AI assistants speak **MCP** (Model Context Protocol), websites speak **WebMCP**, and nobody translates between them.

So we built [**WebMCP Gateway**](https://github.com/salespeak-ai/webmcp-gateway) — an open-source MCP server that uses a headless Playwright browser to discover WebMCP tools on any website and exposes them as MCP tools that Claude and ChatGPT can call directly.

```
┌──────────────┐      MCP       ┌──────────────────┐   Playwright   ┌──────────────────┐
│  Claude /     │ ◄───────────► │  WebMCP Gateway  │ ◄────────────► │  Any Website     │
│  ChatGPT      │   (stdio/sse) │  (MCP server)    │   (headless)   │  with WebMCP     │
└──────────────┘               └──────────────────┘               └──────────────────┘
```

This post walks through exactly how it works, how to set it up, and how to extend it.

---

## The Problem: Two Protocols, No Bridge

**MCP** (Model Context Protocol) is how AI assistants discover and call tools. Claude Desktop, Claude Code, ChatGPT plugins — they all speak MCP. An MCP server exposes tools like `search_docs(query)` or `create_ticket(title, body)`, and the AI calls them.

**WebMCP** is how websites expose capabilities to AI agents running in the browser. A website registers tools via JavaScript:

```javascript
navigator.modelContext.registerTool({
  name: "ask_question",
  description: "Ask about our products",
  execute: async ({ question }) => {
    const answer = await callOurAPI(question);
    return { content: [{ type: "text", text: answer }] };
  }
});
```

The problem: these two worlds don't talk to each other. Claude can't just "visit a website" and discover its WebMCP tools. There's no browser in the loop.

**The gateway is that browser.**

---

## Architecture: How It Works

The gateway is a Python MCP server (~300 lines of core code) built on three layers:

### Layer 1: Fast Detection (`detect.py`)

Before launching a browser, we do a quick HTTP fetch and scan the HTML with regex:

```python
# Check for declarative forms: <form tool-name="ask_question">
form_tags = re.findall(r'<form\b([^>]*\btool-name=[^>]*)>', html, re.I)

# Check for imperative registration in inline scripts
register_matches = re.findall(
    r'(?:navigator\.modelContext|modelContext)\.registerTool\s*\('
    r'\s*\{[^}]*name\s*:\s*["\']([^"\']+)["\']',
    html,
)

# Check for any modelContext reference
if re.search(r"navigator\.modelContext", html):
    return DetectionResult(found=True, provider="webmcp_generic")
```

This is instant and free — no browser, no overhead. It tells you "yes, this site probably has WebMCP tools" without committing to a full browser session.

### Layer 2: Browser Discovery (`browser.py`)

The real magic. We launch headless Chromium via Playwright and inject a script **before the page loads** that intercepts `registerTool()`:

```javascript
// Injected BEFORE page load via page.add_init_script()
(() => {
    window.__webmcp_tools = {};
    window.__webmcp_ready = false;

    const hookInterval = setInterval(() => {
        if (!navigator.modelContext) return;
        clearInterval(hookInterval);

        // Monkey-patch registerTool to capture definitions
        const original = navigator.modelContext.registerTool.bind(
            navigator.modelContext
        );
        navigator.modelContext.registerTool = (toolDef) => {
            window.__webmcp_tools[toolDef.name] = {
                name: toolDef.name,
                description: toolDef.description,
                inputSchema: toolDef.inputSchema || {},
                execute: toolDef.execute,  // Keep the callback!
            };
            original(toolDef);  // Still register it normally
        };
        window.__webmcp_ready = true;
    }, 50);
})();
```

The key detail: we store the `execute` callback. This is the function we'll call later to actually invoke the tool.

On the Python side:

```python
async with async_playwright() as p:
    browser = await p.chromium.launch(
        headless=True,
        args=["--enable-features=WebMCP"],
    )
    page = await (await browser.new_context()).new_page()

    # Inject BEFORE page load
    await page.add_init_script(_INTERCEPT_SCRIPT)

    # Navigate
    await page.goto(url, wait_until="domcontentloaded")

    # Wait for tools to register (15s timeout)
    await page.wait_for_function(
        "window.__webmcp_ready === true && "
        "Object.keys(window.__webmcp_tools).length > 0"
    )

    # Read discovered tools
    tools = await page.evaluate("""
        Object.values(window.__webmcp_tools).map(t => ({
            name: t.name,
            description: t.description,
            inputSchema: t.inputSchema,
        }))
    """)
```

### Layer 3: Tool Invocation

Once we have tools, calling them is one line of JavaScript — but the semantics matter:

```javascript
// This is what the gateway runs in the browser
const result = await tool.execute(args);
```

That `await` is critical. The tool's `execute()` returns a **Promise** that stays pending until the actual answer is ready. For a chat widget backed by an LLM, that could be 5-30 seconds. The gateway waits for the real answer, not just a "message sent" confirmation.

We parse whatever comes back:

```javascript
let answer = '';
if (typeof result === 'string') {
    answer = result;
} else if (result?.content && Array.isArray(result.content)) {
    // MCP content format: { content: [{ type: 'text', text: '...' }] }
    answer = result.content
        .filter(c => c.type === 'text')
        .map(c => c.text)
        .join('\n');
} else if (result && typeof result === 'object') {
    answer = JSON.stringify(result);
}
```

### Layer 4: MCP Server (`server.py`)

All three layers are wrapped in a FastMCP server that exposes three tools:

```python
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("WebMCP Gateway")

@mcp.tool()
def check_webmcp(url: str) -> str:
    """Fast HTTP check — does this URL have WebMCP tools?"""
    result = detect_webmcp_fast(url)
    return json.dumps({ "webmcp_detected": result.found, ... })

@mcp.tool()
def discover_tools(url: str) -> str:
    """Full browser discovery — find all tools with schemas."""
    result = browser_discover_tools(url)
    return json.dumps({ "tools": [...], ... })

@mcp.tool()
def call_tool(url: str, question: str = None, ...) -> str:
    """Call a WebMCP tool and get the actual response."""
    result = browser_call_tool(url=url, question=question, ...)
    return json.dumps({ "answer": result.answer, ... })
```

---

## Setup: 5 Minutes to "Ask Any Website"

### Install

```bash
git clone https://github.com/salespeak-ai/webmcp-gateway.git
cd webmcp-gateway
pip install -e .
playwright install chromium
```

### Connect to Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:

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

Restart Claude Desktop. Now you can ask:

> "Check if acme-corp.com has WebMCP tools and ask them about their enterprise pricing."

Claude will:
1. Call `check_webmcp("https://acme-corp.com")` — fast pre-check
2. Call `discover_tools("https://acme-corp.com")` — find available tools
3. Call `call_tool("https://acme-corp.com", question="What is your enterprise pricing?")` — get the answer

### Connect to Claude Code

Add to your project's `.mcp.json`:

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

### Run as SSE Server (ChatGPT, remote clients)

```bash
webmcp-gateway --transport sse --port 8808
```

---

## Smart Tool Selection

Not all WebMCP tools are simple Q&A. Some are structured — they need specific parameters like `category`, `date`, or `destination`. The gateway handles both:

**Simple tools** (most common): Have a `question` or `query` property, or no schema at all. The gateway auto-detects these and passes the user's question directly.

```python
def _is_simple_question_tool(input_schema):
    props = input_schema.get("properties", {})
    if not props:
        return True  # No schema = treat as simple
    if "question" in props or "query" in props:
        return True  # Has a question field
    return False
```

**Structured tools**: Have specific fields like `category`, `max_price`, etc. The gateway returns the schema to the AI, which fills in the fields and calls again:

```
AI: call_tool("https://travel.com", question="flights to Paris")
Gateway: "This tool requires structured args. Schema: {destination, date, passengers}"
AI: call_tool("https://travel.com", tool_args='{"destination":"Paris","date":"2025-06-15"}')
Gateway: "Found 12 flights. Cheapest: $420 on Air France..."
```

The auto-selection priority:
1. If `tool_args` provided → find a structured tool
2. Try well-known names: `ask_question`, `ask`, `chat`, `search`, `query`
3. Fall back to the first registered tool

---

## Testing with the Demo Site

The repo includes a demo site (`examples/demo_site.html`) that registers two tools:

```javascript
// Simple Q&A tool
navigator.modelContext.registerTool({
  name: "ask_question",
  description: "Ask about our products",
  execute: async ({ question }) => {
    await new Promise(r => setTimeout(r, 2000)); // Simulate LLM delay
    return { content: [{ type: "text", text: "Our pricing starts at..." }] };
  }
});

// Structured tool
navigator.modelContext.registerTool({
  name: "search_products",
  description: "Search by category and price",
  inputSchema: {
    properties: {
      category: { type: "string" },
      max_price: { type: "number" }
    },
    required: ["category"]
  },
  execute: async ({ category, max_price }) => {
    // Filter and return products
  }
});
```

Serve it locally and point the gateway at it:

```bash
python -m http.server 8000 -d examples/
# Then in Claude: "discover tools on http://localhost:8000/demo_site.html"
```

---

## What's Next

The gateway is intentionally minimal — ~300 lines of core code, three tools, no dependencies beyond Playwright and FastMCP. Here's what we're thinking about:

- **Connection pooling**: Reuse browser instances across calls instead of launching a new one each time
- **Caching**: Cache discovered tools for a URL so subsequent calls skip discovery
- **Streaming**: Stream partial responses for long-running LLM tools
- **Multi-tool calls**: Call multiple tools on the same page in one browser session
- **Docker image**: One-command deploy for remote SSE mode

The repo is open source at [github.com/salespeak-ai/webmcp-gateway](https://github.com/salespeak-ai/webmcp-gateway). PRs welcome.

---

## The Bigger Picture

WebMCP Gateway is a bet on a future where every website is also an API — where the line between "browsing" and "using tools" disappears. Today, if you want to check a company's pricing, you visit their website and read. Tomorrow, your AI assistant visits the website and reads for you — using the site's own declared capabilities, not brittle scraping.

The WebMCP standard makes this possible. The gateway makes it practical today.

```
You: "What's the return policy on three different stores?"

Claude: [calls check_webmcp on store-a.com, store-b.com, store-c.com]
Claude: [calls call_tool on each one that has WebMCP]
Claude: "Here's a comparison:
  - Store A: 30-day returns, free shipping on returns
  - Store B: 14-day returns, $8 return label
  - Store C: 60-day returns, free returns for members"
```

That's the vision. Try it: `pip install webmcp-gateway && playwright install chromium`.
