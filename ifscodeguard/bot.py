"""Telegram handlers and polling lifecycle."""

import asyncio
from contextlib import suppress
import json
import logging

from telegram import ChatMember, Update, User
from telegram.constants import ChatType
from telegram.error import TelegramError
from telegram.ext import Application, ChatMemberHandler, CommandHandler, ContextTypes

from .blacklist import BlacklistCache, SheetsSource
from .config import Config

logger = logging.getLogger(__name__)
ADMIN_STATUSES = {ChatMember.OWNER, ChatMember.ADMINISTRATOR}
GROUP_TYPES = {ChatType.GROUP, ChatType.SUPERGROUP}


def is_present(member: ChatMember) -> bool:
    return member.status in {ChatMember.MEMBER, *ADMIN_STATUSES} or (
        member.status == ChatMember.RESTRICTED and bool(member.is_member)
    )


class GuardBot:
    def __init__(self, config: Config, cache: BlacklistCache):
        self.config = config
        self.cache = cache
        self._refresh_task: asyncio.Task | None = None

    async def post_init(self, application: Application) -> None:
        await self.cache.refresh()
        self._refresh_task = asyncio.create_task(self._refresh_loop(), name="blacklist-refresh")
        logger.info("IFSCodeGuardBot started bot_id=%d", application.bot.id)

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self.config.refresh_interval)
            await self.cache.refresh()

    async def post_stop(self, application: Application) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._refresh_task
            self._refresh_task = None

    async def on_chat_member(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        event = update.chat_member
        if event is None or event.chat.type not in GROUP_TYPES:
            return
        # A restricted user may or may not actually belong to the group.
        if not is_present(event.old_chat_member) and is_present(event.new_chat_member):
            await self.check_join(event.chat.id, event.new_chat_member.user, context)

    async def check_join(
        self, chat_id: int, user: User, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        if user.id == context.bot.id:
            return
        username = f"@{user.username}" if user.username else "-"
        display_name = " ".join(part for part in (user.first_name, user.last_name) if part)
        details = (
            chat_id, user.id, username,
            json.dumps(display_name, ensure_ascii=False), str(user.is_bot).lower(),
        )
        snapshot = self.cache.snapshot
        if user.id not in snapshot.user_ids:
            logger.info("JOIN chat_id=%d user_id=%d username=%s display_name=%s is_bot=%s", *details)
            if snapshot.last_updated is None:
                logger.warning("JOIN UNCHECKED chat_id=%d user_id=%d reason=NO_USABLE_CACHE", chat_id, user.id)
            return
        logger.info("BLACKLIST MATCH chat_id=%d user_id=%d username=%s display_name=%s is_bot=%s", *details)
        try:
            own_member = await context.bot.get_chat_member(chat_id, context.bot.id)
            if own_member.status != ChatMember.OWNER and (
                own_member.status != ChatMember.ADMINISTRATOR
                or not own_member.can_restrict_members
            ):
                self._ban_failed(chat_id, user.id, "Bot lacks administrator/BAN permission.")
                return
            target = await context.bot.get_chat_member(chat_id, user.id)
            if target.status in ADMIN_STATUSES:
                self._ban_failed(chat_id, user.id, "Target is an administrator/owner; Telegram cannot ban administrators.")
                return
            # No until_date: permanent ban. Never call unban_chat_member here.
            success = await context.bot.ban_chat_member(chat_id=chat_id, user_id=user.id)
            if success:
                logger.info("BAN SUCCESS chat_id=%d user_id=%d", chat_id, user.id)
            else:
                self._ban_failed(chat_id, user.id, "Telegram returned False.")
        except TelegramError as exc:
            self._ban_failed(chat_id, user.id, str(exc))

    @staticmethod
    def _ban_failed(chat_id: int, user_id: int, error: str) -> None:
        logger.error("BAN FAILED chat_id=%d user_id=%d error=%s", chat_id, user_id, json.dumps(error, ensure_ascii=False))

    async def _is_admin(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
        chat, user, message = update.effective_chat, update.effective_user, update.effective_message
        if message is None:
            return False
        if chat is None or chat.type not in GROUP_TYPES:
            await message.reply_text("This command is only available in groups.")
            return False
        # Anonymous and channel-sent messages cannot identify a human administrator.
        if user is None or user.is_bot or message.sender_chat is not None:
            await message.reply_text("Please send this command as an identifiable group administrator.")
            return False
        try:
            member = await context.bot.get_chat_member(chat.id, user.id)
        except TelegramError as exc:
            logger.warning("ADMIN CHECK FAILED chat_id=%d user_id=%d error=%s", chat.id, user.id, json.dumps(str(exc)))
            await message.reply_text("Failed to verify administrator permissions. Please try again.")
            return False
        if member.status not in ADMIN_STATUSES:
            await message.reply_text("Only group administrators can use this command.")
            return False
        return True

    async def blacklist_reload(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_admin(update, context):
            return
        if await self.cache.refresh():
            text = f"Blacklist reloaded. {len(self.cache.snapshot.user_ids)} entries loaded."
        else:
            text = "Failed to reload blacklist. Using previous cache."
            if self.cache.snapshot.last_updated is None:
                text += "\nWARNING: No usable cache exists. Blacklist enforcement is unavailable."
        await update.effective_message.reply_text(text)

    async def blacklist_status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not await self._is_admin(update, context):
            return
        snapshot = self.cache.snapshot
        updated = (
            snapshot.last_updated.strftime("%Y-%m-%d %H:%M:%S UTC")
            if snapshot.last_updated else "Never (NO USABLE CACHE)"
        )
        state = (
            "UNAVAILABLE (no usable cache)" if snapshot.last_updated is None else
            "Using previous cache (last refresh failed)" if self.cache.last_refresh_ok is False else
            "OK"
        )
        await update.effective_message.reply_text(
            "IFSCodeGuardBot status\n\n"
            f"Blacklist entries: {len(snapshot.user_ids)}\n"
            "Username entries: 0 (not used)\n"
            "Display name entries: 0 (not used)\n"
            f"Last updated: {updated}\n"
            f"Cache status: {state}"
        )

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Do not log the complete Update, which can contain unrelated group messages.
        logger.error("TELEGRAM HANDLER ERROR error=%s", json.dumps(str(context.error), ensure_ascii=False))


def build_application(config: Config) -> Application:
    guard = GuardBot(config, BlacklistCache(SheetsSource(config).fetch))
    application = (
        Application.builder()
        .token(config.bot_token)
        .concurrent_updates(16)
        .post_init(guard.post_init)
        .post_stop(guard.post_stop)
        .post_shutdown(guard.post_stop)
        .build()
    )
    application.add_handler(ChatMemberHandler(guard.on_chat_member, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(CommandHandler("blacklist_reload", guard.blacklist_reload))
    application.add_handler(CommandHandler("blacklist_status", guard.blacklist_status))
    application.add_error_handler(guard.on_error)
    return application
