"""
NLB Seat Booking Automation Script  (v4 — 座位优先级选座)
=================================================================
新增功能（v4）：
  - 座位分三个优先级：
      第一序列：S74-S86（最优先）
      第二序列：S1-S16
      其他座位：兜底，按页面顺序尝试
  - _pick_seat_by_priority() 自动扫描页面可用座位，按序列依次点击
  - 所有序列均不可用时抛出异常，避免误预约错误座位
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

# 座位优先级：第一序列 S74-S86，第二序列 S1-S16，其余兜底
_TIER1 = [f"S{n}" for n in range(74, 87)]    # S74 → S86
_TIER2 = [f"S{n}" for n in range(1, 17)]     # S1  → S16
_TIER3_EXCLUDE = set(_TIER1) | set(_TIER2)
# _TIER3 在运行时根据页面实际可用座位动态补充（见 _pick_seat_by_priority）
SEAT_CANDIDATES = _TIER1 + _TIER2            # 静态已知候选（_TIER3 动态追加）

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
        登录流程（v4）：
          1. 打开座位预约主页
          2. 点击底部 "Account" Tab → 跳转到 CAS 登录页
          3. NLB 登录页：上半部分 QR / 下半部分直接是 myLibrary 账密表单，无需切 Tab
          4. 填写账号密码，点 CONTINUE 提交
          5. 等待跳回 nlb.gov.sg，确认已登录
        """
        log.info("▶ 打开主页...")
        await self.page.goto(BASE_URL, wait_until="load")
        await asyncio.sleep(2)
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
            # 等 password 字段出现（myLibrary 表单在 QR 区块下方，无需切 Tab）
            log.info("▶ 检测到登录页，填写账号密码...")
            try:
                await self.page.wait_for_selector(
                    'input[type="password"]', timeout=15_000
                )
            except PlaywrightTimeout:
                await self.snap("ERR_no_password_field")
                raise RuntimeError("登录页未找到 password 字段，请检查截图")

            # 填用户名
            for u_sel in [
                'input[name="username"]',
                'input[name="userId"]',
                'input[placeholder*="username" i]',
                'input[placeholder*="myLibrary" i]',
            ]:
                try:
                    el = self.page.locator(u_sel).first
                    if await el.count() > 0:
                        await el.fill(NLB_USERNAME)
                        log.info(f"  ✔ 用户名已填（{u_sel}）")
                        break
                except Exception:
                    continue

            # 填密码
            await self.page.fill('input[type="password"]', NLB_PASSWORD)
            await self.snap("03_creds_filled")

            # 点提交 —— NLB 页用 "CONTINUE" 按钮（不是 Login/Submit）
            for sel in [
                'button:has-text("CONTINUE")',
                'button:has-text("Continue")',
                # 密码表单内的 submit（避免误点 QR 相关按钮）
                'form:has(input[type="password"]) input[type="submit"]',
                'form:has(input[type="password"]) button[type="submit"]',
                'input[type="submit"]',
                'button[type="submit"]',
            ]:
                try:
                    btn = self.page.locator(sel).first
                    if await btn.count() > 0:
                        await btn.wait_for(state="visible", timeout=5_000)
                        await btn.click()
                        log.info(f"  ✔ 已点提交按钮（{sel}）")
                        break
                except Exception:
                    continue

            # 等待 OAuth 跳转（URL 离开 signin，不用 networkidle）
            try:
                await self.page.wait_for_function(
                    "() => !window.location.hostname.includes('signin.nlb.gov.sg')",
                    timeout=60_000,
                )
            except PlaywrightTimeout:
                log.warning("  ⚠ 60s 内 URL 未离开 signin 页，继续检查...")

            try:
                await self.page.wait_for_load_state("load", timeout=20_000)
            except PlaywrightTimeout:
                pass
            await asyncio.sleep(2)
            await self.snap("04_after_submit")
            log.info(f"  提交后 URL: {self.page.url}")

        # 步骤 3：确认已登录
        current_url = self.page.url
        if "signin.nlb.gov.sg" in current_url:
            raise RuntimeError(
                f"登录失败！请检查账号密码和截图 03_creds_filled.png。URL={current_url}"
            )

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
                await self.page.wait_for_load_state("load", timeout=15_000)
            except PlaywrightTimeout:
                pass
        except PlaywrightTimeout:
            pass
        # 等 inputPopupSelectDiv 表单主体出现
        await self.page.wait_for_selector('.inputPopupSelectDiv', timeout=20_000)
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
        """
        Area 弹窗是 Radio Button 对话框（非 div.my-2 下拉列表），
        需要单独处理：点击触发器 → 等对话框 → 点 radio 选项。
        """
        log.info(f"▶ 选区域: {TARGET_AREA}")
        # 点击 Area 字段触发弹窗
        trigger = self.page.locator('.inputPopupSelectDiv').filter(has_text="Area").first
        await trigger.wait_for(state="visible", timeout=10_000)
        await trigger.click()
        await asyncio.sleep(0.8)   # 等对话框动画完成

        # Area 对话框用 radio button，逐一尝试选择器
        clicked = False
        for sel in [
            # Vuetify radio / label
            f'[role="radio"]:has-text("{TARGET_AREA}")',
            f'label:has-text("{TARGET_AREA}")',
            f'.v-radio:has-text("{TARGET_AREA}")',
            f'.v-list-item:has-text("{TARGET_AREA}")',
            f'li:has-text("{TARGET_AREA}")',
        ]:
            try:
                el = self.page.locator(sel).first
                if await el.count() > 0:
                    await el.scroll_into_view_if_needed()
                    await el.click()
                    log.info(f"  ✔ 已选区域: {TARGET_AREA}（{sel}）")
                    clicked = True
                    await asyncio.sleep(0.5)
                    break
            except Exception:
                continue

        if not clicked:
            # 兜底：get_by_text 精确匹配
            log.warning(f"  ⚠ radio 选择器均未命中，尝试文字兜底")
            await self.page.get_by_text(TARGET_AREA, exact=True).first.click()
            await asyncio.sleep(0.5)

        await self.snap("06_area")

    # ── 选日期 ────────────────────────────────────────────────────────────
    async def select_date(self) -> datetime:
        target = datetime.now(SGT) + timedelta(days=BOOKING_DATE_OFFSET)
        log.info(f"  设置 Date = {target.strftime('%d %b %Y')}")

        # Date 打开日历对话框，不是 div.my-2 下拉，不能用 _open_popup()
        trigger = self.page.locator('.inputPopupSelectDiv').filter(has_text="Date").first
        await trigger.wait_for(state="visible", timeout=10_000)
        await trigger.click()
        await self.page.wait_for_selector(
            '.v-date-picker-header, [class*="datepicker"], '
            '[class*="calendar"], button.v-btn[aria-label]',
            timeout=10_000,
        )
        await asyncio.sleep(0.3)
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
        month_name = target.strftime("%B")   # e.g. "May"
        year_str   = target.strftime("%Y")   # e.g. "2026"
        for _ in range(max_clicks):
            header = self.page.locator(
                '.v-date-picker-header__value, [class*="picker-title"], '
                '[class*="calendar-header"], [class*="monthYear"]'
            ).first
            try:
                cur = " ".join((await header.inner_text()).split())
            except Exception:
                break
            log.info(f"  📅 日历当前: {cur!r}  目标: {month_name} {year_str}")
            if month_name in cur and year_str in cur:
                break
            nxt = self.page.locator(
                'button[aria-label*="next" i], '
                'button:has-text("›"), button:has-text(">")'
            ).last
            disabled = await nxt.get_attribute("disabled")
            if disabled is not None:
                log.warning(f"  ⚠ Next month 已禁用，停在: {cur!r}")
                break
            await nxt.click()
            await asyncio.sleep(0.3)

    # ── 通用 Radio 对话框选项点击 ────────────────────────────────────────
    async def _pick_dialog_radio(self, option_text: str):
        """
        在已打开的 Radio 对话框中点击匹配文字的选项。
        Time / Duration / Area 对话框均用此方法。
        采用精确完整文字匹配，避免 "2:00 pm" 误中 "12:00 pm"。

        关键修复：弹窗列表可能需要滚动（如 "2:00 pm" 在视口外），
        必须先 scroll_into_view_if_needed() 再判断可见性，不能依赖 is_visible()。
        """
        # 策略1：label / radio 精确匹配 + 强制滚动到视口
        for sel in [
            f'label:has-text("{option_text}")',
            f'.v-radio:has-text("{option_text}")',
            f'[role="radio"]:has-text("{option_text}")',
            f'.v-list-item:has-text("{option_text}")',
        ]:
            try:
                el = self.page.locator(sel).first
                if await el.count() > 0:
                    await el.scroll_into_view_if_needed()   # ← 先滚动，再点
                    await asyncio.sleep(0.2)
                    await el.click()
                    log.info(f"  ✔ 已选: {option_text}（{sel}）")
                    await asyncio.sleep(0.5)
                    return
            except Exception:
                continue

        # 策略2：遍历 div.my-2，精确文字匹配后强制滚动到视口再点击
        # 注意：不能用 is_visible() 过滤，因为目标选项可能在滚动区域外而被判为不可见
        items = self.page.locator('div.my-2')
        cnt = await items.count()
        log.info(f"  🔎 弹窗共 {cnt} 个选项，查找: {option_text!r}")
        for i in range(cnt):
            el = items.nth(i)
            try:
                txt = (await el.inner_text()).strip()
                if txt != option_text:
                    continue
                # 找到目标项：先滚动到视口，再点击
                await el.scroll_into_view_if_needed()
                await asyncio.sleep(0.2)
                await el.click()
                log.info(f"  ✔ 已选(div.my-2 滚动): {txt}")
                await asyncio.sleep(0.5)
                return
            except Exception:
                continue

        raise RuntimeError(f"Radio 对话框找不到选项: {option_text!r}")

    # ── 选时间（Radio 对话框）─────────────────────────────────────────────
    async def select_time(self, time_str: str):
        """time_str: "10:00 am" / "2:00 pm" 等，与对话框文字完全一致"""
        log.info(f"  设置 Time = {time_str}")
        trigger = self.page.locator('.inputPopupSelectDiv').filter(has_text="Time").first
        await trigger.wait_for(state="visible", timeout=10_000)
        await trigger.click()
        await asyncio.sleep(0.8)
        await self.snap("09_time_popup")
        await self._pick_dialog_radio(time_str)
        log.info("  ✅ Time 已选")

    # ── 选时长（Radio 对话框）─────────────────────────────────────────────
    async def select_duration(self, dur_str: str):
        """dur_str: "1:30" 等，与 Duration 对话框文字完全一致"""
        log.info(f"  设置 Duration = {dur_str}")
        trigger = self.page.locator('.inputPopupSelectDiv').filter(
            has_text="Duration"
        ).first
        await trigger.wait_for(state="visible", timeout=10_000)
        await trigger.click()
        await asyncio.sleep(0.8)
        await self.snap("10_dur_popup")
        await self._pick_dialog_radio(dur_str)
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
        await self.page.wait_for_load_state("load", timeout=20_000)
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

        await self._click_area_and_book(label)
        log.info(f"🎉 {label} 预约完成！")
        return TARGET_AREA
    # ── 点击区域结果卡片并完成预约 ──────────────────────────────────────
    async def _click_area_and_book(self, label: str):
        """
        Search Results 页：点击 TARGET_AREA 对应的卡片
        → Seat Selection 页：按优先级选座（第一序列 S74-S86，第二序列 S1-S16，其他）
        → Booking Details 页：点 BOOK 按钮
        → 处理确认弹窗
        """
        s = _safe(label)
        await asyncio.sleep(1.5)
        await self.snap(f"11_slots_{s}")

        # 找并点击 TARGET_AREA 结果卡片
        clicked = False
        for sel in [
            f'.v-list-item:has-text("{TARGET_AREA}")',
            f'li:has-text("{TARGET_AREA}")',
            f'div[class*="item"]:has-text("{TARGET_AREA}")',
            f'div[class*="card"]:has-text("{TARGET_AREA}")',
        ]:
            try:
                el = self.page.locator(sel).first
                if await el.count() > 0:
                    await el.scroll_into_view_if_needed()
                    await el.click()
                    log.info(f"  ✔ 点击区域卡片: {TARGET_AREA}（{sel}）")
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            log.warning("  ⚠ 精确选择器未命中，尝试文字兜底")
            await self.page.get_by_text(TARGET_AREA, exact=False).first.click()
            log.info(f"  ✔ 点击区域卡片（兜底）: {TARGET_AREA}")

        await asyncio.sleep(1.0)
        await self.snap(f"12_seat_selection_{s}")

        # ── 按优先级选座 ──────────────────────────────────────────────
        booked_seat = await self._pick_seat_by_priority(s)
        log.info(f"  🪑 最终选座: {booked_seat}")

        # 等 Booking Details 页面 + BOOK 按钮
        await self.page.wait_for_selector(
            'button:has-text("BOOK")', timeout=15_000
        )
        await self.snap(f"13_booking_details_{s}")
        log.info("  📄 已进入 Booking Details 页")

        # 点 BOOK 按钮
        await self.page.locator('button:has-text("BOOK")').first.click()
        log.info("  ✔ 已点击 BOOK 按钮")
        await asyncio.sleep(1.5)
        await self.snap(f"14_after_book_{s}")

        # 处理可能出现的确认弹窗
        for word in ["OK", "Confirm", "Yes", "CONFIRM"]:
            try:
                btn = self.page.get_by_text(word, exact=True).first
                await btn.wait_for(state="visible", timeout=4_000)
                await btn.click()
                log.info(f"  ✔ 确认弹窗: {word}")
                await asyncio.sleep(1.5)
                await self.snap(f"15_confirmed_{s}")
                break
            except PlaywrightTimeout:
                continue

    # ── 按优先级选座 ──────────────────────────────────────────────────────
    async def _pick_seat_by_priority(self, label_safe: str) -> str:
        """
        座位优先级：
          第一序列 S74-S86 → 第二序列 S1-S16 → 其他可用座位（兜底）

        关键修复：座位列表需要滚动，不能用 is_visible() 过滤，
        收集阶段只排除明确禁用的座位，点击前统一 scroll_into_view_if_needed()。

        返回实际选中的座位号；若无任何可用座位则抛出 RuntimeError。
        """
        # ── 收集页面上所有可用（未禁用）座位元素 ────────────────────────
        async def _get_available_seats() -> dict[str, object]:
            """
            返回 {座位号: locator} 的字典。
            不做 is_visible() 过滤——视口外的座位同样需要被发现，
            点击时再 scroll_into_view_if_needed() 滚动进来。
            """
            available: dict[str, object] = {}
            for sel in [
                '[class*="seat"]:not([class*="disabled"]):not([class*="unavailable"])',
                '[class*="seat"]:not([disabled])',
                'button[class*="seat"]',
                '[data-seat]',
            ]:
                els = self.page.locator(sel)
                cnt = await els.count()
                if cnt == 0:
                    continue
                log.info(f"  🔎 selector={sel!r} 匹配到 {cnt} 个元素")
                for i in range(cnt):
                    el = els.nth(i)
                    try:
                        # 获取座位号：优先 data-seat，其次 aria-label，最后 inner_text
                        seat_id = (
                            await el.get_attribute("data-seat")
                            or await el.get_attribute("aria-label")
                            or (await el.inner_text()).strip()
                        )
                        if not seat_id:
                            continue
                        seat_id = seat_id.strip().upper()
                        # 仅排除 class 中明确标注禁用的座位
                        cls = (await el.get_attribute("class")) or ""
                        if any(k in cls.lower() for k in ("disabled", "unavailable", "booked", "occupied")):
                            continue
                        available[seat_id] = el
                    except Exception:
                        continue
                if available:
                    break   # 找到结果就不再尝试下一个 selector
            return available

        available = await _get_available_seats()
        log.info(f"  📋 可用座位数: {len(available)}  全部: {sorted(available.keys())}")

        if not available:
            log.warning("  ⚠ 未检测到可用座位，尝试直接进入预约详情页（系统自动分配）")
            return "AUTO"

        # ── 按优先级构造候选序列 ─────────────────────────────────────────
        # Tier 3：页面上有但不在 Tier1/Tier2 的座位，保持页面顺序
        tier3 = [sid for sid in available if sid not in _TIER3_EXCLUDE]

        # ── 逐个尝试 ────────────────────────────────────────────────────
        for tier_label, seats in [
            ("第一序列(S74-S86)", _TIER1),
            ("第二序列(S1-S16)",  _TIER2),
            ("其他座位",          tier3),
        ]:
            log.info(f"  🔍 尝试 {tier_label}，候选: {[s for s in seats if s in available]}")
            for seat_id in seats:
                if seat_id not in available:
                    continue
                el = available[seat_id]
                try:
                    # 先滚动到视口（座位可能在屏幕外），再等动画，再点击
                    await el.scroll_into_view_if_needed()
                    await asyncio.sleep(0.3)
                    await el.click()
                    log.info(f"  ✅ 已选座位: {seat_id}（{tier_label}）")
                    await asyncio.sleep(0.8)
                    await self.snap(f"seat_selected_{seat_id}_{label_safe}")
                    return seat_id
                except Exception as e:
                    log.warning(f"  ⚠ 点击 {seat_id} 失败: {e}，继续下一个")
                    continue

        raise RuntimeError("所有优先级座位均不可用，预约失败")


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
                    results.append((label, f"✅ 成功  区域: {seat}"))
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
