# WebMCP Deep Dive: How to Actually Wait for Answers (Not Just Send Messages)

*The async response pattern that separates working WebMCP tools from broken ones — with copy-paste code for every scenario.*

---

Here's the mistake everyone makes when they first wire up a WebMCP tool. They register a tool that sends a message to their chatbot. The chatbot eventually responds. But the tool already returned "Message sent!" three seconds ago, and the AI assistant has no idea what the actual answer was.

```javascript
// THE WRONG WAY — AI gets "Message sent!" instead of the answer
navigator.modelContext.registerTool({
  name: "ask_question",
  execute: ({ question }) => {
    chatWidget.sendMessage(question);
    return { content: [{ type: "text", text: "Message sent!" }] };
    // The real answer arrives 5 seconds later. Nobody's listening.
  }
});
```

The AI assistant receives "Message sent!" and moves on. Your chatbot talks to the void. The entire point of WebMCP — letting AI agents interact with your website's intelligence — is lost.

The fix isn't complicated, but you need to understand one thing first.

---

## The Core Insight: Promises Are the Mechanism

When Chrome (or a headless browser like the [WebMCP Gateway](https://github.com/salespeak-ai/webmcp-gateway)) calls your tool's `execute()` function, it runs:

```javascript
const result = await tool.execute(args);
```

That `await` means the caller will wait **as long as your Promise takes to resolve.** Five seconds? Fine. Thirty seconds while an LLM generates a thoughtful response? Also fine. The caller doesn't time out after 200ms and move on — it waits for the real answer.

Your `execute()` function just needs to return a Promise that resolves with the actual response. Not "got it, working on it." The response itself.

Here are three patterns, from simplest to most real-world.

---

## Pattern 1: Direct API Call

If your website has a backend that answers questions over HTTP, this is all you need:

```javascript
navigator.modelContext.registerTool({
  name: "ask_question",
  description: "Ask about our products and services",
  inputSchema: {
    type: "object",
    properties: {
      question: { type: "string", description: "The question to ask" }
    },
    required: ["question"]
  },
  execute: async ({ question }) => {
    // This fetch might take 5-30 seconds for an LLM-backed endpoint.
    // The async/await keeps the Promise pending until it resolves.
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

That's it. The `async` keyword makes `execute` return a Promise. The `await fetch(...)` keeps that Promise pending until the API responds. The caller — whether it's Chrome natively or a headless browser — waits the whole time and gets the real answer.

**What actually happens:**

```
T+0s:    AI calls execute({ question: "What's your pricing?" })
T+0s:    fetch('/api/chat') fires
T+0.2s:  Request reaches your server
T+3s:    LLM generates the response
T+3.1s:  fetch resolves → data.answer = "Our Pro plan starts at $99/mo..."
T+3.1s:  Promise resolves → AI receives the actual answer
```

Three seconds of waiting. That's the whole trick.

---

## Pattern 2: iframe postMessage Bridge

This is where most people get stuck, because this is how most real websites actually work.

Your chat widget doesn't live on the parent page. It lives in an **iframe** — loaded from a different domain, running its own JavaScript, talking to its own LLM backend. The parent page can't call the widget's API directly. Cross-origin rules prevent it.

The bridge: `window.postMessage`. Parent sends the question to the iframe, iframe processes it, iframe posts the answer back. The key is making `execute()` return a Promise that stays pending until that answer arrives.

### The Parent Page (Your Website)

```javascript
let callCounter = 0;
const TIMEOUT_MS = 60_000;

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
    if (!question || typeof question !== 'string') {
      return { content: [{ type: 'text', text: 'Error: question is required' }] };
    }

    // Unique ID for this call — critical for concurrency
    const callId = `webmcp_${++callCounter}_${Date.now()}`;

    // Return a Promise that stays PENDING until the iframe responds
    return new Promise((resolve) => {
      let timeoutId;

      const cleanup = () => {
        clearTimeout(timeoutId);
        window.removeEventListener('message', onMessage);
      };

      // Listen for the iframe's response
      const onMessage = (event) => {
        // Match: has a botResponse AND the callId matches ours
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

      // Safety net — don't hang forever
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

### The iframe (Chat Widget Side)

Inside the iframe, receive the question, do the LLM work, post the answer back:

```javascript
window.addEventListener('message', async (event) => {
  // Only handle messages with a question and callId
  if (!event.data?.question || !event.data?._callId) return;

  const { question, _callId } = event.data;

  try {
    // Your LLM call — this is the slow part
    const answer = await generateAnswer(question);

    // Post the answer back to the parent, echo the callId
    window.parent.postMessage({
      botResponse: answer,
      _callId: _callId
    }, event.origin);

  } catch (error) {
    window.parent.postMessage({
      botResponse: `Sorry, something went wrong: ${error.message}`,
      _callId: _callId
    }, event.origin);
  }
});
```

### The Full Timeline

```
T+0s:    AI calls execute({ question: "What's your pricing?" })
T+0ms:   callId = "webmcp_1_1710000001" generated
T+0ms:   Promise created — now PENDING
T+0ms:   message listener registered on parent window
T+0ms:   60s timeout started
T+1ms:   postMessage({ question, _callId }) → iframe
         ┌───────────────────────────────────────┐
T+1ms:   │ iframe receives the message           │
T+10ms:  │ iframe calls LLM backend              │
T+4s:    │ LLM generates the response            │
T+4.1s:  │ iframe postMessage({ botResponse,     │
         │   _callId }) → parent                 │
         └───────────────────────────────────────┘
T+4.1s:  onMessage fires, callId matches ours
T+4.1s:  cleanup() — remove listener, clear timeout
T+4.1s:  resolve({ content: [{ text: "Our pricing..." }] })
T+4.1s:  Promise RESOLVES → AI receives the actual answer
```

The AI waited 4.1 seconds. It got the real LLM response, not "message sent." That's the difference between a useful integration and a broken one.

---

## Why `_callId` Matters (Call Correlation)

Without correlation IDs, concurrent requests break in ways that are hard to debug:

```
// Without callId — BROKEN
Agent A asks: "What's your pricing?"      → widget → "Starting at $99/mo"
Agent B asks: "How do returns work?"      → widget → "30-day return policy"

// Agent A's listener picks up Agent B's answer
// because both listeners match on 'botResponse' existing
```

With `_callId`:

```
// With callId — CORRECT
Agent A: callId="webmcp_1_..." → iframe echoes _callId → only A's listener matches
Agent B: callId="webmcp_2_..." → iframe echoes _callId → only B's listener matches
```

Simple pattern: send a unique ID with the request, echo it in the response, filter on it in the listener. Two agents can ask questions at the same time and each one gets its own answer.

---

## Pattern 3: Widget That Opens on Demand

Some chat widgets start minimized or hidden. They need to be opened or initialized before they can receive messages. This is common with third-party widgets that lazy-load their iframe.

```javascript
execute: ({ question }) => {
  const callId = `webmcp_${++callCounter}_${Date.now()}`;

  return new Promise((resolve) => {
    let timeoutId;

    const cleanup = () => {
      clearTimeout(timeoutId);
      window.removeEventListener('message', onMessage);
    };

    const onMessage = (event) => {
      if (event.data?.botResponse && event.data._callId === callId) {
        cleanup();
        resolve({ content: [{ type: 'text', text: event.data.botResponse }] });
      }
    };

    window.addEventListener('message', onMessage);

    timeoutId = setTimeout(() => {
      cleanup();
      resolve({
        content: [{ type: 'text', text: 'Request timed out after 60 seconds.' }]
      });
    }, 60_000);

    // The new part: open widget first if needed, then send
    const sendQuestion = () => {
      const iframe = document.querySelector('#chat-widget-iframe');
      iframe.contentWindow.postMessage(
        { question, _callId: callId },
        widgetOrigin
      );
    };

    if (!isWidgetOpen()) {
      openWidget(() => {
        // Callback fires once widget iframe is loaded and ready
        sendQuestion();
      });
    } else {
      sendQuestion();
    }
  });
}
```

The AI agent's question opens the chat, sends the message, waits for the LLM response — all invisible to the user. No UI flashing, no visible click sequence. The headless browser handles it behind the scenes.

---

## The Three Rules of WebMCP `execute()`

After building the gateway and testing against a bunch of real-world sites, we've boiled it down to three rules that prevent basically every failure mode.

### Rule 1: Always Return a Promise

The most common bug. Someone writes a `.then()` chain inside `execute` but forgets to return it:

```javascript
// BROKEN — execute returns undefined
execute: ({ question }) => {
  fetch('/api/chat', { ... }).then(r => r.json()).then(data => {
    return { content: [{ type: 'text', text: data.answer }] };
    // This return is inside the .then() callback — it goes nowhere
  });
  // execute itself returns undefined
}

// FIXED — use async/await
execute: async ({ question }) => {
  const resp = await fetch('/api/chat', { ... });
  const data = await resp.json();
  return { content: [{ type: 'text', text: data.answer }] };
}

// ALSO WORKS — explicit Promise
execute: ({ question }) => {
  return new Promise((resolve) => {
    // ... resolve when the answer arrives
  });
}
```

If `execute` returns `undefined`, the AI gets nothing. Always return either an `async` function result or an explicit `new Promise(...)`.

### Rule 2: Never Reject — Resolve with an Error Message

```javascript
// BAD — rejected Promise may crash the caller
execute: async ({ question }) => {
  const resp = await fetch('/api/chat', { ... }); // throws on network error
  // Unhandled rejection → caller gets a crash, not an answer
}

// GOOD — catch errors and resolve with a message
execute: async ({ question }) => {
  try {
    const resp = await fetch('/api/chat', { ... });
    const data = await resp.json();
    return { content: [{ type: 'text', text: data.answer }] };
  } catch (error) {
    return {
      content: [{ type: 'text', text: `Error: ${error.message}` }]
    };
  }
}
```

The AI can understand "Error: network timeout" and either retry or tell the human. A rejected Promise is just a crash log that nobody reads.

### Rule 3: Always Set a Timeout

```javascript
// DANGEROUS — hangs forever if iframe never responds
execute: ({ question }) => {
  return new Promise((resolve) => {
    window.addEventListener('message', (event) => {
      // What if the iframe crashed? This listener waits forever.
    });
    iframe.contentWindow.postMessage({ question }, origin);
  });
}

// SAFE — resolve with timeout after 60 seconds
execute: ({ question }) => {
  return new Promise((resolve) => {
    const timeout = setTimeout(() => {
      cleanup();
      resolve({
        content: [{ type: 'text', text: 'Request timed out after 60 seconds.' }]
      });
    }, 60_000);

    // ... listener and postMessage logic
  });
}
```

60 seconds is generous for most LLM-backed tools. If your backend is consistently faster, tighten it. But never skip the timeout entirely — a hanging Promise blocks the AI indefinitely, and there's no way for it to recover.

---

## How the WebMCP Gateway Handles All of This

The [WebMCP Gateway](https://github.com/salespeak-ai/webmcp-gateway) runs this whole flow from the outside using a headless browser:

1. **Inject before page load** — monkey-patches `registerTool()` to capture the `execute` callback
2. **Navigate** — Chromium loads the page, all JavaScript runs normally
3. **Wait for tools** — polls `window.__webmcp_tools` until tools appear
4. **Call the tool** — `await tool.execute(args)` and wait for the Promise

```python
# What actually runs inside the headless browser:
call_result = await page.evaluate("""
    async ([toolName, args]) => {
        const tool = window.__webmcp_tools[toolName];
        const result = await tool.execute(args);  // WAITS for the real answer
        // ... parse the result ...
        return { success: true, answer: parsedAnswer };
    }
""", [tool_name, call_args])
```

Playwright's `page.evaluate()` with an `async` function properly awaits the inner Promise. Even if the tool's `execute()` takes 30 seconds (parent → iframe → LLM → postMessage → resolve), the gateway waits the full duration.

That's why the gateway can "ask any website" — it doesn't just fire off a message. It sits through the entire async dance and comes back with the actual answer.

---

## Complete Copy-Paste Example

Here's a production-ready WebMCP integration you can drop into any page. It works with both Chrome's native WebMCP support and the WebMCP Gateway:

```html
<script>
(function() {
  const check = setInterval(() => {
    if (!navigator.modelContext) return;
    clearInterval(check);

    let callCount = 0;

    navigator.modelContext.registerTool({
      name: "ask_question",
      description: "Ask a question about [Your Company] — products, pricing, support, anything.",
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

Add this to any page. Implement `/api/assistant` on your backend. Your website is now reachable by any AI assistant through WebMCP — whether Chrome calls it natively or the gateway calls it through a headless browser.

---

## What This Actually Enables

Think about what becomes possible when AI agents can ask websites questions and get real answers:

**"Compare pricing across three vendors."** Claude calls three websites, each one answers from its own knowledge base, and Claude synthesizes a comparison table. No tabs. No copying and pasting.

**"What does this company say about their refund policy?"** Instead of reading through a help center and hoping you found the right page, the AI asks the site's own assistant and gets the authoritative answer.

**"Find me a SaaS tool that integrates with Salesforce and costs under $200/month."** The AI checks five vendor sites, asks each one about Salesforce integration and pricing, and gives you a shortlist.

The website decides what to expose. The AI decides what to ask. The Promise-based `execute()` pattern makes sure the AI actually gets the answer.

That's WebMCP.

---

*Built by [Salespeak AI](https://salespeak.ai). [WebMCP Gateway](https://github.com/salespeak-ai/webmcp-gateway) is open source under the MIT license.*
