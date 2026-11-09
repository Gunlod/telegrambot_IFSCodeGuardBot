"""Behavioral tests. No Telegram or Google credentials/network are required."""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telegram import Chat, ChatMember, ChatMemberLeft, ChatMemberMember, ChatMemberUpdated, Update, User
from telegram.error import BadRequest, Forbidden, NetworkError

from ifscodeguard.blacklist import BlacklistCache, SCOPES, SheetFormatError, SheetsSource, Snapshot, parse_rows
from ifscodeguard.bot import GuardBot, build_application, is_present
from ifscodeguard.config import Config, ConfigError, load_config
from ifscodeguard.logging_setup import RedactingFormatter, configure_logging


def configuration(**overrides):
    return replace(Config(
        bot_token="123456:TEST_TOKEN",
        spreadsheet_id="test_sheet",
        worksheet_name="blacklist",
        service_account_file=Path("secrets/service-account.json"),
    ), **overrides)


def command_update(user_id=42, status="supergroup", sender_chat=None, is_bot=False):
    return SimpleNamespace(
        effective_chat=SimpleNamespace(id=-100123, type=status),
        effective_user=SimpleNamespace(id=user_id, is_bot=is_bot),
        effective_message=SimpleNamespace(sender_chat=sender_chat, reply_text=AsyncMock()),
    )


class SheetTests(unittest.TestCase):
    def test_numeric_ids_only_and_deduplicate(self):
        self.assertEqual(parse_rows([
            ["numeric user ID", "remarks"],
            [123, "@user and Display Name are only remarks"], [" 123 ", "duplicate"],
            [456.0], ["789"], [], ["", "memo"], [None],
        ]), {123, 456, 789})

    def test_header_only_is_valid_empty_list(self):
        self.assertEqual(parse_rows([["numeric user ID", "remarks"]]), frozenset())

    def test_rejects_invalid_data_without_partial_results(self):
        for invalid in [True, False, -1, 0, 1.5, float("inf"), float("nan"),
                        "1e6", "1,234", "@user", "123.0", "-123", 2**52]:
            with self.subTest(invalid=invalid), self.assertRaises(SheetFormatError):
                parse_rows([["numeric user ID", "remarks"], [123], [invalid]])

    def test_missing_or_wrong_header(self):
        for rows in [[], [["username", "remarks"]], [["numeric user ID"]], [[123, "note"]]]:
            with self.subTest(rows=rows), self.assertRaises(SheetFormatError):
                parse_rows(rows)

    def test_sheets_request_is_readonly_unformatted_and_escaped(self):
        config = configuration(worksheet_name="Guard's list")
        with patch("ifscodeguard.blacklist.service_account.Credentials.from_service_account_file") as credentials, \
             patch("ifscodeguard.blacklist.AuthorizedSession") as session_class:
            session = session_class.return_value.__enter__.return_value
            session.get.return_value.json.return_value = {
                "values": [["numeric user ID", "remarks"], [123]]
            }
            self.assertEqual(SheetsSource(config).fetch(), {123})
            credentials.assert_called_once_with(str(config.service_account_file), scopes=SCOPES)
            args, kwargs = session.get.call_args
            self.assertIn("%27Guard%27%27s%20list%27%21A%3AB", args[0])
            self.assertEqual(kwargs["params"]["valueRenderOption"], "UNFORMATTED_VALUE")
            self.assertEqual(kwargs["timeout"], 15)
            session.get.return_value.raise_for_status.assert_called_once()

    def test_google_http_error_is_not_treated_as_empty_sheet(self):
        with patch("ifscodeguard.blacklist.service_account.Credentials.from_service_account_file"), \
             patch("ifscodeguard.blacklist.AuthorizedSession") as session_class:
            session = session_class.return_value.__enter__.return_value
            session.get.return_value.raise_for_status.side_effect = RuntimeError("403")
            with self.assertRaises(RuntimeError):
                SheetsSource(configuration()).fetch()
            session.get.return_value.json.assert_not_called()


