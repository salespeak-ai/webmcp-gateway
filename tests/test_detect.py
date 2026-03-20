"""Tests for fast HTTP-based WebMCP detection."""

from unittest.mock import patch, MagicMock

from webmcp_gateway.detect import detect_webmcp_fast, DetectionResult


def _mock_response(html: str) -> MagicMock:
    resp = MagicMock()
    resp.text = html
    return resp


class TestDetectWebMCPFast:
    def test_no_webmcp_signals(self):
        html = "<html><body><h1>Hello World</h1></body></html>"
        with patch("webmcp_gateway.detect.httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = lambda s: s
            mock_client.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.return_value.get.return_value = _mock_response(html)
            result = detect_webmcp_fast("https://example.com")

        assert not result.found
        assert result.provider == "none"
        assert result.tools == []

    def test_declarative_form(self):
        html = '''
        <html><body>
        <form tool-name="ask_question" tool-description="Ask a question about our products">
            <input name="question" />
        </form>
        </body></html>
        '''
        with patch("webmcp_gateway.detect.httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = lambda s: s
            mock_client.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.return_value.get.return_value = _mock_response(html)
            result = detect_webmcp_fast("https://example.com")

        assert result.found
        assert result.provider == "webmcp_declarative"
        assert len(result.tools) == 1
        assert result.tools[0].name == "ask_question"
        assert result.tools[0].description == "Ask a question about our products"

    def test_imperative_registration(self):
        html = '''
        <html><body>
        <script>
        navigator.modelContext.registerTool({
            name: "search_products",
            description: "Search our catalog",
            execute: async (args) => { return "results"; }
        });
        </script>
        </body></html>
        '''
        with patch("webmcp_gateway.detect.httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = lambda s: s
            mock_client.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.return_value.get.return_value = _mock_response(html)
            result = detect_webmcp_fast("https://example.com")

        assert result.found
        assert result.provider == "webmcp_imperative"
        assert len(result.tools) == 1
        assert result.tools[0].name == "search_products"

    def test_generic_model_context(self):
        html = '''
        <html><body>
        <script>
        if (navigator.modelContext) {
            console.log("WebMCP supported");
        }
        </script>
        </body></html>
        '''
        with patch("webmcp_gateway.detect.httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = lambda s: s
            mock_client.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.return_value.get.return_value = _mock_response(html)
            result = detect_webmcp_fast("https://example.com")

        assert result.found
        assert result.provider == "webmcp_generic"
        assert result.tools == []

    def test_http_error_returns_not_found(self):
        with patch("webmcp_gateway.detect.httpx.Client") as mock_client:
            mock_client.return_value.__enter__ = lambda s: s
            mock_client.return_value.__exit__ = MagicMock(return_value=False)
            mock_client.return_value.get.side_effect = Exception("Connection refused")
            result = detect_webmcp_fast("https://unreachable.example.com")

        assert not result.found
        assert result.provider == "none"
