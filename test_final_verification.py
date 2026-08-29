import json
import os
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


os.environ.setdefault("NLB_USERNAME", "test-user")
os.environ.setdefault("NLB_PASSWORD", "test-password")

import book_seat
import run_local


UPCOMING_TEXT = """My Bookings
Today
Upcoming
Cancelled
Monday, 24 Aug 2026
Study Zone, Level 3
11:00 am - 12:00 pm
S74
Check-in no later than 14 minutes from your booking start time.
Study Zone, Level 3
1:00 pm - 2:00 pm
S86
Check-in no later than 14 minutes from your booking start time.
Study Zone, Level 3
3:00 pm - 4:00 pm
Any available seat
Check-in no later than 14 minutes from your booking start time.
Tuesday, 25 Aug 2026
Study Zone, Level 3
11:00 am - 12:00 pm
S17
"""


class FakeResponse:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self):
        return b'{"StatusCode":0,"StatusMessage":"success"}'


class FakeTelegramResponse(FakeResponse):
    def read(self):
        return b'{"ok":true,"result":{"message_id":123}}'


class FinalVerificationTests(unittest.TestCase):
    @staticmethod
    def complete_reports():
        reports = []
        seat_number = 1
        for label, _, _, start_times in run_local.ACCOUNT_SCHEDULES:
            rows = []
            for start_time in start_times:
                rows.append(
                    {
                        "account": label,
                        "label": run_local.slot_label(start_time),
                        "start_time": start_time,
                        "date": "2026-08-24",
                        "seat": f"S{seat_number}",
                        "status": "✅ 已重新登录核实",
                    }
                )
                seat_number += 1
            reports.append({"date": "2026-08-24", "results": rows})
        return reports

    def test_extracts_seat_from_matching_date_and_time(self):
        found, seat = book_seat.extract_final_booking(
            UPCOMING_TEXT, datetime(2026, 8, 24), "11:00 am"
        )
        self.assertTrue(found)
        self.assertEqual(seat, "S74")

    def test_does_not_use_seat_from_another_date(self):
        found, seat = book_seat.extract_final_booking(
            UPCOMING_TEXT, datetime(2026, 8, 24), "5:00 pm"
        )
        self.assertFalse(found)
        self.assertEqual(seat, "")

    def test_any_available_is_never_a_final_seat(self):
        found, seat = book_seat.extract_final_booking(
            UPCOMING_TEXT, datetime(2026, 8, 24), "3:00 pm"
        )
        self.assertTrue(found)
        self.assertEqual(seat, "")

    def test_feishu_payload_contains_only_concrete_seats(self):
        rows = []
        for label, _, _, start_times in run_local.ACCOUNT_SCHEDULES:
            for index, start_time in enumerate(start_times, 1):
                rows.append(
                    {
                        "account": label,
                        "label": run_local.slot_label(start_time),
                        "start_time": start_time,
                        "date": "2026-08-24",
                        "seat": "Any available seat" if index == 4 else f"S{index}",
                        "status": (
                            "✅ Any available seat"
                            if index == 4
                            else "✅ 已重新登录核实"
                        ),
                    }
                )
        reports = [
            {"date": "2026-08-24", "results": rows[:4]},
            {"date": "2026-08-24", "results": rows[4:]},
        ]
        sent = []

        def fake_urlopen(request, timeout):
            sent.append(json.loads(request.data.decode("utf-8")))
            return FakeResponse()

        with patch.object(run_local.urllib.request, "urlopen", fake_urlopen):
            self.assertTrue(
                run_local.send_combined_notification(
                    "https://open.feishu.cn/open-apis/bot/v2/hook/test", reports
                )
            )

        payload_text = json.dumps(sent[0], ensure_ascii=False)
        self.assertNotIn("any available seat", payload_text.lower())
        self.assertNotIn("账号1", payload_text)
        self.assertNotIn("账号2", payload_text)
        self.assertNotIn("已确认", payload_text)
        self.assertIn("❌ 未核实", payload_text)
        self.assertIn("✅ 成功", payload_text)
        self.assertIn("S1", payload_text)
        table_rows = [
            element
            for element in sent[0]["card"]["elements"]
            if element.get("tag") == "column_set"
        ]
        self.assertEqual(len(table_rows), 9)
        self.assertEqual(len(table_rows[0]["columns"]), 3)
        self.assertTrue(
            all(
                column.get("horizontal_align") == "left"
                for table_row in table_rows
                for column in table_row["columns"]
            )
        )
        self.assertEqual(table_rows[0].get("background_style"), "grey")
        self.assertNotIn("background_style", table_rows[1])
        self.assertEqual(table_rows[2].get("background_style"), "grey")
        self.assertNotIn("background_style", table_rows[3])

    def test_resend_only_verifies_then_pushes_without_booking_flow(self):
        reports = self.complete_reports()
        config = {
            "NLB_USERNAME": "one",
            "NLB_PASSWORD": "secret-one",
            "NLB_USERNAME_2": "two",
            "NLB_PASSWORD_2": "secret-two",
            "FEISHU_WEBHOOK_URL": "https://example.invalid/hook",
            "TELEGRAM_BOT_TOKEN": "123456:test-token",
            "TELEGRAM_CHAT_ID": "123456789",
            "BOOKING_DATE_OFFSET": "1",
        }
        with patch.object(
            run_local, "verify_final_bookings", return_value=reports
        ) as verify_mock, patch.object(
            run_local, "send_all_notifications", return_value=True
        ) as send_mock:
            result = run_local.resend_only(config)

        self.assertEqual(result, 0)
        verify_mock.assert_called_once()
        send_mock.assert_called_once_with(config, reports)

    def test_telegram_payload_uses_verified_seats(self):
        reports = self.complete_reports()
        sent = []

        def fake_urlopen(request, timeout):
            sent.append(json.loads(request.data.decode("utf-8")))
            return FakeTelegramResponse()

        with patch.object(run_local.urllib.request, "urlopen", fake_urlopen):
            self.assertTrue(
                run_local.send_telegram_notification(
                    "123456:test-token",
                    "123456789",
                    reports,
                )
            )

        message = sent[0]["text"]
        self.assertIn("S1", message)
        self.assertIn("S8", message)
        self.assertNotIn("any available seat", message.lower())
        self.assertNotIn("账号1", message)
        self.assertNotIn("账号2", message)
        self.assertNotIn("已确认", message)
        self.assertIn("时段", message)
        self.assertIn("座位", message)
        self.assertIn("状态", message)
        self.assertIn("✅ 成功", message)
        self.assertIn("<pre>", message)
        self.assertEqual(sent[0]["parse_mode"], "HTML")
        buttons = sent[0]["reply_markup"]["inline_keyboard"]
        self.assertEqual(buttons[0][0]["text"], "🔄 再推一次任务结果")
        self.assertEqual(buttons[0][0]["callback_data"], "resend")
        self.assertEqual(buttons[1][0]["text"], "🗓 重新预定")
        self.assertEqual(buttons[1][0]["callback_data"], "rebook")

    def test_both_channels_are_attempted_independently(self):
        reports = self.complete_reports()
        config = {
            "FEISHU_WEBHOOK_URL": "https://example.invalid/hook",
            "TELEGRAM_BOT_TOKEN": "123456:test-token",
            "TELEGRAM_CHAT_ID": "123456789",
        }
        with patch.object(
            run_local, "send_combined_notification", return_value=False
        ) as feishu_mock, patch.object(
            run_local, "send_telegram_notification", return_value=True
        ) as telegram_mock:
            result = run_local.send_all_notifications(config, reports)

        self.assertFalse(result)
        feishu_mock.assert_called_once()
        telegram_mock.assert_called_once()

    def test_cross_process_lock_rejects_second_run(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            lock_path = Path(temp_dir) / "nlb.lock"
            first_lock = run_local.acquire_run_lock(lock_path)
            try:
                self.assertIsNotNone(first_lock)
                self.assertIsNone(run_local.acquire_run_lock(lock_path))
            finally:
                run_local.release_run_lock(first_lock)

            second_lock = run_local.acquire_run_lock(lock_path)
            try:
                self.assertIsNotNone(second_lock)
            finally:
                run_local.release_run_lock(second_lock)


if __name__ == "__main__":
    unittest.main()
