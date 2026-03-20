"""Fast HTTP-based WebMCP detection without a browser.

Scans page HTML for:
1. WebMCP declarative forms (<form tool-name="...">)
2. WebMCP imperative registration (navigator.modelContext.registerTool)
3. Generic navigator.modelContext references
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

_USER_AGENT = "Mozilla/5.0 (compatible; WebMCP-Gateway/0.1)"


@dataclass
class DetectedTool:
    """A tool detected via HTML scanning."""

    name: str
    description: str = ""


@dataclass
class DetectionResult:
    """Result of fast WebMCP detection."""

    found: bool
    provider: str  # "webmcp_declarative" | "webmcp_imperative" | "webmcp_generic" | "none"
    tools: list[DetectedTool] = field(default_factory=list)
    detection_method: str = "http_fetch"


def detect_webmcp_fast(url: str, timeout: float = 10.0) -> DetectionResult:
    """Check if a URL has WebMCP tools via simple HTTP fetch + regex.

    This is a lightweight pre-check before launching a full browser.
    It can detect tools declared in HTML but cannot execute them.

    Args:
        url: Website URL to check.
        timeout: HTTP request timeout in seconds.

    Returns:
        DetectionResult with found status and any detected tools.
    """
    try:
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            resp = client.get(url, headers={"User-Agent": _USER_AGENT})
            html = resp.text
    except Exception as e:
        logger.debug(f"HTTP fetch failed for {url}: {e}")
        return DetectionResult(found=False, provider="none")

    # 1. WebMCP declarative forms (<form tool-name="ask_question">)
    form_tags = re.findall(r'<form\b([^>]*\btool-name=[^>]*)>', html, re.I)
    if form_tags:
        tools = []
        for attrs in form_tags:
            name_m = re.search(r'tool-name=["\']([^"\']+)["\']', attrs)
            desc_m = re.search(r'tool-description=["\']([^"\']+)["\']', attrs)
            if name_m:
                tools.append(DetectedTool(name=name_m.group(1), description=desc_m.group(1) if desc_m else ""))
        if tools:
            logger.info(f"Detected {len(tools)} declarative WebMCP tool(s) on {url}")
            return DetectionResult(found=True, provider="webmcp_declarative", tools=tools)

    # 2. WebMCP imperative registration in inline scripts
    register_matches = re.findall(
        r'(?:navigator\.modelContext|modelContext)\.registerTool\s*\(\s*\{[^}]*name\s*:\s*["\']([^"\']+)["\']',
        html,
    )
    if register_matches:
        tools = [DetectedTool(name=name) for name in register_matches]
        logger.info(f"Detected {len(tools)} imperative WebMCP tool(s) on {url}")
        return DetectionResult(found=True, provider="webmcp_imperative", tools=tools)

    # 3. Generic navigator.modelContext reference
    if re.search(r"navigator\.modelContext", html):
        logger.info(f"navigator.modelContext reference found on {url} (tools unknown)")
        return DetectionResult(found=True, provider="webmcp_generic")

    return DetectionResult(found=False, provider="none")
