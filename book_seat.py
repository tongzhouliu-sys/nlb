"""
NLB Seat Booking Automation Script  (v3 — 基于真实 DOM 日志修复)
=================================================================
修复内容（根据 GitHub Actions 运行日志）：
  - Time / Duration 和 Library / Area 一样，都是 inputPopupSelectDiv 弹窗
  - Duration 字段标签全称是 "Duration (Hour : Min)"，不能用 "Duration" 查找
  - CHECK AVAILABLE SLOTS 是 <div>，不是 <button>
  - 弹窗选项统一是 <div class="my-2">文字</div>
  - 所有弹窗字段用同一套 _open_popup() + _pick_option() 方法
"""

import os
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── 配置区 ────────────────────────────────────────────────────────────────
NLB_USERNAME = os.environ["NLB_USERNAME"]
NLB_PASSWORD = os.environ["NLB_PASSWORD"]

TARGET_LIBRARY = "Punggol Library"
TARGET_AREA    = "Study Zone, Level 3"

SEAT_CANDIDATES = [f"S{n}" for n in range(86, 73, -1)]   # S86 → S74

BOOKING_DATE_OFFSET = int(os.environ.get("BOOKING_DATE_OFFSET", "1"))

# (显示标签, Time弹窗选项文字, Duration弹窗选项文字)
TIME_SLOTS = [
    ("10:00–11:30", "10:00 am", "1:30"),
    ("14:00–15:30", "2:00 pm",  "1:30"),
]

BASE_URL  = "https://www.nlb.gov.sg/seatbooking"
LOGIN_URL = f"{BASE_URL}/common/login"
SGT       = ZoneInfo("Asia/Singapore")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ── 工具 ──────────────────────────────────────────────────────────────────
def _safe(s: str) -> str:
    return s.replace(":", "").replace("–", "_").replace(" ", "")


