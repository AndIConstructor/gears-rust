"""Tests verifying context assembly sends conversation history to the LLM.

These tests validate that the context assembly pipeline (system prompt,
recent messages, tools) works correctly end-to-end by observing:
- Input token growth across turns (proves history is sent)
- Model ability to recall earlier conversation content
- Correct behavior with multi-turn context
"""

import uuid

import httpx

from .conftest import (
    API_PREFIX,
    BARE_MODEL,
    CATALOG_SYSTEM_PROMPT,
    DEFAULT_MODEL,
    WEB_SEARCH_GUARD,
    delta_text,
    expect_done,
    list_messages,
    open_stream,
    parse_sse,
    poll_turn,
    provider_input,
    slow_scenario,
    stream_message,
)

import pytest


def _require_offline(request):
    if request.config.getoption("mode") == "online":
        pytest.skip("inspects the mock provider's captured request")


def _send_and_capture(mock_provider, chat_id: str, content: str, **extra) -> dict:
    """Complete one turn; return the provider request body it produced."""
    mock_provider.clear_captured_requests()
    status, events, raw = stream_message(chat_id, content, **extra)
    assert status == 200, raw
    expect_done(events)
    captured = mock_provider.get_captured_requests()
    assert len(captured) == 1, f"expected one provider request, got {len(captured)}"
    return captured[0]


