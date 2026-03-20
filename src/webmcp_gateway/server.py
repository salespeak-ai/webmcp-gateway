"""MCP server that exposes WebMCP gateway tools.

This is the main entry point — an MCP server that AI assistants (Claude, ChatGPT)
connect to. It exposes three tools:

1. check_webmcp(url) — Fast HTTP check if a URL has WebMCP tools
2. discover_tools(url) — Full browser-based tool discovery
3. call_tool(url, question, tool_name, tool_args) — Call a WebMCP tool on a website

The server bridges the gap: AI assistants speak MCP, websites speak WebMCP,
and this gateway translates between them using a headless browser.
"""

import json
import logging
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP

from webmcp_gateway.browser import call_tool as browser_call_tool
from webmcp_gateway.browser import discover_tools as browser_discover_tools
from webmcp_gateway.detect import detect_webmcp_fast

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "WebMCP Gateway",
    instructions=(
        "This server lets you interact with any website that exposes WebMCP tools. "
        "Use check_webmcp to quickly test if a URL supports WebMCP. "
        "Use discover_tools to find available tools on a website. "
        "Use call_tool to invoke a tool and get results. "
        "Behind the scenes, a headless browser navigates to the website and "
        "interacts with the site's WebMCP interface."
    ),
)


@mcp.tool()
def check_webmcp(url: str) -> str:
    """Quickly check if a website supports WebMCP tools (no browser needed).

    Performs a fast HTTP fetch and scans the HTML for WebMCP signals:
    - Declarative forms with tool-name attributes
    - navigator.modelContext.registerTool() calls in inline scripts
    - Any navigator.modelContext references

    This is a lightweight pre-check. Use discover_tools for full discovery.

    Args:
        url: The website URL to check (e.g. "https://example.com").

    Returns:
        JSON with detection results: found (bool), provider type, and detected tool names.
    """
    result = detect_webmcp_fast(url)
    return json.dumps(
        {
            "url": url,
            "webmcp_detected": result.found,
            "provider": result.provider,
            "tools": [{"name": t.name, "description": t.description} for t in result.tools],
            "detection_method": result.detection_method,
            "note": (
                "This is a fast HTTP-based check. Use discover_tools for full browser-based "
                "discovery that can find dynamically registered tools."
                if not result.found
                else "WebMCP tools detected! Use discover_tools for full schemas, or call_tool to invoke them."
            ),
        },
        indent=2,
    )


@mcp.tool()
def discover_tools(url: str) -> str:
    """Discover all WebMCP tools registered on a website using a headless browser.

    Launches Chromium, navigates to the URL, and intercepts calls to
    navigator.modelContext.registerTool(). Returns full tool definitions
    including names, descriptions, and input schemas.

    This is slower than check_webmcp but finds dynamically registered tools
    and returns complete schemas needed for calling structured tools.

    Args:
        url: The website URL to discover tools on.

    Returns:
        JSON with discovered tools including their input schemas.
    """
    result = browser_discover_tools(url)
    tools_data = []
    for t in result.tools_discovered:
        tool_info: dict[str, Any] = {"name": t.name, "description": t.description}
        if t.input_schema:
            tool_info["input_schema"] = t.input_schema
        tools_data.append(tool_info)

    return json.dumps(
        {
            "url": url,
            "success": result.success,
            "page_title": result.page_title,
            "tools_count": len(tools_data),
            "tools": tools_data,
            "error": result.error,
        },
        indent=2,
    )


@mcp.tool()
def call_tool(
    url: str,
    question: Optional[str] = None,
    tool_name: Optional[str] = None,
    tool_args: Optional[str] = None,
) -> str:
    """Call a WebMCP tool on a website and return the result.

    Opens a headless browser, navigates to the URL, discovers tools, and
    calls the specified (or auto-detected) tool.

    For simple question-based tools (e.g. "ask_question"), just pass the question.
    For structured tools (e.g. "searchFlights"), pass tool_name and tool_args as JSON.

    If a structured tool is found but no tool_args provided, returns the tool's
    input schema so you can call again with the right arguments.

    Args:
        url: The website URL with WebMCP tools.
        question: Question to ask (for simple Q&A tools).
        tool_name: Specific tool to call (auto-detected if omitted).
        tool_args: JSON string of arguments for structured tools.

    Returns:
        JSON with the tool's response, including answer text and metadata.
    """
    parsed_args = None
    if tool_args:
        try:
            parsed_args = json.loads(tool_args)
        except json.JSONDecodeError:
            return json.dumps({"success": False, "error": f"Invalid JSON in tool_args: {tool_args}"})

    if not question and not parsed_args:
        return json.dumps({"success": False, "error": "Provide either 'question' or 'tool_args'"})

    result = browser_call_tool(url=url, question=question, tool_name=tool_name, tool_args=parsed_args)

    response: dict[str, Any] = {
        "url": url,
        "success": result.success,
        "tool_name": result.tool_name,
        "page_title": result.page_title,
    }

    if result.success:
        response["answer"] = result.answer
    else:
        response["error"] = result.error
        # If structured tool needs args, include the schema
        if result.error == "structured_tool_requires_args":
            response["hint"] = "This tool requires structured arguments. See the input_schema below."
            for t in result.tools_discovered:
                if t.name == result.tool_name:
                    response["input_schema"] = t.input_schema
                    break

    response["tools_available"] = [
        {"name": t.name, "description": t.description} for t in result.tools_discovered
    ]

    return json.dumps(response, indent=2)