# ═════════════════════════════════════════════════════════════════════════
class NLBBooker:

    def __init__(self, page):
        self.page = page

    # ── 截图 ──────────────────────────────────────────────────────────────
    async def snap(self, name: str):
        path = f"screenshots/{name}.png"
        os.makedirs("screenshots", exist_ok=True)
        await self.page.screenshot(path=path, full_page=True)
        log.info(f"📸 {path}")

    # ── 登录 ──────────────────────────────────────────────────────────────
    async def login(self):
        """
        正确登录流程：
          1. 打开座位预约主页
          2. 点击底部 "Account" Tab → 跳转到 CAS 登录页
          3. 填写账号密码并提交
          4. 等待跳回 nlb.gov.sg，确认已登录
        """
        log.info("▶ 打开主页...")
        await self.page.goto(BASE_URL, wait_until="load")
        await asyncio.sleep(2)   # SPA 初始化缓冲
        await self.snap("01_home")

        # 步骤 1：点击底部导航 "Account" Tab
        log.info("▶ 点击 Account Tab...")
        account_tab = self.page.locator(
            '.v-btn__content:has-text("Account"), '
            'span:has-text("Account"), '
            'button:has-text("Account")'
        ).first
        await account_tab.wait_for(state="visible", timeout=15_000)
        await account_tab.click()
        try:
            await self.page.wait_for_load_state("load", timeout=20_000)
        except PlaywrightTimeout:
            pass
        await asyncio.sleep(1)
        await self.snap("02_account_tab")
        log.info(f"  Account 页 URL: {self.page.url}")

        # 步骤 2：如果跳转到 CAS 登录页，填写账密
        if "signin.nlb.gov.sg" in self.page.url or "login" in self.page.url.lower():
            log.info("▶ 检测到登录页，填写账号密码...")
            await self.page.wait_for_selector(
                'input[name="username"], input[name="userId"], input[type="text"]',
                timeout=20_000
            )
            await self.page.fill(
                'input[name="username"], input[name="userId"], '
                'input[placeholder*="ID" i], input[type="text"]',
                NLB_USERNAME
            )
            await self.page.fill(
                'input[name="password"], input[type="password"]',
                NLB_PASSWORD
            )
            await self.snap("03_creds_filled")

            submit = self.page.locator(
                'input[type="submit"], button[type="submit"], '
                'button:has-text("Login"), button:has-text("Log in"), '
                'button:has-text("Sign in")'
            ).first
            await submit.wait_for(state="visible", timeout=10_000)
            await submit.click()

            # 等待 OAuth 多级跳转完成：URL 离开 signin.nlb.gov.sg 即可
            # 不用 networkidle —— SPA 页面永远有后台 XHR，networkidle 会超时
            try:
                await self.page.wait_for_function(
                    "() => !window.location.hostname.includes('signin.nlb.gov.sg')",
                    timeout=60_000,
                )
            except PlaywrightTimeout:
                log.warning("  ⚠ 60s 内 URL 未离开 signin 页，继续尝试...")

            # 再等页面基本加载完（用 load，不用 networkidle）
            try:
                await self.page.wait_for_load_state("load", timeout=30_000)
            except PlaywrightTimeout:
                pass
            await asyncio.sleep(2)  # SPA 路由渲染缓冲
            await self.snap("04_after_submit")
            log.info(f"  提交后 URL: {self.page.url}")

        # 步骤 3：确认已登录（不再停在 signin 页）
        if "signin.nlb.gov.sg" in self.page.url:
            raise RuntimeError(
                f"登录失败！请检查账号密码和截图 03_creds_filled.png。URL={self.page.url}"
            )

        # 步骤 4：确认 Account 页显示已登录状态（有用户名 / logout 按钮等）
        await self.snap("05_logged_in_account")
        log.info("✅ 登录成功")

    # ── 打开 New Booking 页 ───────────────────────────────────────────────
    async def navigate_to_new_booking(self):
        log.info("▶ 打开新建预约页...")
        await self.page.goto(BASE_URL, wait_until="load")
        await asyncio.sleep(2)
        # 点底部导航 "New" Tab
        try:
            await self.page.locator('.v-btn__content:has-text("New")').first.click()
            try:
                await self.page.wait_for_load_state("load", timeout=20_000)
            except PlaywrightTimeout:
                pass
        except PlaywrightTimeout:
            pass
        # 等 inputPopupSelectDiv 表单主体出现
        await self.page.wait_for_selector('.inputPopupSelectDiv', timeout=15_000)
        await self.snap("04_new_booking")

    # ═══════════════════════════════════════════════════════════════════════
    # 通用弹窗方法（Library / Area / Time / Duration 全部适用）
    # ═══════════════════════════════════════════════════════════════════════

    async def _open_popup(self, field_label: str):
        """
        点击包含 field_label 文字的 .inputPopupSelectDiv 打开弹窗。
        等弹出内容（div.my-2 选项列表）出现后返回。
        """
        trigger = self.page.locator('.inputPopupSelectDiv').filter(
            has_text=field_label
        ).first
        await trigger.wait_for(state="visible", timeout=10_000)
        await trigger.click()
        await self.page.wait_for_selector('div.my-2', timeout=10_000)
        await asyncio.sleep(0.3)

    async def _pick_option(self, option_text: str, exact: bool = True):
        """
        在已打开的弹窗里，从 div.my-2 列表中选择 option_text。
        exact=True 时要求内容完全相等；False 时只要包含即可。
        """
        items = self.page.locator('div.my-2')
        cnt = await items.count()
        for i in range(cnt):
            txt = (await items.nth(i).inner_text()).strip()
            match = (txt == option_text) if exact else (option_text.lower() in txt.lower())
            if match:
                await items.nth(i).click()
                log.info(f"  ✔ 已选: {txt}")
                await asyncio.sleep(0.5)
                return
        # 兜底：Playwright 文字匹配
        log.warning(f"  ⚠ div.my-2 精确匹配失败，尝试兜底: {option_text}")
        await self.page.get_by_text(option_text, exact=False).first.click()
        await asyncio.sleep(0.5)

    # ── 选图书馆 ──────────────────────────────────────────────────────────
    async def select_library(self):
        log.info(f"▶ 选图书馆: {TARGET_LIBRARY}")
        await self._open_popup("Library")
        await self._pick_option(TARGET_LIBRARY)
        await self.snap("05_library")

    # ── 选区域 ────────────────────────────────────────────────────────────
    async def select_area(self):
        log.info(f"▶ 选区域: {TARGET_AREA}")
        await self._open_popup("Area")
        await self._pick_option(TARGET_AREA)
        await self.snap("06_area")

    # ── 选日期 ────────────────────────────────────────────────────────────
    async def select_date(self) -> datetime:
        target = datetime.now(SGT) + timedelta(days=BOOKING_DATE_OFFSET)
        log.info(f"  设置 Date = {target.strftime('%d %b %Y')}")

        # Date 字段也是 inputPopupSelectDiv，点击打开日历
        await self._open_popup("Date")
        await self.snap("07_calendar")

        # 翻到目标月份
        await self._go_to_month(target)

        # 点击目标日期（跳过 disabled 的）
        day = str(target.day)
        clicked = False
        for loc in [
            # Vuetify 日历按钮
            self.page.locator('button.v-btn:not([disabled])').filter(has_text=day),
            # 通用 button / td
            self.page.locator(f'button:has-text("{day}"), td:has-text("{day}")'),
        ]:
            cnt = await loc.count()
            for i in range(cnt):
                el = loc.nth(i)
                txt = (await el.inner_text()).strip()
                if txt != day:
                    continue
                cls = (await el.get_attribute("class")) or ""
                if "disabled" in cls.lower() or "inactive" in cls.lower():
                    continue
                await el.click()
                clicked = True
                break
            if clicked:
                break

        if not clicked:
            await self.page.get_by_text(day, exact=True).first.click()

        await asyncio.sleep(0.5)
        await self.snap("08_date")
        log.info(f"  ✅ Date 已选: {target.strftime('%-d %b %Y')}")
        return target

    async def _go_to_month(self, target: datetime, max_clicks: int = 14):
        want = target.strftime("%B %Y")
        for _ in range(max_clicks):
            header = self.page.locator(
                '.v-date-picker-header__value, [class*="picker-title"], '
                '[class*="calendar-header"], [class*="monthYear"]'
            ).first
            try:
                cur = (await header.inner_text()).strip()
            except Exception:
                break
            if want in cur:
                break
            nxt = self.page.locator(
                'button[aria-label*="next" i], '
                'button:has-text("›"), button:has-text(">")'
            ).last
            await nxt.click()
            await asyncio.sleep(0.3)

    # ── 选时间（弹窗） ────────────────────────────────────────────────────
    async def select_time(self, time_str: str):
        """time_str: "10:00 am" 或 "2:00 pm"（弹窗 div.my-2 里的文字）"""
        log.info(f"  设置 Time = {time_str}")
        await self._open_popup("Time")
        await self.snap("09_time_popup")
        await self._pick_option(time_str, exact=False)
        log.info("  ✅ Time 已选")

    # ── 选时长（弹窗） ────────────────────────────────────────────────────
    async def select_duration(self, dur_str: str):
        """
        dur_str: "1:30"（弹窗 div.my-2 里的文字，可能是 "1:30" 或 "1 hr 30 min"）
        字段标签全称 "Duration (Hour : Min)"，用 "Duration" 做 has_text 模糊匹配即可。
        """
        log.info(f"  设置 Duration = {dur_str}")
        # 用 "Duration" 模糊匹配（contains），避免全称匹配失败
        trigger = self.page.locator('.inputPopupSelectDiv').filter(
            has_text="Duration"
        ).first
        await trigger.wait_for(state="visible", timeout=10_000)
        await trigger.click()
        await self.page.wait_for_selector('div.my-2', timeout=10_000)
        await asyncio.sleep(0.3)
        await self.snap("10_dur_popup")
        await self._pick_option(dur_str, exact=False)
        log.info("  ✅ Duration 已选")

    # ── CHECK AVAILABLE SLOTS ─────────────────────────────────────────────
    async def check_available_slots(self):
        log.info("▶ 点击 CHECK AVAILABLE SLOTS...")
        # DOM: .button-group 下的 div row 内含 "CHECK AVAILABLE SLOTS" 文字
        # Playwright :text() 可匹配包含该文字的最近元素
        btn = self.page.locator(
            '.button-group div:has-text("CHECK AVAILABLE SLOTS"), '
            'button:has-text("CHECK AVAILABLE SLOTS")'
        ).first
        await btn.wait_for(state="visible", timeout=10_000)
        await btn.click()
        try:
            await self.page.wait_for_load_state("load", timeout=20_000)
        except PlaywrightTimeout:
            pass
        await asyncio.sleep(1.5)
        await self.snap("11_slots")

    # ── 完整预约一个时段 ──────────────────────────────────────────────────
    async def book_one_slot(self, label: str, time_str: str, dur_str: str):
        log.info(f"\n{'='*60}")
        log.info(f"▶ 预约时段: {label}")

        await self.navigate_to_new_booking()
        await self.select_library()
        await self.select_area()
        await self.select_date()
        await self.select_time(time_str)
        await self.select_duration(dur_str)
        await self.check_available_slots()

        seat = await self._pick_best_seat(label)
        await self.snap(f"12_seat_{_safe(label)}")
        log.info(f"✅ 座位 {seat} 已选")

        await self._confirm(label)
        log.info(f"🎉 {label} 预约成功！座位: {seat}")
        return seat

    # ── 按优先级选座 ──────────────────────────────────────────────────────
    async def _pick_best_seat(self, slot_label: str) -> str:
        await asyncio.sleep(1.5)

        for seat in SEAT_CANDIDATES:
            log.info(f"  🔍 {seat}...")

            by_attr = self.page.locator(
                f'[data-seat="{seat}"], [id="{seat}"], '
                f'[aria-label="{seat}"], [title="{seat}"]'
            )
            by_text = self.page.locator(
                'td, button, span, div[class*="seat"], div[class*="slot"]'
            ).filter(has_text=seat)

            candidate = None
            if await by_attr.count() > 0:
                candidate = by_attr.first
            else:
                cnt = await by_text.count()
                for i in range(cnt):
                    if (await by_text.nth(i).inner_text()).strip() == seat:
                        candidate = by_text.nth(i)
                        break

            if candidate is None:
                log.warning(f"    ⚠ {seat} 页面找不到，跳过")
                continue

            cls      = (await candidate.get_attribute("class")) or ""
            disabled = await candidate.get_attribute("disabled")
            aria_dis = await candidate.get_attribute("aria-disabled")
            bad      = {"disabled", "booked", "unavailable", "occupied",
                        "reserved", "taken", "grey", "gray"}
            if disabled is not None or aria_dis == "true" or any(c in cls.lower() for c in bad):
                log.info(f"    ✗ {seat} 不可用")
                continue

            await candidate.scroll_into_view_if_needed()
            await candidate.click()
            log.info(f"    ✔ {seat} 已选中！")
            return seat

        await self.snap(f"ERR_noseat_{_safe(slot_label)}")
        raise RuntimeError(f"S74–S86 全部不可用！时段: {slot_label}")

    # ── 提交确认 ──────────────────────────────────────────────────────────
    async def _confirm(self, slot_label: str):
        s = _safe(slot_label)
        confirm_sel = (
            'button:has-text("Book"), button:has-text("Confirm"), '
            'button:has-text("BOOK"), button:has-text("Reserve"), '
            'button[type="submit"]'
        )
        await self.page.locator(confirm_sel).first.wait_for(
            state="visible", timeout=10_000
        )
        await self.page.locator(confirm_sel).first.click()
        try:
            await self.page.wait_for_load_state("load", timeout=20_000)
        except PlaywrightTimeout:
            pass
        await self.snap(f"13_confirm1_{s}")

        for word in ["OK", "Confirm", "Yes"]:
            try:
                btn = self.page.get_by_text(word, exact=True).first
                await btn.wait_for(state="visible", timeout=4_000)
                await btn.click()
                try:
                    await self.page.wait_for_load_state("load", timeout=20_000)
                except PlaywrightTimeout:
                    pass
                await self.snap(f"14_confirm2_{s}")
                break
            except PlaywrightTimeout:
                continue


# ═════════════════════════════════════════════════════════════════════════
async def main():
    log.info(f"=== NLB v3 | {datetime.now(SGT).strftime('%Y-%m-%d %H:%M:%S')} SGT ===")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-setuid-sandbox"],
        )
        ctx = await browser.new_context(
            viewport={"width": 390, "height": 844},
            locale="en-GB",
            timezone_id="Asia/Singapore",
            user_agent=(
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                "Version/17.0 Mobile/15E148 Safari/604.1"
            ),
        )
        page = await ctx.new_page()
        booker = NLBBooker(page)

        try:
            await booker.login()

            results = []
            for label, time_str, dur_str in TIME_SLOTS:
                try:
                    seat = await booker.book_one_slot(label, time_str, dur_str)
                    results.append((label, f"✅ 成功  座位: {seat}"))
                except Exception as e:
                    log.error(f"❌ {label} 失败: {e}")
                    await booker.snap(f"ERR_{_safe(label)}")
                    results.append((label, f"❌ 失败: {e}"))
                await asyncio.sleep(3)

            log.info("\n" + "=" * 60)
            log.info("📋 预约结果汇总:")
            for label, status in results:
                log.info(f"  {label}  →  {status}")
            log.info("=" * 60)

        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
