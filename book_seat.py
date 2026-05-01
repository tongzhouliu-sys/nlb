"""
NLB Seat Booking Automation Script
===================================
自动预约新加坡国家图书馆座位
目标座位: Punggol Regional Library > Study Zone > Level 3 > S86（满则降级至 S74）
预约时段: 10:00-11:30 / 14:00-15:30
"""

import os
import time
import logging
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── 配置区（通过环境变量注入，不要硬编码） ─────────────────────────────────
NLB_USERNAME = os.environ["NLB_USERNAME"]   # GitHub Secret
NLB_PASSWORD = os.environ["NLB_PASSWORD"]   # GitHub Secret

TARGET_LIBRARY  = "Punggol Regional Library"
TARGET_ZONE     = "Study Zone"
TARGET_LEVEL    = "Level 3"
# 座位优先级：S86 最优先，被占则依次降到 S74
SEAT_CANDIDATES = [f"S{n}" for n in range(86, 73, -1)]  # S86, S85, ..., S74

# 想预约哪天？默认预约"明天"（脚本在 12:00 跑，预约次日座位）
# 若系统允许预约当天，可改为 offset=0
BOOKING_DATE_OFFSET = 1   # 1 = 明天

TIME_SLOTS = [
    ("10:00", "11:30"),
    ("14:00", "15:30"),
]

BASE_URL  = "https://www.nlb.gov.sg/seatbooking"
LOGIN_URL = f"{BASE_URL}/common/login"
SGT       = ZoneInfo("Asia/Singapore")

