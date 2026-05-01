"""
NLB Seat Booking Automation Script
====================================
基于真实 UI 截图重写，完整匹配实际预约流程：

  主页 → 选 Library / Area / Date / Time / Duration
       → CHECK AVAILABLE SLOTS
       → Booking Details 页 → BOOK
       → Preferred Seat 弹窗（radio 列表，只显示可用座位）
       → 选最大编号（S86 优先，降级到 S74）→ CONFIRM

目标：Punggol Library > Study Zone, Level 3
时段：10:00 am 起 1h30min / 2:00 pm 起 1h30min
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# ── 配置（从 GitHub Secrets 注入） ─────────────────────────────────────────
NLB_USERNAME = os.environ["NLB_USERNAME"]
NLB_PASSWORD = os.environ["NLB_PASSWORD"]

LIBRARY  = "Punggol Library"
AREA     = "Study Zone, Level 3"
DURATION = "1:30"           # 1 小时 30 分钟
TIME_SLOTS = [
    "10:00 am",
    "2:00 pm",
]

# 座位优先顺序：S86 最优，依次降到 S74
SEAT_PRIORITY = [f"S{n}" for n in range(86, 73, -1)]

# 预约哪天（1 = 明天）
DATE_OFFSET = int(os.environ.get("BOOKING_DATE_OFFSET", "1"))

BASE_URL = "https://www.nlb.gov.sg/seatbooking"
SGT      = ZoneInfo("Asia/Singapore")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
class NLBBooker:

    def __init__(self, page):
        self.page = page

    async def snap(self, name: str):
        os.makedirs("screenshots", exist_ok=True)
        path = f"screenshots/{name}.png"
        await self.page.screenshot(path=path, full_page=True)
        log.info(f"  📸 {path}")

    # ══════════════════════════════════════════════════════════════════════
    # Step 1: 登录（跟随 SSO 重定向到 signin.nlb.gov.sg）
    # ══════════════════════════════════════════════════════════════════════
    async def login(self):
        log.info("▶ 登录（跟随 SSO 重定向）...")

        # 访问主页，让它自动重定向到 signin.nlb.gov.sg SSO 登录页
        await self.page.goto(BASE_URL + "/", wait_until="networkidle")
        await self.snap("01_initial_page")
        log.info(f"  初始 URL: {self.page.url}")

        # 如果已经在 seatbooking 主页（之前有缓存 session），说明已登录
        if "signin.nlb.gov.sg" not in self.page.url and "seatbooking" in self.page.url:
            log.info("✅ 已有登录状态，跳过登录步骤")
            return

        # 若没有重定向到登录页，手动点登录按钮（右上角图标）
        if "signin.nlb.gov.sg" not in self.page.url:
            login_icon = self.page.locator('[class*="login"], [aria-label*="login" i], [aria-label*="account" i], button:has-text("Login")')
            if await login_icon.count() > 0:
                await login_icon.first.click()
                await self.page.wait_for_load_state("networkidle")
                await self.snap("01b_after_login_click")

        log.info(f"  SSO 页 URL: {self.page.url}")

        # ── 填写 NLB SSO 登录表单 ──────────────────────────────────────
        # CAS 表单通常是 input[name="username"] / input[name="password"]
        await self.snap("02_sso_login_page")

        # 用户名字段（尝试多种 selector）
        user_field = self.page.locator(
            'input[name="username"], input[id="username"], '
            'input[placeholder*="ID" i], input[placeholder*="NRIC" i], '
            'input[type="text"], input[type="email"]'
        ).first
        await user_field.wait_for(state="visible", timeout=10_000)
        await user_field.fill(NLB_USERNAME)

        # 密码字段
        await self.page.locator('input[type="password"]').first.fill(NLB_PASSWORD)
        await self.snap("03_credentials_filled")

        # 提交按钮
        await self.page.locator(
            'input[type="submit"], button[type="submit"], '
            'button:has-text("Login"), button:has-text("Log In"), '
            'button:has-text("Sign In")'
        ).first.click()

        # 等待跳回 seatbooking（最多 20 秒）
        try:
            await self.page.wait_for_url("**/seatbooking/**", timeout=20_000)
        except PWTimeout:
            await self.snap("03b_login_timeout")
            raise RuntimeError(
                f"登录超时，仍停留在: {self.page.url}\n"
                "请检查截图 03b_login_timeout.png 确认错误原因（密码错误 / 验证码 / 账号锁定）"
            )

        await self.page.wait_for_load_state("networkidle")
        await self.snap("04_after_login")
        log.info(f"✅ 登录成功，当前: {self.page.url}")

    # ══════════════════════════════════════════════════════════════════════
    # Step 2: 主页表单 Library / Area / Date / Time / Duration
    # ══════════════════════════════════════════════════════════════════════
    async def fill_booking_form(self, time_slot: str, booking_date: datetime):
        log.info(f"▶ 填写表单 — {time_slot}，{booking_date.strftime('%d %b %Y')}")
        await self.page.goto(BASE_URL + "/", wait_until="networkidle")
        await asyncio.sleep(1.5)
        await self.snap("05_main_form")

        await self._pick_field("Library", LIBRARY)
        await self._pick_field("Area",    AREA)
        await self._set_date(booking_date)
        await self._set_picker("Time",     time_slot)
        await self._set_picker("Duration", DURATION)
        await self.snap("09_form_complete")

        log.info("▶ 点击 CHECK AVAILABLE SLOTS...")
        await self.page.locator('button:has-text("CHECK AVAILABLE SLOTS")').click()
        await self.page.wait_for_url("**/bookingdetails**", timeout=15_000)
        await self.page.wait_for_load_state("networkidle")
        await self.snap("10_booking_details")

    # ── Library / Area 字段（点击后弹出选项列表） ────────────────────────
    async def _pick_field(self, label: str, value: str):
        log.info(f"  选 {label} = {value}")

        # 点击字段行
        row = self.page.locator(f'text="{label}"').locator('..')
        await row.click()
        await asyncio.sleep(1.2)
        await self.snap(f"05_picker_{label.lower()}_open")

        # ── 打印当前所有可见文字元素（调试用） ──────────────────────
        visible_texts = await self.page.evaluate("""() => {
            const tags = ['li','div','span','p','label','option',
                          '[class*="item"]','[class*="option"]','[class*="list"]'];
            const seen = new Set();
            const results = [];
            document.querySelectorAll(tags.join(',')).forEach(el => {
                const txt = el.innerText?.trim();
                if (txt && txt.length < 80 && !seen.has(txt)) {
                    seen.add(txt);
                    results.push({tag: el.tagName, cls: el.className.slice(0,60), txt});
                }
            });
            return results.slice(0, 60);
        }""")
        log.info(f"  ── 点开后可见元素（供调试）──")
        for el in visible_texts:
            log.info(f"    <{el['tag']}> cls={el['cls']!r}  →  {el['txt']!r}")
        log.info(f"  ── END ──")

        # ── 策略 1: role=option ───────────────────────────────────────
        opt1 = self.page.get_by_role("option", name=value)
        if await opt1.count() > 0:
            await opt1.first.click()
            log.info(f"  ✔ {label} 已选（role=option）")
            await asyncio.sleep(0.5)
            return

        # ── 策略 2: Vuetify v-list-item（常见结构） ──────────────────
        vuetify_selectors = [
            f'.v-list-item:has-text("{value}")',
            f'.v-list-item__title:has-text("{value}")',
            f'[class*="list-item"]:has-text("{value}")',
            f'[class*="v-list"]:has-text("{value}")',
        ]
        for sel in vuetify_selectors:
            els = self.page.locator(sel)
            if await els.count() > 0:
                await els.first.click()
                log.info(f"  ✔ {label} 已选（{sel}）")
                await asyncio.sleep(0.5)
                return

        # ── 策略 3: 精确文字匹配所有可见元素 ─────────────────────────
        for tag in ["li", "div", "span", "p", "label"]:
            els = self.page.locator(tag)
            cnt = await els.count()
            for i in range(cnt):
                try:
                    txt = (await els.nth(i).inner_text()).strip()
                    if txt == value:
                        visible = await els.nth(i).is_visible()
                        if visible:
                            await els.nth(i).click()
                            log.info(f"  ✔ {label} 已选（<{tag}> 文字精确匹配）")
                            await asyncio.sleep(0.5)
                            return
                except Exception:
                    continue

        await self.snap(f"ERR_picker_{label.lower()}_no_option")
        raise RuntimeError(
            f"找不到 {label} 的选项 {value!r}。\n"
            f"请查看截图 05_picker_{label.lower()}_open.png 和日志中的可见元素列表。"
        )

    # ── Date 字段 ─────────────────────────────────────────────────────────
    async def _set_date(self, booking_date: datetime):
        log.info(f"  设置 Date = {booking_date.strftime('%d %b %Y')}")
        row = self.page.locator('text="Date"').locator('..')
        await row.click()
        await asyncio.sleep(0.5)
        await self.snap("06_date_picker_open")

        # 尝试 input[type=date]
        date_input = self.page.locator('input[type="date"]')
        if await date_input.count() > 0:
            await date_input.first.fill(booking_date.strftime("%Y-%m-%d"))
            await date_input.first.press("Enter")
            return

        # 日历格子：找数字等于目标日的 td/div
        day = str(booking_date.day)
        cells = self.page.locator('td, [class*="day"]:not([class*="month"]):not([class*="year"])')
        cnt = await cells.count()
        for i in range(cnt):
            if (await cells.nth(i).inner_text()).strip() == day:
                await cells.nth(i).click()
                return
        raise RuntimeError(f"日历中找不到日期 {day}")

    # ── Time / Duration 字段（scroll-picker 或下拉） ──────────────────────
    async def _set_picker(self, label: str, value: str):
        log.info(f"  设置 {label} = {value}")
        row = self.page.locator(f'text="{label}"').locator('..')
        await row.click()
        await asyncio.sleep(0.5)

        # 先尝试 role=option
        option = self.page.get_by_role("option", name=value)
        if await option.count() > 0:
            await option.first.click()
            log.info(f"  ✔ {label} 已选（option）")
            return

        # 再尝试 li / item 文字精确匹配
        items = self.page.locator('li, [class*="option"], [class*="item"], [class*="col"] div')
        cnt = await items.count()
        for i in range(cnt):
            txt = (await items.nth(i).inner_text()).strip()
            if txt == value:
                await items.nth(i).click()
                log.info(f"  ✔ {label} 已选（text-match）")
                return

        raise RuntimeError(f"找不到 {label} 的选项: {value!r}")

    # ══════════════════════════════════════════════════════════════════════
    # Step 3: Booking Details → BOOK → Preferred Seat 弹窗 → CONFIRM
    # ══════════════════════════════════════════════════════════════════════
    async def do_book(self, time_slot: str) -> str:
        tag = time_slot.replace(":", "").replace(" ", "")

        # 检查未登录情况
        if await self.page.locator('button:has-text("LOGIN TO BOOK")').count() > 0:
            raise RuntimeError("页面显示 LOGIN TO BOOK，说明会话已过期，请检查登录流程。")

        log.info("▶ 点击 BOOK...")
        await self.page.locator('button:has-text("BOOK")').first.click()

        # 等待 Preferred Seat 弹窗
        await self.page.locator('text="Preferred Seat"').wait_for(state="visible", timeout=10_000)
        await self.snap(f"11_preferred_seat_{tag}")

        # 收集弹窗内所有 radio label 文字（即可用座位列表）
        # Vuetify dialog 结构：.v-dialog 内有 .v-radio-group 的 label
        dialog = self.page.locator('.v-dialog:visible, [role="dialog"]:visible').last
        radio_labels = dialog.locator('label, .v-label, [class*="label"]')
        cnt = await radio_labels.count()
        available = []
        for i in range(cnt):
            txt = (await radio_labels.nth(i).inner_text()).strip()
            if txt:
                available.append(txt)
        log.info(f"  弹窗可用座位: {available}")

        # 按优先级选座
        chosen_seat = None
        for seat in SEAT_PRIORITY:
            if seat in available:
                chosen_seat = seat
                break

        if chosen_seat:
            log.info(f"  🎯 选座: {chosen_seat}")
            await dialog.locator(f'label:has-text("{chosen_seat}")').first.click()
        else:
            log.warning("  ⚠ S74~S86 均不可用，保持 No preferred seat（系统随机分配）")
            chosen_seat = "auto"
            await dialog.locator('label:has-text("No preferred seat")').first.click()

        await self.snap(f"12_seat_{chosen_seat}_{tag}")

        # 点 CONFIRM
        log.info("  点击 CONFIRM...")
        await dialog.locator('button:has-text("CONFIRM")').first.click()
        await self.page.wait_for_load_state("networkidle")
        await asyncio.sleep(1.5)
        await self.snap(f"13_done_{tag}")

        # 验证成功
        success_texts = ["Booking confirmed", "successfully", "Booking ID", "booking ref"]
        for t in success_texts:
            if await self.page.locator(f'text="{t}"').count() > 0:
                log.info(f"  ✅ 预约成功！座位: {chosen_seat}")
                return chosen_seat

        log.warning("  ⚠ 未检测到成功提示，请查看截图 13_done_*.png 确认。")
        return chosen_seat


# ═══════════════════════════════════════════════════════════════════════════
async def main():
    now          = datetime.now(SGT)
    booking_date = now + timedelta(days=DATE_OFFSET)

    log.info(f"{'='*60}")
    log.info(f"NLB 自动预约 | 当前时间(SGT): {now.strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"预约日期: {booking_date.strftime('%A, %d %b %Y')}")
    log.info(f"目标: {LIBRARY} | {AREA}")
    log.info(f"时段: {TIME_SLOTS}  时长: {DURATION}")
    log.info(f"座位优先级: S86 → S85 → … → S74")
    log.info(f"{'='*60}")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            locale="en-GB",
            timezone_id="Asia/Singapore",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page    = await ctx.new_page()
        booker  = NLBBooker(page)
        results = []

        try:
            await booker.login()

            for slot in TIME_SLOTS:
                log.info(f"\n── 时段: {slot} ──────────────────────────")
                try:
                    await booker.fill_booking_form(slot, booking_date)
                    seat = await booker.do_book(slot)
                    results.append((slot, f"✅ 成功，座位: {seat}"))
                except Exception as e:
                    log.error(f"❌ {slot} 失败: {e}")
                    await booker.snap(f"ERR_{slot.replace(':', '').replace(' ', '')}")
                    results.append((slot, f"❌ 失败: {e}"))
                await asyncio.sleep(2)

        finally:
            await browser.close()

        log.info(f"\n{'='*60}")
        log.info("📋 预约结果汇总:")
        for slot, status in results:
            log.info(f"  {slot}  →  {status}")
        log.info(f"{'='*60}")

        if any("❌" in s for _, s in results):
            raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
