"""Tests for web search integration.

Offline mode: mock provider returns canned web_search tool events.
Online mode: real LLM provider performs actual web search.

Provider-parameterized — runs against both OpenAI and Azure.
Use ``-m openai`` or ``-m azure`` to target a single provider.
"""

import uuid

import pytest
import httpx

from .conftest import API_PREFIX, expect_done, expect_stream_started, parse_sse, poll_turn
from .mock_provider.responses import MockEvent, Scenario


def stream_search(chat_id: str, content: str, **extra) -> list:
    """Send `content` with web search enabled; return the SSE events of a `done` stream."""
    resp = httpx.post(
        f"{API_PREFIX}/chats/{chat_id}/messages:stream",
        json={"content": content, "web_search": {"enabled": True}, **extra},
        headers={"Accept": "text/event-stream"},
        timeout=90,
    )
    assert resp.status_code == 200, resp.text
    events = parse_sse(resp.text)
    expect_done(events)
    return events


def tool_events(events) -> list[tuple[str, str]]:
    """(name, phase) of each `tool` event."""
    return [(e.data["name"], e.data["phase"]) for e in events if e.event == "tool"]


@pytest.mark.multi_provider
class TestWebSearchBasic:
    """Web search happy path — tool events, usage, deltas."""

    @pytest.mark.usefixtures("offline_only")
    def test_web_search_tool_events_name_and_phases(self, provider_chat):
        """05-05, 18-01: the mock's one web search is sent as two `tool`
        events named `web_search`: phase `start`, then `done`."""
        events = stream_search(provider_chat["id"], "SEARCH: current weather in Berlin")
        assert tool_events(events) == [("web_search", "start"), ("web_search", "done")], events

    def test_web_search_done_has_usage(self, provider_chat):
        """Done event after web search should include usage with tokens."""
        events = stream_search(provider_chat["id"], "SEARCH: population of Tokyo")
        done = expect_done(events)
        ss = expect_stream_started(events)
        assert "request_id" in ss.data
        assert "message_id" in ss.data
        assert ss.data.get("is_new_turn") is True
        assert done.data["effective_model"] == provider_chat["model"]
        assert done.data["selected_model"] == provider_chat["model"]
        assert done.data["quota_decision"] == "allow"
        usage = done.data.get("usage")
        assert usage is not None, f"Done event missing usage: {done.data}"
        assert usage["input_tokens"] > 0
        assert usage["output_tokens"] > 0

    def test_web_search_has_delta_events(self, provider_chat):
        """Web search stream should still have delta text events."""
        events = stream_search(provider_chat["id"], "SEARCH: what year was Python created?")
        ss = expect_stream_started(events)
        assert "request_id" in ss.data
        assert "message_id" in ss.data
        assert ss.data.get("is_new_turn") is True

        deltas = [e for e in events if e.event == "delta"]
        assert len(deltas) > 0, "Expected delta events in web search response"
        text = "".join(e.data["content"] for e in deltas if isinstance(e.data, dict))
        assert len(text.strip()) > 0, "Assembled text from deltas is empty"


@pytest.mark.multi_provider
@pytest.mark.usefixtures("offline_only")
class TestWebSearchCitations:
    """Web search citation event structure, with the mock's one `url_citation`
    (mock_provider/responses.py, scenario `SEARCH:*`). Real providers may omit
    citations: see TestWebSearchOnline::test_citations_structure_if_present."""

    def test_web_search_produces_citations(self, provider_chat):
        """Web search emits one citations event with the mock's one citation."""
        events = stream_search(provider_chat["id"], "SEARCH: capital of Australia")
        citation_events = [e.data for e in events if e.event == "citations"]
        assert len(citation_events) == 1, [e.event for e in events]
        assert len(citation_events[0]["items"]) == 1, citation_events

    def test_citation_has_required_fields(self, provider_chat):
        """The citation carries source `web`, the title and the snippet."""
        events = stream_search(provider_chat["id"], "SEARCH: when was the Eiffel Tower built")
        (citations,) = [e.data for e in events if e.event == "citations"]
        (c,) = citations["items"]
        assert (c["source"], c["title"], c["snippet"]) == (
            "web", "Mock Search Result", "Searching",
        ), c

    def test_web_citation_has_url(self, provider_chat):
        """A web citation carries the URL of the source."""
        events = stream_search(provider_chat["id"], "SEARCH: population of Japan")
        (citations,) = [e.data for e in events if e.event == "citations"]
        assert [c["url"] for c in citations["items"]] == ["https://example.com"], citations


@pytest.mark.multi_provider
class TestWebSearchEventOrdering:
    """SSE event grammar: ping* (delta|tool)* citations? (done|error)"""

    @pytest.mark.usefixtures("offline_only")
    def test_citations_before_done(self, provider_chat):
        """One `citations` event, sent right before `done` (grammar: ... citations? done)."""
        events = stream_search(provider_chat["id"], "SEARCH: capital of France")
        types = [e.event for e in events]
        assert types.count("citations") == 1, types
        assert types[-2:] == ["citations", "done"], types

    def test_tool_events_before_done(self, provider_chat):
        """Tool events must appear before the terminal done event."""
        events = stream_search(
            provider_chat["id"], "SEARCH: who won the latest Nobel Prize in Physics?",
        )
        done_idx = next(i for i, e in enumerate(events) if e.event == "done")
        tool_idx = [i for i, e in enumerate(events) if e.event == "tool"]
        assert len(tool_idx) > 0, (
            f"Expected tool events for web search but got none. "
            f"Event types: {[e.event for e in events]}"
        )
        assert max(tool_idx) < done_idx, [e.event for e in events]


