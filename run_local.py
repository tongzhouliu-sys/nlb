"""Run the NLB booking flow for both locally configured accounts."""

from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from datetime import datetime, timedelta
from html import escape
import urllib.error
import urllib.request
from zoneinfo import ZoneInfo


REPO_ROOT = Path(__file__).resolve().parent
SGT = ZoneInfo("Asia/Singapore")
DEFAULT_CONFIG = (
    Path.home() / "Library" / "Application Support" / "NLB Seat Booking" / "config.env"
)
RUN_LOCK_PATH = Path("/private/tmp/nlb-seat-booking.lock")
ACCOUNT_SCHEDULES = [
    ("账号1", "NLB_USERNAME", "NLB_PASSWORD", ["10:00 am", "12:00 pm", "2:00 pm", "4:00 pm"]),
    ("账号2", "NLB_USERNAME_2", "NLB_PASSWORD_2", ["11:00 am", "1:00 pm", "3:00 pm", "5:00 pm"]),
]


def load_config(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise FileNotFoundError(f"本机配置文件不存在：{path}")

    config: dict[str, str] = {}
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"配置文件第 {line_number} 行格式错误")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        config[key] = value

    required = [
        "NLB_USERNAME",
        "NLB_PASSWORD",
        "NLB_USERNAME_2",
        "NLB_PASSWORD_2",
        "FEISHU_WEBHOOK_URL",
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
    ]
    missing = [key for key in required if not config.get(key)]
    if missing:
        raise ValueError(f"本机配置缺少变量：{', '.join(missing)}")
    return config


