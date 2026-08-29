"""Private Telegram command listener for the local NLB resend workflow."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.error
import urllib.request

import run_local


POLL_TIMEOUT_SECONDS = 50
RESEND_TIMEOUT_SECONDS = 300
REBOOK_TIMEOUT_SECONDS = 900
STALE_MESSAGE_SECONDS = 120
RESEND_BUTTON_TEXT = "🔄 再推一次任务结果"
REBOOK_BUTTON_TEXT = "🗓 重新预定"
COMMANDS = [
    {"command": "start", "description": "显示操作菜单"},
    {"command": "resend", "description": "再推一次任务结果"},
    {"command": "rebook", "description": "重新运行完整订座程序"},
    {"command": "status", "description": "查看机器人运行状态"},
    {"command": "help", "description": "查看使用说明"},
]


class TelegramAPIError(RuntimeError):
    pass


def telegram_id(value: str):
    stripped = str(value).strip()
    try:
        return int(stripped)
    except ValueError:
        return stripped


class TelegramMenuBot:
    def __init__(self, config_path: Path):
        self.config_path = config_path.expanduser()
        self.config = run_local.load_config(self.config_path)
        self.bot_token = self.config["TELEGRAM_BOT_TOKEN"].strip()
        self.chat_id = str(self.config["TELEGRAM_CHAT_ID"]).strip()
        self.allowed_user_id = str(
            self.config.get("TELEGRAM_ALLOWED_USER_ID", self.chat_id)
        ).strip()
        self.offset_file = self.config_path.parent / "telegram-menu-offset.json"

    def api(self, method: str, payload: dict | None = None, *, timeout: int = 15):
        request = urllib.request.Request(
            f"https://api.telegram.org/bot{self.bot_token}/{method}",
            data=json.dumps(payload or {}, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise TelegramAPIError(f"{method} HTTP {exc.code}: {body}") from exc
        except Exception as exc:
            safe_error = str(exc).replace(self.bot_token, "<redacted-token>")
            raise TelegramAPIError(f"{method}: {safe_error}") from exc

        try:
            result = json.loads(body)
        except json.JSONDecodeError as exc:
            raise TelegramAPIError(f"{method} 返回了非 JSON 响应") from exc
        if result.get("ok") is not True:
            raise TelegramAPIError(
                f"{method} 被 Telegram 拒绝：{result.get('description', '未知错误')}"
            )
        return result.get("result")

    @property
    def result_button(self) -> dict:
        return {
            "inline_keyboard": [
                [{"text": RESEND_BUTTON_TEXT, "callback_data": "resend"}],
                [{"text": REBOOK_BUTTON_TEXT, "callback_data": "rebook"}],
            ]
        }

    @property
    def rebook_confirmation(self) -> dict:
        return {
            "inline_keyboard": [
                [
                    {
                        "text": "✅ 确认重新预定",
                        "callback_data": "rebook_confirm",
                    },
                    {
                        "text": "取消",
                        "callback_data": "rebook_cancel",
                    },
                ]
            ]
        }

    def send_text(
        self,
        text: str,
        *,
        with_button: bool = False,
        reply_markup: dict | None = None,
    ) -> None:
        payload = {
            "chat_id": telegram_id(self.chat_id),
            "text": text,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        elif with_button:
            payload["reply_markup"] = self.result_button
        self.api("sendMessage", payload)

    def replace_callback_message(self, message: dict, text: str) -> None:
        self.api(
            "editMessageText",
            {
                "chat_id": telegram_id(str((message.get("chat") or {}).get("id"))),
                "message_id": message.get("message_id"),
                "text": text,
                "reply_markup": {"inline_keyboard": []},
            },
        )

    def answer_callback(self, callback_id: str, text: str, *, alert=False) -> None:
        self.api(
            "answerCallbackQuery",
            {
                "callback_query_id": callback_id,
                "text": text,
                "show_alert": alert,
            },
        )

    def _check_no_webhook(self) -> dict:
        webhook_info = self.api("getWebhookInfo")
        if webhook_info.get("url"):
            raise TelegramAPIError(
                "当前机器人配置了 webhook，无法启动本机 getUpdates 监听；"
                "请先确认 webhook 用途再删除"
            )
        return webhook_info

    def setup_menu(self) -> None:
        identity = self.api("getMe")
        self._check_no_webhook()
        scope = {
            "type": "chat",
            "chat_id": telegram_id(self.chat_id),
        }
        self.api("setMyCommands", {"commands": COMMANDS, "scope": scope})
        self.api(
            "setChatMenuButton",
            {
                "chat_id": telegram_id(self.chat_id),
                "menu_button": {"type": "commands"},
            },
        )
        registered = self.api("getMyCommands", {"scope": scope})
        if not any(command.get("command") == "resend" for command in registered):
            raise TelegramAPIError("菜单注册后未读取到 /resend")
        self.send_text(
            "🪑 NLB 订座机器人菜单已启用\n\n"
            "点击下方按钮或输入 /resend，可重新登录核验并补推最终座位。",
            with_button=True,
        )
        print(
            f"Telegram 菜单注册成功：@{identity.get('username', 'unknown')}，"
            f"授权 Chat ID={self.chat_id}"
        )

    def check(self) -> None:
        identity = self.api("getMe")
        webhook = self._check_no_webhook()
        commands = self.api(
            "getMyCommands",
            {
                "scope": {
                    "type": "chat",
                    "chat_id": telegram_id(self.chat_id),
                }
            },
        )
        menu = self.api(
            "getChatMenuButton",
            {"chat_id": telegram_id(self.chat_id)},
        )
        print(
            json.dumps(
                {
                    "bot": identity.get("username"),
                    "webhook_url": webhook.get("url", ""),
                    "commands": commands,
                    "menu_button": menu,
                    "chat_id": self.chat_id,
                },
                ensure_ascii=False,
                indent=2,
            )
        )

    def _is_authorized(self, chat_id, user_id) -> bool:
        return str(chat_id) == self.chat_id and str(user_id) == self.allowed_user_id

    def _run_is_active(self) -> bool:
        lock_file = run_local.RUN_LOCK_PATH.open("a+", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            return True
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()
        return False

    def _execute_local_task(
        self,
        arguments: list[str],
        *,
        start_message: str,
        task_name: str,
        timeout: int,
    ) -> None:
        if self._run_is_active():
            self.send_text("⏳ 已有订座或核验任务正在运行，请稍后再试。")
            return

        self.send_text(start_message)
        env = os.environ.copy()
        env["NLB_LOCAL_CONFIG"] = str(self.config_path)
        env["PYTHONUNBUFFERED"] = "1"
        env["TZ"] = "Asia/Singapore"
        try:
            result = subprocess.run(
                [
                    sys.executable,
                    str(run_local.REPO_ROOT / "run_local.py"),
                    *arguments,
                ],
                cwd=run_local.REPO_ROOT,
                env=env,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            timeout_minutes = max(1, timeout // 60)
            self.send_text(
                f"❌ {task_name}超过 {timeout_minutes} 分钟，已终止等待，请检查本机日志。"
            )
            return

        if result.returncode == 75:
            self.send_text("⏳ 已有订座或核验任务正在运行，请稍后再试。")
        elif result.returncode != 0:
            self.send_text(
                f"⚠️ {task_name}结束，但订座、核验或推送存在异常"
                f"（退出码 {result.returncode}）。"
            )

    def _execute_resend(self) -> None:
        self._execute_local_task(
            ["--resend"],
            start_message="⏳ 正在重新登录 NLB，核验最终座位并补推结果…",
            task_name="补推任务",
            timeout=RESEND_TIMEOUT_SECONDS,
        )

    def _execute_rebook(self) -> None:
        self._execute_local_task(
            [],
            start_message="🚀 正在重新运行完整订座程序，请勿重复点击…",
            task_name="重新预定任务",
            timeout=REBOOK_TIMEOUT_SECONDS,
        )

    def _ask_rebook_confirmation(self) -> None:
        self.send_text(
            "⚠️ 确认重新运行完整订座程序？\n\n"
            "程序将按现有配置重新执行两个账号的全部时段，"
            "并在完成后核验座位、推送最终结果。",
            reply_markup=self.rebook_confirmation,
        )

    def _send_help(self) -> None:
        self.send_text(
            "🪑 NLB 订座机器人\n\n"
            "/resend — 重新登录读取 Upcoming 并补推最终座位\n"
            "/rebook — 重新运行完整订座程序（需要二次确认）\n"
            "/status — 查看本机是否正在执行订座或核验\n"
            "/help — 查看使用说明\n\n"
            "补推不会进入订座流程。",
            with_button=True,
        )

    def handle_update(self, update: dict) -> None:
        callback = update.get("callback_query")
        if callback:
            message = callback.get("message") or {}
            chat_id = (message.get("chat") or {}).get("id")
            user_id = (callback.get("from") or {}).get("id")
            callback_id = callback.get("id", "")
            if not self._is_authorized(chat_id, user_id):
                if callback_id:
                    self.answer_callback(callback_id, "无权执行此操作", alert=True)
                print(f"忽略未授权 callback：chat={chat_id}, user={user_id}")
                return
            if callback.get("data") == "resend":
                self.answer_callback(callback_id, "开始核验最终座位")
                self._execute_resend()
            elif callback.get("data") == "rebook":
                self.answer_callback(callback_id, "请确认是否重新预定")
                self._ask_rebook_confirmation()
            elif callback.get("data") == "rebook_confirm":
                self.answer_callback(callback_id, "已确认，开始重新预定")
                self.replace_callback_message(
                    message,
                    "🚀 已确认重新预定，正在启动完整订座程序…",
                )
                self._execute_rebook()
            elif callback.get("data") == "rebook_cancel":
                self.answer_callback(callback_id, "已取消")
                self.replace_callback_message(message, "已取消重新预定。")
            return

        message = update.get("message") or {}
        if not message:
            return
        chat_id = (message.get("chat") or {}).get("id")
        user_id = (message.get("from") or {}).get("id")
        if not self._is_authorized(chat_id, user_id):
            print(f"忽略未授权消息：chat={chat_id}, user={user_id}")
            return
        message_date = int(message.get("date") or 0)
        if message_date and time.time() - message_date > STALE_MESSAGE_SECONDS:
            print(f"忽略过期 Telegram 消息：update={update.get('update_id')}")
            return

        text = str(message.get("text") or "").strip()
        command = text.split("@", 1)[0]
        if command == "/resend" or text == RESEND_BUTTON_TEXT:
            self._execute_resend()
        elif command == "/rebook" or text == REBOOK_BUTTON_TEXT:
            self._ask_rebook_confirmation()
        elif command == "/status":
            status = "运行中" if self._run_is_active() else "空闲"
            self.send_text(f"🤖 NLB 本机任务状态：{status}")
        elif command in {"/start", "/help"}:
            self._send_help()

    def _load_offset(self) -> int | None:
        if not self.offset_file.is_file():
            return None
        try:
            data = json.loads(self.offset_file.read_text(encoding="utf-8"))
            return int(data["offset"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def _save_offset(self, offset: int) -> None:
        self.offset_file.parent.mkdir(parents=True, exist_ok=True)
        temp_file = self.offset_file.with_suffix(".tmp")
        temp_file.write_text(
            json.dumps({"offset": offset}, ensure_ascii=False),
            encoding="utf-8",
        )
        temp_file.replace(self.offset_file)

    def _initial_offset(self) -> int:
        stored = self._load_offset()
        if stored is not None:
            return stored
        pending = self.api(
            "getUpdates",
            {
                "timeout": 0,
                "limit": 100,
                "allowed_updates": ["message", "callback_query"],
            },
        )
        offset = max(
            (int(update["update_id"]) + 1 for update in pending),
            default=0,
        )
        self._save_offset(offset)
        print(f"首次启动已跳过 {len(pending)} 条历史 Telegram 更新")
        return offset

    def run_forever(self) -> None:
        self._check_no_webhook()
        offset = self._initial_offset()
        print(
            f"Telegram 菜单监听已启动：chat={self.chat_id}, "
            f"allowed_user={self.allowed_user_id}"
        )
        while True:
            try:
                updates = self.api(
                    "getUpdates",
                    {
                        "offset": offset,
                        "timeout": POLL_TIMEOUT_SECONDS,
                        "limit": 20,
                        "allowed_updates": ["message", "callback_query"],
                    },
                    timeout=POLL_TIMEOUT_SECONDS + 10,
                )
                for update in updates:
                    offset = int(update["update_id"]) + 1
                    self._save_offset(offset)
                    try:
                        self.handle_update(update)
                    except Exception as exc:
                        print(
                            f"处理 Telegram 更新失败：{type(exc).__name__}: {exc}",
                            file=sys.stderr,
                        )
            except TelegramAPIError as exc:
                print(f"Telegram 监听异常：{exc}；5 秒后重试", file=sys.stderr)
                time.sleep(5)


def main() -> int:
    parser = argparse.ArgumentParser(description="NLB Telegram 菜单监听器")
    parser.add_argument("--setup-menu", action="store_true", help="注册命令菜单")
    parser.add_argument("--check", action="store_true", help="检查机器人和菜单状态")
    args = parser.parse_args()

    config_path = Path(
        os.environ.get("NLB_LOCAL_CONFIG", run_local.DEFAULT_CONFIG)
    ).expanduser()
    bot = TelegramMenuBot(config_path)
    if args.setup_menu:
        bot.setup_menu()
        return 0
    if args.check:
        bot.check()
        return 0
    try:
        bot.run_forever()
    except KeyboardInterrupt:
        print("Telegram 菜单监听已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