@pytest.mark.multi_provider
class TestWebSearchDisabledByDefault:
    """When web_search is not requested, no tool events should appear."""

    def test_no_tool_events_without_web_search(self, provider_chat):
        """A normal message (no web_search flag) should not trigger web search."""
        resp = httpx.post(
            f"{API_PREFIX}/chats/{provider_chat['id']}/messages:stream",
            json={"content": "What is 2+2? Answer in one word."},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        expect_done(events)

        tool_events = [e for e in events if e.event == "tool"]
        assert len(tool_events) == 0, (
            f"Unexpected tool events without web_search: {[t.data for t in tool_events]}"
        )

    def test_no_citations_without_web_search(self, provider_chat):
        """A normal message should not produce citation events."""
        resp = httpx.post(
            f"{API_PREFIX}/chats/{provider_chat['id']}/messages:stream",
            json={"content": "Say hello."},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        expect_done(events)

        citations = [e for e in events if e.event == "citations"]
        assert len(citations) == 0, "Unexpected citations without web_search"


@pytest.mark.multi_provider
class TestWebSearchTurnStatus:
    """Turn status should reflect web search completion."""

    def test_turn_done_after_web_search(self, provider_chat):
        """Turn state should be 'done' after a successful web search stream."""
        chat_id = provider_chat["id"]
        request_id = str(uuid.uuid4())
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "SEARCH: speed of light", "web_search": {"enabled": True}, "request_id": request_id},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        expect_done(events)

        resp = httpx.get(f"{API_PREFIX}/chats/{chat_id}/turns/{request_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "done"
        assert "updated_at" in body, "turn status must have updated_at"
        assert body.get("assistant_message_id") is not None, "done turn must have assistant_message_id"

    def test_messages_persisted_after_web_search(self, provider_chat):
        """Both user and assistant messages should be persisted after web search."""
        chat_id = provider_chat["id"]
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "SEARCH: when was the Eiffel Tower built?", "web_search": {"enabled": True}},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        expect_done(events)

        resp = httpx.get(f"{API_PREFIX}/chats/{chat_id}/messages")
        assert resp.status_code == 200
        msgs = resp.json()["items"]
        roles = [m["role"] for m in msgs]
        assert "user" in roles
        assert "assistant" in roles


class TestWebSearchPerMessageLimit:
    """At most `quota.web_search_max_calls_per_message` (2, QuotaConfig default
    in config.rs, not overridden in base.yaml) web searches per message."""

    @pytest.mark.usefixtures("offline_only")
    def test_third_web_search_fails_the_turn(self, chat, mock_provider):
        """18-10: the provider starts a third web search in one answer: the
        stream ends with SSE `error` `web_search_calls_exceeded` and the turn
        fails."""
        search = [
            MockEvent("response.web_search_call.searching", {}),
            MockEvent("response.web_search_call.completed", {}),
        ]
        mock_provider.set_next_scenario(Scenario(events=[
            *search, *search, *search,
            MockEvent("response.output_text.delta", {"delta": "Too many searches"}),
            MockEvent("response.output_text.done", {"text": "Too many searches"}),
        ]))
        rid = str(uuid.uuid4())
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat['id']}/messages:stream",
            json={"content": "SEARCH: everything", "web_search": {"enabled": True},
                  "request_id": rid},
            headers={"Accept": "text/event-stream"},
            timeout=30,
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        assert events[-1].event == "error", [e.event for e in events]
        assert events[-1].data["code"] == "web_search_calls_exceeded"
        assert poll_turn(chat["id"], rid)["state"] == "error"


# ── Online-only tests ────────────────────────────────────────────────────
# Require real provider responses (natural language quality).

@pytest.mark.multi_provider
@pytest.mark.online_only
class TestWebSearchOnline:
    """Online web search tests — provider-parameterized."""

    def test_web_search_produces_meaningful_answer(self, provider_chat):
        """Real web search should produce a substantive text answer."""
        resp = httpx.post(
            f"{API_PREFIX}/chats/{provider_chat['id']}/messages:stream",
            json={"content": "Search the web: who is the current president of France?", "web_search": {"enabled": True}},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        expect_done(events)

        text = "".join(
            e.data["content"] for e in events
            if e.event == "delta" and isinstance(e.data, dict)
        )
        assert len(text) > 20, f"Answer too short for a real web search: '{text}'"

    def test_web_search_tool_event_has_name(self, provider_chat):
        """Real provider should emit tool events with web_search name."""
        resp = httpx.post(
            f"{API_PREFIX}/chats/{provider_chat['id']}/messages:stream",
            json={"content": "What is the current weather in Berlin right now? Search the web.", "web_search": {"enabled": True}},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert resp.status_code == 200
        events = parse_sse(resp.text)
        expect_done(events)

        assert ("web_search", "done") in tool_events(events), tool_events(events)

    def test_citations_structure_if_present(self, provider_chat):
        """A real provider may answer without citations (then the test skips);
        when it cites, there is one `citations` event right before `done`
        and each item is a web citation with title, snippet and URL."""
        events = stream_search(provider_chat["id"], "Search the web: capital of Australia")
        types = [e.event for e in events]
        if "citations" not in types:
            pytest.skip("Provider did not return citations for this query")
        assert types.count("citations") == 1, types
        assert types[-2:] == ["citations", "done"], types
        (citations,) = [e.data for e in events if e.event == "citations"]
        assert len(citations["items"]) > 0, citations
        for c in citations["items"]:
            assert c["source"] == "web", c
            assert c["title"] and c["snippet"], c
            assert c["url"].startswith("http"), c
