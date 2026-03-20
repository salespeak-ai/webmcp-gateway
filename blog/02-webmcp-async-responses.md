# WebMCP Deep Dive: How to Actually Wait for Answers (Not Just Send Messages)

*The async response pattern that makes WebMCP tools actually useful — with full code examples*

---

Here's the mistake everyone makes when first implementing WebMCP tools: they register a tool that *sends* a message to a chatbot, but the tool returns "message sent!" instead of the actual answer.

```javascript
// THE WRONG WAY — returns immediately, AI never gets the answer
navigator.modelContext.registerTool({
  name: "ask_question",
  execute: ({ question }) => {
    chatWidget.sendMessage(question);
    return { content: [{ type: "text", text: "Message sent!" }] };
    // AI gets "Message sent!" — useless.
  }
});
```

The AI assistant receives "Message sent!" and has no idea what the chatbot actually responded. The entire point of WebMCP is lost.

The fix is surprisingly simple, but you have to understand one thing: **`execute()` can return a Promise, and the caller will wait for it.**

---

## The Core Insight: Promises Are the Mechanism

When Chrome (or a headless browser like the [WebMCP Gateway](https://github.com/salespeak-ai/webmcp-gateway)) calls your tool's `execute()` function, it does:

```javascript
const result = await tool.execute(args);
```

That `await` means: **the caller will wait as long as your Promise takes to resolve.** If your LLM backend takes 10 seconds, the caller waits 10 seconds. If it takes 45 seconds, the caller waits 45 seconds.

This is the entire trick. Your `execute()` function should return a Promise that resolves with the **actual answer**, not an acknowledgment that something was sent.

---

## Pattern 1: Direct API Call (The Simplest Way)

If your website has a backend API that answers questions, this is all you need:

```javascript
navigator.modelContext.registerTool({
  name: "ask_question",
  description: "Ask about our products and services",
  inputSchema: {
    type: "object",
    properties: {
      question: {
        type: "string",
        description: "The question to ask"
      }
    },
    required: ["question"]
  },
  execute: async ({ question }) => {
    // This fetch might take 5-30 seconds for an LLM response.
    // That's fine — the caller awaits this Promise.
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question })
    });
    const data = await response.json();

    return {
      content: [{ type: 'text', text: data.answer }]
    };
  }
});
```

**Why this works:** `execute` is an `async` function. The `await fetch(...)` keeps the Promise pending until the API responds. The caller (Claude, ChatGPT, or the WebMCP Gateway) gets the real answer.

**Timeline:**

```
T+0s:   AI calls execute({ question: "What's your pricing?" })
T+0s:   fetch('/api/chat') fires
T+0.2s: Request reaches your server
T+3s:   LLM generates response
T+3.1s: fetch resolves with { answer: "Our Pro plan starts at..." }
T+3.1s: Promise resolves → AI receives the answer
```

The AI was waiting those 3 seconds. That's the point.

---

## Pattern 2: iframe postMessage Bridge (For Chat Widgets)

This is the real-world pattern. Most websites don't have a simple `/api/chat` endpoint — they have a **chat widget running in an iframe** from a third-party provider (Intercom, Drift, Zendesk, or a custom widget). The LLM backend is inside the iframe, and the parent page can't call it directly due to cross-origin restrictions.

The solution: **postMessage with call correlation.**

### The Parent Page (Your Website)

```javascript
let callCounter = 0;
const TIMEOUT_MS = 60_000; // 60 seconds

navigator.modelContext.registerTool({
  name: "ask_question",
  description: "Ask our AI assistant and get the full answer",
  inputSchema: {
    type: "object",
    properties: {
      question: { type: "string", description: "Your question" }
    },
    required: ["question"]
  },
  execute: ({ question }) => {
    // Input validation
    if (!question || typeof question !== 'string') {
      return {
        content: [{ type: 'text', text: 'Error: question is required' }]
      };
    }

    // Generate a unique ID for this call.
    // This is critical for concurrent requests — without it,
    // two simultaneous calls could get each other's answers.
    const callId = `webmcp_${++callCounter}_${Date.now()}`;

    // Return a Promise that resolves when the iframe responds
    return new Promise((resolve) => {
      let timeoutId;

      // Cleanup function — always remove the listener
      const cleanup = () => {
        clearTimeout(timeoutId);
        window.removeEventListener('message', onMessage);
      };

      // Listen for the response from the iframe
      const onMessage = (event) => {
        // Security: validate the origin
        if (event.origin !== 'https://your-widget-domain.com') {
          return;
        }

        // Match on: (1) has a response, (2) matches our callId
        if (
          event.data &&
          typeof event.data === 'object' &&
          'botResponse' in event.data &&
          event.data._callId === callId
        ) {
          cleanup();
          resolve({
            content: [{
              type: 'text',
              text: event.data.botResponse
            }]
          });
        }
      };

      window.addEventListener('message', onMessage);

      // Timeout — never let the Promise hang forever
      timeoutId = setTimeout(() => {
        cleanup();
        resolve({
          content: [{
            type: 'text',
            text: `The assistant did not respond within ${TIMEOUT_MS / 1000} seconds.`
          }]
        });
      }, TIMEOUT_MS);

      // Send the question to the iframe
      const iframe = document.querySelector('#chat-widget-iframe');
      iframe.contentWindow.postMessage(
        { question: question, _callId: callId },
        'https://your-widget-domain.com'
      );
    });
  }
});
```

### The iframe (Chat Widget)

Inside the iframe, you receive the question, process it through your LLM, and post the answer back:

```javascript
window.addEventListener('message', async (event) => {
  // Only handle messages with a question and callId
  if (!event.data?.question || !event.data?._callId) return;

  const { question, _callId } = event.data;

  try {
    // This is where your LLM call happens — could take seconds
    const answer = await generateAnswer(question);

    // Post the answer back to the parent page
    window.parent.postMessage({
      botResponse: answer,
      _callId: _callId  // Echo the callId for correlation
    }, event.origin);

  } catch (error) {
    window.parent.postMessage({
      botResponse: `Sorry, an error occurred: ${error.message}`,
      _callId: _callId
    }, event.origin);
  }
});
```

### The Full Timeline

```
T+0s:    AI calls execute({ question: "What's your pricing?" })
T+0ms:   callId = "webmcp_1_1710000001" generated
T+0ms:   Promise created (PENDING)
T+0ms:   window.message listener registered
T+0ms:   60s timeout started
T+1ms:   postMessage({ question, _callId }) → iframe
         ┌───────────────────────────────────────┐
T+1ms:   │ iframe receives message               │
T+10ms:  │ iframe sends question to LLM backend  │
T+4s:    │ LLM generates response                │
T+4.1s:  │ iframe postMessage({ botResponse,     │
         │   _callId }) → parent                 │
         └───────────────────────────────────────┘
T+4.1s:  onMessage fires, callId matches
T+4.1s:  cleanup() — remove listener, clear timeout
T+4.1s:  resolve({ content: [{ text: "Our pricing..." }] })
T+4.1s:  Promise RESOLVES → AI receives the actual answer
```

The AI waited 4.1 seconds and got the real answer. Not "message sent" — the actual LLM-generated response.

---

## Why Call Correlation Matters

Without the `_callId`, concurrent requests break:

```
// Without callId — BROKEN
Agent A asks: "What's your pricing?"      → iframe processes → answer: "Starting at $99"
Agent B asks: "How do returns work?"      → iframe processes → answer: "30-day policy"

// Agent A's listener picks up Agent B's answer (or vice versa)
// because there's no way to match responses to requests
```

With callId:

```
// With callId — CORRECT
Agent A: callId="webmcp_1_..." → iframe echoes _callId → only Agent A's listener matches
Agent B: callId="webmcp_2_..." → iframe echoes _callId → only Agent B's listener matches
```

The pattern is simple: send a unique ID with the request, echo it in the response, filter on it in the listener.

---

## Pattern 3: Widget That Opens on Demand

Some widgets start closed and need to be opened before they can receive messages. Handle this with a callback:

```javascript
execute: ({ question }) => {
  const callId = `webmcp_${++callCounter}_${Date.now()}`;

  return new Promise((resolve) => {
    // ... setup listener and timeout (same as above) ...

    const sendQuestion = () => {
      widgetIframe.contentWindow.postMessage(
        { question, _callId: callId },
        widgetOrigin
      );
    };

    // If widget is closed, open it first, then send
    if (!isWidgetOpen) {
      openWidget(() => {
        // Callback fires when widget is ready
        sendQuestion();
      });
    } else {
      sendQuestion();
    }
  });
}
```

This mirrors how real chat widgets work — the AI agent's question opens the chat, sends the message, and waits for the response, all invisibly.

---

## The Three Rules of WebMCP execute()

### Rule 1: Always Return a Promise (for async tools)

```javascript
// WRONG — returns undefined, AI gets nothing
execute: ({ question }) => {
  fetch('/api/chat', { ... }).then(r => r.json()).then(data => {
    return { content: [{ type: 'text', text: data.answer }] };
    // This return goes nowhere — it's inside a .then() callback
  });
}

// RIGHT — returns the Promise chain
execute: async ({ question }) => {
  const resp = await fetch('/api/chat', { ... });
  const data = await resp.json();
  return { content: [{ type: 'text', text: data.answer }] };
}

// ALSO RIGHT — explicit Promise
execute: ({ question }) => {
  return new Promise((resolve) => {
    // ... resolve later when answer arrives
  });
}
```

### Rule 2: Never Reject — Always Resolve with an Error Message

```javascript
// WRONG — rejected Promise may crash the caller
execute: async ({ question }) => {
  const resp = await fetch('/api/chat', { ... }); // throws on network error
  // If fetch throws, the Promise rejects → bad
}

// RIGHT — catch and resolve with error text
execute: async ({ question }) => {
  try {
    const resp = await fetch('/api/chat', { ... });
    const data = await resp.json();
    return { content: [{ type: 'text', text: data.answer }] };
  } catch (error) {
    return {
      content: [{
        type: 'text',
        text: `Error: ${error.message}`
      }]
    };
  }
}
```

The AI assistant can understand error messages and retry or rephrase. A rejected Promise is just a crash.

### Rule 3: Always Set a Timeout

```javascript
// WRONG — hangs forever if iframe never responds
execute: ({ question }) => {
  return new Promise((resolve) => {
    window.addEventListener('message', (event) => {
      // What if this never fires?
    });
    iframe.contentWindow.postMessage({ question }, origin);
  });
}

// RIGHT — resolve with timeout message after 60s
execute: ({ question }) => {
  return new Promise((resolve) => {
    const timeout = setTimeout(() => {
      cleanup();
      resolve({
        content: [{
          type: 'text',
          text: 'The request timed out after 60 seconds.'
        }]
      });
    }, 60_000);

    // ... rest of the message listener logic
  });
}
```

60 seconds is a good default for LLM-backed tools. Adjust based on your backend's typical response time.

---

## How the WebMCP Gateway Captures All of This

The [WebMCP Gateway](https://github.com/salespeak-ai/webmcp-gateway) is a headless browser that runs this exact flow from the outside:

1. **Inject before page load**: Monkey-patches `registerTool()` to capture the `execute` callback
2. **Navigate to the site**: Chromium loads the page, all JavaScript runs normally
3. **Wait for registration**: Polls `window.__webmcp_tools` until tools appear
4. **Call the tool**: `await tool.execute(args)` — waits for the Promise

```python
# In the headless browser:
call_result = await page.evaluate("""
    async ([toolName, args]) => {
        const tool = window.__webmcp_tools[toolName];
        const result = await tool.execute(args);  // WAITS for the answer
        // ... parse result ...
        return { success: true, answer: parsedAnswer };
    }
""", [tool_name, call_args])
```

Playwright's `page.evaluate()` with an `async` function awaits the inner Promise. So even if the tool's `execute()` takes 30 seconds (iframe → LLM → postMessage → resolve), the gateway waits.

This is why the gateway can "ask any website a question" — it's not just sending a message, it's participating in the full async response cycle.

---

## Putting It All Together: A Complete Example

Here's a complete, copy-paste-ready WebMCP integration for a website with a backend API:

```html
<script>
(function() {
  // Wait for WebMCP support (Chrome 146+ or headless with --enable-features=WebMCP)
  const check = setInterval(() => {
    if (!navigator.modelContext) return;
    clearInterval(check);

    let callCount = 0;

    navigator.modelContext.registerTool({
      name: "ask_question",
      description: "Ask a question about Acme Corp — products, pricing, support, anything.",
      inputSchema: {
        type: "object",
        properties: {
          question: {
            type: "string",
            description: "The question or request"
          }
        },
        required: ["question"]
      },
      execute: async ({ question }) => {
        callCount++;
        console.log(`[WebMCP] Call #${callCount}: "${question}"`);

        try {
          const response = await fetch('/api/assistant', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              question,
              source: 'webmcp',
              call_id: callCount
            })
          });

          if (!response.ok) {
            return {
              content: [{
                type: 'text',
                text: `Our assistant is temporarily unavailable (HTTP ${response.status}).`
              }]
            };
          }

          const data = await response.json();

          return {
            content: [{
              type: 'text',
              text: data.answer || 'No answer available.'
            }]
          };

        } catch (error) {
          return {
            content: [{
              type: 'text',
              text: `Connection error: ${error.message}. Please try again.`
            }]
          };
        }
      }
    });

    console.log('[WebMCP] Tool registered: ask_question');
  }, 50);
})();
</script>
```

Add this script to any page, implement a `/api/assistant` endpoint, and your website is now reachable by any AI assistant through the WebMCP Gateway.

---

## What This Enables

Think about the use cases this unlocks:

**"Compare pricing across three vendors"** — Claude calls `call_tool` on three different websites, gets real answers from each one's knowledge base, and synthesizes a comparison. No manual browsing.

**"Book a demo with the top-rated CRM"** — The AI discovers a `book_demo` tool on the vendor's site, fills in the structured fields (name, email, preferred time), and submits. The website handles it through its normal flow.

**"What does this company's support say about refunds?"** — Instead of reading through a help center, the AI asks the site's own assistant and gets the authoritative answer.

The website decides what to expose. The AI decides what to ask. The gateway handles the plumbing.

That's WebMCP.