def acquire_run_lock(path: Path = RUN_LOCK_PATH):
    """Acquire the cross-process lock shared by scheduled and menu-triggered runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_file = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_file.close()
        return None
    lock_file.seek(0)
    lock_file.truncate()
    lock_file.write(f"pid={os.getpid()} started={datetime.now(SGT).isoformat()}\n")
    lock_file.flush()
    return lock_file


def release_run_lock(lock_file) -> None:
    if lock_file is None:
        return
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def slot_label(start_time: str) -> str:
    start = datetime.strptime(start_time, "%I:%M %p")
    end_hour = (start.hour + 1) % 24
    return f"{start.hour:02d}:{start.minute:02d}–{end_hour:02d}:{start.minute:02d}"


def ensure_complete_report(
    report: dict,
    account_label: str,
    start_times: list[str],
) -> dict:
    """Fill planned rows when an account fails before producing slot results."""
    existing_labels = {row.get("label") for row in report.get("results", [])}
    fatal_error = report.get("fatal_error") or "未生成预约结果"
    for start_time in start_times:
        label = slot_label(start_time)
        if label in existing_labels:
            continue
        report.setdefault("results", []).append(
            {
                "account": account_label,
                "label": label,
                "start_time": start_time,
                "date": report.get("date", ""),
                "seat": "",
                "status": f"❌ 失败: {fatal_error}",
            }
        )
    return report


def concrete_seat(value: str) -> str:
    """Accept only a final S-number; placeholders can never reach Feishu."""
    match = re.fullmatch(r"S\s*-?\s*(\d{1,3})", (value or "").strip(), re.IGNORECASE)
    return f"S{match.group(1)}" if match else ""


def verification_failed(reports: list[dict]) -> bool:
    rows = [row for report in reports for row in report.get("results", [])]
    expected_count = sum(len(start_times) for _, _, _, start_times in ACCOUNT_SCHEDULES)
    return (
        len(rows) != expected_count
        or any(row.get("status", "").startswith("❌") for row in rows)
        or any(not concrete_seat(row.get("seat", "")) for row in rows)
    )


def compact_status(row: dict, seat: str) -> str:
    """Return the short, mobile-friendly status shown in push tables."""
    if seat:
        return "✅ 成功"
    return "❌ 未核实"


def feishu_table_row(
    time_label: str,
    seat: str,
    status: str,
    *,
    header: bool = False,
    shaded: bool = False,
) -> dict:
    """Build one stable three-column Feishu row for both desktop and mobile."""
    weight = "**" if header else ""
    return {
        "tag": "column_set",
        "flex_mode": "none",
        **({"background_style": "grey"} if header or shaded else {}),
        "columns": [
            {
                "tag": "column",
                "width": "weighted",
                "weight": 5,
                "horizontal_align": "left",
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"{weight}{time_label}{weight}",
                        },
                    }
                ],
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": 2,
                "horizontal_align": "left",
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"{weight}{seat}{weight}",
                        },
                    }
                ],
            },
            {
                "tag": "column",
                "width": "weighted",
                "weight": 3,
                "horizontal_align": "left",
                "elements": [
                    {
                        "tag": "div",
                        "text": {
                            "tag": "lark_md",
                            "content": f"{weight}{status}{weight}",
                        },
                    }
                ],
            },
        ],
    }


def send_combined_notification(webhook_url: str, reports: list[dict]) -> bool:
    rows = [row for report in reports for row in report.get("results", [])]
    rows.sort(key=lambda row: datetime.strptime(row["start_time"], "%I:%M %p"))
    has_failure = verification_failed(reports)
    success_count = sum(
        1 for row in rows if concrete_seat(row.get("seat", ""))
    )
    target_date = next((report.get("date") for report in reports if report.get("date")), "未知")

    table_rows = []
    for index, row in enumerate(rows):
        seat = concrete_seat(row.get("seat", ""))
        table_rows.append(
            feishu_table_row(
                str(row.get("label", "未知时段")),
                seat or "—",
                compact_status(row, seat),
                shaded=index % 2 == 1,
            )
        )

    if has_failure and success_count:
        title = "⚠️ NLB 座位预约部分失败"
    elif has_failure:
        title = "❌ NLB 座位预约失败"
    else:
        title = "🪑 NLB 座位预约成功"

    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": "red" if has_failure else "green",
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": (
                            f"📅 **{target_date}**　·　"
                            "📍 Punggol Library · L3 Study Zone\n"
                            "**🪑 最终座位**"
                        ),
                    },
                },
                {"tag": "hr"},
                feishu_table_row("时段", "座位", "状态", header=True),
                *table_rows,
            ],
        },
    }
    serialized_payload = json.dumps(payload, ensure_ascii=False)
    if "any available seat" in serialized_payload.lower():
        raise ValueError("飞书内容包含临时座位占位符，已阻止推送")
    try:
        request = urllib.request.Request(
            webhook_url,
            data=serialized_payload.encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            response_body = response.read().decode("utf-8", errors="replace")
        response_data = json.loads(response_body)
        response_code = response_data.get(
            "StatusCode", response_data.get("code")
        )
        if str(response_code) != "0":
            response_message = response_data.get(
                "StatusMessage", response_data.get("msg", "未知错误")
            )
            raise RuntimeError(
                f"飞书拒绝消息：code={response_code}, message={response_message}"
            )
        print("两账号合并飞书推送成功（飞书回执 code=0）")
        return True
    except Exception as exc:
        print(f"两账号合并飞书推送失败：{exc}", file=sys.stderr)
        return False


def send_telegram_notification(
    bot_token: str,
    chat_id: str,
    reports: list[dict],
) -> bool:
    """Push the same verified final seats to Telegram and validate ok=true."""
    rows = [row for report in reports for row in report.get("results", [])]
    rows.sort(key=lambda row: datetime.strptime(row["start_time"], "%I:%M %p"))
    has_failure = verification_failed(reports)
    success_count = sum(1 for row in rows if concrete_seat(row.get("seat", "")))
    target_date = next(
        (report.get("date") for report in reports if report.get("date")),
        "未知",
    )

    if has_failure and success_count:
        title = "⚠️ NLB 座位预约部分失败"
    elif has_failure:
        title = "❌ NLB 座位预约失败"
    else:
        title = "🪑 NLB 座位预约成功"

    table_lines = ["时段          座位  状态"]
    for row in rows:
        seat = concrete_seat(row.get("seat", ""))
        label = str(row.get("label", "未知时段"))
        table_lines.append(
            f"{label:<13} {seat or '—':<5} {compact_status(row, seat)}"
        )

    lines = [
        f"<b>{escape(title)}</b>",
        "",
        f"📅 <b>{escape(str(target_date))}</b> · 📍 Punggol Library · L3 Study Zone",
        "",
        "<b>🪑 最终座位</b>",
        f"<pre>{escape(chr(10).join(table_lines))}</pre>",
    ]

    message = "\n".join(lines)
    if "any available seat" in message.lower():
        print("Telegram 内容包含临时座位占位符，已阻止推送", file=sys.stderr)
        return False

    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
        "reply_markup": {
            "inline_keyboard": [
                [
                    {
                        "text": "🔄 再推一次任务结果",
                        "callback_data": "resend",
                    }
                ],
                [
                    {
                        "text": "🗓 重新预定",
                        "callback_data": "rebook",
                    }
                ]
            ]
        },
    }
    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        request = urllib.request.Request(
            api_url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10) as response:
            response_body = response.read().decode("utf-8", errors="replace")
        response_data = json.loads(response_body)
        if response_data.get("ok") is not True:
            description = response_data.get("description", "未知错误")
            raise RuntimeError(f"Telegram 拒绝消息：{description}")
        print("Telegram 推送成功（Telegram 回执 ok=true）")
        return True
    except urllib.error.HTTPError as exc:
        print(f"Telegram 推送失败：HTTP {exc.code}", file=sys.stderr)
        return False
    except Exception as exc:
        safe_error = str(exc).replace(bot_token, "<redacted-token>")
        print(
            f"Telegram 推送失败：{type(exc).__name__}: {safe_error}",
            file=sys.stderr,
        )
        return False


def send_all_notifications(config: dict[str, str], reports: list[dict]) -> bool:
    """Attempt both channels independently; success requires both receipts."""
    feishu_ok = send_combined_notification(config["FEISHU_WEBHOOK_URL"], reports)
    telegram_ok = send_telegram_notification(
        config["TELEGRAM_BOT_TOKEN"],
        config["TELEGRAM_CHAT_ID"],
        reports,
    )
    return feishu_ok and telegram_ok


def failed_verification_report(
    account_label: str,
    start_times: list[str],
    target_date: str,
    error: str,
) -> dict:
    return {
        "account": account_label,
        "date": target_date,
        "fatal_error": error,
        "verification_source": "My Bookings > Upcoming（重新登录读取）",
        "results": [
            {
                "account": account_label,
                "label": slot_label(start_time),
                "start_time": start_time,
                "date": target_date,
                "seat": "",
                "status": f"❌ 最终核验失败: {error}",
            }
            for start_time in start_times
        ],
    }


def verify_final_bookings(
    config: dict[str, str],
    accounts: list[tuple[str, str, str, list[str]]],
    run_root: Path,
) -> list[dict]:
    """Re-login to every account and read final seats without booking anything."""
    target_date = (
        datetime.now(SGT) + timedelta(days=int(config.get("BOOKING_DATE_OFFSET", "1")))
    ).strftime("%Y-%m-%d")
    reports: list[dict] = []
    print("飞书推送前开始只读核验：重新登录两账号查看 My Bookings → Upcoming")

    for index, (label, username, password, start_times) in enumerate(accounts, 1):
        results_file = run_root / f"account-{index}-final-results.json"
        verification_env = os.environ.copy()
        verification_env.update(
            {
                "NLB_USERNAME": username,
                "NLB_PASSWORD": password,
                "NLB_ACCOUNT_LABEL": label,
                "NLB_START_TIMES": "|".join(start_times),
                "NLB_RESULTS_FILE": str(results_file),
                "NLB_DEFER_FEISHU": "1",
                "FEISHU_WEBHOOK_URL": "",
                "BOOKING_DATE_OFFSET": config.get("BOOKING_DATE_OFFSET", "1"),
                "SCREENSHOTS_DIR": str(run_root / f"account-{index}-final-check"),
                "TZ": "Asia/Singapore",
                "PYTHONUNBUFFERED": "1",
            }
        )

        last_return_code = 1
        for attempt in range(1, 3):
            if results_file.exists():
                results_file.unlink()
            print(f"开始核验 {label} 最终座位（第 {attempt}/2 次）")
            result = subprocess.run(
                [sys.executable, str(REPO_ROOT / "book_seat.py"), "--verify-only"],
                cwd=REPO_ROOT,
                env=verification_env,
                check=False,
            )
            last_return_code = result.returncode
            if result.returncode == 0 and results_file.is_file():
                break
            if attempt == 1:
                print(f"{label} 最终核验未完整读取，5 秒后重试")
                time.sleep(5)

        if results_file.is_file():
            report = json.loads(results_file.read_text(encoding="utf-8"))
            report = ensure_complete_report(report, label, start_times)
        else:
            report = failed_verification_report(
                label,
                start_times,
                target_date,
                f"核验子进程退出码 {last_return_code}，未生成结果文件",
            )
        reports.append(report)
    return reports


def resend_only(config: dict[str, str]) -> int:
    """Read final bookings and push Feishu without executing any booking action."""
    accounts = [
        (label, config[username_key], config[password_key], start_times)
        for label, username_key, password_key, start_times in ACCOUNT_SCHEDULES
    ]
    run_stamp = datetime.now(SGT).strftime("%Y%m%d-%H%M%S-resend")
    run_root = REPO_ROOT / "local-runs" / run_stamp
    reports = verify_final_bookings(config, accounts, run_root)
    notification_ok = send_all_notifications(config, reports)
    return 1 if verification_failed(reports) or not notification_ok else 0


def run_from_cli(config: dict[str, str]) -> int:
    """Execute the requested mode after configuration and locking are complete."""
    if sys.argv[1:] == ["--resend"]:
        print("执行飞书补推：仅查询最终预约结果，不运行订座流程")
        return resend_only(config)

    accounts = [
        (label, config[username_key], config[password_key], start_times)
        for label, username_key, password_key, start_times in ACCOUNT_SCHEDULES
    ]

    run_stamp = datetime.now(SGT).strftime("%Y%m%d-%H%M%S")
    run_root = REPO_ROOT / "local-runs" / run_stamp

    print(f"[{datetime.now(SGT):%Y-%m-%d %H:%M:%S} SGT] 开始执行两组账号预约")
    for index, (label, username, password, start_times) in enumerate(accounts, 1):
        results_file = run_root / f"account-{index}-results.json"
        account_env = os.environ.copy()
        account_env.update(
            {
                "NLB_USERNAME": username,
                "NLB_PASSWORD": password,
                "NLB_ACCOUNT_LABEL": label,
                "NLB_START_TIMES": "|".join(start_times),
                "NLB_RESULTS_FILE": str(results_file),
                "NLB_DEFER_FEISHU": "1",
                "FEISHU_WEBHOOK_URL": "",
                "BOOKING_DATE_OFFSET": config.get("BOOKING_DATE_OFFSET", "1"),
                "SCREENSHOTS_DIR": str(run_root / f"account-{index}"),
                "TZ": "Asia/Singapore",
                "PYTHONUNBUFFERED": "1",
            }
        )
        print(f"开始执行 {label}（不会在日志中输出用户名或密码）")
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "book_seat.py")],
            cwd=REPO_ROOT,
            env=account_env,
            check=False,
        )
        print(f"{label} 执行结束，退出码：{result.returncode}")

    final_reports = verify_final_bookings(config, accounts, run_root)
    notification_ok = send_all_notifications(config, final_reports)
    return 1 if verification_failed(final_reports) or not notification_ok else 0


def main() -> int:
    if sys.argv[1:] not in ([], ["--resend"]):
        print(f"不支持的参数：{' '.join(sys.argv[1:])}", file=sys.stderr)
        return 2

    config_path = Path(os.environ.get("NLB_LOCAL_CONFIG", DEFAULT_CONFIG)).expanduser()
    config = load_config(config_path)
    lock_file = acquire_run_lock()
    if lock_file is None:
        print("已有 NLB 订座或核验任务正在运行", file=sys.stderr)
        return 75
    try:
        return run_from_cli(config)
    finally:
        release_run_lock(lock_file)


if __name__ == "__main__":
    raise SystemExit(main())
