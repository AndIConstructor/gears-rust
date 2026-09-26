"""Tests for chat CRUD operations."""

import uuid
from datetime import datetime

import pytest
import httpx

from .conftest import (
    API_PREFIX,
    DEFAULT_MODEL,
    DISABLED_MODEL,
    RESOURCE_ODATA,
    STANDARD_MODEL,
    TOKEN_USER_B,
    assert_problem,
    auth_headers,
    expect_done,
    stream_message,
)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def list_all_chats(page_size: int, token: str) -> list[str]:
    """Follow `page_info.next_cursor` to the end; return chat ids in order."""
    ids: list[str] = []
    params = {"limit": page_size}
    for _ in range(1000):
        resp = httpx.get(f"{API_PREFIX}/chats", params=params, headers=auth_headers(token))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert len(body["items"]) <= page_size
        ids.extend(c["id"] for c in body["items"])
        cursor = body["page_info"].get("next_cursor")
        if not cursor:
            return ids
        params = {"limit": page_size, "cursor": cursor}
    raise AssertionError("pagination did not terminate")


class TestCreateChat:
    """POST /v1/chats"""

    def test_create_chat_default_model(self, server):
        resp = httpx.post(f"{API_PREFIX}/chats", json={})
        assert resp.status_code == 201
        body = resp.json()
        assert "id" in body
        assert body["model"] == DEFAULT_MODEL
        assert body["message_count"] == 0
        assert "created_at" in body
        assert "updated_at" in body

    def test_create_chat_with_model(self, server):
        resp = httpx.post(f"{API_PREFIX}/chats", json={"model": STANDARD_MODEL})
        assert resp.status_code == 201
        assert resp.json()["model"] == STANDARD_MODEL

    def test_create_chat_with_title(self, server):
        resp = httpx.post(f"{API_PREFIX}/chats", json={"title": "My Test Chat"})
        assert resp.status_code == 201
        assert resp.json()["title"] == "My Test Chat"

    def test_create_chat_invalid_model(self, server):
        resp = httpx.post(f"{API_PREFIX}/chats", json={"model": "nonexistent-model"})
        assert_problem(resp, 400, "invalid_argument", field_reason="INVALID_MODEL")

    def test_create_chat_disabled_model(self, server):
        resp = httpx.post(f"{API_PREFIX}/chats", json={"model": DISABLED_MODEL})
        assert_problem(resp, 400, "invalid_argument", field_reason="INVALID_MODEL")

    def test_create_chat_title_length_boundary(self, server):
        """Title of 255 characters is accepted, 256 is rejected."""
        resp = httpx.post(f"{API_PREFIX}/chats", json={"title": "T" * 255})
        assert resp.status_code == 201, resp.text
        assert resp.json()["title"] == "T" * 255
        assert_problem(
            httpx.post(f"{API_PREFIX}/chats", json={"title": "T" * 256}), 400, "invalid_argument",
        )

    def test_create_chat_whitespace_title_rejected(self, server):
        """A whitespace-only title is 400 invalid_argument."""
        resp = httpx.post(f"{API_PREFIX}/chats", json={"title": "   "})
        assert_problem(resp, 400, "invalid_argument")

    def test_create_chat_schema_invalid_is_422(self, server):
        """A body that does not match the schema (`model` is not a string) is 422."""
        resp = httpx.post(f"{API_PREFIX}/chats", json={"model": 123})
        assert_problem(resp, 422, "invalid_argument")

    def test_create_chat_malformed_json_is_400(self, server):
        resp = httpx.post(
            f"{API_PREFIX}/chats", content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert_problem(resp, 400, "invalid_argument", field_reason="json_syntax_error")


class TestGetChat:
    """GET /v1/chats/{id}"""

    @pytest.mark.multi_provider
    def test_get_chat(self, provider_chat):
        chat_id = provider_chat["id"]
        resp = httpx.get(f"{API_PREFIX}/chats/{chat_id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == chat_id
        assert body["model"] == provider_chat["model"]
        assert "created_at" in body
        assert "updated_at" in body

    def test_get_chat_not_found(self, server):
        fake_id = str(uuid.uuid4())
        resp = httpx.get(f"{API_PREFIX}/chats/{fake_id}")
        assert_problem(resp, 404, "not_found")


class TestListChats:
    """GET /v1/chats"""

    @pytest.mark.multi_provider
    def test_list_chats(self, provider_chat):
        resp = httpx.get(f"{API_PREFIX}/chats")
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "page_info" in body
        assert len(body["items"]) >= 1
        assert provider_chat["id"] in [c["id"] for c in body["items"]]

    def test_list_chats_pagination(self, server):
        """Following next_cursor visits every chat exactly once, in list order.

        Runs as user B, whose chat list stays short (user A owns hundreds of chats).
        """
        headers = auth_headers(TOKEN_USER_B)
        for _ in range(5):
            r = httpx.post(f"{API_PREFIX}/chats", json={}, headers=headers)
            assert r.status_code == 201
        full = httpx.get(f"{API_PREFIX}/chats", params={"limit": 100}, headers=headers).json()
        assert full["page_info"].get("next_cursor") is None, "B's chats must fit in one page"
        full = full["items"]
        assert len(full) >= 5

        paged = list_all_chats(page_size=2, token=TOKEN_USER_B)
        assert len(paged) == len(set(paged)), "duplicate chats across pages"
        assert paged == [c["id"] for c in full]

    def test_list_chats_unknown_filter_field_400(self, server):
        resp = httpx.get(f"{API_PREFIX}/chats", params={"$filter": "nosuchfield eq 'x'"})
        assert_problem(
            resp, 400, "invalid_argument",
            field_reason="INVALID_FILTER", resource_type=RESOURCE_ODATA,
        )

    def test_list_chats_unknown_orderby_field_400(self, server):
        resp = httpx.get(f"{API_PREFIX}/chats", params={"$orderby": "nosuchfield desc"})
        assert_problem(
            resp, 400, "invalid_argument",
            field_reason="INVALID_ORDERBY_FIELD", resource_type=RESOURCE_ODATA,
        )

    def test_list_chats_malformed_cursor_400(self, server):
        resp = httpx.get(f"{API_PREFIX}/chats", params={"cursor": "not-a-cursor"})
        assert_problem(resp, 400, "invalid_argument", field_reason="INVALID_CURSOR")

    def test_list_chats_cursor_with_other_filter_400(self, server):
        """A cursor issued for one `$filter` is rejected under another."""
        tag = f"fm-{uuid.uuid4().hex}"
        for _ in range(2):
            r = httpx.post(f"{API_PREFIX}/chats", json={"title": f"{tag} chat"})
            assert r.status_code == 201, r.text
        first = httpx.get(
            f"{API_PREFIX}/chats",
            params={"limit": 1, "$filter": f"contains(title, '{tag}')"},
        )
        assert first.status_code == 200, first.text
        cursor = first.json()["page_info"].get("next_cursor")
        assert cursor, first.json()

        resp = httpx.get(
            f"{API_PREFIX}/chats",
            params={"limit": 1, "cursor": cursor, "$filter": "contains(title, 'other')"},
        )
        assert_problem(
            resp, 400, "invalid_argument",
            field_reason="FILTER_MISMATCH", resource_type=RESOURCE_ODATA,
        )

    def test_list_chats_zero_limit_400(self, server):
        resp = httpx.get(f"{API_PREFIX}/chats", params={"limit": 0})
        assert_problem(resp, 400, "invalid_argument", field_reason="INVALID_LIMIT")

    def test_list_chats_orderby_with_cursor_400(self, server):
        first = httpx.get(f"{API_PREFIX}/chats", params={"limit": 1})
        assert first.status_code == 200, first.text
        cursor = first.json()["page_info"].get("next_cursor")
        assert cursor, "user A must own more than one chat"
        resp = httpx.get(
            f"{API_PREFIX}/chats", params={"cursor": cursor, "$orderby": "updated_at desc"},
        )
        assert_problem(resp, 400, "invalid_argument", field_reason="ORDER_WITH_CURSOR")

    def test_send_moves_older_chat_to_top(self, server):
        """The list is ordered by activity: sending into an older chat puts it first."""
        older = httpx.post(f"{API_PREFIX}/chats", json={}).json()["id"]
        newer = httpx.post(f"{API_PREFIX}/chats", json={}).json()["id"]
        ids = [c["id"] for c in httpx.get(f"{API_PREFIX}/chats").json()["items"]]
        assert ids.index(newer) < ids.index(older)

        status, events, _ = stream_message(older, "Say OK.")
        assert status == 200
        expect_done(events)

        ids = [c["id"] for c in httpx.get(f"{API_PREFIX}/chats").json()["items"]]
        assert ids[0] == older


class TestUpdateChat:
    """PATCH /v1/chats/{id}"""

    @pytest.mark.multi_provider
    def test_update_title(self, provider_chat):
        """PATCH title is persisted and bumps updated_at."""
        chat_id = provider_chat["id"]
        resp = httpx.patch(
            f"{API_PREFIX}/chats/{chat_id}",
            json={"title": "Updated Title"},
        )
        assert resp.status_code == 200
        assert resp.json()["title"] == "Updated Title"

        fetched = httpx.get(f"{API_PREFIX}/chats/{chat_id}").json()
        assert fetched["title"] == "Updated Title"
        assert _ts(fetched["updated_at"]) > _ts(provider_chat["updated_at"])

    def test_update_not_found(self, server):
        fake_id = str(uuid.uuid4())
        resp = httpx.patch(
            f"{API_PREFIX}/chats/{fake_id}",
            json={"title": "Nope"},
        )
        assert_problem(resp, 404, "not_found")

    @pytest.mark.multi_provider
    def test_update_whitespace_title_rejected(self, provider_chat):
        """02-12: Whitespace-only title should be rejected."""
        chat_id = provider_chat["id"]
        resp = httpx.patch(
            f"{API_PREFIX}/chats/{chat_id}",
            json={"title": "   "},
        )
        assert_problem(resp, 400, "invalid_argument")
        assert httpx.get(f"{API_PREFIX}/chats/{chat_id}").json().get("title") == provider_chat.get("title")

    @pytest.mark.multi_provider
    def test_update_without_title_is_422(self, provider_chat):
        """A body that does not match the schema is 422 invalid_argument."""
        resp = httpx.patch(f"{API_PREFIX}/chats/{provider_chat['id']}", json={})
        assert_problem(resp, 422, "invalid_argument")

    @pytest.mark.multi_provider
    def test_update_malformed_json_is_400(self, provider_chat):
        resp = httpx.patch(
            f"{API_PREFIX}/chats/{provider_chat['id']}",
            content=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        assert_problem(resp, 400, "invalid_argument")

    @pytest.mark.multi_provider
    def test_update_title_length_boundary(self, provider_chat):
        """02-13: a 255-character title is accepted, 256 is rejected."""
        chat_id = provider_chat["id"]
        resp = httpx.patch(f"{API_PREFIX}/chats/{chat_id}", json={"title": "A" * 255})
        assert resp.status_code == 200, resp.text
        assert resp.json()["title"] == "A" * 255

        resp = httpx.patch(f"{API_PREFIX}/chats/{chat_id}", json={"title": "A" * 256})
        assert_problem(resp, 400, "invalid_argument")
        assert httpx.get(f"{API_PREFIX}/chats/{chat_id}").json()["title"] == "A" * 255


class TestDeleteChat:
    """DELETE /v1/chats/{id}"""

    @pytest.mark.multi_provider
    def test_delete_chat(self, provider_chat):
        chat_id = provider_chat["id"]
        resp = httpx.delete(f"{API_PREFIX}/chats/{chat_id}")
        assert resp.status_code == 204

        # Verify gone, and a second DELETE is 404
        assert_problem(httpx.get(f"{API_PREFIX}/chats/{chat_id}"), 404, "not_found")
        assert_problem(httpx.delete(f"{API_PREFIX}/chats/{chat_id}"), 404, "not_found")
        ids = [c["id"] for c in httpx.get(f"{API_PREFIX}/chats").json()["items"]]
        assert chat_id not in ids

    def test_delete_not_found(self, server):
        fake_id = str(uuid.uuid4())
        resp = httpx.delete(f"{API_PREFIX}/chats/{fake_id}")
        assert_problem(resp, 404, "not_found")
