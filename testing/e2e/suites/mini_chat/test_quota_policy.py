"""Quota policy E2E profile: 429, downgrade, tool quotas, retry preflight, warning flags.

The rig's limits are the static policy plugin defaults
(infra/plugins/static_model_policy/config.rs):

    standard ("total" bucket):     daily 100_000_000, monthly 1_000_000_000 credits_micro
    premium ("tier:premium"):      daily  50_000_000, monthly   500_000_000 credits_micro

Usage is seeded deterministically: a normal turn creates the user's
quota_usage rows (daily + monthly, total + tier:premium), then the rows are
rewritten in the DB. Seeded values are ones the product can reach: a daily
row never exceeds its limit, the monthly row of a bucket is at least the
daily one, and `total` includes `tier:premium`. Each test runs as a
dedicated quota user (config/base.yaml) so the seeded state never reaches
user A, and the rows are zeroed before and after every test.

Kill switches are fixed plugin configuration (all off) and are covered by
unit tests, not here.
"""

from __future__ import annotations

import io
import uuid

import httpx
import pytest

from .conftest import (
    API_PREFIX,
    DEFAULT_MODEL,
    DISABLED_MODEL,
    QUOTA_USER_1_ID,
    QUOTA_USER_2_ID,
    QUOTA_USER_3_ID,
    STANDARD_MODEL,
    TOKEN_QUOTA_USER_1,
    TOKEN_QUOTA_USER_2,
    TOKEN_QUOTA_USER_3,
    assert_no_reserves,
    assert_problem,
    auth_headers,
    exec_db,
    expect_done,
    find_period,
    get_quota_status,
    list_messages,
    parse_sse,
    poll_turn,
    query_db,
    stream_message,
    turn_count,
)
from .test_code_interpreter import XLSX_CONTENT_TYPE, _make_minimal_xlsx

TOTAL_MONTHLY_LIMIT = 1_000_000_000
TOTAL_DAILY_LIMIT = 100_000_000
PREMIUM_DAILY_LIMIT = 50_000_000
WEB_SEARCH_DAILY_QUOTA = 75  # QuotaConfig default (config.rs), not overridden in base.yaml
CODE_INTERPRETER_DAILY_QUOTA = 50  # QuotaConfig default (config.rs), not overridden in base.yaml

pytestmark = pytest.mark.timeout(30)


class QuotaUser:
    """A dedicated user with helpers bound to its token."""

    def __init__(self, token: str, user_id: str):
        self.token = token
        self.user_id = user_id
        self.headers = auth_headers(token)

    def create_chat(self, model: str = DEFAULT_MODEL) -> str:
        resp = httpx.post(f"{API_PREFIX}/chats", json={"model": model}, headers=self.headers)
        assert resp.status_code == 201, resp.text
        return resp.json()["id"]

    def complete_turn(self, chat_id: str, content: str = "Say OK.") -> tuple[str, dict]:
        """Send a message, wait for the turn to be done; return (request_id, done data)."""
        rid = str(uuid.uuid4())
        status, events, raw = stream_message(chat_id, content, request_id=rid, token=self.token)
        assert status == 200, raw
        done = expect_done(events)
        poll_turn(chat_id, rid, ("done",), token=self.token)
        assert_no_reserves(self.user_id)
        return rid, done.data

    def post_stream(self, chat_id: str, body: dict) -> httpx.Response:
        return httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/messages:stream", json=body,
            headers={"Accept": "text/event-stream", **self.headers}, timeout=30,
        )

    def seed(self, *, bucket: str | None = None, period_type: str | None = None, **columns) -> None:
        """Set quota_usage columns on the user's rows (optionally one bucket/period)."""
        assignments = ", ".join(f"{name} = ?" for name in columns)
        sql = f"UPDATE quota_usage SET {assignments} WHERE user_id = ?"
        params: list = [*columns.values(), self.user_id]
        if bucket is not None:
            sql += " AND bucket = ?"
            params.append(bucket)
        if period_type is not None:
            sql += " AND period_type = ?"
            params.append(period_type)
        assert exec_db(sql, tuple(params)) > 0, f"no quota_usage rows to seed for {self.user_id}"

    def seed_spent(self, *, total: int, premium: int = 0, total_monthly: int | None = None) -> None:
        """Spent credits of today: `total` and `premium` in the daily rows and,
        unless `total_monthly` is given, the same amounts in the monthly rows."""
        self.seed(bucket="total", period_type="daily", spent_credits_micro=total)
        self.seed(
            bucket="total", period_type="monthly",
            spent_credits_micro=total if total_monthly is None else total_monthly,
        )
        self.seed(bucket="tier:premium", period_type="daily", spent_credits_micro=premium)
        self.seed(bucket="tier:premium", period_type="monthly", spent_credits_micro=premium)

    def reset(self) -> None:
        exec_db(
            "UPDATE quota_usage SET spent_credits_micro = 0, reserved_credits_micro = 0, "
            "calls = 0, input_tokens = 0, output_tokens = 0, web_search_calls = 0, "
            "code_interpreter_calls = 0, file_search_calls = 0 WHERE user_id = ?",
            (self.user_id,),
        )

    def usage_rows(self) -> int:
        return query_db(
            "SELECT COUNT(*) AS n FROM quota_usage WHERE user_id = ?", (self.user_id,),
        )[0]["n"]


