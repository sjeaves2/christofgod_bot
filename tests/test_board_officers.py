"""The board_officer flag and the gate built on it.

Two things are being defended here. The flag must be read strictly — a
hand-edited users.yaml eventually contains the string "false", and a truthiness
check would make that an officer. And the gate must NOT fall back to admin
rights: board items are corporate records, some admins hold no board seat, and
a quietly permissive gate is the kind of thing nobody notices until it matters.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

import permissions
from telegram.ext import ConversationHandler


def make_update(user_id: int, username: str | None = None):
    update = MagicMock()
    update.effective_user.id = user_id
    update.effective_user.username = username
    update.effective_user.full_name = "Test Person"
    update.message.reply_text = AsyncMock()
    return update


@pytest.fixture
def users(monkeypatch):
    """Patch storage.get_all_users as permissions sees it."""
    records: list[dict] = []

    async def _get_all_users():
        return records

    monkeypatch.setattr(permissions.storage, "get_all_users", _get_all_users)
    return records


class TestIsBoardOfficer:
    @pytest.mark.asyncio
    async def test_true_flag_makes_an_officer(self, users):
        users.append({"chat_id": 1, "board_officer": True})
        assert await permissions.is_board_officer(make_update(1)) is True

    @pytest.mark.asyncio
    async def test_absent_flag_is_not_an_officer(self, users):
        users.append({"chat_id": 1})
        assert await permissions.is_board_officer(make_update(1)) is False

    @pytest.mark.asyncio
    async def test_explicit_false_is_not_an_officer(self, users):
        users.append({"chat_id": 1, "board_officer": False})
        assert await permissions.is_board_officer(make_update(1)) is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("truthy", ["false", "true", "no", 1, "yes", [], {}])
    async def test_only_the_boolean_counts(self, users, truthy):
        """A YAML file edited by hand holds strings. "false" is the dangerous
        one: it is truthy in Python and would grant access."""
        users.append({"chat_id": 1, "board_officer": truthy})
        assert await permissions.is_board_officer(make_update(1)) is False

    @pytest.mark.asyncio
    async def test_an_unregistered_user_is_not_an_officer(self, users):
        users.append({"chat_id": 999, "board_officer": True})
        assert await permissions.is_board_officer(make_update(1)) is False

    @pytest.mark.asyncio
    async def test_another_officers_flag_does_not_leak(self, users):
        users.extend([{"chat_id": 1, "board_officer": True}, {"chat_id": 2}])
        assert await permissions.is_board_officer(make_update(2)) is False


class TestOfficerRoster:
    @pytest.mark.asyncio
    async def test_names_lists_only_officers(self, users):
        users.extend([
            {"chat_id": 1, "display_name": "Samuel Eaves", "board_officer": True},
            {"chat_id": 2, "display_name": "Someone Else"},
        ])
        assert await permissions.board_officer_names() == ["Samuel Eaves"]

    @pytest.mark.asyncio
    async def test_chat_ids_lists_only_officers(self, users):
        users.extend([
            {"chat_id": 1, "board_officer": True},
            {"chat_id": 2, "board_officer": True},
            {"chat_id": 3},
        ])
        assert await permissions.board_officer_chat_ids() == [1, 2]

    @pytest.mark.asyncio
    async def test_an_officer_without_a_chat_id_is_skipped(self, users):
        """Nothing can be delivered to them, and None would crash send_message."""
        users.extend([{"chat_id": None, "board_officer": True},
                      {"chat_id": 7, "board_officer": True}])
        assert await permissions.board_officer_chat_ids() == [7]

    @pytest.mark.asyncio
    async def test_names_falls_back_when_there_is_no_display_name(self, users):
        users.append({"chat_id": 5, "username": "someone", "board_officer": True})
        assert await permissions.board_officer_names() == ["someone"]


class TestBoardOfficerOnly:
    @pytest.mark.asyncio
    async def test_an_officer_reaches_the_handler(self, users):
        users.append({"chat_id": 1, "board_officer": True})
        inner = AsyncMock(return_value="ran")
        inner.__name__ = "inner"
        guarded = permissions.board_officer_only(inner)
        assert await guarded(make_update(1), MagicMock()) == "ran"

    @pytest.mark.asyncio
    async def test_a_non_officer_is_blocked(self, users):
        users.append({"chat_id": 1})
        inner = AsyncMock()
        inner.__name__ = "inner"
        guarded = permissions.board_officer_only(inner)
        result = await guarded(make_update(1), MagicMock())
        inner.assert_not_awaited()
        assert result == ConversationHandler.END

    @pytest.mark.asyncio
    async def test_the_refusal_does_not_advertise_the_feature(self, users):
        """Same wording as the prayer gate: someone who cannot act on board
        items should not learn that a board queue exists."""
        users.append({"chat_id": 1})
        inner = AsyncMock()
        inner.__name__ = "inner"
        update = make_update(1)
        await permissions.board_officer_only(inner)(update, MagicMock())
        sent = update.message.reply_text.await_args.args[0]
        assert "Unknown command" in sent
        assert "board" not in sent.lower()

    @pytest.mark.asyncio
    async def test_an_administrator_is_not_admitted_by_being_an_admin(self, users,
                                                                     monkeypatch):
        """The gate must not fall back to admin rights. Mutation check: adding
        `or is_admin(update)` to the gate makes this fail."""
        monkeypatch.setattr(permissions, "ADMIN_USERNAMES", {"bigboss"})
        users.append({"chat_id": 1, "username": "bigboss"})
        update = make_update(1, username="bigboss")
        assert permissions.is_admin(update) is True
        inner = AsyncMock()
        inner.__name__ = "inner"
        await permissions.board_officer_only(inner)(update, MagicMock())
        inner.assert_not_awaited()

    def test_the_decorator_preserves_the_handler_name(self):
        """PTB and the activity log both read __name__."""
        async def cmd_something(update, context):
            return None
        assert permissions.board_officer_only(cmd_something).__name__ == "cmd_something"
