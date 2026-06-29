"""
NLB Seat Booking Automation Script  (v12 — 依据实机截图重写选座流程)
=========================================================================
v12 核心修正（基于真实手机截图，纠正了对 NLB UI 的两个错误认知）：
  1. 【真实流程】区域卡片 → 直接进 Booking Details（没有座位图页！）
     → 点 BOOK → "Selection" 对话框 → 必须先点 "Select seat" 输入框
     → 弹出 "Seat" 单选列表（Any available seat / S3 / S7...，可滚动）
     → 点圆圈选中 → 回 Selection 框等 CONFIRM 由灰变蓝 → 点击
     之前脚本在等永远不会出现的 "Preferred Seat" 文字 → 假成功根因
  2. 【12:00pm 误订修复】_pick_dialog_radio 的 :has-text() 是子串匹配，
     "12:00 pm" 包含 "2:00 pm" → 下午时段被订成中午！改为 ^...$ 精确匹配
  3. 【BOOK 前核对】Booking Details 页核对日期+起始时段，不符即抛错重试
     （结束时间不符仅警告——Duration 没选上 1.5h 时能从日志看出来）
  4. 选座兜底：优先序列都不在列表 → 选 "Any available seat"（用户指定）
  5. CONFIRM 必须确认已脱离禁用态才点击，仍禁用则抛错（选座未生效）
  6. 移除基于错误 UI 认知的死代码（座位图扫描、Preferred Seat 弹窗处理）

v11 继承：^BOOK$ 防误中 Bookings 导航；终极校验 verify_booking_exists
          （Bookings 列表查到真实预约才算成功）；失败非零退出码标红
v10 继承：Tier0(S74,S86,S1,S17) → Tier1(S74-S86,S1-S17)；normalize_seat_id；
          单时段自动重试
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
FEISHU_WEBHOOK_URL = os.environ.get("FEISHU_WEBHOOK_URL", "")


def send_feishu_notification(title: str, content_markdown: str, is_success: bool = True):
    webhook_url = FEISHU_WEBHOOK_URL.strip()
    if not webhook_url:
        log.info("ℹ 未配置 FEISHU_WEBHOOK_URL，跳过飞书推送。")
        return
    import json
    import urllib.request
    card_template = "green" if is_success else "red"
    payload = {
        "msg_type": "interactive",
        "card": {
            "header": {
                "title": {"tag": "plain_text", "content": title},
                "template": card_template
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": content_markdown}
                }
            ]
        }
    }
    try:
        req = urllib.request.Request(
            webhook_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            log.info("📱 飞书推送成功")
    except Exception as e:
        log.error(f"❌ 飞书推送失败: {e}")

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


BOOKING_DATE_OFFSET = int(os.environ.get("BOOKING_DATE_OFFSET", "1"))

# (显示标签, Time弹窗选项文字, Duration弹窗选项文字)
# Duration 候选列表：NLB 页面可能显示多种格式，按顺序尝试
TIME_SLOTS = [
    ("10:00–11:30", "10:00 am", ["1:00", "1 hour", "60 mins", "1 hr", "1:30", "1 hour 30 mins", "90 mins", "1.5 hours", "1 hr 30 min"]),
    ("14:00–15:30", "2:00 pm",  ["1:00", "1 hour", "60 mins", "1 hr", "1:30", "1 hour 30 mins", "90 mins", "1.5 hours", "1 hr 30 min"]),
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
        # v12 修复：:has-text() 是子串匹配，"12:00 pm" 会命中 "2:00 pm"！
        # 改为 ^...$ 整词精确匹配（这就是下午时段被订成 12:00 pm 的根因）
        import re as _re
        _exact = _re.compile(rf'^\s*{_re.escape(option_text)}\s*$', _re.IGNORECASE)
        for base_sel in ['label', '.v-radio', '[role="radio"]', '.v-list-item']:
            try:
                el = dialog_root.locator(base_sel).filter(has_text=_exact).first
                if await el.count() > 0:
                    await el.scroll_into_view_if_needed(timeout=3_000)
                    await asyncio.sleep(0.2)
                    await el.click()
                    log.info(f"  ✔ 已选(精确): {option_text}（{base_sel}）")
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

        await self._click_area_and_book(label, time_str)
        log.info(f"🎉 {label} 预约完成！")
        return TARGET_AREA
    # ── 点击区域结果卡片并完成预约 ──────────────────────────────────────
    async def _click_area_and_book(self, label: str, time_str: str):
        """
        真实流程（v12，依据实机截图修正）：
          Search Results 页点 TARGET_AREA 卡片
          → 直接进入 Booking Details 页（没有座位图页！）
          → 核对页面显示的日期/时段（防止 Time/Duration 选择悄悄失效）
          → 点 BOOK → 弹 "Selection" 对话框（含 "Select seat" 输入框）
          → 点 "Select seat" → 弹 "Seat" 单选列表 → 按优先级选座
          → 回 Selection 框点 CONFIRM（选座后才会从灰色变可点）
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

        # 等 Booking Details 页面 + BOOK 按钮（^BOOK$ 整词，防误中 Bookings 导航）
        import re as _re
        _book_exact = _re.compile(r'^\s*BOOK\s*$', _re.IGNORECASE)
        book_btn = self.page.locator('button, [role="button"], .v-btn').filter(
            has_text=_book_exact
        ).first
        await book_btn.wait_for(state="visible", timeout=15_000)
        await self.snap(f"13_booking_details_{s}")
        log.info("  📄 已进入 Booking Details 页")

        # ── v12：点 BOOK 前核对页面显示的日期与时段 ──────────────────────
        await self._verify_booking_details(label, time_str)

        # 点 BOOK 按钮
        await book_btn.click()
        log.info("  ✔ 已点击 BOOK 按钮")
        await asyncio.sleep(1.5)
        await self.snap(f"14_after_book_{s}")

        # ── 处理 "Selection" 选座对话框 ──────────────────────────────────
        await self._handle_selection_dialog(s)

    # ── 点 BOOK 前核对 Booking Details（v12）────────────────────────────
    async def _verify_booking_details(self, label: str, time_str: str):
        """
        核对 Booking Details 页文字：
          - 起始时间（如 "10:00 am"）必须出现，否则说明 Time 选择失效
            （实测曾被订成 12:00 pm，因 :has-text 子串匹配 bug）→ 抛错触发重试
          - 结束时间（label 里的 "11:30" 等）未出现 → 仅警告（Duration 可能不支持）
          - 目标日期未出现 → 抛错
        """
        body = await self.page.locator("body").inner_text()
        body_l = body.lower()

        t = self._last_target_date
        date_ok = any(v.lower() in body_l for v in [
            f"{t.day} {t.strftime('%b')}", f"{t.day:02d} {t.strftime('%b')}",
            f"{t.day} {t.strftime('%B')}", f"{t.strftime('%B')} {t.day}",
        ])
        if not date_ok:
            raise RuntimeError(f"Booking Details 日期不符（期望 {t.strftime('%d %b %Y')}），中止本次")

        if time_str.lower() not in body_l:
            raise RuntimeError(
                f"Booking Details 起始时间不符（期望 {time_str!r}），"
                f"Time 选择可能失效，中止本次以触发重试"
            )

        # 结束时间：label 形如 "10:00–11:30"，取 "–" 后半段
        end_hm = label.split("–")[-1].strip() if "–" in label else None
        if end_hm:
            # "11:30" / "15:30"→"3:30" 两种写法都试
            h, m = end_hm.split(":")
            variants = {end_hm, f"{int(h) % 12 or 12}:{m}"}
            if not any(v in body for v in variants):
                log.warning(f"  ⚠ Booking Details 结束时间未见 {variants}（Duration 可能没选上 1.5h，请查截图 13）")

        log.info(f"  ✅ Booking Details 核对通过: {t.strftime('%d %b')} {time_str}")

    # ── 处理 "Selection" 选座对话框（v12，依据实机截图重写）────────────
    async def _handle_selection_dialog(self, label_safe: str):
        """
        点 BOOK 后弹出 "Selection" 对话框：
          1. 必须先点 "Select seat" 输入框 → 才会弹出 "Seat" 单选列表
          2. "Seat" 列表可滚动，选项形如 Any available seat / S3 / S7 / S14...
             点中圆圈即自动选中（列表随之关闭）
          3. 优先级座位都不在列表里 → 选最上面的 "Any available seat"
          4. 回到 Selection 框，CONFIRM 由灰变蓝后点击
        """
        # 等 Selection 对话框出现
        # v13 关键修复："Select seat" 是输入框的【占位符/label】，不是文本节点，
        # text= 选择器永远匹配不到 —— 这就是上次日志"未见 Selection 对话框"
        # 但截图明明有的原因！改用对话框标题 "Selection"（真实文本）检测。
        dialog_seen = False
        for det_sel in [
            '.v-dialog--active:has-text("Selection")',
            'text="Selection"',
            '.v-dialog--active:has-text("CONFIRM")',
        ]:
            try:
                await self.page.wait_for_selector(det_sel, timeout=4_000)
                dialog_seen = True
                log.info(f"  📋 检测到 Selection 对话框（{det_sel}）")
                break
            except PlaywrightTimeout:
                continue

        if not dialog_seen:
            log.warning("  ⚠ 未见 Selection 对话框，检查其他确认弹窗...")
            await self._click_confirm_dialog(label_safe)
            await self._verify_booking_result(label_safe)
            return

        await self.snap(f"14b_selection_dialog_{label_safe}")

        # 步骤1：点击 "Select seat" 字段，打开 Seat 单选列表
        # v13："Select seat" 是占位符 → text= 点不到，改用 input/placeholder/
        # v-select 等结构选择器，每轮换一个选择器点击，点完检查列表是否弹出
        click_sels = [
            '.v-dialog--active [placeholder*="seat" i]',
            '.v-dialog--active input',
            '.v-dialog--active .v-select',
            '.v-dialog--active .v-input__slot',
            '.v-dialog--active .v-text-field',
            '.v-dialog--active label:has-text("Select seat")',
        ]
        seat_list_opened = False
        for attempt in range(len(click_sels)):
            sel = click_sels[attempt]
            try:
                el = self.page.locator(sel).first
                if await el.count() == 0:
                    log.info(f"  ↳ 选择器无命中，跳过: {sel}")
                    continue
                await el.click()
                log.info(f"  ✔ 已点击 Select seat 字段（{sel}）")

                # Seat 列表的标志：出现 "Any available seat"（radio 标签是真实文本）
                try:
                    await self.page.wait_for_selector(
                        'text="Any available seat"', timeout=4_000
                    )
                    seat_list_opened = True
                    log.info("  ✅ Seat 单选列表已打开")
                    break
                except PlaywrightTimeout:
                    log.warning(f"  ⚠ 点击后 Seat 列表未弹出，换下一个选择器...")
                    await asyncio.sleep(0.5)
                    continue
            except Exception as e:
                log.warning(f"  ⚠ 点击 {sel} 异常: {e}")
                continue

        if not seat_list_opened:
            await self.snap(f"ERR_seat_list_not_open_{label_safe}")
            raise RuntimeError("点击 Select seat 多次后仍未弹出 Seat 列表，请查截图")

        await self.snap(f"14c_seat_list_{label_safe}")

        # 步骤2：在 Seat 列表中按优先级选座
        chosen = await self._pick_seat_in_seat_dialog()
        log.info(f"  🪑 Seat 列表已选: {chosen}")
        await asyncio.sleep(0.8)
        await self.snap(f"14d_seat_chosen_{label_safe}")

        # 步骤3：等 CONFIRM 由禁用变可点，然后点击
        import re as _re
        confirm = self.page.locator(
            '.v-dialog--active button, .v-dialog--active .v-btn'
        ).filter(has_text=_re.compile(r'^\s*CONFIRM\s*$', _re.IGNORECASE)).first
        if await confirm.count() == 0:
            confirm = self.page.get_by_text("CONFIRM", exact=True).first

        enabled = False
        for _ in range(20):   # 最多等 10s
            try:
                dis_attr = await confirm.get_attribute("disabled")
                cls = (await confirm.get_attribute("class")) or ""
                if dis_attr is None and "disabled" not in cls.lower():
                    enabled = True
                    break
            except Exception:
                pass
            await asyncio.sleep(0.5)
        if not enabled:
            await self.snap(f"ERR_confirm_still_disabled_{label_safe}")
            raise RuntimeError("选座后 CONFIRM 按钮仍是禁用态（选座未生效），请查截图")

        await confirm.click()
        log.info("  ✔ 已点击 CONFIRM（按钮已启用）")
        await asyncio.sleep(1.5)
        await self.snap(f"15_confirmed_{label_safe}")
        await self._verify_booking_result(label_safe)

    async def _pick_seat_in_seat_dialog(self) -> str:
        """
        在 "Seat" 单选列表中按优先级选座：
          Tier0(S74,S86,S1,S17) → Tier1(S74-S86, S1-S17 其余)
          → 都没有 → "Any available seat"（用户指定的兜底）
        列表可滚动：边滚边收集，滚到底为止。
        """
        # 活跃弹窗 = Seat 列表（最后弹出的那个）
        dialog = None
        for sel in ['.v-dialog--active', '.v-overlay--active .v-dialog', '[role="dialog"]']:
            loc = self.page.locator(sel)
            n = await loc.count()
            if n > 0:
                dialog = loc.nth(n - 1)   # 取最后一个（最新弹出）
                break
        if dialog is None:
            dialog = self.page.locator('.v-dialog').last

        # 滚动收集所有选项
        options: dict[str, object] = {}   # {normalize后座位号或"ANY": locator}
        scroll_container = None
        for sc_sel in ['.v-card__text', '.v-list', '[class*="list"]']:
            sc = dialog.locator(sc_sel).first
            if await sc.count() > 0:
                scroll_container = sc
                break

        for _ in range(20):
            for rel_sel in ['.v-radio', '[role="radio"]', 'label', '.v-list-item']:
                els = dialog.locator(rel_sel)
                cnt = await els.count()
                if cnt == 0:
                    continue
                for i in range(cnt):
                    el = els.nth(i)
                    try:
                        txt = (await el.inner_text()).strip()
                    except Exception:
                        continue
                    if not txt:
                        continue
                    if "any available" in txt.lower():
                        options.setdefault("ANY", el)
                    else:
                        sid = normalize_seat_id(txt)
                        if sid and sid not in options:
                            options[sid] = el
                if cnt > 0:
                    break
            # 滚动一屏，滚到底就停
            if scroll_container and await scroll_container.count() > 0:
                at_bottom = await scroll_container.evaluate(
                    "el => el.scrollTop + el.clientHeight >= el.scrollHeight - 5"
                )
                if at_bottom:
                    break
                await scroll_container.evaluate("el => el.scrollBy(0, 250)")
                await asyncio.sleep(0.25)
            else:
                break

        seat_ids = sorted(k for k in options if k != "ANY")
        log.info(f"  📋 Seat 列表选项: {seat_ids}  ANY={'ANY' in options}")

        # 按优先级点击：Tier0 → Tier1 → Any available seat
        for tier_label, candidates in [
            ("Tier0(最优先4座)",      [s for s in _TIER0 if s in options]),
            ("Tier1(S74-S86,S1-S17)", [s for s in _TIER1 if s in options]),
        ]:
            log.info(f"  🔍 {tier_label} 候选: {candidates}")
            for sid in candidates:
                try:
                    el = options[sid]
                    await el.scroll_into_view_if_needed(timeout=5_000)
                    await asyncio.sleep(0.3)
                    await el.click()
                    log.info(f"  ✅ 已点选 {sid}（{tier_label}）")
                    await asyncio.sleep(0.6)
                    return sid
                except Exception as e:
                    log.warning(f"  ⚠ 点击 {sid} 失败: {e}")
                    continue

        # 兜底：Any available seat（用户指定）
        if "ANY" in options:
            log.warning("  ⚠ 优先序列座位均不在列表，选 'Any available seat'")
            el = options["ANY"]
            await el.scroll_into_view_if_needed(timeout=5_000)
            await el.click()
            await asyncio.sleep(0.6)
            return "Any available seat"

        raise RuntimeError(f"Seat 列表无法选座，已见选项: {seat_ids}")

    async def _verify_booking_result(self, label_safe: str):
        """
        v14：CONFIRM 后检查页面结果。
          - 命中错误关键词时改发 warning 提示，不阻断流程，
            最终由 verify_booking_exists 登录 My Bookings 列表做真实性核查（彻底解决页面静态/隐藏文本导致的误报）。
        """
        await asyncio.sleep(1.0)
        try:
            body_text = (await self.page.locator("body").inner_text()).lower()
        except Exception:
            return
        err_words = ["booking unsuccessful", "unsuccessful booking", "booking was unsuccessful",
                     "booking failed", "failed to book", "submission failed",
                     "error occurred", "already booked", "not available", "no longer available",
                     "exceeded", "limit reached"]
        ok_words  = ["booking reference", "has been booked", "booked successfully",
                     "booking confirmed", "booking successful"]

        hit_err = [w for w in err_words if w in body_text]
        hit_ok = [w for w in ok_words if w in body_text]

        if hit_ok:
            log.info(f"  ✅ 预约结果页面验证通过（命中: {hit_ok}）")
        elif hit_err:
            log.warning(f"  ⚠ 结果页检测到提示词 {hit_err}（可能是页面静态文案/历史残留），将交由 Bookings 列表做终极核查...")
            await self.snap(f"WARN_result_{label_safe}")
        else:
            log.warning("  ⚠ 结果页未见明确成功/失败文案（最终以 Bookings 列表核实为准）")

    # ── 终极校验：去 Bookings 列表核实预约真实存在（v11）──────────────────
    async def verify_booking_exists(self, time_str: str, label_safe: str):
        """
        打开底部导航 Bookings Tab，读取列表页全文，
        同时核对【目标日期】和【时段】是否出现。
        返回三态：True=确认成功 / False=确认失败 / None=无法核实（导航/渲染异常）
        None 时视为预订流程本身已完成，不触发失败。
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
            log.warning(f"  ⚠ 打开 Bookings 列表失败（无法核实）: {e}")
            await self.snap(f"ERR_open_bookings_{label_safe}")
            return None  # 无法核实，不等于失败

        await self.snap(f"16_my_bookings_{label_safe}")

        # 最多重试3次，等待列表渲染完成
        body = ""
        for attempt in range(1, 4):
            try:
                body = await self.page.locator("body").inner_text()
            except Exception:
                body = ""
            if body.strip():
                break
            log.info(f"  ⏳ Bookings 列表内容为空，等待渲染（第{attempt}次）...")
            await asyncio.sleep(3)
        else:
            log.warning("  ⚠ Bookings 列表始终无内容，无法核实")
            return None  # 无法核实，不等于失败

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

# ═════════════════════════════════════════════════════════════════════════
MAX_RETRIES_PER_SLOT = 2   # 每个时段最多尝试次数（含首次）


async def main():
    log.info(f"=== NLB v13 | {datetime.now(SGT).strftime('%Y-%m-%d %H:%M:%S')} SGT ===")
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

            # 第一阶段：依次预订所有时段，记录座位号，不做 Bookings 列表校验
            booked = []   # [(label, time_str, seat)]
            results = []  # 最终结果（含失败项）
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
                        booked.append((label, time_str, seat))
                        last_err = None
                        break
                    except Exception as e:
                        last_err = e
                        log.error(f"❌ {label} 第 {attempt} 次失败: {e}")
                        await booker.snap(f"ERR_{_safe(label)}_attempt{attempt}")
                if last_err is not None:
                    results.append((label, f"❌ 失败: {last_err}"))
                await asyncio.sleep(3)

            # 第二阶段：所有时段预订完成后，等待10秒再统一做 Bookings 列表校验
            if booked:
                log.info(f"⏳ 所有时段预订完毕，等待 10 秒后统一核实 Bookings 列表...")
                await asyncio.sleep(10)
                for label, time_str, seat in booked:
                    verified = await booker.verify_booking_exists(time_str, _safe(label))
                    if verified is True:
                        results.append((label, f"✅ 成功(已核实)  座位: {seat}"))
                    elif verified is None:
                        log.warning(f"  ⚠ {label} 无法核实 Bookings 列表，但预约流程已完成，视为成功")
                        results.append((label, f"⚠️ 成功(待核实)  座位: {seat}"))
                    else:
                        log.error(f"❌ {label} Bookings 列表未找到该预约（假成功），请查截图 16_my_bookings")
                        results.append((label, f"❌ 失败: Bookings 列表未找到该预约"))

            log.info("\n" + "=" * 60)
            log.info("📋 预约结果汇总:")
            for label, status in results:
                log.info(f"  {label}  →  {status}")
            log.info("=" * 60)

            # 飞书消息推送（✅ 已核实 或 ⚠️ 待核实 均视为成功推送）
            success_count = sum(1 for _, status in results if "✅" in status or "⚠️" in status)
            if success_count > 0:
                target_date = (datetime.now(SGT) + timedelta(days=BOOKING_DATE_OFFSET)).strftime("%Y-%m-%d")
                msg_lines = [
                    f"**📅 预约日期**：{target_date}",
                    f"**📍 图书馆**：{TARGET_LIBRARY} ({TARGET_AREA})",
                    "**📋 预约详情**："
                ]
                for label, status in results:
                    msg_lines.append(f"- **{label}**：{status}")
                
                send_feishu_notification(
                    title="🪑 NLB 图书馆座位预约成功！",
                    content_markdown="\n".join(msg_lines),
                    is_success=True
                )

            # 任一时段预订流程本身失败（❌）→ 非零退出码，让 GitHub Actions 标红
            # ⚠️ 待核实（验证步骤异常但预订流程完成）不触发失败
            if any(status.startswith("❌") for _, status in results):
                raise SystemExit(1)

        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
