"""Tests for browser-based WebMCP discovery."""

from webmcp_gateway.browser import WebMCPTool, _is_simple_question_tool


class TestIsSimpleQuestionTool:
    def test_empty_schema(self):
        assert _is_simple_question_tool({}) is True

    def test_question_property(self):
        schema = {"properties": {"question": {"type": "string"}}}
        assert _is_simple_question_tool(schema) is True

    def test_query_property(self):
        schema = {"properties": {"query": {"type": "string"}}}
        assert _is_simple_question_tool(schema) is True

    def test_structured_schema(self):
        schema = {
            "properties": {
                "destination": {"type": "string"},
                "date": {"type": "string"},
                "passengers": {"type": "integer"},
            }
        }
        assert _is_simple_question_tool(schema) is False

    def test_no_properties_key(self):
        schema = {"type": "object"}
        assert _is_simple_question_tool(schema) is True
