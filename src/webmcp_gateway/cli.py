"""CLI entry point for WebMCP Gateway."""

import argparse
import logging
import sys


def main() -> None:
    """Start the WebMCP Gateway MCP server."""
    parser = argparse.ArgumentParser(
        description="WebMCP Gateway — MCP server that bridges AI assistants to WebMCP-enabled websites"
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport (default: stdio for Claude Desktop, sse for remote)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Host for SSE transport (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8808, help="Port for SSE transport (default: 8808)")
    parser.add_argument("--log-level", default="INFO", help="Log level (default: INFO)")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    from webmcp_gateway.server import mcp

    if args.transport == "stdio":
        mcp.run(transport="stdio")
    else:
        mcp.run(transport="sse", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
