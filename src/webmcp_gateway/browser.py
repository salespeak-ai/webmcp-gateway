"""Playwright-based WebMCP tool discovery and invocation.

Uses headless Chromium to:
1. Inject a script BEFORE page load to intercept navigator.modelContext.registerTool()
2. Navigate to the target website
3. Wait for tools to register
4. Discover tool names, descriptions, and input schemas
5. Call tools and return results
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Timeouts
TOOL_REGISTRATION_TIMEOUT_MS = 15_000
RESPONSE_TIMEOUT_MS = 60_000

_CHROME_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/146.0.0.0 Safari/537.36"
)


@dataclass
class WebMCPTool:
    """A tool discovered via WebMCP on a website."""

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)


@dataclass
class WebMCPResult:
    """Result of calling a WebMCP tool."""

    success: bool
    tool_name: str = ""
    answer: Optional[str] = None
    tools_discovered: list[WebMCPTool] = field(default_factory=list)
    error: Optional[str] = None
    page_title: Optional[str] = None


# JavaScript injected BEFORE page load to intercept registerTool calls
_INTERCEPT_SCRIPT = """
(() => {
    window.__webmcp_tools = {};
    window.__webmcp_ready = false;

    const hookInterval = setInterval(() => {
        if (!navigator.modelContext) return;
        clearInterval(hookInterval);

        const original = navigator.modelContext.registerTool.bind(navigator.modelContext);
        navigator.modelContext.registerTool = (toolDef) => {
            window.__webmcp_tools[toolDef.name] = {
                name: toolDef.name,
                description: toolDef.description,
                inputSchema: toolDef.inputSchema || {},
                execute: toolDef.execute,
            };
            original(toolDef);
        };
        window.__webmcp_ready = true;
    }, 50);
})();
"""

# JavaScript to call a discovered tool and return its result.
# The critical behavior: `await tool.execute(args)` waits for the tool's
# Promise to resolve. For iframe-based widgets, this means waiting for the
# postMessage response — the gateway gets the ACTUAL answer, not just
# confirmation that the message was sent.
_CALL_TOOL_SCRIPT = """
async ([toolName, args]) => {
    const tool = window.__webmcp_tools[toolName];
    if (!tool) {
        return { success: false, error: `Tool '${toolName}' not found` };
    }

    try {
        // This await is the key — it blocks until the tool's execute()
        // Promise resolves, which for real-world widgets means waiting
        // for the LLM to generate a response (could take 1-60 seconds).
        const result = await tool.execute(args);

        let answer = '';
        if (typeof result === 'string') {
            answer = result;
        } else if (result?.content && Array.isArray(result.content)) {
            const texts = result.content
                .filter(c => c.type === 'text')
                .map(c => c.text);
            answer = texts.join('\\n');
        } else if (result && typeof result === 'object') {
            answer = JSON.stringify(result);
        }

        return {
            success: !!answer && !answer.includes('timed out'),
            answer: answer,
        };
    } catch (e) {
        return { success: false, error: `Tool execute failed: ${e.message}` };
    }
}
"""


def _is_simple_question_tool(input_schema: dict[str, Any]) -> bool:
    """Check if a tool accepts a simple {question: str} input."""
    props = input_schema.get("properties", {})
    if not props:
        return True
    if "question" in props or "query" in props:
        return True
    return False


def _run_async(coro: Any) -> Any:
    """Run an async coroutine from sync code, handling nested event loops."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            import concurrent.futures

            with concurrent.futures.ThreadPoolExecutor() as pool:
                return pool.submit(asyncio.run, coro).result()
        return loop.run_until_complete(coro)
    except RuntimeError:
        return asyncio.run(coro)


