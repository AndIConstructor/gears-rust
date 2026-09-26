"""Tests for message listing with OData query options and field presence."""

import httpx
from uuid import uuid4

from .conftest import API_PREFIX, RESOURCE_ODATA, assert_problem, expect_done, stream_message


def _create_chat_with_messages(count: int = 1) -> str:
    """Create a chat and send `count` messages. Returns chat_id."""
    resp = httpx.post(f"{API_PREFIX}/chats", json={})
    assert resp.status_code == 201
    chat_id = resp.json()["id"]

    for i in range(count):
        status, events, raw = stream_message(chat_id, f"Message number {i + 1}")
        assert status == 200, f"stream_message failed: {status} {raw[:300]}"
        expect_done(events)

    return chat_id


class TestMessages:
    """GET /chats/{cid}/messages with OData query options."""

    def test_odata_orderby(self, server):
        """$orderby=created_at desc should return messages in reverse chronological order."""
        chat_id = _create_chat_with_messages(2)

        resp = httpx.get(
            f"{API_PREFIX}/chats/{chat_id}/messages",
            params={"$orderby": "created_at desc"},
        )
        assert resp.status_code == 200, f"GET messages failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert "page_info" in body
        items = body["items"]
        assert len(items) >= 2, f"Expected at least 2 messages, got {len(items)}"

        # Verify descending order by created_at
        timestamps = [m["created_at"] for m in items]
        for i in range(len(timestamps) - 1):
            assert timestamps[i] >= timestamps[i + 1], (
                f"Messages not in descending order: {timestamps[i]} < {timestamps[i + 1]} "
                f"at positions {i}, {i + 1}"
            )

    def test_odata_filter_role(self, server):
        """$filter=role eq 'assistant' should return only assistant messages."""
        chat_id = _create_chat_with_messages(1)

        resp = httpx.get(
            f"{API_PREFIX}/chats/{chat_id}/messages",
            params={"$filter": "role eq 'assistant'"},
        )
        assert resp.status_code == 200, f"GET messages failed: {resp.status_code} {resp.text}"
        body = resp.json()
        assert "page_info" in body
        items = body["items"]
        assert len(items) >= 1, "Expected at least one assistant message"

        for msg in items:
            assert msg["role"] == "assistant", (
                f"Expected only assistant messages, got role={msg['role']}"
            )

    def test_my_reaction_field(self, server):
        """Assistant messages should include a 'my_reaction' field (null when no reaction set)."""
        chat_id = _create_chat_with_messages(1)

        resp = httpx.get(f"{API_PREFIX}/chats/{chat_id}/messages")
        assert resp.status_code == 200
        items = resp.json()["items"]

        assistant_msgs = [m for m in items if m["role"] == "assistant"]
        assert len(assistant_msgs) >= 1, "No assistant messages found"

        for msg in assistant_msgs:
            assert "my_reaction" in msg, (
                f"Assistant message {msg['id']} missing 'my_reaction' field. "
                f"Keys present: {list(msg.keys())}"
            )
            assert msg["my_reaction"] is None, (
                f"Expected my_reaction=null for untouched message, got: {msg['my_reaction']}"
            )

        user_msgs = [m for m in items if m["role"] == "user"]
        assert len(user_msgs) >= 1, "No user messages found"
        for user_msg in user_msgs:
            assert user_msg.get("my_reaction") is None, (
                f"Expected my_reaction=null for user message {user_msg['id']}, "
                f"got: {user_msg.get('my_reaction')}"
            )

    def test_cursor_pagination(self, server):
        """Following next_cursor with limit=1 visits every message exactly once, in order."""
        chat_id = _create_chat_with_messages(3)
        full = httpx.get(f"{API_PREFIX}/chats/{chat_id}/messages").json()["items"]
        assert len(full) == 6

        ids: list[str] = []
        params = {"limit": 1}
        for _ in range(20):
            resp = httpx.get(f"{API_PREFIX}/chats/{chat_id}/messages", params=params)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            assert len(body["items"]) == 1
            ids.extend(m["id"] for m in body["items"])
            cursor = body["page_info"].get("next_cursor")
            if not cursor:
                break
            params = {"limit": 1, "cursor": cursor}
        assert ids == [m["id"] for m in full]

    def test_unknown_filter_field_400(self, chat):
        resp = httpx.get(
            f"{API_PREFIX}/chats/{chat['id']}/messages", params={"$filter": "nosuchfield eq 'x'"},
        )
        assert_problem(
            resp, 400, "invalid_argument",
            field_reason="INVALID_FILTER", resource_type=RESOURCE_ODATA,
        )

    def test_unknown_orderby_field_400(self, chat):
        resp = httpx.get(
            f"{API_PREFIX}/chats/{chat['id']}/messages", params={"$orderby": "nosuchfield desc"},
        )
        assert_problem(
            resp, 400, "invalid_argument",
            field_reason="INVALID_ORDERBY_FIELD", resource_type=RESOURCE_ODATA,
        )

    def test_malformed_cursor_400(self, chat):
        resp = httpx.get(f"{API_PREFIX}/chats/{chat['id']}/messages", params={"cursor": "not-a-cursor"})
        assert_problem(resp, 400, "invalid_argument", field_reason="INVALID_CURSOR")

    def test_cursor_with_other_filter_400(self, server):
        """A cursor issued for one `$filter` is rejected under another."""
        chat_id = _create_chat_with_messages(1)
        url = f"{API_PREFIX}/chats/{chat_id}/messages"
        first = httpx.get(url, params={"limit": 1, "$filter": "role ne 'system'"})
        assert first.status_code == 200, first.text
        cursor = first.json()["page_info"].get("next_cursor")
        assert cursor, first.json()

        resp = httpx.get(url, params={"limit": 1, "cursor": cursor, "$filter": "role eq 'user'"})
        assert_problem(
            resp, 400, "invalid_argument",
            field_reason="FILTER_MISMATCH", resource_type=RESOURCE_ODATA,
        )

    def test_zero_limit_400(self, chat):
        resp = httpx.get(f"{API_PREFIX}/chats/{chat['id']}/messages", params={"limit": 0})
        assert_problem(resp, 400, "invalid_argument", field_reason="INVALID_LIMIT")

    def test_orderby_with_cursor_400(self, server):
        chat_id = _create_chat_with_messages(1)
        url = f"{API_PREFIX}/chats/{chat_id}/messages"
        first = httpx.get(url, params={"limit": 1})
        assert first.status_code == 200, first.text
        cursor = first.json()["page_info"].get("next_cursor")
        assert cursor, first.json()
        resp = httpx.get(url, params={"cursor": cursor, "$orderby": "created_at desc"})
        assert_problem(resp, 400, "invalid_argument", field_reason="ORDER_WITH_CURSOR")

    def test_messages_of_nonexistent_chat_404(self, server):
        resp = httpx.get(f"{API_PREFIX}/chats/{uuid4()}/messages")
        assert_problem(resp, 404, "not_found")

    def test_request_id_non_null(self, server):
        """03-05: Every message must have a non-null request_id."""
        chat_id = _create_chat_with_messages()
        resp = httpx.get(f"{API_PREFIX}/chats/{chat_id}/messages")
        assert resp.status_code == 200
        for msg in resp.json()["items"]:
            assert msg.get("request_id") is not None, f"request_id null on {msg['role']} message"

    def test_attachments_array_present(self, server):
        """03-06: Every message must have an attachments array."""
        chat_id = _create_chat_with_messages()
        resp = httpx.get(f"{API_PREFIX}/chats/{chat_id}/messages")
        assert resp.status_code == 200
        for msg in resp.json()["items"]:
            assert isinstance(msg.get("attachments"), list), f"attachments not array on {msg['role']} message"

    def test_request_id_shared_per_turn(self, server):
        """03-08: User and assistant messages in same turn share request_id."""
        chat_id = _create_chat_with_messages(2)
        resp = httpx.get(f"{API_PREFIX}/chats/{chat_id}/messages")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert [m["role"] for m in items] == ["user", "assistant"] * 2
        pairs = [(items[i]["request_id"], items[i + 1]["request_id"]) for i in (0, 2)]
        for user_rid, assistant_rid in pairs:
            assert user_rid == assistant_rid, pairs
        assert pairs[0][0] != pairs[1][0], "each turn has its own request_id"
