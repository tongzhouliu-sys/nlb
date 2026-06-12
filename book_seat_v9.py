"""
NLB Seat Booking Automation Script  (v9 — 座位图诊断 + Preferred Seat弹窗放宽)
=========================================================================
v9 新增（基于"上午AUTO"问题分析）：
  1. 【座位图诊断】_pick_seat_by_priority 打印命中元素的全部属性（class/data-seat/
     aria-label/inner_text），搞清楚 NLB 座位图用什么字段存座位 ID
  2. 【Preferred Seat 弹窗放宽】_pick_preferred_seat_in_dialog 放弃严格 ^S[0-9]+$ 正则，
     改为：打印弹窗所有原始文字，同时接受 "S74"/"74"/"Seat74" 等多种格式，
     过滤掉明显非座位条目（图书馆名/时间/区域/Duration选项）

v8 继承修复：
  - _pick_dialog_radio 策略1 限定在 .v-dialog--active；scroll_into_view_if_needed timeout=3s
  - _pick_preferred_seat_in_dialog 限定在 .v-dialog--active
  - div.my-2 全局选择器改成弹窗内相对查找
  - _verify_form_fields 改为 warn-only
  - Duration 候选 "1:30" 调到第一位
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
# Duration 候选列表：NLB 页面可能显示多种格式，按顺序尝试
TIME_SLOTS = [
    ("10:00–11:30", "10:00 am", ["1:30", "1 hour 30 mins", "90 mins", "1.5 hours", "1 hr 30 min"]),
    ("14:00–15:30", "2:00 pm",  ["1:30", "1 hour 30 mins", "90 mins", "1.5 hours", "1 hr 30 min"]),
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

    async def _pick_option(self, option_text: str):
        """
        在已打开的弹窗里，精确选择 option_text。

        弹窗列表是可滚动的，Punggol Library 等选项可能在视窗下方尚未渲染。
        策略：
          1. 先找弹窗的可滚动容器
          2. 每次向下滚动一屏，扫描当前 div.my-2 列表
          3. 找到精确匹配就立即点击并返回
          4. 滚到底仍未找到 → 列出所有已见选项并抛错
        """
        # 定位弹窗滚动容器（尝试多种选择器）
        scroll_container = None
        for sel in [
            '.v-dialog .v-list',
            '.v-dialog [class*="list"]',
            '.v-dialog__content .v-list',
            '.v-menu__content',
            '.v-dialog',
        ]:
            el = self.page.locator(sel).first
            if await el.count() > 0:
                scroll_container = el
                log.info(f"  弹窗滚动容器: {sel!r}")
                break

        all_opts_seen: list[str] = []
        max_scrolls = 20
        scroll_step_px = 300

        dialog_scope = (
            scroll_container
            if scroll_container is not None
            else self.page.locator('.v-dialog, [role="dialog"]').last
        )

        for scroll_idx in range(max_scrolls + 1):
            items = dialog_scope.locator('div.my-2')
            cnt = await items.count()

            for i in range(cnt):
                el = items.nth(i)
                try:
                    txt = (await el.inner_text()).strip()
                    if txt and txt not in all_opts_seen:
                        all_opts_seen.append(txt)
                    if txt == option_text:
                        await el.scroll_into_view_if_needed()
                        await asyncio.sleep(0.2)
                        await el.click()
                        log.info(f"  ✔ 已选: {txt}（第 {scroll_idx} 次滚动后找到）")
                        await asyncio.sleep(0.5)
                        return
                except Exception:
                    continue

            if scroll_idx >= max_scrolls:
                break

            log.info(f"  第 {scroll_idx + 1} 次滚动弹窗，已见 {len(all_opts_seen)} 个选项...")

            if scroll_container and await scroll_container.count() > 0:
                await scroll_container.evaluate(
                    f"el => el.scrollBy(0, {scroll_step_px})"
                )
            else:
                await self.page.evaluate(
                    f"window.scrollBy(0, {scroll_step_px})"
                )
            await asyncio.sleep(0.4)

            if scroll_container and await scroll_container.count() > 0:
                at_bottom = await scroll_container.evaluate(
                    "el => el.scrollTop + el.clientHeight >= el.scrollHeight - 5"
                )
                if at_bottom:
                    log.info("  已滚到弹窗底部，未找到目标")
                    break

        log.error(f"  弹窗所有已见选项: {all_opts_seen}")
        raise RuntimeError(
            f"弹窗找不到选项 {option_text!r}，已见选项: {all_opts_seen}"
        )

    # ── 选图书馆 ──────────────────────────────────────────────────────────
    async def select_library(self, max_retries: int = 3):
        """
        选择图书馆。

        验证逻辑：
          - 点击选项后，NLB 对话框会自动关闭（.v-dialog 消失）= 选择已被接受
          - 等对话框消失后，再用 textContent 读表单字段做二次确认
          - 不使用 inner_text / splitlines，避免弹窗动画期间读到旧值

        最多重试 max_retries 次，仍失败则 RuntimeError 硬退。
        """
        log.info(f"▶ 选图书馆: {TARGET_LIBRARY}")

        for attempt in range(1, max_retries + 1):
            log.info(f"  第 {attempt} 次尝试选择图书馆...")

            # 1. 打开弹窗
            await self._open_popup("Library")
            await self.snap(f"05a_library_popup_attempt{attempt}")

            # 2. 精确点击目标图书馆（含滚动查找）
            await self._pick_option(TARGET_LIBRARY)

            # 3. 等对话框完全关闭（对话框消失 = 选择被接受的信号）
            dialog_closed = False
            for close_sel in [
                '.v-dialog--active',
                '.v-dialog:has-text("Library")',
                '.v-overlay--active',
            ]:
                try:
                    await self.page.wait_for_selector(
                        close_sel, state='hidden', timeout=5_000
                    )
                    dialog_closed = True
                    log.info(f"  ✔ 对话框已关闭（{close_sel}）")
                    break
                except PlaywrightTimeout:
                    continue

            if not dialog_closed:
                # 备用：等 div.my-2 列表消失
                try:
                    await self.page.wait_for_selector(
                        'div.my-2', state='hidden', timeout=3_000
                    )
                    dialog_closed = True
                    log.info("  ✔ 对话框已关闭（div.my-2 消失）")
                except PlaywrightTimeout:
                    pass

            await asyncio.sleep(0.6)   # 等 Vue 渲染完成

            # 4. 用 textContent（不是 inner_text）读表单字段做二次确认
            field_ok = await self._field_contains(TARGET_LIBRARY)
            await self.snap(f"05_library_attempt{attempt}")

            if field_ok or dialog_closed:
                # 对话框关闭 或 字段包含目标文字，均视为成功
                log.info(f"  ✅ 图书馆选择完成: {TARGET_LIBRARY}（dialog_closed={dialog_closed}, field_ok={field_ok}）")
                return

            log.warning(f"  ⚠ 第 {attempt} 次：对话框未关闭且字段未更新，重试...")
            await asyncio.sleep(1.0)

        # 所有重试均失败
        await self.snap("ERR_library_select_failed")
        raise RuntimeError(
            f"连续 {max_retries} 次图书馆选择均失败，"
            f"请检查截图 ERR_library_select_failed.png"
        )

    async def _field_contains(self, value: str) -> bool:
        """
        检查页面上任意 .inputPopupSelectDiv 的 textContent 是否包含 value。
        用 evaluate/textContent 而非 inner_text，避免 Vuetify 渲染时机问题。
        """
        try:
            containers = self.page.locator('.inputPopupSelectDiv')
            cnt = await containers.count()
            for i in range(cnt):
                tc = await containers.nth(i).evaluate("el => el.textContent || ''")
                if value in tc:
                    log.info(f"  📋 字段包含 {value!r}（container #{i}）")
                    return True
            return False
        except Exception as e:
            log.warning(f"  ⚠ _field_contains({value!r}) 异常: {e}")
            return False

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

        关键修复：
          1. 所有选择器限定在 .v-dialog--active 内，防止旧弹窗 DOM 残留污染。
          2. 策略1的 scroll_into_view_if_needed 用 3s 短超时，不可见残留元素快速失败。
        """
        # 找当前活跃弹窗容器
        dialog_root = None
        for active_sel in [
            '.v-dialog--active',
            '.v-overlay--active .v-dialog',
            '.v-overlay--active [role="dialog"]',
        ]:
            loc = self.page.locator(active_sel)
            if await loc.count() > 0:
                dialog_root = loc.first
                log.info(f"  弹窗容器: {active_sel!r}")
                break
        if dialog_root is None:
            dialog_root = self.page.locator('.v-dialog, [role="dialog"]').last
            log.info("  弹窗容器（兜底）: 最后一个 .v-dialog")

        # 策略1：在活跃弹窗内用 label/radio 选择器，3s 短超时快速失败
        for rel_sel in [
            f'label:has-text("{option_text}")',
            f'.v-radio:has-text("{option_text}")',
            f'[role="radio"]:has-text("{option_text}")',
            f'.v-list-item:has-text("{option_text}")',
        ]:
            try:
                el = dialog_root.locator(rel_sel).first
                if await el.count() > 0:
                    await el.scroll_into_view_if_needed(timeout=3_000)
                    await asyncio.sleep(0.2)
                    await el.click()
                    log.info(f"  ✔ 已选: {option_text}（{rel_sel}）")
                    await asyncio.sleep(0.5)
                    return
            except Exception:
                continue

        # 策略2：遍历活跃弹窗内的 div.my-2
        items = dialog_root.locator('div.my-2')
        cnt = await items.count()
        log.info(f"  🔎 弹窗共 {cnt} 个选项，查找: {option_text!r}")
        for i in range(cnt):
            el = items.nth(i)
            try:
                txt = (await el.inner_text()).strip()
                if txt != option_text:
                    continue
                await el.scroll_into_view_if_needed()
                await asyncio.sleep(0.2)
                await el.click()
                log.info(f"  ✔ 已选(div.my-2): {txt}")
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
    async def select_duration(self, dur_candidates):
        """
        dur_candidates: str 或 list[str]
        NLB 的 Duration 选项文字格式可能因版本变化，
        传入候选列表，逐一尝试，首个匹配成功即返回。
        """
        if isinstance(dur_candidates, str):
            dur_candidates = [dur_candidates]

        log.info(f"  设置 Duration，候选: {dur_candidates}")
        trigger = self.page.locator('.inputPopupSelectDiv').filter(
            has_text="Duration"
        ).first
        await trigger.wait_for(state="visible", timeout=10_000)
        await trigger.click()
        await asyncio.sleep(0.8)
        await self.snap("10_dur_popup")

        # 先记录弹窗所有选项（限定在当前活跃弹窗内）
        dialog_root = self.page.locator('.v-dialog, [role="dialog"]').last
        items = dialog_root.locator('div.my-2')
        cnt = await items.count()
        all_opts = []
        for i in range(cnt):
            try:
                all_opts.append((await items.nth(i).inner_text()).strip())
            except Exception:
                pass
        log.info(f"  Duration 弹窗选项: {all_opts}")

        for dur_str in dur_candidates:
            try:
                await self._pick_dialog_radio(dur_str)
                log.info(f"  ✅ Duration 已选: {dur_str}")
                return
            except RuntimeError:
                log.warning(f"  ⚠ Duration 候选 {dur_str!r} 未找到，尝试下一个")
                continue

        # 兜底：选第一个非空选项
        log.warning("  ⚠ 所有 Duration 候选均未匹配，选择第一个可用选项作为兜底")
        if all_opts:
            await self._pick_dialog_radio(all_opts[0])
            log.info(f"  ✅ Duration 兜底选择: {all_opts[0]}")
        else:
            raise RuntimeError(f"Duration 弹窗无可用选项，候选: {dur_candidates}")

    # ── 全字段预检（仅警告，不阻断）─────────────────────────────────────────
    async def _verify_form_fields(self, time_str: str):
        log.info("▶ 预检表单字段（仅警告，不阻断）...")
        for expected in [TARGET_LIBRARY, TARGET_AREA]:
            ok = await self._field_contains(expected)
            if ok:
                log.info(f"  ✅ 字段包含: {expected!r}")
            else:
                log.warning(f"  ⚠ textContent 未读到 {expected!r}（Vuetify 内部状态，忽略继续）")
        log.info("  ✅ 预检完成，继续查询...")

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
    async def book_one_slot(self, label: str, time_str: str, dur_candidates):
        log.info(f"\n{'='*60}")
        log.info(f"▶ 预约时段: {label}")

        await self.navigate_to_new_booking()
        await self.select_library()
        await self.select_area()
        await self.select_date()
        await self.select_time(time_str)
        await self.select_duration(dur_candidates)

        # ── 点 CHECK 前验证关键字段 ──────────────────────────────────────
        await self._verify_form_fields(time_str)

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

        # ── 处理 "Preferred Seat" 弹窗 ────────────────────────────────────
        # 点击 BOOK 后会弹出 "Preferred Seat" 对话框，
        # 默认选中 "No preferred seat"（系统分配），
        # 需要往下滚动选择具体座位，然后点 CONFIRM
        await self._handle_preferred_seat_dialog(s)

    # ── 处理 "Preferred Seat" 弹窗 ──────────────────────────────────────────
    async def _handle_preferred_seat_dialog(self, label_safe: str):
        """
        点击 BOOK 后出现的 "Preferred Seat" 弹窗处理：
          - 弹窗默认选中 "No preferred seat"（系统分配）
          - 需要往下滚动，按优先级选择具体座位（S74-S86 → S1-S16 → 其他）
          - 选好后点 CONFIRM 确认

        若弹窗未出现（某些时段直接跳过），则检查是否已有其他确认弹窗。
        """
        # 等待 "Preferred Seat" 弹窗出现
        try:
            await self.page.wait_for_selector(
                'text="Preferred Seat"', timeout=6_000
            )
            log.info("  📋 检测到 Preferred Seat 弹窗")
        except PlaywrightTimeout:
            log.info("  ℹ Preferred Seat 弹窗未出现，检查其他确认弹窗...")
            await self._click_confirm_dialog(label_safe)
            return

        await self.snap(f"14b_preferred_seat_dialog_{label_safe}")

        # 弹窗内的座位列表：收集所有可点击的座位选项（跳过 "No preferred seat"）
        # 选项通常是 radio label 或 div.my-2 格式，文字形如 "S74", "S1" 等
        preferred_seat = await self._pick_preferred_seat_in_dialog()
        log.info(f"  🪑 Preferred Seat 弹窗选座: {preferred_seat}")

        await self.snap(f"14c_preferred_seat_selected_{label_safe}")

        # 点 CONFIRM 确认
        confirm_btn = None
        for sel in [
            'button:has-text("CONFIRM")',
            'button:has-text("Confirm")',
            '[role="button"]:has-text("CONFIRM")',
        ]:
            try:
                btn = self.page.locator(sel).first
                if await btn.count() > 0:
                    await btn.wait_for(state="visible", timeout=5_000)
                    confirm_btn = btn
                    break
            except Exception:
                continue

        if confirm_btn:
            await confirm_btn.click()
            log.info("  ✔ Preferred Seat 弹窗：已点击 CONFIRM")
        else:
            # 兜底：文字匹配
            await self.page.get_by_text("CONFIRM", exact=True).first.click()
            log.info("  ✔ Preferred Seat 弹窗：已点击 CONFIRM（兜底）")

        await asyncio.sleep(1.5)
        await self.snap(f"15_confirmed_{label_safe}")

    async def _pick_preferred_seat_in_dialog(self) -> str:
        """
        在 Preferred Seat 弹窗内，按优先级选择座位。

        v9 修复：
          1. 限定在 .v-dialog--active，不再被历史弹窗 DOM 残留污染。
          2. 打印弹窗所有原始文字，方便日志诊断座位标签格式。
          3. 放宽座位号识别：接受 "S74"/"74"/"SEAT74" 等多种格式，
             用黑名单（图书馆名/时间/区域/Duration）过滤明显非座位条目。
        """
        await asyncio.sleep(0.5)

        # ── 找当前活跃弹窗容器 ───────────────────────────────────────────
        active_dialog = None
        for active_sel in [
            '.v-dialog--active',
            '.v-overlay--active .v-dialog',
            '.v-overlay--active [role="dialog"]',
        ]:
            loc = self.page.locator(active_sel)
            if await loc.count() > 0:
                active_dialog = loc.first
                log.info(f"  Preferred Seat 弹窗容器: {active_sel!r}")
                break
        if active_dialog is None:
            active_dialog = self.page.locator(
                '.v-dialog:has-text("Preferred Seat"), [role="dialog"]:has-text("Preferred Seat")'
            ).last
            log.info("  Preferred Seat 弹窗容器（兜底）")

        # ── 黑名单：明确不是座位的条目 ───────────────────────────────────
        import re as _re
        _BLACKLIST_PATTERNS = [
            _re.compile(r'\d{1,2}:\d{2}\s*(AM|PM)', _re.IGNORECASE),  # 时间 "10:00AM"
            _re.compile(r'\d+:\d{2}$'),            # Duration "1:30"
            _re.compile(r'LIBRARY$'),              # 图书馆名
            _re.compile(r'LEVEL\s*\d'),            # 区域含 Level
            _re.compile(r'ZONE'),                  # 区域含 Zone
            _re.compile(r'NO\s+PREFERRED', _re.IGNORECASE),
        ]

        def _is_likely_seat(raw: str) -> bool:
            """粗判断：是否像座位号而非图书馆/时间/区域"""
            u = raw.upper().replace(" ", "")
            for pat in _BLACKLIST_PATTERNS:
                if pat.search(u):
                    return False
            # 接受 "S74" / "74" / "SEAT74" / "S-74" 等
            if _re.search(r'S[-]?\d+', u):
                return True
            if _re.fullmatch(r'\d+', u):
                return True
            # 其他短字符串也暂时保留，日志会打出来
            if len(u) <= 8 and u:
                return True
            return False

        # ── 收集弹窗内所有选项（含滚动扫描）────────────────────────────
        available_in_dialog: dict[str, object] = {}
        all_raw_texts: list[str] = []

        scroll_container = None
        for sc_sel in ['.v-list', '[class*="list"]', '.v-card__text']:
            sc = active_dialog.locator(sc_sel).first
            if await sc.count() > 0:
                scroll_container = sc
                break

        for rel_sel in ['label', '[role="radio"]', '.v-radio', 'div.my-2']:
            if available_in_dialog:
                break
            for scroll_idx in range(16):
                els = active_dialog.locator(rel_sel)
                cnt = await els.count()
                for i in range(cnt):
                    el = els.nth(i)
                    try:
                        txt = (await el.inner_text()).strip()
                        if not txt:
                            continue
                        if txt not in all_raw_texts:
                            all_raw_texts.append(txt)
                        key = txt.upper().replace(" ", "")
                        if key not in available_in_dialog and _is_likely_seat(txt):
                            available_in_dialog[key] = el
                    except Exception:
                        continue

                if scroll_idx >= 15:
                    break
                if scroll_container and await scroll_container.count() > 0:
                    at_bottom = await scroll_container.evaluate(
                        "el => el.scrollTop + el.clientHeight >= el.scrollHeight - 5"
                    )
                    if at_bottom:
                        break
                    await scroll_container.evaluate("el => el.scrollBy(0, 200)")
                else:
                    await active_dialog.evaluate("el => el.scrollBy(0, 200)")
                await asyncio.sleep(0.2)

            if available_in_dialog:
                log.info(f"  📋 弹窗所有原始文字({rel_sel}): {all_raw_texts}")
                log.info(f"  🪑 过滤后候选: {sorted(available_in_dialog.keys())}")
                break

        if not available_in_dialog:
            log.warning(f"  ⚠ 弹窗所有原始文字: {all_raw_texts}")
            log.warning("  ⚠ 未找到座位候选，保持 'No preferred seat'")
            return "AUTO"

        # ── 按优先级选座 ─────────────────────────────────────────────────
        # 标准化：把 "74" 映射成 "S74" 方便与 _TIER1/_TIER2 对比
        def _normalize(k: str) -> str:
            if _re.fullmatch(r'\d+', k):
                return f"S{k}"
            if k.startswith("SEAT"):
                return "S" + k[4:]
            return k

        normalized = {_normalize(k): v for k, v in available_in_dialog.items()}
        log.info(f"  🪑 标准化后: {sorted(normalized.keys())}")

        tier3 = [sid for sid in normalized if sid not in _TIER3_EXCLUDE]
        for tier_label, seats in [
            ("第一序列(S74-S86)", _TIER1),
            ("第二序列(S1-S16)",  _TIER2),
            ("其他座位",          tier3),
        ]:
            candidates = [s for s in seats if s in normalized]
            log.info(f"  🔍 弹窗尝试 {tier_label}，候选: {candidates}")
            for seat_id in candidates:
                el = normalized[seat_id]
                try:
                    await el.scroll_into_view_if_needed()
                    await asyncio.sleep(0.3)
                    await el.click()
                    log.info(f"  ✅ Preferred Seat 弹窗已选: {seat_id}（{tier_label}）")
                    await asyncio.sleep(0.5)
                    return seat_id
                except Exception as e:
                    log.warning(f"  ⚠ 弹窗点击 {seat_id} 失败: {e}")
                    continue

        log.warning("  ⚠ 所有优先级座位均不可用，保持默认")
        return "AUTO"

    async def _click_confirm_dialog(self, label_safe: str):
        """处理普通确认弹窗（OK / Confirm / Yes / CONFIRM）"""
        for word in ["OK", "Confirm", "Yes", "CONFIRM"]:
            try:
                btn = self.page.get_by_text(word, exact=True).first
                await btn.wait_for(state="visible", timeout=4_000)
                await btn.click()
                log.info(f"  ✔ 确认弹窗: {word}")
                await asyncio.sleep(1.5)
                await self.snap(f"15_confirmed_{label_safe}")
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
            不做 is_visible() 过滤——视口外的座位同样需要被发现。
            v9 新增：打印每个命中元素的全部属性，用于诊断 NLB 座位图的 DOM 结构。
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
                        # ── 诊断：打印元素所有属性，帮助识别座位 ID 字段 ──
                        tag      = await el.evaluate("e => e.tagName")
                        cls_val  = await el.get_attribute("class") or ""
                        ds       = await el.get_attribute("data-seat") or ""
                        aria     = await el.get_attribute("aria-label") or ""
                        txt      = (await el.inner_text()).strip()
                        all_attrs = await el.evaluate(
                            "e => Object.fromEntries([...e.attributes].map(a=>[a.name,a.value]))"
                        )
                        log.info(
                            f"  📍 元素#{i}: <{tag}> class={cls_val!r} "
                            f"data-seat={ds!r} aria-label={aria!r} text={txt!r} "
                            f"all_attrs={all_attrs}"
                        )
                        # 获取座位号：优先 data-seat，其次 aria-label，最后 inner_text
                        seat_id = ds or aria or txt
                        if not seat_id:
                            log.info(f"  ⚠ 元素#{i}: 无法读取座位号，跳过")
                            continue
                        seat_id = seat_id.strip().upper()
                        if any(k in cls_val.lower() for k in ("disabled", "unavailable", "booked", "occupied")):
                            log.info(f"  ⚠ 元素#{i} {seat_id}: class 含禁用标记，跳过")
                            continue
                        available[seat_id] = el
                    except Exception as e:
                        log.warning(f"  ⚠ 元素#{i} 处理异常: {e}")
                        continue
                if available:
                    break
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
    log.info(f"=== NLB v6 | {datetime.now(SGT).strftime('%Y-%m-%d %H:%M:%S')} SGT ===")

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