@pytest.mark.multi_provider
class TestSystemPrompt:
    """The catalog system prompt is delivered to the provider as `instructions`."""

    def test_system_prompt_sent_as_instructions(self, request, provider_chat, mock_provider):
        """Offline: the captured provider request carries the catalog system prompt."""
        if request.config.getoption("mode") == "online":
            pytest.skip("inspects the mock provider's captured request")
        _resp = httpx.post(
            f"{API_PREFIX}/chats/{provider_chat['id']}/messages:stream",
            json={"content": "Hello"},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        expect_done(parse_sse(_resp.text))

        instructions = mock_provider.get_last_request()["instructions"]
        assert CATALOG_SYSTEM_PROMPT in instructions

    @pytest.mark.online_only
    def test_ping_pong_proves_system_prompt(self, provider_chat):
        """Online: 'PING' gets 'PONG' only because the system prompt says so."""
        _resp = httpx.post(
            f"{API_PREFIX}/chats/{provider_chat['id']}/messages:stream",
            json={"content": "PING"},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        events = parse_sse(_resp.text) if _resp.status_code == 200 else []
        expect_done(events)

        text = "".join(e.data["content"] for e in events if e.event == "delta")
        assert "PONG" in text.upper(), (
            f"System prompt instructs model to reply 'PONG' to 'PING'. Got: {text!r}"
        )


class TestSystemInstructions:
    """Tool guards and a missing system prompt in the provider `instructions`."""

    def test_web_search_guard_appended(self, request, chat_with_model, mock_provider):
        """16-14: with web search enabled the `instructions` are the catalog
        prompt followed by the web_search guard; without it, the guard is absent."""
        _require_offline(request)
        chat_id = chat_with_model(DEFAULT_MODEL)["id"]  # web_search-capable model

        with_ws = _send_and_capture(
            mock_provider, chat_id, "SEARCH: weather", web_search={"enabled": True},
        )["instructions"]
        assert with_ws.startswith(CATALOG_SYSTEM_PROMPT), with_ws
        assert with_ws.endswith(WEB_SEARCH_GUARD), with_ws

        without_ws = _send_and_capture(mock_provider, chat_id, "No search.")["instructions"]
        assert without_ws == CATALOG_SYSTEM_PROMPT

    def test_no_system_prompt_no_instructions(self, request, chat_with_model, mock_provider):
        """16-15: a model without a system prompt (and no tools) sends no `instructions`."""
        _require_offline(request)
        chat_id = chat_with_model(BARE_MODEL)["id"]
        body = _send_and_capture(mock_provider, chat_id, "Hello")
        assert "instructions" not in body, body.get("instructions")


class TestCancelledTurnContext:
    """A cancelled turn contributes to the next context only through its persisted text."""

    @pytest.mark.timeout(30)
    def test_cancelled_partial_answer_in_next_request(self, request, chat, mock_provider):
        """16-12: the partial answer persisted at the cancel point is sent to the
        provider as the assistant message of that turn."""
        _require_offline(request)
        chat_id = chat["id"]
        rid = str(uuid.uuid4())
        mock_provider.set_next_scenario(slow_scenario(20, slow=0.3, prefix="part"))

        with open_stream(chat_id, "Write a long essay.", request_id=rid) as s:
            seen = []
            s.read_until(lambda e: e.event == "delta" and (seen.append(e) or len(seen) == 3))
            received = delta_text(s.events)
        assert poll_turn(chat_id, rid)["state"] == "cancelled"

        partial = [
            m["content"] for m in list_messages(chat_id)
            if m["role"] == "assistant" and m["request_id"] == rid
        ]
        assert len(partial) == 1, list_messages(chat_id)
        # The gear may have read a few more deltas than the client before the cancel.
        assert partial[0].startswith(received), (partial[0], received)

        body = _send_and_capture(mock_provider, chat_id, "Continue.")
        assert provider_input(body) == [
            ("user", "Write a long essay."),
            ("assistant", partial[0]),
            ("user", "Continue."),
        ]

    @pytest.mark.timeout(30)
    def test_empty_cancelled_turn_adds_no_message(self, request, chat, mock_provider):
        """16-13: a turn cancelled before the first delta leaves no assistant
        message, so the next provider request has none for it."""
        _require_offline(request)
        chat_id = chat["id"]
        rid = str(uuid.uuid4())
        scenario = slow_scenario(3, slow=0.5)
        scenario.initial_delay = 3.0  # provider silent after the headers
        mock_provider.set_next_scenario(scenario)

        with open_stream(chat_id, "Write slowly.", request_id=rid) as s:
            s.read_until_started()
        assert poll_turn(chat_id, rid)["state"] == "cancelled"

        body = _send_and_capture(mock_provider, chat_id, "Are you there?")
        assert provider_input(body) == [
            ("user", "Write slowly."),
            ("user", "Are you there?"),
        ]


@pytest.mark.multi_provider
class TestContextInputTokenGrowth:
    """Input tokens should increase as conversation context grows."""

    def test_input_tokens_increase_with_turns(self, provider_chat):
        """Turn 2 input_tokens > turn 1 input_tokens, proving history is sent."""
        chat_id = provider_chat["id"]

        # Turn 1
        _r1 = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "What is the capital of France?"},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        ev1 = parse_sse(_r1.text) if _r1.status_code == 200 else []
        done1 = expect_done(ev1)
        assert "effective_model" in done1.data, "done must have effective_model"
        assert "selected_model" in done1.data, "done must have selected_model"
        assert done1.data.get("quota_decision") in ("allow", "downgrade"), f"unexpected quota_decision: {done1.data.get('quota_decision')}"
        assert done1.data.get("usage", {}).get("output_tokens", 0) > 0, "done usage must have output_tokens > 0"
        input_tokens_1 = done1.data["usage"]["input_tokens"]

        # Turn 2 — context now includes turn 1 (user + assistant)
        _r2 = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "And what about Germany?"},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        ev2 = parse_sse(_r2.text) if _r2.status_code == 200 else []
        done2 = expect_done(ev2)
        assert "effective_model" in done2.data, "done must have effective_model"
        assert "selected_model" in done2.data, "done must have selected_model"
        assert done2.data.get("quota_decision") in ("allow", "downgrade"), f"unexpected quota_decision: {done2.data.get('quota_decision')}"
        assert done2.data.get("usage", {}).get("output_tokens", 0) > 0, "done usage must have output_tokens > 0"
        input_tokens_2 = done2.data["usage"]["input_tokens"]

        # Turn 2 must have strictly more input tokens (it includes turn 1 history)
        assert input_tokens_2 > input_tokens_1, (
            f"Turn 2 input_tokens ({input_tokens_2}) should be greater than "
            f"turn 1 ({input_tokens_1}) because conversation history is included"
        )

    def test_input_tokens_grow_over_three_turns(self, provider_chat):
        """Input tokens should monotonically increase across 3 turns."""
        chat_id = provider_chat["id"]
        input_tokens = []

        prompts = [
            "Name a fruit that starts with A.",
            "Name one that starts with B.",
            "Name one that starts with C.",
        ]

        for prompt in prompts:
            _r = httpx.post(
                f"{API_PREFIX}/chats/{chat_id}/messages:stream",
                json={"content": prompt},
                headers={"Accept": "text/event-stream"},
                timeout=90,
            )
            assert _r.status_code == 200
            events = parse_sse(_r.text)
            done = expect_done(events)
            assert "effective_model" in done.data, "done must have effective_model"
            assert "selected_model" in done.data, "done must have selected_model"
            assert done.data.get("quota_decision") in ("allow", "downgrade"), f"unexpected quota_decision: {done.data.get('quota_decision')}"
            assert done.data.get("usage", {}).get("output_tokens", 0) > 0, "done usage must have output_tokens > 0"
            input_tokens.append(done.data["usage"]["input_tokens"])

        # Each turn should have more input tokens than the previous
        for i in range(1, len(input_tokens)):
            assert input_tokens[i] > input_tokens[i - 1], (
                f"Turn {i + 1} input_tokens ({input_tokens[i]}) should be greater than "
                f"turn {i} ({input_tokens[i - 1]})"
            )


