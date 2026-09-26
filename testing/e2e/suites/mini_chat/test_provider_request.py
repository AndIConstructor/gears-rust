"""Provider request body verification tests (offline only).

These tests inspect the request body that mini-chat sends to the LLM provider
via the mock provider's request capture. They verify that fields like
max_tool_calls, search_context_size, and max_num_results are serialized
correctly in the outgoing provider request.

Provider-parameterized — runs against both OpenAI and Azure mock endpoints.
"""

import httpx
import pytest

from .conftest import API_PREFIX, expect_done, poll_until, stream_message


@pytest.fixture(autouse=True)
def _clear_captures(mock_provider):
    """Clear captured requests before each test."""
    mock_provider.clear_captured_requests()


@pytest.fixture(autouse=True)
def _skip_online(request):
    """Skip these tests in online mode — mock capture only works offline."""
    if request.config.getoption("mode") == "online":
        pytest.skip("provider request capture requires offline mode")


@pytest.mark.multi_provider
class TestMaxToolCalls:
    """Verify max_tool_calls is sent in provider request body."""

    def test_max_tool_calls_present_in_request(self, provider_chat, mock_provider):
        """Provider request should include max_tool_calls from model config."""
        status, events, _ = stream_message(provider_chat["id"], "Say hello.")
        assert status == 200
        expect_done(events)

        req = mock_provider.get_last_request()
        assert req is not None, "No request captured by mock provider"
        assert "max_tool_calls" in req, (
            f"max_tool_calls missing from provider request body. Keys: {list(req.keys())}"
        )
        # max_tool_calls: 2 for both models (config/base.yaml), sent as a JSON integer.
        assert req["max_tool_calls"] == 2, (
            f"Expected max_tool_calls=2, got {req['max_tool_calls']}"
        )
        assert type(req["max_tool_calls"]) is int, req["max_tool_calls"]


@pytest.mark.multi_provider
class TestMaxOutputTokens:
    """14-08: the max_output_tokens cap reaches the provider."""

    def test_max_output_tokens_in_request(self, provider_chat, mock_provider):
        """max_output_tokens = min(catalog max_output_tokens 8192 of both
        default models, StreamingConfig max_output_tokens 32768) = 8192."""
        status, events, _ = stream_message(provider_chat["id"], "Say hello.")
        assert status == 200
        expect_done(events)

        req = mock_provider.get_last_request()
        assert req is not None, "No request captured by mock provider"
        assert req["max_output_tokens"] == 8192, req


@pytest.mark.multi_provider
class TestWebSearchToolType:
    """Verify web_search tool serialization in provider request."""

    def test_web_search_tool_type_is_web_search(self, provider_chat, mock_provider):
        """When web_search enabled, tool type should be 'web_search' (not 'web_search_preview')."""
        status, events, _ = stream_message(
            provider_chat["id"],
            "SEARCH: test query",
            web_search={"enabled": True},
        )
        assert status == 200
        expect_done(events)

        req = mock_provider.get_last_request()
        assert req is not None, "No request captured"
        tools = req.get("tools", [])
        ws_tools = [t for t in tools if t.get("type", "").startswith("web_search")]
        assert len(ws_tools) == 1, (
            f"Expected exactly one web_search tool, got {len(ws_tools)}. "
            f"Tools: {tools}"
        )
        assert ws_tools[0]["type"] == "web_search", (
            f"Expected type='web_search', got '{ws_tools[0]['type']}'"
        )

    def test_web_search_has_search_context_size(self, provider_chat, mock_provider):
        """web_search tool should include search_context_size."""
        status, _, _ = stream_message(
            provider_chat["id"],
            "SEARCH: test context size",
            web_search={"enabled": True},
        )
        assert status == 200
        req = mock_provider.get_last_request()
        assert req is not None
        tools = req.get("tools", [])
        ws_tools = [t for t in tools if t.get("type") == "web_search"]
        assert len(ws_tools) == 1
        # The catalog entries set no web_search_context_size: the default `low`
        # (WebSearchContextSize, mini-chat-sdk models.rs).
        assert ws_tools[0]["search_context_size"] == "low", ws_tools[0]

    def test_no_web_search_tool_without_flag(self, provider_chat, mock_provider):
        """Without web_search flag, no web_search tool in provider request."""
        status, _, _ = stream_message(provider_chat["id"], "Say hello.")
        assert status == 200
        req = mock_provider.get_last_request()
        assert req is not None
        tools = req.get("tools", [])
        ws_tools = [t for t in tools if t.get("type", "").startswith("web_search")]
        assert len(ws_tools) == 0, (
            f"Unexpected web_search tool without flag: {ws_tools}"
        )


@pytest.mark.multi_provider
class TestFileSearchMaxNumResults:
    """Verify file_search tool includes max_num_results."""

    def test_file_search_has_max_num_results(self, provider_chat, mock_provider):
        """A message with a ready document sends file_search with the catalog max_num_results."""
        chat_id = provider_chat["id"]
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/attachments",
            files={"file": ("test.txt", b"test content", "text/plain")},
            timeout=30,
        )
        assert resp.status_code == 201, resp.text
        att_id = resp.json()["id"]
        detail = poll_until(
            lambda: httpx.get(f"{API_PREFIX}/chats/{chat_id}/attachments/{att_id}", timeout=10),
            until=lambda r: r.json()["status"] in ("ready", "failed"),
            timeout=30,
        ).json()
        assert detail["status"] == "ready", detail

        mock_provider.clear_captured_requests()
        status, events, _ = stream_message(
            chat_id, "What does the attached file say?", attachment_ids=[att_id],
        )
        assert status == 200
        expect_done(events)

        tools = mock_provider.get_last_request()["tools"]
        fs_tools = [t for t in tools if t.get("type") == "file_search"]
        assert len(fs_tools) == 1, tools
        # max_num_results: 10 for both gpt-5.2 and azure-gpt-4.1 (config/base.yaml).
        assert fs_tools[0]["max_num_results"] == 10