def _make_user(token: str, user_id: str):
    user = QuotaUser(token, user_id)
    # A premium turn creates all four rows (daily/monthly x total/tier:premium).
    if user.usage_rows() < 4:
        user.complete_turn(user.create_chat(DEFAULT_MODEL), "Create usage rows.")
    assert user.usage_rows() == 4
    user.reset()
    yield user
    user.reset()


@pytest.fixture
def user1(request):
    if request.config.getoption("mode") == "online":
        pytest.skip("seeds quota usage and inspects the mock provider")
    yield from _make_user(TOKEN_QUOTA_USER_1, QUOTA_USER_1_ID)


@pytest.fixture
def user2(request):
    if request.config.getoption("mode") == "online":
        pytest.skip("seeds quota usage and inspects the mock provider")
    yield from _make_user(TOKEN_QUOTA_USER_2, QUOTA_USER_2_ID)


@pytest.fixture
def user3(request):
    if request.config.getoption("mode") == "online":
        pytest.skip("seeds quota usage and inspects the mock provider")
    yield from _make_user(TOKEN_QUOTA_USER_3, QUOTA_USER_3_ID)


class TestQuotaExhaustion:
    """All tiers exhausted: the request is rejected before anything is written."""

    def test_all_tiers_exhausted_429(self, user1, mock_provider):
        chat_id = user1.create_chat(DEFAULT_MODEL)
        user1.complete_turn(chat_id, "Before exhaustion.")
        messages_before = list_messages(chat_id, token=user1.token)
        turns_before = turn_count(chat_id)
        user1.seed_spent(total=TOTAL_DAILY_LIMIT, premium=PREMIUM_DAILY_LIMIT)

        mock_provider.clear_captured_requests()
        resp = user1.post_stream(chat_id, {"content": "Blocked?", "request_id": str(uuid.uuid4())})

        assert_problem(resp, 429, "resource_exhausted", violation_subject="tokens")
        assert mock_provider.get_captured_requests() == [], "provider must not be called"
        assert list_messages(chat_id, token=user1.token) == messages_before
        assert turn_count(chat_id) == turns_before
        assert_no_reserves(user1.user_id)

    def test_retry_while_exhausted_keeps_old_turn(self, user1, mock_provider):
        """A retry rejected by the quota preflight changes nothing; after the reset, sending works."""
        chat_id = user1.create_chat(DEFAULT_MODEL)
        rid, _ = user1.complete_turn(chat_id, "Keep my answer.")
        messages_before = list_messages(chat_id, token=user1.token)
        user1.seed_spent(total=TOTAL_DAILY_LIMIT, premium=PREMIUM_DAILY_LIMIT)

        mock_provider.clear_captured_requests()
        resp = httpx.post(
            f"{API_PREFIX}/chats/{chat_id}/turns/{rid}/retry",
            headers={"Accept": "text/event-stream", **user1.headers}, timeout=30,
        )
        assert_problem(resp, 429, "resource_exhausted", violation_subject="tokens")
        assert mock_provider.get_captured_requests() == []

        assert list_messages(chat_id, token=user1.token) == messages_before
        turn = httpx.get(f"{API_PREFIX}/chats/{chat_id}/turns/{rid}", headers=user1.headers)
        assert turn.status_code == 200
        assert turn.json()["state"] == "done"

        user1.reset()
        new_rid, _ = user1.complete_turn(chat_id, "Quota is back.")
        roles = [(m["role"], m["request_id"]) for m in list_messages(chat_id, token=user1.token)]
        assert roles == [("user", rid), ("assistant", rid), ("user", new_rid), ("assistant", new_rid)]

    def test_edit_while_exhausted_keeps_old_turn(self, user1, mock_provider):
        """An edit rejected by the quota preflight (429 resource_exhausted,
        `tokens`) changes nothing: same messages, the turn stays `done`, no
        new turn, the provider is not called."""
        chat_id = user1.create_chat(DEFAULT_MODEL)
        rid, _ = user1.complete_turn(chat_id, "Keep my question.")
        messages_before = list_messages(chat_id, token=user1.token)
        user1.seed_spent(total=TOTAL_DAILY_LIMIT, premium=PREMIUM_DAILY_LIMIT)

        mock_provider.clear_captured_requests()
        resp = httpx.patch(
            f"{API_PREFIX}/chats/{chat_id}/turns/{rid}", json={"content": "Edited."},
            headers={"Accept": "text/event-stream", **user1.headers}, timeout=30,
        )
        assert_problem(resp, 429, "resource_exhausted", violation_subject="tokens")
        assert mock_provider.get_captured_requests() == []

        assert list_messages(chat_id, token=user1.token) == messages_before
        assert poll_turn(chat_id, rid, token=user1.token)["state"] == "done"
        assert turn_count(chat_id) == 1
        assert_no_reserves(user1.user_id)


