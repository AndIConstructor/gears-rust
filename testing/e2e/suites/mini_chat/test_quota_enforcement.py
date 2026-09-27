"""Tests for quota bucket accounting and policy version persistence.

Exhaustion, downgrade and warning flags need seeded usage and live in
test_quota_policy.py.
"""

from __future__ import annotations

import uuid

import pytest

from .conftest import (
    STANDARD_MODEL,
    USER_A_ID,
    assert_no_reserves,
    expect_done,
    find_period,
    get_quota_status,
    poll_turn,
    query_db,
    stream_message,
)


def daily_used() -> tuple[int, int]:
    """(total daily, premium daily) used_credits_micro."""
    status = get_quota_status()
    return (
        find_period(status, "total", "daily")["used_credits_micro"],
        find_period(status, "premium", "daily")["used_credits_micro"],
    )


def send_first_message(chat_id: str) -> None:
    rid = str(uuid.uuid4())
    status, events, _ = stream_message(chat_id, "Say OK.", request_id=rid)
    assert status == 200
    done = expect_done(events)
    # First message of a chat: mock usage input = max(50, 50 * 1 item), output 12.
    assert done.data["usage"] == {"input_tokens": 50, "output_tokens": 12}
    poll_turn(chat_id, rid, ("done",))
    assert_no_reserves(USER_A_ID)


class TestQuotaEnforcement:
    """Bucket accounting for premium and standard models."""

    @pytest.fixture(autouse=True)
    def _offline_only(self, request):
        if request.config.getoption("mode") == "online":
            pytest.skip("literal credit amounts depend on the mock's fixed usage")

    @pytest.mark.timeout(30)
    def test_bucket_model_premium_counts_total(self, chat):
        """A premium turn charges both `total` and `tier:premium` by its cost.

        azure-gpt-4.1: ceil(50 * 3_000_000 / 1e6) + ceil(12 * 15_000_000 / 1e6) = 150 + 180 = 330.
        """
        total_before, premium_before = daily_used()
        send_first_message(chat["id"])
        total_after, premium_after = daily_used()

        assert total_after - total_before == 330
        assert premium_after - premium_before == 330

    @pytest.mark.timeout(30)
    def test_bucket_model_standard_counts_total(self, chat_with_model):
        """A standard turn charges `total` only.

        gpt-5.2: ceil(50 * 1_000_000 / 1e6) + ceil(12 * 3_000_000 / 1e6) = 50 + 36 = 86.
        """
        chat = chat_with_model(STANDARD_MODEL)
        total_before, premium_before = daily_used()
        send_first_message(chat["id"])
        total_after, premium_after = daily_used()

        assert total_after - total_before == 86
        assert premium_after == premium_before

    @pytest.mark.timeout(30)
    def test_policy_version_persisted_per_turn(self, chat):
        """The completed turn records `policy_version_applied` 1: the only
        version of the static model policy plugin (`SUPPORTED_POLICY_VERSION`
        in gears/mini-chat/mini-chat/src/infra/plugins/static_model_policy/service.rs)."""
        request_id = str(uuid.uuid4())
        status, events, _ = stream_message(chat["id"], "Say OK.", request_id=request_id)
        assert status == 200
        expect_done(events)
        poll_turn(chat["id"], request_id, ("done",))

        rows = query_db(
            "SELECT policy_version_applied FROM chat_turns WHERE request_id = ?",
            (request_id,),
        )
        assert rows == [{"policy_version_applied": 1}], rows
