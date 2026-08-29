import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import telegram_menu_bot


class TelegramMenuTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp_dir.name) / "config.env"
        self.config_path.write_text(
            "\n".join(
                [
                    "NLB_USERNAME=one",
                    "NLB_PASSWORD=secret-one",
                    "NLB_USERNAME_2=two",
                    "NLB_PASSWORD_2=secret-two",
                    "FEISHU_WEBHOOK_URL=https://example.invalid/hook",
                    "TELEGRAM_BOT_TOKEN=123456:test-token",
                    "TELEGRAM_CHAT_ID=123456789",
                ]
            ),
            encoding="utf-8",
        )
        self.bot = telegram_menu_bot.TelegramMenuBot(self.config_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_setup_registers_private_chat_menu(self):
        calls = []

        def fake_api(method, payload=None, **kwargs):
            calls.append((method, payload or {}))
            if method == "getMe":
                return {"username": "nlb_test_bot"}
            if method == "getWebhookInfo":
                return {"url": ""}
            if method == "getMyCommands":
                return telegram_menu_bot.COMMANDS
            return True

        with patch.object(self.bot, "api", fake_api):
            self.bot.setup_menu()

        methods = [method for method, _ in calls]
        self.assertIn("setMyCommands", methods)
        self.assertIn("setChatMenuButton", methods)
        command_payload = next(payload for method, payload in calls if method == "setMyCommands")
        self.assertEqual(command_payload["scope"]["type"], "chat")
        self.assertEqual(command_payload["scope"]["chat_id"], 123456789)
        self.assertTrue(
            any(command["command"] == "rebook" for command in command_payload["commands"])
        )

    def test_authorized_resend_command_runs_handler(self):
        update = {
            "update_id": 1,
            "message": {
                "date": int(time.time()),
                "text": "/resend",
                "chat": {"id": 123456789},
                "from": {"id": 123456789},
            },
        }
        with patch.object(self.bot, "_execute_resend") as execute:
            self.bot.handle_update(update)
        execute.assert_called_once()

    def test_unauthorized_user_cannot_run_resend(self):
        update = {
            "update_id": 2,
            "message": {
                "date": int(time.time()),
                "text": "/resend",
                "chat": {"id": 123456789},
                "from": {"id": 999},
            },
        }
        with patch.object(self.bot, "_execute_resend") as execute:
            self.bot.handle_update(update)
        execute.assert_not_called()

    def test_callback_is_answered_before_resend(self):
        update = {
            "update_id": 3,
            "callback_query": {
                "id": "callback-1",
                "data": "resend",
                "from": {"id": 123456789},
                "message": {"chat": {"id": 123456789}},
            },
        }
        order = []
        with patch.object(
            self.bot,
            "answer_callback",
            side_effect=lambda *args, **kwargs: order.append("answer"),
        ), patch.object(
            self.bot,
            "_execute_resend",
            side_effect=lambda: order.append("resend"),
        ):
            self.bot.handle_update(update)
        self.assertEqual(order, ["answer", "resend"])

    def test_stale_command_is_ignored(self):
        update = {
            "update_id": 4,
            "message": {
                "date": int(time.time()) - telegram_menu_bot.STALE_MESSAGE_SECONDS - 1,
                "text": "/resend",
                "chat": {"id": 123456789},
                "from": {"id": 123456789},
            },
        }
        with patch.object(self.bot, "_execute_resend") as execute:
            self.bot.handle_update(update)
        execute.assert_not_called()

    def test_offset_is_persisted(self):
        self.bot._save_offset(42)
        self.assertEqual(self.bot._load_offset(), 42)
        self.assertEqual(
            json.loads(self.bot.offset_file.read_text(encoding="utf-8"))["offset"],
            42,
        )

    def test_execute_resend_invokes_read_only_cli(self):
        completed = Mock(returncode=0)
        with patch.object(self.bot, "_run_is_active", return_value=False), patch.object(
            self.bot, "send_text"
        ) as send_text, patch.object(
            telegram_menu_bot.subprocess, "run", return_value=completed
        ) as run:
            self.bot._execute_resend()

        self.assertIn("正在重新登录", send_text.call_args_list[0].args[0])
        command = run.call_args.args[0]
        self.assertEqual(command[-1], "--resend")
        self.assertTrue(command[1].endswith("run_local.py"))

    def test_busy_run_is_not_started(self):
        with patch.object(self.bot, "_run_is_active", return_value=True), patch.object(
            self.bot, "send_text"
        ) as send_text, patch.object(telegram_menu_bot.subprocess, "run") as run:
            self.bot._execute_resend()

        run.assert_not_called()
        self.assertIn("已有订座或核验任务", send_text.call_args.args[0])

    def test_rebook_command_requires_confirmation(self):
        update = {
            "update_id": 5,
            "message": {
                "date": int(time.time()),
                "text": "/rebook",
                "chat": {"id": 123456789},
                "from": {"id": 123456789},
            },
        }
        with patch.object(self.bot, "send_text") as send_text, patch.object(
            self.bot, "_execute_rebook"
        ) as execute:
            self.bot.handle_update(update)

        execute.assert_not_called()
        markup = send_text.call_args.kwargs["reply_markup"]
        callbacks = [button["callback_data"] for button in markup["inline_keyboard"][0]]
        self.assertEqual(callbacks, ["rebook_confirm", "rebook_cancel"])

    def test_rebook_confirmation_runs_full_cli(self):
        update = {
            "update_id": 6,
            "callback_query": {
                "id": "callback-rebook",
                "data": "rebook_confirm",
                "from": {"id": 123456789},
                "message": {
                    "message_id": 88,
                    "chat": {"id": 123456789},
                },
            },
        }
        order = []
        with patch.object(
            self.bot,
            "answer_callback",
            side_effect=lambda *args, **kwargs: order.append("answer"),
        ), patch.object(
            self.bot,
            "replace_callback_message",
            side_effect=lambda *args, **kwargs: order.append("replace"),
        ), patch.object(
            self.bot,
            "_execute_rebook",
            side_effect=lambda: order.append("rebook"),
        ):
            self.bot.handle_update(update)

        self.assertEqual(order, ["answer", "replace", "rebook"])

    def test_rebook_cancel_does_not_run(self):
        update = {
            "update_id": 7,
            "callback_query": {
                "id": "callback-cancel",
                "data": "rebook_cancel",
                "from": {"id": 123456789},
                "message": {
                    "message_id": 89,
                    "chat": {"id": 123456789},
                },
            },
        }
        with patch.object(self.bot, "answer_callback"), patch.object(
            self.bot, "replace_callback_message"
        ) as replace, patch.object(self.bot, "_execute_rebook") as execute:
            self.bot.handle_update(update)

        execute.assert_not_called()
        self.assertIn("已取消", replace.call_args.args[1])

    def test_execute_rebook_invokes_full_cli_without_resend_flag(self):
        completed = Mock(returncode=0)
        with patch.object(self.bot, "_run_is_active", return_value=False), patch.object(
            self.bot, "send_text"
        ), patch.object(
            telegram_menu_bot.subprocess, "run", return_value=completed
        ) as run:
            self.bot._execute_rebook()

        command = run.call_args.args[0]
        self.assertTrue(command[1].endswith("run_local.py"))
        self.assertEqual(len(command), 2)


if __name__ == "__main__":
    unittest.main()