class TestPeriodsCheckedSeparately:
    """14-04: the daily and the monthly period are each enforced on their own."""

    @pytest.mark.parametrize(
        ("user_fixture", "daily", "monthly", "exhausted", "other", "other_used"),
        [
            # The daily limit spent today; the month holds only today's usage.
            ("user1", TOTAL_DAILY_LIMIT, TOTAL_DAILY_LIMIT, "daily", "monthly", TOTAL_DAILY_LIMIT),
            # The monthly limit spent on earlier days; nothing today.
            ("user2", 0, TOTAL_MONTHLY_LIMIT, "monthly", "daily", 0),
        ],
    )
    def test_single_exhausted_period_rejects(
        self, request, mock_provider, user_fixture, daily, monthly, exhausted, other, other_used,
    ):
        """14-04: only the `total` bucket of one period is at its limit → 429
        `tokens`; the other period is not exhausted."""
        user = request.getfixturevalue(user_fixture)
        chat_id = user.create_chat(STANDARD_MODEL)
        user.seed_spent(total=daily, total_monthly=monthly)

        status = get_quota_status(token=user.token)
        assert find_period(status, "total", exhausted)["exhausted"] is True
        untouched = find_period(status, "total", other)
        assert (untouched["used_credits_micro"], untouched["exhausted"]) == (other_used, False)

        mock_provider.clear_captured_requests()
        resp = user.post_stream(chat_id, {"content": "Blocked?", "request_id": str(uuid.uuid4())})
        assert_problem(resp, 429, "resource_exhausted", violation_subject="tokens")
        assert mock_provider.get_captured_requests() == []
        assert_no_reserves(user.user_id)


class TestDowngrade:
    """Premium bucket exhausted, or chat model disabled: the turn runs on a standard model."""

    def test_premium_exhausted_downgrades_to_standard(self, user2, mock_provider):
        chat_id = user2.create_chat(DEFAULT_MODEL)
        user2.seed_spent(total=PREMIUM_DAILY_LIMIT, premium=PREMIUM_DAILY_LIMIT)
        premium_before = find_period(get_quota_status(token=user2.token), "premium", "daily")

        mock_provider.clear_captured_requests()
        _, done = user2.complete_turn(chat_id, "Downgrade me.")

        assert done["quota_decision"] == "downgrade"
        assert done["selected_model"] == DEFAULT_MODEL
        assert done["effective_model"] == STANDARD_MODEL
        assert done["downgrade_from"] == DEFAULT_MODEL
        assert done["downgrade_reason"] == "premium_quota_exhausted"
        # The provider got the standard model (provider_model_id of gpt-5.2).
        assert mock_provider.get_last_request()["model"] == "gpt-5.2"
        # A standard turn does not charge the premium bucket.
        premium_after = find_period(get_quota_status(token=user2.token), "premium", "daily")
        assert premium_after["used_credits_micro"] == premium_before["used_credits_micro"]

    def test_disabled_chat_model_downgrades(self, user3):
        """A chat locked to a model that was disabled later is downgraded with model_disabled."""
        chat_id = user3.create_chat(STANDARD_MODEL)
        # Simulate the catalog change: point the chat at the disabled catalog entry.
        assert exec_db("UPDATE chats SET model = ? WHERE id = ?", (DISABLED_MODEL, chat_id)) == 1

        _, done = user3.complete_turn(chat_id, "My model is gone.")

        assert done["quota_decision"] == "downgrade"
        assert done["selected_model"] == DISABLED_MODEL
        assert done["effective_model"] == STANDARD_MODEL
        assert done["downgrade_reason"] == "model_disabled"