class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_failure_preserves_last_good_ids_and_timestamp(self):
        fetch = Mock(side_effect=[frozenset({123, 456}), RuntimeError("API unavailable")])
        cache = BlacklistCache(fetch)
        self.assertTrue(await cache.refresh())
        previous = cache.snapshot
        with self.assertLogs("ifscodeguard.blacklist", level="WARNING") as logs:
            self.assertFalse(await cache.refresh())
        self.assertIs(cache.snapshot, previous)
        self.assertIn("Using previous cache", " ".join(logs.output))
        self.assertFalse(cache.last_refresh_ok)

    async def test_initial_failure_is_explicit_and_recoverable(self):
        fetch = Mock(side_effect=[FileNotFoundError("key missing"), frozenset({123})])
        cache = BlacklistCache(fetch)
        with self.assertLogs("ifscodeguard.blacklist", level="ERROR") as logs:
            self.assertFalse(await cache.refresh())
        self.assertIn("NO USABLE CACHE", " ".join(logs.output))
        self.assertIsNone(cache.snapshot.last_updated)
        self.assertTrue(await cache.refresh())
        self.assertEqual(cache.snapshot.user_ids, {123})
        self.assertIsNotNone(cache.snapshot.last_updated)

    async def test_successfully_empty_sheet_replaces_previous_ids(self):
        cache = BlacklistCache(Mock(side_effect=[frozenset({123}), frozenset()]))
        await cache.refresh()
        self.assertTrue(await cache.refresh())
        self.assertEqual(cache.snapshot.user_ids, frozenset())
        self.assertIsNotNone(cache.snapshot.last_updated)

    async def test_malformed_refresh_preserves_entire_previous_snapshot(self):
        cache = BlacklistCache(Mock(side_effect=[frozenset({123}), SheetFormatError("bad row")]))
        await cache.refresh()
        previous = cache.snapshot
        with self.assertLogs(level="WARNING"):
            await cache.refresh()
        self.assertIs(cache.snapshot, previous)

    async def test_refresh_runs_off_event_loop_and_serializes_writers(self):
        owner_thread = threading.get_ident()
        workers = []
        def fetch():
            workers.append(threading.get_ident())
            return frozenset({123})
        cache = BlacklistCache(fetch)
        self.assertEqual(await asyncio.gather(cache.refresh(), cache.refresh()), [True, True])
        self.assertTrue(all(worker != owner_thread for worker in workers))


class GuardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fetch = Mock(return_value=frozenset({123, 456}))
        self.cache = BlacklistCache(self.fetch)
        await self.cache.refresh()
        self.guard = GuardBot(configuration(), self.cache)
        own_member = SimpleNamespace(status=ChatMember.ADMINISTRATOR, can_restrict_members=True)
        target = SimpleNamespace(status=ChatMember.MEMBER)
        self.bot = SimpleNamespace(
            id=999,
            get_chat_member=AsyncMock(side_effect=lambda chat, user: own_member if user == 999 else target),
            ban_chat_member=AsyncMock(return_value=True),
        )
        self.context = SimpleNamespace(bot=self.bot)

    async def test_join_without_username_or_last_name_logs_and_uses_no_api(self):
        with self.assertLogs("ifscodeguard.bot", level="INFO") as logs:
            await self.guard.check_join(-100123, User(777, 'Example\nName', False), self.context)
        text = " ".join(logs.output)
        self.assertIn("username=-", text)
        self.assertIn('display_name="Example\\nName"', text)
        self.bot.get_chat_member.assert_not_awaited()
        self.bot.ban_chat_member.assert_not_awaited()
        self.fetch.assert_called_once()  # no Sheets fetch per join

    async def test_blacklisted_user_is_permanently_banned_and_logged(self):
        with self.assertLogs("ifscodeguard.bot", level="INFO") as logs:
            await self.guard.check_join(-100123, User(123, "Example", False, last_name="User", username="example"), self.context)
        self.bot.ban_chat_member.assert_awaited_once_with(chat_id=-100123, user_id=123)
        self.assertIn("BLACKLIST MATCH", " ".join(logs.output))
        self.assertIn("BAN SUCCESS", " ".join(logs.output))
        self.assertIn('display_name="Example User"', " ".join(logs.output))

    async def test_other_bots_are_checked(self):
        with self.assertLogs("ifscodeguard.bot", level="INFO") as logs:
            await self.guard.check_join(-100123, User(123, "OtherBot", True), self.context)
        self.bot.ban_chat_member.assert_awaited_once()
        self.assertIn("is_bot=true", " ".join(logs.output))

    async def test_self_is_excluded_even_if_blacklisted(self):
        self.cache.snapshot = Snapshot(frozenset({999}), datetime.now(timezone.utc))
        await self.guard.check_join(-100123, User(999, "Self", True), self.context)
        self.bot.get_chat_member.assert_not_awaited()
        self.bot.ban_chat_member.assert_not_awaited()

    async def test_several_join_updates_all_checked(self):
        chat = Chat(-100123, Chat.SUPERGROUP)
        updates = []
        for number, user_id in enumerate([123, 456, 777]):
            user = User(user_id, "Example", False)
            event = ChatMemberUpdated(chat, user, datetime.now(timezone.utc),
                                      ChatMemberLeft(user), ChatMemberMember(user))
            updates.append(Update(number, chat_member=event))
        await asyncio.gather(*(self.guard.on_chat_member(update, self.context) for update in updates))
        self.assertEqual({call.kwargs["user_id"] for call in self.bot.ban_chat_member.await_args_list}, {123, 456})

    async def test_promotion_and_departure_are_not_joins(self):
        for old, new in [(ChatMember.MEMBER, ChatMember.ADMINISTRATOR),
                         (ChatMember.MEMBER, ChatMember.LEFT),
                         (ChatMember.RESTRICTED, ChatMember.MEMBER)]:
            event = SimpleNamespace(
                chat=SimpleNamespace(id=-100123, type=Chat.SUPERGROUP),
                old_chat_member=SimpleNamespace(status=old, is_member=True),
                new_chat_member=SimpleNamespace(status=new, user=User(123, "Name", False)),
            )
            await self.guard.on_chat_member(SimpleNamespace(chat_member=event), self.context)
        self.bot.ban_chat_member.assert_not_awaited()

    def test_restricted_membership_uses_is_member(self):
        self.assertFalse(is_present(SimpleNamespace(status=ChatMember.RESTRICTED, is_member=False)))
        self.assertTrue(is_present(SimpleNamespace(status=ChatMember.RESTRICTED, is_member=True)))

    async def test_missing_ban_permission(self):
        self.bot.get_chat_member.side_effect = None
        self.bot.get_chat_member.return_value = SimpleNamespace(status=ChatMember.ADMINISTRATOR, can_restrict_members=False)
        with self.assertLogs("ifscodeguard.bot", level="ERROR") as logs:
            await self.guard.check_join(-100123, User(123, "Name", False), self.context)
        self.assertIn("BAN FAILED", " ".join(logs.output))
        self.bot.ban_chat_member.assert_not_awaited()

    async def test_administrator_and_owner_cannot_be_banned(self):
        for status in [ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
            self.bot.get_chat_member.side_effect = [
                SimpleNamespace(status=ChatMember.ADMINISTRATOR, can_restrict_members=True),
                SimpleNamespace(status=status),
            ]
            with self.assertLogs("ifscodeguard.bot", level="ERROR") as logs:
                await self.guard.check_join(-100123, User(123, "Name", False), self.context)
            self.assertIn("administrator/owner", " ".join(logs.output))
        self.bot.ban_chat_member.assert_not_awaited()

    async def test_telegram_failures_do_not_break_later_joins(self):
        for error in [Forbidden("no permission"), BadRequest("cannot ban"), NetworkError("offline")]:
            self.bot.ban_chat_member.side_effect = error
            with self.assertLogs("ifscodeguard.bot", level="ERROR") as logs:
                await self.guard.check_join(-100123, User(123, "Name", False), self.context)
            self.assertIn("BAN FAILED", " ".join(logs.output))
        self.bot.ban_chat_member.side_effect = None
        await self.guard.check_join(-100123, User(456, "Name", False), self.context)
        self.assertEqual(self.bot.ban_chat_member.await_count, 4)

    async def test_no_cache_warns_on_join(self):
        self.cache.snapshot = Snapshot()
        with self.assertLogs("ifscodeguard.bot", level="WARNING") as logs:
            await self.guard.check_join(-100123, User(777, "Name", False), self.context)
        self.assertIn("NO_USABLE_CACHE", " ".join(logs.output))
        self.bot.ban_chat_member.assert_not_awaited()

    async def test_admin_reload_success(self):
        self.bot.get_chat_member.side_effect = None
        self.bot.get_chat_member.return_value = SimpleNamespace(status=ChatMember.ADMINISTRATOR)
        update = command_update()
        await self.guard.blacklist_reload(update, self.context)
        update.effective_message.reply_text.assert_awaited_once_with("Blacklist reloaded. 2 entries loaded.")

    async def test_nonadmin_cannot_reload_or_read_status(self):
        for handler in [self.guard.blacklist_reload, self.guard.blacklist_status]:
            update = command_update()
            await handler(update, self.context)
            update.effective_message.reply_text.assert_awaited_once_with("Only group administrators can use this command.")
        self.fetch.assert_called_once()

    async def test_private_and_anonymous_commands_rejected(self):
        for update in [command_update(status="private"), command_update(sender_chat=object()), command_update(is_bot=True)]:
            await self.guard.blacklist_reload(update, self.context)
            update.effective_message.reply_text.assert_awaited_once()
        self.bot.get_chat_member.assert_not_awaited()
        self.fetch.assert_called_once()

    async def test_admin_check_errors_reject_commands(self):
        self.bot.get_chat_member.side_effect = NetworkError("offline")
        update = command_update()
        with self.assertLogs("ifscodeguard.bot", level="WARNING"):
            await self.guard.blacklist_reload(update, self.context)
        self.assertIn("Failed to verify", update.effective_message.reply_text.call_args.args[0])
        self.fetch.assert_called_once()

    async def test_reload_failure_keeps_cache(self):
        self.bot.get_chat_member.side_effect = None
        self.bot.get_chat_member.return_value = SimpleNamespace(status=ChatMember.OWNER)
        self.fetch.side_effect = RuntimeError("Google unavailable")
        update = command_update()
        with self.assertLogs("ifscodeguard.blacklist", level="WARNING"):
            await self.guard.blacklist_reload(update, self.context)
        update.effective_message.reply_text.assert_awaited_once_with("Failed to reload blacklist. Using previous cache.")
        self.assertEqual(self.cache.snapshot.user_ids, {123, 456})

    async def test_status_distinguishes_stale_and_unavailable_cache(self):
        self.bot.get_chat_member.side_effect = None
        self.bot.get_chat_member.return_value = SimpleNamespace(status=ChatMember.ADMINISTRATOR)
        self.cache.last_refresh_ok = False
        update = command_update()
        await self.guard.blacklist_status(update, self.context)
        text = update.effective_message.reply_text.call_args.args[0]
        self.assertIn("Blacklist entries: 2", text)
        self.assertIn("Username entries: 0 (not used)", text)
        self.assertIn("last refresh failed", text)
        self.cache.snapshot = Snapshot()
        await self.guard.blacklist_status(update, self.context)
        text = update.effective_message.reply_text.call_args.args[0]
        self.assertIn("NO USABLE CACHE", text)
        self.assertIn("UNAVAILABLE", text)

    async def test_periodic_task_stops_cleanly(self):
        await self.guard.post_init(SimpleNamespace(bot=self.bot))
        task = self.guard._refresh_task
        self.assertIsNotNone(task)
        await self.guard.post_stop(None)
        self.assertTrue(task.cancelled())
        self.assertIsNone(self.guard._refresh_task)


class ConfigAndLoggingTests(unittest.TestCase):
    def test_config_defaults_and_paths_relative_to_config(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                'telegram:\n  bot_token: "123456:TEST_TOKEN"\n'
                'google:\n  spreadsheet_url: "https://docs.google.com/spreadsheets/d/test_id/edit#gid=0"\n',
                encoding="utf-8",
            )
            config = load_config(path)
            self.assertEqual(config.refresh_interval, 60)
            self.assertEqual(config.service_account_file, Path(directory).resolve() / "secrets/service-account.json")
            self.assertEqual(config.log_file, Path(directory).resolve() / "logs/ifscodeguard.log")
            self.assertNotIn("TEST_TOKEN", repr(config))

    def test_invalid_config_does_not_echo_secrets(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            for content in [
                "telegram: [123456:TEST_TOKEN\n",
                "telegram:\n  bot_token: SECRET\n",
                "[]",
            ]:
                path.write_text(content, encoding="utf-8")
                with self.assertRaises(ConfigError) as error:
                    load_config(path)
                self.assertNotIn("SECRET", str(error.exception))
                self.assertNotIn("TEST_TOKEN", str(error.exception))

    def test_zero_refresh_interval_is_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                'telegram:\n  bot_token: "123456:TEST_TOKEN"\n'
                'google:\n  spreadsheet_url: "https://docs.google.com/spreadsheets/d/test_id/edit"\n'
                'blacklist:\n  refresh_interval: 0\n', encoding="utf-8",
            )
            with self.assertRaisesRegex(ConfigError, "positive integer"):
                load_config(path)

    def test_formatter_redacts_token_in_exception_url(self):
        record = logging.LogRecord("test", logging.ERROR, "", 0,
                                   "error=%s", ("https://api.telegram.org/bot123456:TEST_TOKEN/getMe",), None)
        result = RedactingFormatter("123456:TEST_TOKEN").format(record)
        self.assertNotIn("TEST_TOKEN", result)
        self.assertIn("[REDACTED]", result)

    def test_stdout_file_redaction_and_rotation(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "guard.log"
            root = logging.getLogger()
            old_handlers, old_level = root.handlers[:], root.level
            # Avoid closing unittest's/caller's handlers via basicConfig(force=True).
            root.handlers = []
            try:
                with patch("ifscodeguard.logging_setup.sys.stdout") as stdout:
                    configure_logging(configuration(log_file=path, log_max_bytes=180, log_backup_count=2))
                    logging.getLogger("ifscodeguard.test").info("secret=%s", "123456:TEST_TOKEN")
                    self.assertTrue(stdout.write.called)
                    self.assertNotIn("TEST_TOKEN", path.read_text(encoding="utf-8"))
                    for number in range(8):
                        logging.getLogger("ifscodeguard.test").info("rotation record %d %s", number, "x" * 70)
                    self.assertTrue(Path(str(path) + ".1").exists())
                    self.assertEqual(sum(isinstance(handler, RotatingFileHandler) for handler in root.handlers), 1)
            finally:
                for handler in root.handlers:
                    handler.close()
                root.handlers, root.level = old_handlers, old_level

    def test_application_registers_handlers_without_network(self):
        app = build_application(configuration())
        self.assertEqual(len(app.handlers[0]), 3)
        self.assertEqual(app.concurrent_updates, 16)


if __name__ == "__main__":
    unittest.main()