@pytest.mark.multi_provider
@pytest.mark.online_only
class TestContextRecall:
    """Model should recall information from earlier turns, proving context is sent."""

    def test_recall_specific_number(self, provider_chat):
        """Model must recall a specific number from an earlier turn."""
        chat_id = provider_chat["id"]

        # Turn 1: tell the model a specific fact
        _r1 = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "Remember this number: 73921. Just confirm you got it."},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        s1 = _r1.status_code
        ev1 = parse_sse(_r1.text) if s1 == 200 else []
        assert s1 == 200
        expect_done(ev1)

        # Turn 2: ask it to recall
        _r2 = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "What was the number I told you to remember? Reply with just the number."},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        s2 = _r2.status_code
        ev2 = parse_sse(_r2.text) if s2 == 200 else []
        assert s2 == 200
        expect_done(ev2)

        text = "".join(e.data["content"] for e in ev2 if e.event == "delta")
        assert "73921" in text, (
            f"Model should recall '73921' from conversation history. Got: {text!r}"
        )

    def test_recall_after_intervening_turn(self, provider_chat):
        """Model recalls info from turn 1 even after an unrelated turn 2."""
        chat_id = provider_chat["id"]

        # Turn 1: establish a fact
        _r1 = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "The secret word is PELICAN. Acknowledge it."},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert _r1.status_code == 200
        expect_done(parse_sse(_r1.text))

        # Turn 2: unrelated topic
        _r2 = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "What is 5 + 3? Reply with just the number."},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        assert _r2.status_code == 200
        expect_done(parse_sse(_r2.text))

        # Turn 3: recall turn 1
        _r3 = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream",
            json={"content": "What was the secret word I told you earlier? Reply with just the word."},
            headers={"Accept": "text/event-stream"},
            timeout=90,
        )
        ev3 = parse_sse(_r3.text) if _r3.status_code == 200 else []
        expect_done(ev3)

        text = "".join(e.data["content"] for e in ev3 if e.event == "delta")
        assert "PELICAN" in text.upper(), (
            f"Model should recall 'PELICAN' from turn 1. Got: {text!r}"
        )


@pytest.mark.multi_provider
class TestContextMessageTokens:
    """Verify input_tokens reflect growing context."""

    def test_message_input_tokens_increase(self, provider_chat):
        """Assistant message input_tokens should grow with each turn."""
        chat_id = provider_chat["id"]

        # 3 turns
        for prompt in ["Say A.", "Say B.", "Say C."]:
            _r = httpx.post(
                f"{API_PREFIX}/chats/{chat_id}/messages:stream",
                json={"content": prompt},
                headers={"Accept": "text/event-stream"},
                timeout=90,
            )
            _events = parse_sse(_r.text) if _r.status_code == 200 else []
            expect_done(_events)

        # Fetch assistant messages via REST API
        resp = httpx.get(f"{API_PREFIX}/chats/{chat_id}/messages")
        assert resp.status_code == 200
        msgs = resp.json()["items"]
        asst_msgs = [m for m in msgs if m["role"] == "assistant"]

        assert len(asst_msgs) == 3
        tokens = [m["input_tokens"] for m in asst_msgs]

        # Each subsequent assistant message should have more input tokens
        for i in range(1, len(tokens)):
            assert tokens[i] > tokens[i - 1], (
                f"Assistant msg {i + 1} input_tokens ({tokens[i]}) should be > "
                f"msg {i} ({tokens[i - 1]}). All: {tokens}"
            )