class TestWebSearchDailyQuota:
    """The daily web_search quota only applies to requests that use web search."""

    def test_web_search_quota_only_blocks_web_search(self, user3, mock_provider):
        chat_id = user3.create_chat(DEFAULT_MODEL)  # web_search-capable model
        user3.seed(
            bucket="total", period_type="daily", web_search_calls=WEB_SEARCH_DAILY_QUOTA,
        )

        # Without web search: allowed.
        user3.complete_turn(chat_id, "No tools needed.")

        # With web search: 429 on the web_search quota, provider not called.
        mock_provider.clear_captured_requests()
        resp = user3.post_stream(chat_id, {
            "content": "SEARCH: weather", "web_search": {"enabled": True},
            "request_id": str(uuid.uuid4()),
        })
        assert_problem(resp, 429, "resource_exhausted", violation_subject="web_search")
        assert mock_provider.get_captured_requests() == []
        assert_no_reserves(user3.user_id)

    def test_web_search_below_quota_allowed(self, user3):
        chat_id = user3.create_chat(DEFAULT_MODEL)
        user3.seed(
            bucket="total", period_type="daily", web_search_calls=WEB_SEARCH_DAILY_QUOTA - 1,
        )
        resp = user3.post_stream(chat_id, {
            "content": "SEARCH: weather", "web_search": {"enabled": True},
        })
        assert resp.status_code == 200, resp.text
        expect_done(parse_sse(resp.text))


class TestCodeInterpreterDailyQuota:
    """The daily code_interpreter quota only applies to chats with code-interpreter files."""

    def test_code_interpreter_quota_only_blocks_ci_chats(self, user3, mock_provider):
        """14-20: code_interpreter_calls at the daily quota → a message in a chat
        with a ready XLSX is 429 `code_interpreter` (provider not called); a
        message in a chat without one is allowed."""
        ci_chat = user3.create_chat(STANDARD_MODEL)  # code_interpreter-capable model
        resp = httpx.post(
            f"{API_PREFIX}/chats/{ci_chat}/attachments",
            files={"file": ("data.xlsx", io.BytesIO(_make_minimal_xlsx()), XLSX_CONTENT_TYPE)},
            headers=user3.headers, timeout=60,
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["status"] == "ready", resp.json()
        user3.seed(
            bucket="total", period_type="daily",
            code_interpreter_calls=CODE_INTERPRETER_DAILY_QUOTA,
        )

        mock_provider.clear_captured_requests()
        resp = user3.post_stream(ci_chat, {
            "content": "CODEINTERP: analyze", "request_id": str(uuid.uuid4()),
        })
        assert_problem(resp, 429, "resource_exhausted", violation_subject="code_interpreter")
        assert mock_provider.get_captured_requests() == []
        assert_no_reserves(user3.user_id)

        plain_chat = user3.create_chat(STANDARD_MODEL)
        user3.complete_turn(plain_chat, "No files here.")


class TestQuotaStatusFlags:
    """warning: remaining_percentage <= 100 - warning_threshold_pct (80 -> 20); exhausted: 0."""

    @pytest.mark.parametrize(
        ("spent", "remaining_pct", "warning", "exhausted"),
        [
            (0, 100, False, False),
            (79_000_000, 21, False, False),
            (80_000_000, 20, True, False),
            (99_000_000, 1, True, False),
            (TOTAL_DAILY_LIMIT, 0, True, True),
        ],
    )
    def test_total_daily_flags(self, user2, spent, remaining_pct, warning, exhausted):
        user2.seed_spent(total=spent)

        status = get_quota_status(token=user2.token)
        assert status["warning_threshold_pct"] == 80
        daily = find_period(status, "total", "daily")
        assert daily["limit_credits_micro"] == TOTAL_DAILY_LIMIT
        assert daily["used_credits_micro"] == spent
        assert daily["remaining_credits_micro"] == TOTAL_DAILY_LIMIT - spent
        assert daily["remaining_percentage"] == remaining_pct
        assert (daily["warning"], daily["exhausted"]) == (warning, exhausted)

        # The monthly row holds the same spend, far from the monthly limit;
        # the premium tier is untouched (standard usage).
        monthly = find_period(status, "total", "monthly")
        assert (monthly["used_credits_micro"], monthly["warning"], monthly["exhausted"]) == (
            spent, False, False,
        )
        for period in ("daily", "monthly"):
            p = find_period(status, "premium", period)
            assert (p["used_credits_micro"], p["warning"], p["exhausted"]) == (0, False, False)

    def test_done_quota_warnings_match_status(self, user2):
        """The SSE done `quota_warnings` report the same flags as GET /quota/status."""
        user2.seed_spent(total=90_000_000)
        chat_id = user2.create_chat(STANDARD_MODEL)
        _, done = user2.complete_turn(chat_id, "Near the limit.")

        status = get_quota_status(token=user2.token)
        sse = {(w["tier"], w["period"]): w for w in done["quota_warnings"]}
        assert sse[("total", "daily")]["warning"] is True
        assert sse[("total", "daily")]["exhausted"] is False
        for (tier, period), w in sse.items():
            rest = find_period(status, tier, period)
            assert (w["warning"], w["exhausted"]) == (rest["warning"], rest["exhausted"])
