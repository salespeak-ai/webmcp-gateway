# How We Built WebMCP Gateway: Letting AI Assistants Talk to Any Website

*We built an open-source MCP server that bridges Claude and ChatGPT to websites using a headless browser. Here's every layer of how it works.*

---

Every website has knowledge locked behind its UI. Pricing buried three clicks deep. A knowledge base that only works if you type into the search box. A chat widget that knows everything about the company but can only talk to humans.

AI assistants like Claude and ChatGPT can't access any of it. They can't click buttons, fill forms, or wait for a chat widget to think for 10 seconds before responding.

[WebMCP](https://developer.chrome.com/blog/webmcp-epp) changes that. It's a new browser standard from Google where websites declare their capabilities through `navigator.modelContext.registerTool()`. Instead of scraping DOMs or puppeteering browsers, AI agents can discover what a site offers and call it like a structured API.

But here's the gap nobody was filling: AI assistants speak **MCP** (Model Context Protocol). Websites speak **WebMCP**. Same family, different languages. No translator in the room.

So we built [**WebMCP Gateway**](https://github.com/salespeak-ai/webmcp-gateway) — an open-source MCP server that uses a headless Playwright browser to discover WebMCP tools on any website and expose them to Claude and ChatGPT. You install it, point it at a URL, and ask your question. The gateway handles the browser, the tool discovery, and the waiting.

```
┌──────────────┐      MCP       ┌──────────────────┐   Playwright   ┌──────────────────┐
│  Claude /     │ ◄───────────► │  WebMCP Gateway  │ ◄────────────► │  Any Website     │
│  ChatGPT      │   (stdio/sse) │  (MCP server)    │   (headless)   │  with WebMCP     │
└──────────────┘               └──────────────────┘               └──────────────────┘
```

This post walks through exactly how it works — the detection chain, the browser interception trick, the smart tool selection, and how to set it up in five minutes.

---

## The Problem: Two Protocols, No Bridge

**MCP** is how AI assistants discover and call external tools. Claude Desktop, Claude Code, ChatGPT — they all speak MCP. You give them a server that exposes functions like `search_docs(query)` or `create_ticket(title)`, and the AI calls them when it needs to.

**WebMCP** is how websites expose capabilities to AI agents in the browser. A website registers tools in JavaScript:

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

The problem: these two don't talk to each other. Claude can't "visit" a website and call its WebMCP tools. There's no browser in the loop.

The gateway is that browser.

---

## Architecture: How It Actually Works

The whole thing is about 300 lines of Python across five files. It's built as a pipeline with three layers, each one doing progressively more work.

### Layer 1: Fast Detection — No Browser Needed

Spinning up a headless browser takes time and resources. Before we commit to that, we do something much cheaper: download the page HTML and scan it with regex.

```python
def detect_webmcp_fast(url: str, timeout: float = 10.0) -> DetectionResult:
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": _USER_AGENT})
            html = resp.text
    except Exception:
        return DetectionResult(found=False, provider="none")

    # Check 1: Declarative forms — <form tool-name="ask_question">
    form_tags = re.findall(r'<form\b([^>]*\btool-name=[^>]*)>', html, re.I)
    if form_tags:
        # ... extract tool names and descriptions
        return DetectionResult(found=True, provider="webmcp_declarative", tools=tools)

    # Check 2: Imperative JS — navigator.modelContext.registerTool()
    register_matches = re.findall(
        r'(?:navigator\.modelContext|modelContext)\.registerTool\s*\(\s*\{'
        r'[^}]*name\s*:\s*["\']([^"\']+)["\']',
        html,
    )
    if register_matches:
        return DetectionResult(found=True, provider="webmcp_imperative", tools=tools)

    # Check 3: Any mention of navigator.modelContext
    if re.search(r"navigator\.modelContext", html):
        return DetectionResult(found=True, provider="webmcp_generic")

    return DetectionResult(found=False, provider="none")
```

Three checks in priority order: declarative HTML forms, imperative JS registrations, then any generic modelContext reference. This runs in milliseconds. If it finds nothing, we skip the browser entirely and save everyone the wait.

### Layer 2: Browser Discovery — The Interception Trick

Here's where it gets clever. WebMCP tools are usually registered dynamically — JavaScript runs, widgets load asynchronously, and tools get registered after the page finishes rendering. You'll never find these with a static HTML scan.

So we launch headless Chromium and inject a script **before the page even loads**:

```javascript
// Injected via page.add_init_script() — runs before ANY page JavaScript
(() => {
    window.__webmcp_tools = {};
    window.__webmcp_ready = false;

    const hookInterval = setInterval(() => {
        if (!navigator.modelContext) return;
        clearInterval(hookInterval);

        // Monkey-patch registerTool to capture every definition
        const original = navigator.modelContext.registerTool.bind(navigator.modelContext);
        navigator.modelContext.registerTool = (toolDef) => {
            window.__webmcp_tools[toolDef.name] = {
                name: toolDef.name,
                description: toolDef.description,
                inputSchema: toolDef.inputSchema || {},
                execute: toolDef.execute,  // Keep the callback!
            };
            original(toolDef);  // Still register normally — page works fine
        };
        window.__webmcp_ready = true;
    }, 50);
})();
```

This is the core trick: we wrap `registerTool` before the page's own scripts get to it. Every tool that registers itself gets captured — name, description, schema, and crucially, the `execute` callback. We store that callback because we're going to call it later.

The page doesn't know anything happened. Its tools still register normally. We're just eavesdropping.

On the Python side:

```python
async with async_playwright() as p:
    browser = await p.chromium.launch(
        headless=True,
        args=["--enable-features=WebMCP"],
    )
    page = await (await browser.new_context()).new_page()

    await page.add_init_script(_INTERCEPT_SCRIPT)  # Before page load!

    await page.goto(url, wait_until="domcontentloaded")

    # Wait up to 15 seconds for tools to register
    await page.wait_for_function(
        "window.__webmcp_ready === true && "
        "Object.keys(window.__webmcp_tools).length > 0"
    )

    # Read what we caught
    tools = await page.evaluate("""
        Object.values(window.__webmcp_tools).map(t => ({
            name: t.name,
            description: t.description,
            inputSchema: t.inputSchema,
        }))
    """)
```

### Layer 3: Smart Tool Selection and Invocation

A website might expose one tool or five. Some are simple Q&A (pass a question, get an answer). Others are structured (pass a category, price range, and feature list to search products). The gateway needs to figure out which one to call.

The logic is straightforward:

```python
def _is_simple_question_tool(input_schema):
    """Does this tool just take a question string?"""
    props = input_schema.get("properties", {})
    if not props:
        return True  # No schema = treat as simple
    if "question" in props or "query" in props:
        return True
    return False
```

Selection priority:
1. If the AI passed `tool_args` (structured arguments) → find a structured tool
2. If the AI passed `question` → look for known Q&A names: `ask_question`, `ask`, `chat`, `search`, `query`
3. Fall back to the first tool that registered

And here's the smart part: if a structured tool is found but the AI only sent a question (no structured args), the gateway doesn't guess. It returns the tool's schema so the AI can fill in the fields and call again:

```
Claude: call_tool("travel.com", question="flights to Paris")
Gateway: "This tool needs structured args. Schema: {destination, date, passengers}"
Claude: call_tool("travel.com", tool_args='{"destination":"Paris","date":"2026-06-15"}')
Gateway: "Found 12 flights. Cheapest: $420 on Air France..."
```

Two-step dance. The AI figures out the right arguments on its own.

### The Invocation Itself

Once we know what to call, one line of JavaScript does the real work:

```javascript
const result = await tool.execute(args);
```

That `await` is the whole point. The tool's `execute()` returns a Promise that stays pending until the real answer is ready. For a chat widget backed by an LLM, that might take 5–30 seconds. The gateway waits. It doesn't return "message delivered" — it returns the actual response.

The result parsing handles whatever format comes back:

```javascript
let answer = '';
if (typeof result === 'string') {
    answer = result;
} else if (result?.content && Array.isArray(result.content)) {
    // Standard MCP format: { content: [{ type: 'text', text: '...' }] }
    answer = result.content.filter(c => c.type === 'text').map(c => c.text).join('\n');
} else if (result && typeof result === 'object') {
    answer = JSON.stringify(result);
}
```

### Tying It All Together: The MCP Server

All three layers are wrapped in a FastMCP server — five lines of setup, three tool definitions:

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

That's the entire server. Three tools that map to three layers of the pipeline.

---

## Setup: Five Minutes to "Ask Any Website"

### Install

```bash
git clone https://github.com/salespeak-ai/webmcp-gateway.git
cd webmcp-gateway
pip install -e .
playwright install chromium
```

### Claude Desktop

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

Restart Claude Desktop. Now you can ask something like: *"Check if acme-corp.com has WebMCP tools and ask them about their enterprise pricing."*

Claude will chain the calls itself:
1. `check_webmcp("https://acme-corp.com")` — fast pre-check
2. `discover_tools("https://acme-corp.com")` — find what's available
3. `call_tool("https://acme-corp.com", question="What is your enterprise pricing?")` — get the answer

### Claude Code

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

### SSE Mode (Remote Clients, ChatGPT)

```bash
webmcp-gateway --transport sse --port 8808
```

Any MCP client that supports SSE transport can connect to `http://your-server:8808`.

---

## Test Drive: The Demo Site

The repo includes a demo site (`examples/demo_site.html`) that registers two tools so you can try the full flow locally:

```javascript
// A simple Q&A tool
navigator.modelContext.registerTool({
  name: "ask_question",
  description: "Ask about our products",
  execute: async ({ question }) => {
    await new Promise(r => setTimeout(r, 2000)); // Simulate LLM thinking
    return { content: [{ type: "text", text: "Our pricing starts at..." }] };
  }
});

// A structured search tool
navigator.modelContext.registerTool({
  name: "search_products",
  description: "Search by category and price range",
  inputSchema: {
    properties: {
      category: { type: "string" },
      max_price: { type: "number" }
    },
    required: ["category"]
  },
  execute: async ({ category, max_price }) => {
    // Filter products, return results
  }
});
```

Serve it and point Claude at it:

```bash
python -m http.server 8000 -d examples/
# Then ask Claude: "Discover tools on http://localhost:8000/demo_site.html"
```

When Claude calls `call_tool` with a question, the gateway launches a browser, discovers both tools, picks `ask_question` (it matches the Q&A pattern), calls `await tool.execute({question: "..."})`, waits 2 seconds for the simulated response, and returns the answer. The AI gets a real answer, not a confirmation.

---

## The Codebase

```
src/webmcp_gateway/
├── __init__.py      # Package version
├── __main__.py      # python -m webmcp_gateway
├── cli.py           # CLI entry point (stdio/sse transport)
├── server.py        # MCP server with 3 tools
├── detect.py        # Fast HTTP-based detection (no browser)
└── browser.py       # Playwright discovery & invocation
```

Five files. About 300 lines of core code. The complexity isn't in the gateway — it's in the async patterns that websites use to respond. That's what [the next post](/blog/webmcp-async-responses) covers in depth.

---

## The Bigger Picture

WebMCP Gateway is a bet on a future where every website is also an API. Where the line between "browsing" and "calling tools" disappears.

Today, if you want to compare pricing across three vendors, you open three browser tabs and manually read through each site. Tomorrow, you ask Claude:

```
You: "Compare pricing on store-a.com, store-b.com, and store-c.com"

Claude: [calls check_webmcp on all three]
Claude: [calls call_tool on each one that has WebMCP]
Claude: "Here's a comparison:
  - Store A: 30-day returns, free shipping. Pro plan $99/mo.
  - Store B: 14-day returns, $8 return shipping. Starts at $79/mo.
  - Store C: 60-day returns, free for members. Enterprise only, custom pricing."
```

The website decides what to expose. The AI decides what to ask. The gateway handles the plumbing.

Try it: `pip install webmcp-gateway && playwright install chromium`.

---

*WebMCP Gateway is open source under the MIT license. Built by [Salespeak AI](https://salespeak.ai).*
