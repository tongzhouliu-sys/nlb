"""
NLB Seat Booking Automation Script  (v11 — 假成功修复：终极校验 Bookings 列表)
=========================================================================
v11 新增（针对"Actions 显示成功但实际没预约上"）：
  1. 【BOOK按钮误点修复】:has-text("BOOK") 是大小写不敏感的子串匹配，
     会误中底部导航 "Bookings" Tab → 改为 ^BOOK$ 整词精确匹配
  2. 【终极校验】每个时段流程跑完后，打开 Bookings 列表页，
     核对目标日期+时段真实存在；不存在 → 判失败 → 触发重试 → 最终标红
  3. Preferred Seat 弹窗未出现的分支补上结果校验（之前该路径零校验）
  4. _click_confirm_dialog 找不到确认按钮时不再静默跳过，截图+大声警告
  ⇒ 现在 "✅ 成功(已核实)" = Bookings 列表里真的查到了这条预约

v10 继承：
  - Tier0(S74,S86,S1,S17) → Tier1(S74-S86,S1-S17) → Tier2 兜底；
    座位图与弹窗共用 normalize_seat_id + iter_priority_tiers
  - 座位号标准化修复（"74"≠"S74" 的匹配 bug）、evaluate_all 批量读属性
  - 单时段失败自动重试、失败时非零退出码
v8/v9 继承：弹窗限定 .v-dialog--active、座位识别放宽、Duration 候选等
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

# 座位优先级（v10）：
#   Tier 0：最优先的 4 个座位（按顺序尝试）——在下方列表填入具体座位号
#   Tier 1：第一序列 = S74-S86 + S1-S17
#   Tier 2：页面上其余可用座位（运行时动态补充，兜底）
_TIER0 = ["S74", "S86", "S1", "S17"]   # 最优先的 4 个座位，按此顺序尝试
_TIER1 = (
    [f"S{n}" for n in range(74, 87)]   # S74 → S86
    + [f"S{n}" for n in range(1, 18)]  # S1  → S17
)
_TIER1 = [s for s in _TIER1 if s not in _TIER0]   # 去重：Tier0 优先
_KNOWN_SEATS = set(_TIER0) | set(_TIER1)
# 其余座位（Tier 2）在运行时根据页面实际可用座位动态补充


def normalize_seat_id(raw: str) -> str:
    """
    统一座位号格式："74" / "SEAT74" / "S-74" / "s74 " → "S74"
    座位图和 Preferred Seat 弹窗共用，保证两条路径的匹配口径一致。
    """
    import re as _re
    u = (raw or "").strip().upper().replace(" ", "")
    m = _re.search(r'S?E?A?T?-?(\d+)', u)
    if m and (u.isdigit() or u.startswith("S")):
        return f"S{m.group(1)}"
    return u


def iter_priority_tiers(available_ids):
    """
    按优先级生成 (tier名称, 候选列表)。
    available_ids: 当前可用座位号集合（已 normalize）。
    """
    avail = set(available_ids)
    tier2 = [sid for sid in available_ids if sid not in _KNOWN_SEATS]
    yield ("Tier0(最优先4座)",            [s for s in _TIER0 if s in avail])
    yield ("Tier1(S74-S86,S1-S17)",       [s for s in _TIER1 if s in avail])
    yield ("Tier2(其他座位)",             tier2)

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
        self._last_target_date = await self.select_date()   # v11：记录目标日期供终极校验用
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
        # v11 修复：:has-text("BOOK") 是大小写不敏感的【子串】匹配，
        # 会误中底部导航的 "Bookings" Tab —— 改为整词精确匹配
        import re as _re
        _book_exact = _re.compile(r'^\s*BOOK\s*$', _re.IGNORECASE)
        book_btn = self.page.locator('button, [role="button"], .v-btn').filter(
            has_text=_book_exact
        ).first
        await book_btn.wait_for(state="visible", timeout=15_000)
        await self.snap(f"13_booking_details_{s}")
        log.info("  📄 已进入 Booking Details 页")

        # 点 BOOK 按钮
        await book_btn.click()
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
            # v11：这条路径之前没有任何结果校验，是假成功的主要漏洞之一
            await self._verify_booking_result(label_safe)
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
        await self._verify_booking_result(label_safe)

    async def _verify_booking_result(self, label_safe: str):
        """
        v10：CONFIRM 后检查页面结果。
          - 命中错误关键词（fail/error/already booked/not available/limit）→ 抛错，
            触发 main() 的单时段重试。
          - 命中成功关键词 → 记录日志。
          - 都没命中 → warn-only 放行（最终以 verify_booking_exists 为准）。
        """
        await asyncio.sleep(1.0)
        try:
            body_text = (await self.page.locator("body").inner_text()).lower()
        except Exception:
            return
        err_words = ["unsuccessful", "failed", "error occurred", "already booked",
                     "not available", "no longer available", "exceeded", "limit reached"]
        ok_words  = ["success", "confirmed", "booking reference", "has been booked"]

        hit_err = [w for w in err_words if w in body_text]
        if hit_err:
            await self.snap(f"ERR_result_{label_safe}")
            raise RuntimeError(f"预约结果页含错误提示: {hit_err}")
        hit_ok = [w for w in ok_words if w in body_text]
        if hit_ok:
            log.info(f"  ✅ 预约结果验证通过（命中: {hit_ok}）")
        else:
            log.warning("  ⚠ 结果页未见明确成功/失败文案（最终以 Bookings 列表核实为准）")

    # ── 终极校验：去 Bookings 列表核实预约真实存在（v11）──────────────────
    async def verify_booking_exists(self, time_str: str, label_safe: str) -> bool:
        """
        打开底部导航 Bookings Tab，读取列表页全文，
        同时核对【目标日期】和【时段】是否出现。
        这是判定成功的最终标准——杜绝"流程跑完但实际没订上"的假成功。
        """
        log.info("▶ 终极校验：核实 Bookings 列表...")
        try:
            await self.page.goto(BASE_URL, wait_until="load")
            await asyncio.sleep(2)
            tab = self.page.locator(
                '.v-btn__content:has-text("Booking"), '
                'span:has-text("Booking"), button:has-text("Booking")'
            ).first
            await tab.wait_for(state="visible", timeout=10_000)
            await tab.click()
            try:
                await self.page.wait_for_load_state("load", timeout=15_000)
            except PlaywrightTimeout:
                pass
            await asyncio.sleep(2)
        except Exception as e:
            log.warning(f"  ⚠ 打开 Bookings 列表失败: {e}")
            await self.snap(f"ERR_open_bookings_{label_safe}")
            return False

        await self.snap(f"16_my_bookings_{label_safe}")

        try:
            body = await self.page.locator("body").inner_text()
        except Exception:
            return False
        body_l = body.lower()

        # 日期匹配：兼容 "13 Jun" / "13 June" / "Jun 13" / "13/06/2026" / "2026-06-13"
        t = self._last_target_date
        date_variants = [
            f"{t.day} {t.strftime('%b')}",  f"{t.day:02d} {t.strftime('%b')}",
            f"{t.day} {t.strftime('%B')}",  f"{t.strftime('%b')} {t.day}",
            f"{t.strftime('%B')} {t.day}",
            t.strftime("%d/%m/%Y"),         t.strftime("%Y-%m-%d"),
        ]
        has_date = any(v.lower() in body_l for v in date_variants)

        # 时段匹配："10:00 am" → "10:00" / "10.00" / "10:00am"
        hm = time_str.split()[0]            # "10:00"
        has_time = (hm in body) or (hm.replace(":", ".") in body) \
                   or (time_str.replace(" ", "").lower() in body_l.replace(" ", ""))

        log.info(f"  📋 Bookings 列表核对: 日期命中={has_date} 时段命中={has_time} "
                 f"(目标 {t.strftime('%d %b %Y')} {time_str})")
        return has_date and has_time

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

        # ── 按优先级选座（v10：与座位图共用 normalize + tier 逻辑）──────
        normalized = {normalize_seat_id(k): v for k, v in available_in_dialog.items()}
        log.info(f"  🪑 标准化后: {sorted(normalized.keys())}")

        for tier_label, candidates in iter_priority_tiers(list(normalized.keys())):
            log.info(f"  🔍 弹窗尝试 {tier_label}，候选: {candidates}")
            for seat_id in candidates:
                el = normalized[seat_id]
                try:
                    await el.scroll_into_view_if_needed(timeout=5_000)
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
        clicked = False
        for word in ["OK", "Confirm", "Yes", "CONFIRM"]:
            try:
                btn = self.page.get_by_text(word, exact=True).first
                await btn.wait_for(state="visible", timeout=4_000)
                await btn.click()
                log.info(f"  ✔ 确认弹窗: {word}")
                clicked = True
                await asyncio.sleep(1.5)
                await self.snap(f"15_confirmed_{label_safe}")
                break
            except PlaywrightTimeout:
                continue
        if not clicked:
            # v11：之前这里静默跳过，是假成功的隐患之一
            log.warning("  ⚠ 未找到任何确认按钮（OK/Confirm/Yes），可能根本没触发预约提交！")
            await self.snap(f"WARN_no_confirm_btn_{label_safe}")

    # ── 按优先级选座 ──────────────────────────────────────────────────────
    async def _pick_seat_by_priority(self, label_safe: str) -> str:
        """
        座位优先级（v10）：Tier0(最优先4座) → Tier1(S74-S86, S1-S17) → Tier2(其他)

        v10 修复：
          1. 【关键BUG】收集到的座位号现在统一 normalize_seat_id()，
             之前 "74"/"Seat 74" 等格式永远匹配不上 "S74"，导致只能走兜底。
          2. 属性读取改为单次 evaluate_all 批量获取，原来每个元素 5+ 次
             round-trip，大座位图下耗时数分钟、易超时。
          3. 点击后校验 class 是否变为 selected/active（warn-only），
             点击未生效时自动重试一次。

        返回实际选中的座位号；若无任何可用座位则返回 "AUTO"。
        """
        # ── 收集页面上所有可用（未禁用）座位元素 ────────────────────────
        async def _get_available_seats() -> dict[str, object]:
            """返回 {normalize后座位号: locator}。批量读属性，单次 round-trip。"""
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

                # 一次性批量读取所有元素的关键属性（v10：替代逐元素 evaluate）
                try:
                    infos = await els.evaluate_all(
                        """els => els.map(e => ({
                            tag:  e.tagName,
                            cls:  e.getAttribute('class') || '',
                            ds:   e.getAttribute('data-seat') || '',
                            aria: e.getAttribute('aria-label') || '',
                            txt:  (e.innerText || '').trim(),
                        }))"""
                    )
                except Exception as e:
                    log.warning(f"  ⚠ 批量读取属性失败: {e}")
                    continue

                for i, info in enumerate(infos):
                    raw = info["ds"] or info["aria"] or info["txt"]
                    if not raw:
                        continue
                    seat_id = normalize_seat_id(raw)
                    cls_low = info["cls"].lower()
                    if any(k in cls_low for k in ("disabled", "unavailable", "booked", "occupied", "selected")):
                        continue
                    if seat_id not in available:
                        available[seat_id] = els.nth(i)
                        log.info(
                            f"  📍 元素#{i}: <{info['tag']}> {seat_id} "
                            f"(raw={raw!r} class={info['cls']!r})"
                        )
                if available:
                    break
            return available

        available = await _get_available_seats()
        log.info(f"  📋 可用座位数: {len(available)}  全部: {sorted(available.keys())}")

        if not available:
            log.warning("  ⚠ 未检测到可用座位，尝试直接进入预约详情页（系统自动分配）")
            return "AUTO"

        # ── 按优先级逐个尝试 ────────────────────────────────────────────
        for tier_label, candidates in iter_priority_tiers(list(available.keys())):
            log.info(f"  🔍 尝试 {tier_label}，候选: {candidates}")
            for seat_id in candidates:
                el = available[seat_id]
                for attempt in (1, 2):   # 点击未生效时自动重试一次
                    try:
                        await el.scroll_into_view_if_needed(timeout=5_000)
                        await asyncio.sleep(0.3)
                        await el.click()
                        await asyncio.sleep(0.5)
                        # 校验点击是否生效（warn-only：class 出现 selected/active）
                        try:
                            cls_after = (await el.get_attribute("class")) or ""
                            if any(k in cls_after.lower() for k in ("selected", "active", "chosen")):
                                log.info(f"  ✔ 点击已生效（class={cls_after!r}）")
                            elif attempt == 1:
                                log.warning(f"  ⚠ class 未见选中标记（{cls_after!r}），重试点击...")
                                continue
                        except Exception:
                            pass
                        log.info(f"  ✅ 已选座位: {seat_id}（{tier_label}）")
                        await asyncio.sleep(0.3)
                        await self.snap(f"seat_selected_{seat_id}_{label_safe}")
                        return seat_id
                    except Exception as e:
                        log.warning(f"  ⚠ 点击 {seat_id} 失败(attempt {attempt}): {e}")
                        break   # 元素本身点不动，换下一个座位

        raise RuntimeError("所有优先级座位均不可用，预约失败")


# ═════════════════════════════════════════════════════════════════════════
MAX_RETRIES_PER_SLOT = 2   # 每个时段最多尝试次数（含首次）


async def main():
    log.info(f"=== NLB v10 | {datetime.now(SGT).strftime('%Y-%m-%d %H:%M:%S')} SGT ===")
    log.info(f"座位优先级 Tier0: {_TIER0 or '(未配置)'}  Tier1: {_TIER1[0]}…{_TIER1[-1]} 共{len(_TIER1)}个")

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
                # v10：单时段失败自动重试（页面状态可能残留，先回主页再重来）
                last_err = None
                for attempt in range(1, MAX_RETRIES_PER_SLOT + 1):
                    try:
                        if attempt > 1:
                            log.warning(f"🔁 {label} 第 {attempt} 次尝试（共{MAX_RETRIES_PER_SLOT}次）...")
                            await page.goto(BASE_URL, wait_until="load")
                            await asyncio.sleep(2)
                        seat = await booker.book_one_slot(label, time_str, dur_str)

                        # v11 终极校验：Bookings 列表里必须真的有这条预约
                        verified = await booker.verify_booking_exists(time_str, _safe(label))
                        if not verified:
                            raise RuntimeError(
                                "流程跑完但 Bookings 列表未找到该预约（假成功），"
                                "请查截图 16_my_bookings"
                            )
                        results.append((label, f"✅ 成功(已核实)  座位: {seat}"))
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        log.error(f"❌ {label} 第 {attempt} 次失败: {e}")
                        await booker.snap(f"ERR_{_safe(label)}_attempt{attempt}")
                if last_err is not None:
                    results.append((label, f"❌ 失败: {last_err}"))
                await asyncio.sleep(3)

            log.info("\n" + "=" * 60)
            log.info("📋 预约结果汇总:")
            for label, status in results:
                log.info(f"  {label}  →  {status}")
            log.info("=" * 60)

            # 任一时段最终失败 → 非零退出码，让 GitHub Actions 标红便于发现
            if any(status.startswith("❌") for _, status in results):
                raise SystemExit(1)

        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