# ── 日志配置 ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════
class NLBBooker:
    """封装整套预约流程"""

    def __init__(self, page):
        self.page = page

    # ── 辅助：截图（便于 CI debug） ────────────────────────────────────────
    async def snap(self, name: str):
        path = f"screenshots/{name}.png"
        os.makedirs("screenshots", exist_ok=True)
        await self.page.screenshot(path=path, full_page=True)
        log.info(f"Screenshot saved → {path}")

    # ── 辅助：等待并点击 ───────────────────────────────────────────────────
    async def click(self, selector: str, timeout: int = 15_000):
        await self.page.wait_for_selector(selector, timeout=timeout)
        await self.page.click(selector)

    # ── 步骤 1: 登录 ───────────────────────────────────────────────────────
    async def login(self):
        log.info("▶ 正在登录 NLB 账户...")
        await self.page.goto(LOGIN_URL, wait_until="networkidle")
        await self.snap("01_login_page")

        # 填写用户名（myLibrary ID / NRIC / Email）
        await self.page.fill('input[name="userId"], input[placeholder*="ID"], input[type="text"]',
                              NLB_USERNAME)
        await self.page.fill('input[name="password"], input[type="password"]',
                              NLB_PASSWORD)
        await self.snap("02_credentials_filled")

        # 点击登录按钮
        await self.click('button[type="submit"], button:has-text("Login"), button:has-text("Log in")')
        await self.page.wait_for_load_state("networkidle")
        await self.snap("03_after_login")

        # 校验登录成功
        if "login" in self.page.url.lower():
            raise RuntimeError("登录失败！请检查账号密码。当前 URL: " + self.page.url)
        log.info("✅ 登录成功")

    # ── 步骤 2: 进入座位预约页 ─────────────────────────────────────────────
    async def navigate_to_booking(self):
        log.info("▶ 跳转到座位预约页面...")
        await self.page.goto(f"{BASE_URL}/seatavailability", wait_until="networkidle")
        await self.snap("04_seat_availability")

    # ── 步骤 3: 选择图书馆 ─────────────────────────────────────────────────
    async def select_library(self):
        log.info(f"▶ 选择图书馆: {TARGET_LIBRARY}")
        # 等待下拉框或按钮出现
        await self.page.wait_for_selector(
            'select, [role="combobox"], [class*="library"], [class*="outlet"]',
            timeout=15_000,
        )

        # 尝试下拉 select 方式
        library_select = self.page.locator('select').first
        if await library_select.count() > 0:
            await library_select.select_option(label=TARGET_LIBRARY)
        else:
            # 点击式下拉（Vue/React 组件）
            await self.click(f'[class*="outlet"], [class*="library"]')
            await self.page.get_by_text(TARGET_LIBRARY, exact=False).click()

        await self.page.wait_for_load_state("networkidle")
        await self.snap("05_library_selected")
        log.info(f"✅ 图书馆已选: {TARGET_LIBRARY}")

    # ── 步骤 4: 选择日期 ───────────────────────────────────────────────────
    async def select_date(self):
        target_date = datetime.now(SGT) + timedelta(days=BOOKING_DATE_OFFSET)
        date_str = target_date.strftime("%Y-%m-%d")        # ISO 格式
        date_display = target_date.strftime("%-d %b %Y")  # 1 May 2026
        log.info(f"▶ 选择日期: {date_str}")

        # 常见实现：日期选择器 input 或 flatpickr
        date_inputs = self.page.locator('input[type="date"], input[placeholder*="date" i]')
        if await date_inputs.count() > 0:
            await date_inputs.first.fill(date_str)
        else:
            # 点击日历触发按钮
            await self.click('[class*="date"], [class*="calendar"], button[aria-label*="date" i]')
            # 尝试点击对应日期数字
            await self.page.get_by_text(str(target_date.day), exact=True).first.click()

        await self.snap("06_date_selected")
        log.info(f"✅ 日期已选: {date_display}")
        return target_date

    # ── 步骤 5: 预约单个时段 ──────────────────────────────────────────────
    async def book_slot(self, start_time: str, end_time: str):
        log.info(f"▶ 预约时段: {start_time} – {end_time}")

        # 5a: 选时段（可能是 radio / button / dropdown）
        slot_label = f"{start_time}"
        try:
            # 尝试匹配时间文字按钮
            slot_btn = self.page.get_by_text(slot_label, exact=False).first
            await slot_btn.wait_for(state="visible", timeout=10_000)
            await slot_btn.click()
        except PlaywrightTimeout:
            # 备用：下拉选时段
            selects = self.page.locator('select')
            cnt = await selects.count()
            for i in range(cnt):
                opts = await selects.nth(i).inner_text()
                if start_time in opts:
                    await selects.nth(i).select_option(label=start_time)
                    break

        await self.snap(f"07_slot_{start_time.replace(':', '')}")

        # 5b: 选区域 / 层数
        for label in [TARGET_ZONE, TARGET_LEVEL]:
            try:
                el = self.page.get_by_text(label, exact=False).first
                await el.wait_for(state="visible", timeout=8_000)
                await el.click()
                await asyncio.sleep(0.5)
            except PlaywrightTimeout:
                log.warning(f"⚠ 找不到 '{label}'，跳过（可能已自动选中）")

        # 5c: 按优先级依次尝试 S86 → S85 → ... → S74
        booked_seat = await self._pick_best_seat(start_time)
        await self.snap(f"08_seat_{start_time.replace(':', '')}_selected")
        log.info(f"✅ 座位 {booked_seat} 已选")

        # 5d: 提交预约
        confirm_sel = (
            'button:has-text("Book"), button:has-text("Confirm"), '
            'button:has-text("Reserve"), button[type="submit"]'
        )
        await self.click(confirm_sel)
        await self.page.wait_for_load_state("networkidle")
        await self.snap(f"09_confirm_{start_time.replace(':', '')}")

        # 5e: 二次确认弹窗（若有）
        try:
            ok_btn = self.page.get_by_text("OK", exact=True).or_(
                self.page.get_by_text("Confirm", exact=True)
            )
            await ok_btn.wait_for(state="visible", timeout=5_000)
            await ok_btn.click()
            await self.page.wait_for_load_state("networkidle")
            await self.snap(f"10_final_{start_time.replace(':', '')}")
        except PlaywrightTimeout:
            pass  # 无二次确认弹窗，正常

        log.info(f"🎉 时段 {start_time}–{end_time} 预约成功！（座位: {booked_seat}）")
        return booked_seat

    # ── 辅助：按优先级选座（S86 → S85 → … → S74） ──────────────────────────
    async def _pick_best_seat(self, start_time: str) -> str:
        """
        遍历 SEAT_CANDIDATES（从高到低），找到第一个可用座位并点击。
        判断"可用"的依据：
          1. 座位元素存在于页面
          2. 没有 disabled / booked / unavailable 等 class 或 aria 属性
        """
        # 先收集页面上所有座位单元格，便于后续匹配
        await asyncio.sleep(0.5)  # 等座位地图渲染完毕

        for seat in SEAT_CANDIDATES:
            log.info(f"  🔍 尝试座位 {seat}...")

            # 策略 A：data-seat 属性
            el = self.page.locator(
                f'[data-seat="{seat}"], [id="{seat}"], [aria-label="{seat}"]'
            )
            # 策略 B：文字匹配（td / span / div 内精确文字）
            el_text = self.page.locator('td, [class*="seat"], span, div').filter(
                has_text=seat
            )

            # 取两种策略中第一个可见的
            candidate = None
            if await el.count() > 0:
                candidate = el.first
            elif await el_text.count() > 0:
                # 文字匹配可能命中多个，取 innerText 严格等于 seat 的那个
                cnt = await el_text.count()
                for i in range(cnt):
                    txt = (await el_text.nth(i).inner_text()).strip()
                    if txt == seat:
                        candidate = el_text.nth(i)
                        break

            if candidate is None:
                log.warning(f"    ⚠ 座位 {seat} 在页面上找不到，跳过")
                continue

            # 检查是否被禁用 / 已预约
            is_disabled = await candidate.get_attribute("disabled")
            aria_disabled = await candidate.get_attribute("aria-disabled")
            class_attr = (await candidate.get_attribute("class")) or ""
            unavailable_classes = {"disabled", "booked", "unavailable", "occupied", "reserved"}
            is_unavailable = any(c in class_attr.lower() for c in unavailable_classes)

            if is_disabled is not None or aria_disabled == "true" or is_unavailable:
                log.info(f"    ✗ 座位 {seat} 已被预约或不可用（class={class_attr[:60]}）")
                continue

            # 可用！点击
            await candidate.scroll_into_view_if_needed()
            await candidate.click()
            log.info(f"    ✔ 座位 {seat} 可用，已选中")
            return seat

        # 所有候选都不可用
        await self.snap(f"ERR_no_seat_{start_time.replace(':', '')}")
        raise RuntimeError(
            f"S74–S86 全部不可用！请检查截图 ERR_no_seat_{start_time.replace(':', '')}.png"
        )


# ═══════════════════════════════════════════════════════════════════════════
async def main():
    now_sgt = datetime.now(SGT)
    log.info(f"=== NLB 座位自动预约脚本启动 | 当前时间(SGT): {now_sgt.strftime('%Y-%m-%d %H:%M:%S')} ===")

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
        page = await ctx.new_page()
        booker = NLBBooker(page)

        try:
            await booker.login()
            await booker.navigate_to_booking()
            await booker.select_library()
            await booker.select_date()

            results = []
            for start, end in TIME_SLOTS:
                try:
                    # 每个时段前重新进入预约页，避免状态残留
                    await booker.navigate_to_booking()
                    await booker.select_library()
                    await booker.select_date()
                    await booker.book_slot(start, end)
                    results.append((start, end, "✅ 成功"))
                except Exception as e:
                    log.error(f"❌ 时段 {start}–{end} 预约失败: {e}")
                    results.append((start, end, f"❌ 失败: {e}"))
                await asyncio.sleep(2)

            log.info("\n" + "="*50)
            log.info("预约结果汇总:")
            for start, end, status in results:
                log.info(f"  {start}–{end}  →  {status}")
            log.info("="*50)

        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