async def _discover_and_call_async(
    url: str,
    question: Optional[str] = None,
    tool_name: Optional[str] = None,
    tool_args: Optional[dict[str, Any]] = None,
    timeout_ms: int = RESPONSE_TIMEOUT_MS,
) -> WebMCPResult:
    """Navigate to a URL, discover WebMCP tools, optionally call one.

    Args:
        url: The website URL to navigate to.
        question: Question to ask (for simple question-based tools).
        tool_name: Specific tool to call. If None, auto-selects.
        tool_args: Structured arguments for the tool.
        timeout_ms: How long to wait for the response.

    Returns:
        WebMCPResult with discovered tools and optional answer.
    """
    from playwright.async_api import async_playwright

    result = WebMCPResult(success=False)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--enable-features=WebMCP",
                "--disable-blink-features=AutomationControlled",
            ],
        )

        context = await browser.new_context(user_agent=_CHROME_USER_AGENT)
        page = await context.new_page()

        try:
            # Inject interception script before page loads
            await page.add_init_script(_INTERCEPT_SCRIPT)

            logger.info(f"Navigating to {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            result.page_title = await page.title()

            # Wait for WebMCP tools to register
            logger.info("Waiting for WebMCP tool registration...")
            try:
                await page.wait_for_function(
                    "window.__webmcp_ready === true && Object.keys(window.__webmcp_tools).length > 0",
                    timeout=TOOL_REGISTRATION_TIMEOUT_MS,
                )
            except Exception:
                has_mc = await page.evaluate("'modelContext' in navigator")
                if not has_mc:
                    result.error = "WebMCP not available — navigator.modelContext not found on this page"
                    return result

                tool_count = await page.evaluate("Object.keys(window.__webmcp_tools || {}).length")
                if tool_count == 0:
                    result.error = "No WebMCP tools registered on this page"
                    return result

            # Discover registered tools
            tools_raw = await page.evaluate(
                """
                Object.values(window.__webmcp_tools).map(t => ({
                    name: t.name,
                    description: t.description,
                    inputSchema: t.inputSchema,
                }))
            """
            )

            result.tools_discovered = [
                WebMCPTool(
                    name=t["name"],
                    description=t["description"],
                    input_schema=t.get("inputSchema", {}),
                )
                for t in tools_raw
            ]

            discovered_names = [t.name for t in result.tools_discovered]
            logger.info(f"Discovered {len(discovered_names)} tool(s): {discovered_names}")

            # If no question/args, just return discovery results
            if not question and not tool_args:
                result.success = True
                return result

            # Find the target tool
            target = tool_name
            if not target:
                if tool_args:
                    # Prefer structured tools when args are provided
                    for t in result.tools_discovered:
                        if not _is_simple_question_tool(t.input_schema):
                            target = t.name
                            break
                if not target:
                    for name in ["ask_question", "ask_question_chat", "ask", "chat", "search", "query"]:
                        if name in discovered_names:
                            target = name
                            break
                if not target and discovered_names:
                    target = discovered_names[0]

            if not target:
                result.error = "No callable tool found"
                return result

            result.tool_name = target

            # Determine call arguments
            target_obj = next((t for t in result.tools_discovered if t.name == target), None)
            target_schema = target_obj.input_schema if target_obj else {}

            if tool_args:
                call_args = tool_args
            elif _is_simple_question_tool(target_schema):
                call_args = {"question": question}
            else:
                # Structured tool but no args — return schema for caller to fill
                result.error = "structured_tool_requires_args"
                return result

            logger.info(f"Calling tool '{target}'")
            call_result = await page.evaluate(_CALL_TOOL_SCRIPT, [target, call_args])

            if call_result.get("success"):
                result.success = True
                result.answer = call_result.get("answer", "")
                return result

            answer = call_result.get("answer", "")
            if answer:
                result.success = True
                result.answer = answer
                return result

            result.error = call_result.get("error", "No response from tool")
            return result

        except Exception as e:
            logger.exception(f"Error during WebMCP interaction on {url}: {e}")
            result.error = str(e)
            return result

        finally:
            await browser.close()


def discover_tools(url: str) -> WebMCPResult:
    """Discover WebMCP tools on a website.

    Launches a headless browser, navigates to the URL, and intercepts
    any tools registered via navigator.modelContext.registerTool().

    Args:
        url: Website URL to check.

    Returns:
        WebMCPResult with discovered tools.
    """
    return _run_async(_discover_and_call_async(url))


def call_tool(
    url: str,
    question: Optional[str] = None,
    tool_name: Optional[str] = None,
    tool_args: Optional[dict[str, Any]] = None,
    timeout_ms: int = RESPONSE_TIMEOUT_MS,
) -> WebMCPResult:
    """Call a WebMCP tool on a website.

    For simple question-based tools, pass question.
    For structured tools, pass tool_name and tool_args.

    Args:
        url: Website URL with WebMCP tools.
        question: Question to ask (for simple tools).
        tool_name: Specific tool to call (auto-detected if None).
        tool_args: Structured arguments for the tool.
        timeout_ms: Response timeout in milliseconds.

    Returns:
        WebMCPResult with the tool's response.
    """
    return _run_async(
        _discover_and_call_async(
            url, question=question, tool_name=tool_name, tool_args=tool_args, timeout_ms=timeout_ms
        )
    )
