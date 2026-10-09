import smtplib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import bot
from mail_service import MailSendError
from storage import Storage


class MailStatusTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(str(Path(self.temp.name) / "bot.db"))
        self.storage.save_smtp_settings("mail.example.com", 465, "bot@example.com", "test", "bot@example.com")
        self.update = SimpleNamespace(effective_user=SimpleNamespace(id=7), callback_query=None,
                                      message=SimpleNamespace(reply_text=AsyncMock()))
        self.context = SimpleNamespace(args=[], bot_data={"storage": self.storage})

    async def asyncTearDown(self):
        self.storage._conn.close()
        self.temp.cleanup()

    async def test_emoji_preview_is_exact_and_within_utf16_limit(self):
        body = "🙂" * 4000
        draft_id = self.storage.create_mail_draft(7, "owner@example.com", "**literal**", body)
        await bot._mail_preview(self.update, self.storage, self.storage.get_mail_draft(7, draft_id))
        calls = self.update.message.reply_text.await_args_list
        self.assertTrue(all(len(call.args[0].encode("utf-16-le")) // 2 <= 4096 for call in calls))
        self.assertTrue(all(call.kwargs["parse_mode"] is None for call in calls))
        self.assertIn(body, "".join(call.args[0] for call in calls))
        self.assertIn("**literal**", "".join(call.args[0] for call in calls))
        self.assertTrue(all(call.kwargs["reply_markup"] is None for call in calls[:-1]))
        self.assertIsNotNone(calls[-1].kwargs["reply_markup"])

    async def test_restart_uncertain_mail_can_be_queried_without_sending(self):
        draft_id = self.storage.create_mail_draft(7, "owner@example.com", "test", "body")
        self.storage.mark_mail_previewed(7, draft_id)
        self.storage.claim_mail_draft(7, draft_id)
        self.storage.recover_interrupted_mail_drafts()
        self.context.args = ["status", draft_id]
        with patch.object(bot, "send_email") as send:
            await bot.cmd_mail(self.update, self.context)
            self.assertIn("状态未确认", self.update.message.reply_text.await_args.args[0])
            self.context.args = ["history"]
            await bot.cmd_mail(self.update, self.context)
            self.assertIn(draft_id, self.update.message.reply_text.await_args.args[0])
        send.assert_not_called()

    async def test_mail_status_does_not_disclose_another_users_draft(self):
        draft_id = self.storage.create_mail_draft(8, "private@example.com", "private subject", "private body")
        self.context.args = ["status", draft_id]
        await bot.cmd_mail(self.update, self.context)
        text = self.update.message.reply_text.await_args.args[0]
        self.assertIn("属于你的", text)
        self.assertNotIn("private", text)

    async def test_explicit_smtp_rejection_is_failed_and_timeout_is_uncertain(self):
        for cause, expected in ((smtplib.SMTPAuthenticationError(535, b"denied"), "failed"),
                                (TimeoutError("response lost"), "uncertain")):
            draft_id = self.storage.create_mail_draft(7, "owner@example.com", "test", "body")
            self.storage.mark_mail_previewed(7, draft_id)
            self.context.args = ["confirm", draft_id]
            error = MailSendError("mail error")
            error.__cause__ = cause
            with patch.object(bot, "send_email", side_effect=error) as send:
                await bot.cmd_mail(self.update, self.context)
                await bot.cmd_mail(self.update, self.context)
                send.assert_called_once()
            self.assertEqual(self.storage.get_mail_draft(7, draft_id)["state"], expected)


if __name__ == "__main__":
    unittest.main()
