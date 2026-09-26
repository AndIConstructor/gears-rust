"""Authentication and isolation between users and tenants.

User A owns the resources. User B is another user in the same tenant; user C
is in another tenant. Both must see A's resources as nonexistent (404) and
must not be able to change them. Requests without valid credentials get 401.
"""

from __future__ import annotations

import io
import uuid

import httpx
import pytest

from .conftest import (
    API_PREFIX,
    NO_AUTH,
    TOKEN_TENANT_B,
    TOKEN_USER_B,
    auth_headers,
    expect_done,
    expect_stream_started,
    stream_message,
)

# Every public operation, with placeholders filled from the owned resources.
OPERATIONS = [
    ("POST", "/chats"),
    ("GET", "/chats"),
    ("GET", "/chats/{chat_id}"),
    ("PATCH", "/chats/{chat_id}"),
    ("DELETE", "/chats/{chat_id}"),
    ("GET", "/chats/{chat_id}/messages"),
    ("POST", "/chats/{chat_id}/messages:stream"),
    ("POST", "/chats/{chat_id}/attachments"),
    ("GET", "/chats/{chat_id}/attachments/{attachment_id}"),
    ("DELETE", "/chats/{chat_id}/attachments/{attachment_id}"),
    ("GET", "/chats/{chat_id}/turns/{request_id}"),
    ("POST", "/chats/{chat_id}/turns/{request_id}/retry"),
    ("PATCH", "/chats/{chat_id}/turns/{request_id}"),
    ("DELETE", "/chats/{chat_id}/turns/{request_id}"),
    ("PUT", "/chats/{chat_id}/messages/{message_id}/reaction"),
    ("DELETE", "/chats/{chat_id}/messages/{message_id}/reaction"),
    ("GET", "/models"),
    ("GET", "/models/{model_id}"),
    ("GET", "/quota/status"),
]

BODIES = {
    ("POST", "/chats"): {},
    ("PATCH", "/chats/{chat_id}"): {"title": "hijacked"},
    ("POST", "/chats/{chat_id}/messages:stream"): {"content": "hello"},
    ("PATCH", "/chats/{chat_id}/turns/{request_id}"): {"content": "edited"},
    ("PUT", "/chats/{chat_id}/messages/{message_id}/reaction"): {"reaction": "like"},
}


@pytest.fixture(scope="module")
def owned(server) -> dict:
    """A chat of user A with one completed turn and one ready document."""
    resp = httpx.post(f"{API_PREFIX}/chats", json={"title": "owned by A"})
    assert resp.status_code == 201, resp.text
    chat_id = resp.json()["id"]

    request_id = str(uuid.uuid4())
    status, events, _ = stream_message(chat_id, "Say OK.", request_id=request_id)
    assert status == 200
    expect_done(events)
    message_id = expect_stream_started(events).data["message_id"]

    resp = httpx.post(
        f"{API_PREFIX}/chats/{chat_id}/attachments",
        files={"file": ("notes.txt", io.BytesIO(b"owned document"), "text/plain")},
        timeout=60,
    )
    assert resp.status_code == 201, resp.text

    model_id = httpx.get(f"{API_PREFIX}/models").json()["items"][0]["model_id"]
    return {
        "chat_id": chat_id,
        "request_id": request_id,
        "message_id": message_id,
        "attachment_id": resp.json()["id"],
        "model_id": model_id,
    }


def _call(method: str, path: str, ids: dict, headers: dict) -> httpx.Response:
    url = API_PREFIX + path.format(**ids)
    if path.endswith("/attachments") and method == "POST":
        return httpx.post(
            url,
            files={"file": ("x.txt", io.BytesIO(b"x"), "text/plain")},
            headers=headers,
            timeout=30,
        )
    body = BODIES.get((method, path))
    return httpx.request(method, url, json=body, headers=headers, timeout=30)


class TestAuthentication:
    """Every operation requires a valid bearer token."""

    @pytest.mark.parametrize(("method", "path"), OPERATIONS)
    def test_missing_token_is_401(self, owned, method, path):
        resp = _call(method, path, owned, NO_AUTH)
        assert resp.status_code == 401, f"{method} {path}: {resp.status_code} {resp.text}"

    @pytest.mark.parametrize(("method", "path"), OPERATIONS)
    def test_unknown_token_is_401(self, owned, method, path):
        resp = _call(method, path, owned, auth_headers("not-a-valid-token"))
        assert resp.status_code == 401, f"{method} {path}: {resp.status_code} {resp.text}"


# Operations on A's resources that another user must see as 404.
FOREIGN_OPERATIONS = [
    (m, p) for (m, p) in OPERATIONS
    if "{chat_id}" in p
]


@pytest.mark.parametrize("token", [TOKEN_USER_B, TOKEN_TENANT_B], ids=["same_tenant", "other_tenant"])
class TestIsolation:
    """User B (same tenant) and user C (other tenant) cannot reach A's chat."""

    @pytest.mark.parametrize(("method", "path"), FOREIGN_OPERATIONS)
    def test_foreign_resource_is_404(self, owned, mock_provider, token, method, path):
        resp = _call(method, path, owned, auth_headers(token))
        assert resp.status_code == 404, f"{method} {path}: {resp.status_code} {resp.text}"
        assert mock_provider.get_last_request() is None, (
            f"{method} {path} must not reach the provider"
        )

    def test_foreign_chat_not_listed(self, owned, token):
        resp = httpx.get(f"{API_PREFIX}/chats", headers=auth_headers(token))
        assert resp.status_code == 200
        ids = {c["id"] for c in resp.json()["items"]}
        assert owned["chat_id"] not in ids

    def test_owner_resources_unchanged(self, owned, token):
        # Runs after the foreign PATCH/DELETE attempts in this class.
        chat = httpx.get(f"{API_PREFIX}/chats/{owned['chat_id']}")
        assert chat.status_code == 200
        assert chat.json()["title"] == "owned by A"

        turn = httpx.get(
            f"{API_PREFIX}/chats/{owned['chat_id']}/turns/{owned['request_id']}"
        )
        assert turn.status_code == 200
        assert turn.json()["state"] == "done"

        att = httpx.get(
            f"{API_PREFIX}/chats/{owned['chat_id']}/attachments/{owned['attachment_id']}"
        )
        assert att.status_code == 200

        messages = httpx.get(f"{API_PREFIX}/chats/{owned['chat_id']}/messages")
        assistant = [m for m in messages.json()["items"] if m["role"] == "assistant"]
        assert assistant and assistant[0]["my_reaction"] is None


class TestQuotaIsolation:
    """Usage is accounted per user."""

    def test_other_user_usage_is_not_charged(self, owned):
        def used(token: str) -> int:
            resp = httpx.get(f"{API_PREFIX}/quota/status", headers=auth_headers(token))
            assert resp.status_code == 200, resp.text
            return sum(
                p["used_credits_micro"] for t in resp.json()["tiers"] for p in t["periods"]
            )

        before_b = used(TOKEN_USER_B)
        status, events, _ = stream_message(owned["chat_id"], "Say OK again.")
        assert status == 200
        expect_done(events)
        assert used(TOKEN_USER_B) == before_b
