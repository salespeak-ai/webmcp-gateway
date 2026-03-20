# WebMCP Protocol: How Tool Execution Actually Works

This document explains the complete async flow of a WebMCP tool call — from the AI agent invoking `execute()` to receiving the actual response. Understanding this is critical for anyone building WebMCP tools on their website.

## The Core Pattern: Promise-Based Execute

When a website registers a WebMCP tool, the `execute` callback must return a **Promise** that resolves with the actual result. The AI agent (via Chrome's `navigator.modelContext`) awaits this Promise before returning the answer.

```
AI Agent                    Parent Page                     Backend/Widget
  │                            │                                │
  │  executeToolCall()         │                                │
  │ ──────────────────────►    │                                │
  │                            │  execute({ question })         │
  │                            │  returns new Promise(...)      │
  │                            │                                │
  │    (waiting for Promise    │  send question to backend      │
  │     to resolve...)         │ ──────────────────────────►    │
  │                            │                                │
  │                            │         (LLM processing...)    │
  │                            │                                │
  │                            │  ◄──── response arrives ─────  │
  │                            │                                │
  │                            │  resolve({ content: [...] })   │
  │  ◄──── result ───────────  │                                │
  │                            │                                │
```

**The key:** `execute()` does NOT return immediately. It returns a Promise that stays pending until the actual answer is ready. The gateway's headless browser calls `await tool.execute(args)` and waits for this Promise to resolve.

## Implementation Patterns

### Pattern 1: Direct API Call (Simplest)

The tool makes an API call and resolves with the response:

```javascript
navigator.modelContext.registerTool({
  name: "ask_question",
  description: "Ask about our products",
  inputSchema: {
    type: "object",
    properties: {
      question: { type: "string" }
    },
    required: ["question"]
  },
  execute: async ({ question }) => {
    // Call your backend directly
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question })
    });
    const data = await resp.json();

    // Return in MCP content format
    return {
      content: [{ type: 'text', text: data.answer }]
    };
  }
});
```

This is the simplest pattern — `execute` is an async function that `await`s the API response and returns the answer directly.

### Pattern 2: iframe postMessage Bridge (For Widget-Based Sites)

When the AI backend runs inside an iframe (common for chat widgets), the parent page must bridge messages:

```javascript
let callCounter = 0;
const TIMEOUT_MS = 60_000;

navigator.modelContext.registerTool({
  name: "ask_question",
  description: "Ask our AI assistant",
  inputSchema: {
    type: "object",
    properties: {
      question: { type: "string" }
    },
    required: ["question"]
  },
  execute: ({ question }) => {
    // Unique ID to correlate this call with its response
    const callId = `webmcp_${++callCounter}_${Date.now()}`;

    return new Promise((resolve) => {
      let timeoutId;

      const cleanup = () => {
        clearTimeout(timeoutId);
        window.removeEventListener('message', onMessage);
      };

      // Listen for the response from the iframe
      const onMessage = (event) => {
        if (
          event.data &&
          typeof event.data === 'object' &&
          'botResponse' in event.data &&
          event.data._callId === callId
        ) {
          cleanup();
          resolve({
            content: [{ type: 'text', text: event.data.botResponse }]
          });
        }
      };

      window.addEventListener('message', onMessage);

      // Timeout — don't hang forever
      timeoutId = setTimeout(() => {
        cleanup();
        resolve({
          content: [{
            type: 'text',
            text: `Request timed out after ${TIMEOUT_MS / 1000}s.`
          }]
        });
      }, TIMEOUT_MS);

      // Send the question to the iframe
      const iframe = document.querySelector('#my-chat-iframe');
      iframe.contentWindow.postMessage(
        { question, _callId: callId },
        'https://my-widget-domain.com'
      );
    });
  }
});
```

**Inside the iframe**, when the bot response arrives:

```javascript
// In the iframe's code
window.addEventListener('message', async (event) => {
  if (event.data?.question && event.data?._callId) {
    // Process the question through your AI
    const answer = await generateBotResponse(event.data.question);

    // Post the answer back to the parent
    window.parent.postMessage({
      botResponse: answer,
      _callId: event.data._callId
    }, event.origin);
  }
});
```

### Pattern 3: Structured Tools (Non-Question)

For tools with complex inputs (e.g., search, booking):

```javascript
navigator.modelContext.registerTool({
  name: "search_products",
  description: "Search our product catalog",
  inputSchema: {
    type: "object",
    properties: {
      category: { type: "string", description: "Product category" },
      max_price: { type: "number", description: "Max price in USD" },
      in_stock: { type: "boolean", description: "Only show in-stock items" }
    },
    required: ["category"]
  },
  execute: async ({ category, max_price, in_stock }) => {
    const params = new URLSearchParams({ category });
    if (max_price) params.set('max_price', max_price);
    if (in_stock) params.set('in_stock', 'true');

    const resp = await fetch(`/api/products/search?${params}`);
    const products = await resp.json();

    return {
      content: [{
        type: 'text',
        text: JSON.stringify(products, null, 2)
      }]
    };
  }
});
```

## How the Gateway Captures Responses

The WebMCP Gateway's headless browser:

1. **Injects an interception script BEFORE page load** that hooks `navigator.modelContext.registerTool()` to capture both the tool definition AND the `execute` callback
2. **Calls `await tool.execute(args)`** — this invokes the website's actual execute function
3. **Waits for the Promise to resolve** — whether it's a direct API call or an iframe postMessage bridge
4. **Parses the result** — handles string returns, `{ content: [...] }` format, and plain objects

```javascript
// What the gateway runs in the browser context:
const result = await tool.execute(args);

// Parse the response
let answer = '';
if (typeof result === 'string') {
    answer = result;
} else if (result?.content && Array.isArray(result.content)) {
    answer = result.content
        .filter(c => c.type === 'text')
        .map(c => c.text)
        .join('\n');
} else if (result && typeof result === 'object') {
    answer = JSON.stringify(result);
}
```

## Call Correlation for Concurrent Requests

When multiple AI agents (or the same agent making multiple calls) interact with a site simultaneously, call correlation prevents response mixup:

```
Agent A: callId = "webmcp_1_1710000001"  →  Widget processes  →  Response tagged with _callId
Agent B: callId = "webmcp_2_1710000002"  →  Widget processes  →  Response tagged with _callId
```

Each `execute()` call generates a unique `callId` and the message listener only resolves when it sees a response with the matching ID.

## Response Format

Tools should return the standard MCP content format:

```javascript
// Preferred: MCP content array
return {
  content: [
    { type: 'text', text: 'The answer to your question...' }
  ]
};

// Also supported: plain string
return "The answer to your question...";

// Also supported: any object (will be JSON.stringify'd)
return { answer: "...", sources: [...] };
```

## Error Handling

Tools should **never reject** the Promise. Instead, resolve with an error message:

```javascript
execute: async ({ question }) => {
  try {
    const answer = await getAnswer(question);
    return { content: [{ type: 'text', text: answer }] };
  } catch (error) {
    return { content: [{ type: 'text', text: `Error: ${error.message}` }] };
  }
}
```

This ensures the AI agent always gets a response it can work with, rather than an unhandled Promise rejection.

## Timeouts

- **Recommended timeout:** 60 seconds for LLM-backed tools
- **Always implement a timeout** — don't let the Promise hang forever
- **Resolve (don't reject) on timeout** with a descriptive message
