"""Canned response scenarios for the mock LLM provider."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field


@dataclass
class Usage:
    input_tokens: int = 50
    output_tokens: int = 12


@dataclass
class MockEvent:
    """A single SSE event in a scenario."""
    event_type: str  # e.g. "response.output_text.delta"
    data: dict = field(default_factory=dict)


@dataclass
class Scenario:
    """Ordered list of events the mock should emit, plus terminal metadata."""
    events: list[MockEvent] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    citations: list[dict] = field(default_factory=list)
    # Extra `response.completed` output items placed before the message item,
    # e.g. a `function_call` item (the Responses API reports function calls
    # only there).
    output_items: list[dict] = field(default_factory=list)
    # Terminal type: "completed" (default), "failed", "incomplete"
    terminal: str = "completed"
    error: dict | None = None
    incomplete_reason: str | None = None
    # Seconds to sleep between SSE events (0 = instant). Used for cancellation tests.
    slow: float = 0
    # Seconds to wait after the response headers and before the first SSE
    # event. Keeps the stream idle so the gear sends `ping` keepalives.
    initial_delay: float = 0
    # Seconds to wait before sending the response status line and headers.
    # Longer than OAGW `proxy_timeout_secs` makes the gateway time out.
    header_delay: float = 0
    # HTTP-level error: return this status code + JSON body instead of SSE stream.
    # When set, no SSE is produced — the mock returns a plain JSON error response.
    http_error_status: int | None = None
    http_error_body: dict | None = None


# ── Built-in scenario registry ─────────────────────────────────────────────

SCENARIOS: dict[str, Scenario] = {
    "PING": Scenario(
        events=[
            MockEvent("response.output_text.delta", {"delta": "PONG"}),
            MockEvent("response.output_text.done", {"text": "PONG"}),
        ],
        usage=Usage(input_tokens=30, output_tokens=2),
    ),
    "SEARCH:*": Scenario(
        events=[
            MockEvent("response.output_text.delta", {"delta": "Searching"}),
            MockEvent("response.web_search_call.searching", {}),
            MockEvent("response.output_text.delta", {"delta": "...found"}),
            MockEvent("response.web_search_call.completed", {}),
            MockEvent("response.output_text.delta", {"delta": " results"}),
            MockEvent("response.output_text.done", {"text": "Searching...found results"}),
        ],
        usage=Usage(input_tokens=80, output_tokens=15),
        # OpenAI url_citation: no text; the snippet comes from the text range.
        citations=[{
            "type": "url_citation",
            "url": "https://example.com",
            "title": "Mock Search Result",
            "start_index": 0,
            "end_index": 9,
        }],
    ),
    "FILESEARCH:*": Scenario(
        events=[
            MockEvent("response.file_search_call.searching", {}),
            # OpenAI sends no results in this event.
            MockEvent("response.file_search_call.completed", {
                "item_id": "fs_mock_1", "output_index": 0,
            }),
            MockEvent("response.output_text.delta", {"delta": "Based on docs"}),
            MockEvent("response.output_text.done", {"text": "Based on docs"}),
        ],
        usage=Usage(input_tokens=200, output_tokens=10),
        # OpenAI file_citation: file_id, filename and a position; no text or range.
        citations=[{
            "type": "file_citation",
            "file_id": "file_mock_1",
            "filename": "doc.pdf",
            "index": 13,
        }],
    ),
    "CODEINTERP:*": Scenario(
        events=[
            MockEvent("response.code_interpreter_call.in_progress", {
                "item_id": "ci_mock_1", "output_index": 0,
            }),
            MockEvent("response.code_interpreter_call.interpreting", {
                "item_id": "ci_mock_1", "output_index": 0,
            }),
            # No outputs here; they come in response.output_item.done, and
            # only when the request has include: ["code_interpreter_call.outputs"].
            MockEvent("response.code_interpreter_call.completed", {
                "item_id": "ci_mock_1", "output_index": 0,
            }),
            MockEvent("response.output_item.done", {
                "output_index": 0,
                "item": {
                    "type": "code_interpreter_call",
                    "id": "ci_mock_1",
                    "status": "completed",
                    "code": "print(total, average)",
                    "outputs": [
                        {"type": "logs", "logs": "Total: 42\nAverage: 7.0"},
                    ],
                },
            }),
            MockEvent("response.output_text.delta", {"delta": "The spreadsheet "}),
            MockEvent("response.output_text.delta", {"delta": "analysis shows "}),
            MockEvent("response.output_text.delta", {"delta": "a total of 42."}),
            MockEvent("response.output_text.done", {"text": "The spreadsheet analysis shows a total of 42."}),
        ],
        usage=Usage(input_tokens=300, output_tokens=20),
    ),
    "Write*": Scenario(
        events=[
            MockEvent("response.output_text.delta", {"delta": "The history of "}),
            MockEvent("response.output_text.delta", {"delta": "computing spans "}),
            MockEvent("response.output_text.delta", {"delta": "many decades "}),
            MockEvent("response.output_text.delta", {"delta": "of innovation "}),
            MockEvent("response.output_text.delta", {"delta": "and discovery. "}),
            MockEvent("response.output_text.delta", {"delta": "From Babbage "}),
            MockEvent("response.output_text.delta", {"delta": "to quantum. "}),
            MockEvent("response.output_text.done", {"text": "The history of computing spans many decades of innovation and discovery. From Babbage to quantum. "}),
        ],
        usage=Usage(input_tokens=100, output_tokens=50),
        slow=0.3,  # 300ms between events — gives client time to disconnect
    ),
    "ERROR": Scenario(
        events=[
            MockEvent("response.output_text.delta", {"delta": "Partial"}),
        ],
        terminal="failed",
        error={"code": "server_error", "message": "Mock provider error"},
    ),
    "TRUNCATE": Scenario(
        events=[
            MockEvent("response.output_text.delta", {"delta": "Truncated text"}),
            MockEvent("response.output_text.done", {"text": "Truncated text"}),
        ],
        terminal="incomplete",
        incomplete_reason="max_output_tokens",
        usage=Usage(input_tokens=50, output_tokens=100),
    ),
    "*": Scenario(
        events=[
            MockEvent("response.output_text.delta", {"delta": "Hello! "}),
            MockEvent("response.output_text.delta", {"delta": "How can I help?"}),
            MockEvent("response.output_text.done", {"text": "Hello! How can I help?"}),
        ],
        usage=Usage(input_tokens=50, output_tokens=12),
    ),
}


def match_scenario(user_input: str) -> Scenario:
    """Match user input to a scenario: exact match -> glob -> default."""
    if user_input in SCENARIOS:
        return SCENARIOS[user_input]
    for pattern, scenario in SCENARIOS.items():
        if pattern != "*" and fnmatch.fnmatch(user_input, pattern):
            return scenario
    return SCENARIOS["*"]


SUMMARY_OUTPUT_TOKENS = 25


def mock_summary_text(prompt: str) -> str:
    """Summary the mock returns for a thread summary prompt.

    Counts the `User:` / `Assistant:` lines of the prompt, so a test can tell
    which messages the prompt contained.
    """
    lines = prompt.splitlines()
    users = sum(1 for line in lines if line.startswith("User: "))
    assistants = sum(1 for line in lines if line.startswith("Assistant: "))
    return f"MOCK-SUMMARY {users} user and {assistants} assistant messages"


def build_summary_response(body: dict, model: str, response_id: str) -> dict:
    """Non-streaming Responses API object for a thread summary request.

    The text has the `<analysis>` and `<summary>` blocks that the summary
    worker parses (thread_summary_worker.rs, `format_summary_output`).
    """
    summary = mock_summary_text(extract_last_user_message(body))
    text = f"<analysis>Mock analysis.</analysis>\n<summary>{summary}</summary>"
    return {
        "id": response_id,
        "object": "response",
        "status": "completed",
        "model": model,
        "output": [{
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }],
        "usage": {
            "input_tokens": 100,
            "output_tokens": SUMMARY_OUTPUT_TOKENS,
            "total_tokens": 100 + SUMMARY_OUTPUT_TOKENS,
        },
    }


def extract_last_user_message(body: dict) -> str:
    """Extract the last user message content from a Responses API request body."""
    input_field = body.get("input", "")
    if isinstance(input_field, str):
        return input_field
    if isinstance(input_field, list):
        for msg in reversed(input_field):
            if isinstance(msg, dict) and msg.get("role") == "user":
                content = msg.get("content", "")
                if isinstance(content, str):
                    return content
                if isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "input_text":
                            return part.get("text", "")
                        if isinstance(part, str):
                            return part
    return ""


def has_tool(body: dict, tool_type: str) -> bool:
    """Check if the request body includes a specific tool type."""
    tools = body.get("tools", [])
    return any(
        isinstance(t, dict) and t.get("type", "").startswith(tool_type)
        for t in tools
    )


def should_include_tool_event(event: MockEvent, body: dict) -> bool:
    """Check if a tool event should be included based on request tools."""
    et = event.event_type
    item = event.data.get("item")
    if isinstance(item, dict) and isinstance(item.get("type"), str):
        et = item["type"]
    if "web_search" in et:
        return has_tool(body, "web_search")
    if "file_search" in et:
        return has_tool(body, "file_search")
    if "code_interpreter" in et:
        return has_tool(body, "code_interpreter")
    return True
