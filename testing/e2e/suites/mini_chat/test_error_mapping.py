"""Tests for error mapping: provider errors -> client-facing SSE error events.

A provider failure after the stream opened is an SSE `error` event with
`{code, message}` (ADR-0004); the codes are listed in DESIGN §3.3
"Streaming error codes".
"""

import httpx
import pytest

from .conftest import API_PREFIX, expect_stream_started, parse_sse, poll_turn
from .mock_provider.responses import MockEvent, Scenario

# Provider identifiers the sanitizer must scrub (shapes of real IDs; the
# storage-ID pattern needs at least 12 characters after the prefix).
PROVIDER_IDS = (
    "resp_0a1b2c3d4e5f6a7b8c9d",
    "file-AbCdEf0123456789XyZ",
    "vs_0123456789abcdefABCD",
    "assistant-0123456789abcdEF",
)
LEAKY_MESSAGE = "Upstream failure for " + ", ".join(PROVIDER_IDS)



def _stream_error(chat_id: str) -> tuple[dict, str]:
    """Send a message and return (error event data, request_id)."""
    resp = httpx.post(
        f"{API_PREFIX}/chats/{chat_id}/messages:stream",
        json={"content": "trigger error"},
        headers={"Accept": "text/event-stream"},
        timeout=90,
    )
    assert resp.status_code == 200, f"expected an SSE stream, got {resp.status_code}: {resp.text}"
    events = parse_sse(resp.text)
    assert [e.event for e in events][-1] == "error", [e.event for e in events]
    rid = expect_stream_started(events).data["request_id"]
    return events[-1].data, rid


class TestErrorMapping:
    """Provider-level errors map to stable streaming error codes."""

    @pytest.fixture(autouse=True)
    def _skip_online(self, request):
        if request.config.getoption("mode") == "online":
            pytest.skip("requires mock provider (offline mode)")

    def test_post_stream_sse_error_event(self, chat, mock_provider):
        """A `response.failed` mid-stream is an SSE error `provider_error`; the turn fails."""
        mock_provider.set_next_scenario(Scenario(
            terminal="failed",
            error={"code": "server_error", "message": "Mock fail"},
            events=[MockEvent("response.output_text.delta", {"delta": "Partial"})],
        ))
        data, rid = _stream_error(chat["id"])
        assert data["code"] == "provider_error"
        assert isinstance(data["message"], str) and data["message"]
        assert poll_turn(chat["id"], rid)["state"] == "error"

    def test_provider_504_is_provider_error(self, chat, mock_provider):
        """An HTTP 504 returned by the provider is a non-429 provider error: `provider_error`.

        `provider_timeout` is for the gateway's own timeout
        (test_provider_timeout_error_code).
        """
        mock_provider.set_next_scenario(Scenario(
            http_error_status=504,
            http_error_body={"error": {"message": "Gateway Timeout", "type": "timeout"}},
        ))
        data, _ = _stream_error(chat["id"])
        assert data["code"] == "provider_error"

    @pytest.mark.timeout(30)
    def test_provider_timeout_error_code(self, chat, mock_provider):
        """17-03: the provider sends no response headers within OAGW
        `proxy_timeout_secs` (8 s in base.yaml): SSE error `provider_timeout`;
        the turn fails."""
        mock_provider.set_next_scenario(Scenario(header_delay=10))
        data, rid = _stream_error(chat["id"])
        assert data["code"] == "provider_timeout", data
        turn = poll_turn(chat["id"], rid)
        assert turn["state"] == "error"

    def test_provider_unavailable_error_code(self, chat, mock_provider):
        """An HTTP 503 from the provider is `provider_error`."""
        mock_provider.set_next_scenario(Scenario(
            http_error_status=503,
            http_error_body={"error": {"message": "Service Unavailable", "type": "server_error"}},
        ))
        data, _ = _stream_error(chat["id"])
        assert data["code"] == "provider_error"

    def test_rate_limited_error_code(self, chat, mock_provider):
        """An HTTP 429 from the provider is `rate_limited`."""
        mock_provider.set_next_scenario(Scenario(
            http_error_status=429,
            http_error_body={"error": {"message": "Rate limited", "type": "rate_limit_error"}},
        ))
        data, _ = _stream_error(chat["id"])
        assert data["code"] == "rate_limited"

    @pytest.mark.parametrize("source", ["response_failed", "http_500"])
    def test_error_message_no_provider_ids(self, chat, mock_provider, source):
        """Provider response, file, vector store and assistant IDs never reach the client."""
        if source == "response_failed":
            scenario = Scenario(
                terminal="failed",
                error={"code": "server_error", "message": LEAKY_MESSAGE},
                events=[MockEvent("response.output_text.delta", {"delta": "x"})],
            )
        else:
            scenario = Scenario(
                http_error_status=500,
                http_error_body={"error": {"message": LEAKY_MESSAGE, "type": "server_error"}},
            )
        mock_provider.set_next_scenario(scenario)
        data, _ = _stream_error(chat["id"])
        assert data["code"] == "provider_error"
        for provider_id in PROVIDER_IDS:
            assert provider_id not in data["message"], (
                f"provider id {provider_id} leaked: {data['message']!r}"
            )
