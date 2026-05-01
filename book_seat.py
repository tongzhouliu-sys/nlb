"""
NLB Seat Booking Automation Script  (v5.1 — 严格锁定目标 + Preferred Seat 强过滤)
修复说明：
1. 图书馆/区域改为严格匹配，未找到直接报错，绝不兜底选错
2. Preferred Seat 弹窗增加 ^S\d+$ 正则过滤，彻底屏蔽图书馆/时间干扰项
3. 放宽座位 class 过滤条件，解决“匹配到元素但可用数为0”的误杀问题
"""
import os
import re
import asyncio
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeout

# ── 配置区 ────────────────────────────────────────────────────────────────
NLB_USERNAME = os.environ["NLB_USERNAME"]
NLB_PASSWORD = os.environ["NLB_PASSWORD"]

# 🔒 严格锁定：程序将只尝试这两个目标，找不到直接报错
TARGET_LIBRARY = "Punggol Library"
TARGET_AREA    = "Study Zone, Level 3"

# 座位优先级：第一序列 S74-S86，第二序列 S1-S16，其余兜底
_TIER1 = [f"S{n}" for n in range(74, 87)]    # S74 → S86
_TIER2 = [f"S{n}" for n in range(1, 17)]     # S1  → S16
_TIER3_EXCLUDE = set(_TIER1) | set(_TIER2)
SEAT_CANDIDATES = _TIER1 + _TIER2            # 静态已知候选

BOOKING_DATE_OFFSET = int(os.environ.get("BOOKING_DATE_OFFSET", "1"))

# (显示标签, Time弹窗选项文字, Duration弹窗候选列表)
TIME_SLOTS = [
    ("10:00–11:30",  "10:00 am",  ["1 hour 30 mins", "1:30", "90 mins", "1.5 hours", "1 hr 30 min"]),
    ("14:00–15:30",  "2:00 pm",   ["1 hour 30 mins", "1:30", "90 mins", "1.5 hours", "1 hr 30 min"]),
]

BASE_URL  = "https://www.nlb.gov.sg/seatbooking"
SGT       = ZoneInfo("Asia/Singapore")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

def safe(s: str) -> str:
    return s.replace(":", "").replace("–", "").replace(" ", "")

# ═════════════════════════════════════════════════════════════════════════
class NLBBooker:
    def __init__(self, page):
        self.page = page

    async def snap(self, name: str):
        path = f"screenshots/{name}.png"
        os.makedirs("screenshots", exist_ok=True)
        await self.page.screenshot(path=path, full_page=True)
        log.info(f"📸 {path}")

    # ── 登录 ──────────────────────────────────────────────────────────────
    async def login(self):
        log.info("▶ 打开主页...")
        await self.page.goto(BASE_URL, wait_until="load")
        await asyncio.sleep(2)
        await self.snap("01_home")

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

        if "signin.nlb.gov.sg" in self.page.url or "login" in self.page.url.lower():
            log.info("▶ 检测到登录页，填写账号密码...")
            try:
                await self.page.wait_for_selector('input[type="password"]', timeout=15_000)
            except PlaywrightTimeout:
                await self.snap("ERR_no_password_field")
                raise RuntimeError("登录页未找到 password 字段，请检查截图")

            for u_sel in [
                'input[name="username"]', 'input[name="userId"]',
                'input[placeholder*="username" i]', 'input[placeholder*="myLibrary" i]',
            ]:
                try:
                    el = self.page.locator(u_sel).first
                    if await el.count() > 0:
                        await el.fill(NLB_USERNAME)
                        log.info(f"  ✔ 用户名已填（{u_sel}）")
                        break
                except Exception:
                    continue

            await self.page.fill('input[type="password"]', NLB_PASSWORD)
            await self.snap("03_creds_filled")

            for sel in [
                'button:has-text("CONTINUE")', 'button:has-text("Continue")',
                'form:has(input[type="password"]) button[type="submit"]',
                'button[type="submit"]', 'input[type="submit"]',
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

        if "signin.nlb.gov.sg" in self.page.url:
            raise RuntimeError("登录失败！请检查账号密码和截图 03_creds_filled.png。")

        await self.snap("05_logged_in_account")
        log.info("✅ 登录成功")

    # ── 打开 New Booking 页 ───────────────────────────────────────────────
    async def navigate_to_new_booking(self):
        log.info("▶ 打开新建预约页...")
        await self.page.goto(BASE_URL, wait_until="load")
        await asyncio.sleep(2)
        try:
            await self.page.locator('.v-btn__content:has-text("New")').first.click()
            try:
                await self.page.wait_for_load_state("load", timeout=15_000)
            except PlaywrightTimeout:
                pass
        except PlaywrightTimeout:
            pass
        await self.page.wait_for_selector('.inputPopupSelectDiv', timeout=20_000)
        await self.snap("04_new_booking")

    # ═══════════════════════════════════════════════════════════════════════
    # 通用弹窗方法（严格匹配，绝不兜底）
    # ═══════════════════════════════════════════════════════════════════════

    async def _open_popup(self, field_label: str):
        trigger = self.page.locator('.inputPopupSelectDiv').filter(has_text=field_label).first
        await trigger.wait_for(state="visible", timeout=10_000)
        await trigger.click()
        await self.page.wait_for_selector('div.my-2', timeout=10_000)
        await asyncio.sleep(0.3)

    async def _pick_option(self, option_text: str):
        items = self.page.locator('div.my-2')
        cnt = await items.count()
        for i in range(cnt):
            txt = (await items.nth(i).inner_text()).strip()
            if txt == option_text:
                await items.nth(i).click()
                log.info(f"  ✔ 已选: {txt}")
                await asyncio.sleep(0.5)
                return
        # 🔒 严格模式：找不到直接报错
        all_opts = [(await items.nth(i).inner_text()).strip() for i in range(cnt)]
        raise RuntimeError(f"❌ 找不到目标选项: {option_text!r}\n弹窗实际选项: {all_opts}")

    # ── 选图书馆 ──────────────────────────────────────────────────────────
    async def select_library(self):
        log.info(f"▶ 选图书馆: {TARGET_LIBRARY}")
        await self._open_popup("Library")
        await self._pick_option(TARGET_LIBRARY)
        await self.snap("05_library")

    # ── 选区域 ────────────────────────────────────────────────────────────
    async def select_area(self):
        log.info(f"▶ 选区域: {TARGET_AREA}")
        trigger = self.page.locator('.inputPopupSelectDiv').filter(has_text="Area").first
        await trigger.wait_for(state="visible", timeout=10_000)
        await trigger.click()
        await asyncio.sleep(0.8)

        clicked = False
        for sel in [
            f'[role="radio"]:has-text("{TARGET_AREA}")',
            f'label:has-text("{TARGET_AREA}")',
            f'.v-radio:has-text("{TARGET_AREA}")',
            f'.v-list-item:has-text("{TARGET_AREA}")',
        ]:
            try:
                el = self.page.locator(sel).first
                if await el.count() > 0:
                    await el.scroll_into_view_if_needed()
                    await el.click()
                    log.info(f"  ✔ 已选区域: {TARGET_AREA}（{sel}）")
                    clicked = True
                    break
            except Exception:
                continue

        if not clicked:
            raise RuntimeError(f"❌ 区域弹窗未找到精确匹配: {TARGET_AREA}")
        await self.snap("06_area")

    # ── 选日期 ────────────────────────────────────────────────────────────
    async def select_date(self) -> datetime:
        target = datetime.now(SGT) + timedelta(days=BOOKING_DATE_OFFSET)
        log.info(f"  设置 Date = {target.strftime('%d %b %Y')}")

        trigger = self.page.locator('.inputPopupSelectDiv').filter(has_text="Date").first
        await trigger.wait_for(state="visible", timeout=10_000)
        await trigger.click()
        await self.page.wait_for_selector(
            '.v-date-picker-header, [class*="datepicker"], [class*="calendar"], button.v-btn[aria-label]',
            timeout=10_000,
        )
        await asyncio.sleep(0.3)
        await self.snap("07_calendar")
        await self._go_to_month(target)

        day = str(target.day)
        clicked = False
        for loc in [
            self.page.locator('button.v-btn:not([disabled])').filter(has_text=day),
            self.page.locator(f'button:has-text("{day}"), td:has-text("{day}")'),
        ]:
            cnt = await loc.count()
            for i in range(cnt):
                el = loc.nth(i)
                txt = (await el.inner_text()).strip()
                if txt != day: continue
                cls = (await el.get_attribute("class")) or " "
                if "disabled" in cls.lower() or "inactive" in cls.lower(): continue
                await el.click()
                clicked = True
                break
            if clicked: break

        if not clicked:
            raise RuntimeError(f"❌ 日期 {day} 不可用或已禁用")
        await asyncio.sleep(0.5)
        await self.snap("08_date")
        log.info(f"  ✅ Date 已选: {target.strftime('%-d %b %Y')}")
        return target

    async def _go_to_month(self, target: datetime, max_clicks: int = 14):
        month_name = target.strftime("%B")
        year_str = target.strftime("%Y")
        for _ in range(max_clicks):
            header = self.page.locator(
                '.v-date-picker-header__value, [class*="picker-title"], [class*="calendar-header"], [class*="monthYear"]'
            ).first
            try:
                cur = "  ".join((await header.inner_text()).split())
            except Exception:
                break
            if month_name in cur and year_str in cur:
                break
            nxt = self.page.locator(
                'button[aria-label*="next" i], button:has-text("›"), button:has-text(">")'
            ).last
            disabled = await nxt.get_attribute("disabled")
            if disabled is not None:
                log.warning(f"  ⚠ Next month 已禁用，停在: {cur!r}")
                break
            await nxt.click()
            await asyncio.sleep(0.3)

    # ── 通用 Radio 对话框选项点击 ────────────────────────────────────────
    async def _pick_dialog_radio(self, option_text: str):
        for sel in [
            f'label:has-text("{option_text}")',
            f'.v-radio:has-text("{option_text}")',
            f'[role="radio"]:has-text("{option_text}")',
            f'.v-list-item:has-text("{option_text}")',
        ]:
            try:
                el = self.page.locator(sel).first
                if await el.count() > 0:
                    await el.scroll_into_view_if_needed()
                    await asyncio.sleep(0.2)
                    await el.click()
                    log.info(f"  ✔ 已选: {option_text}（{sel}）")
                    await asyncio.sleep(0.5)
                    return
            except Exception:
                continue

        items = self.page.locator('div.my-2')
        cnt = await items.count()
        log.info(f"  🔎 弹窗共 {cnt} 个选项，查找: {option_text!r}")
        for i in range(cnt):
            el = items.nth(i)
            try:
                txt = (await el.inner_text()).strip()
                if txt != option_text: continue
                await el.scroll_into_view_if_needed()
                await asyncio.sleep(0.2)
                await el.click()
                log.info(f"  ✔ 已选(div.my-2 滚动): {txt}")
                await asyncio.sleep(0.5)
                return
            except Exception:
                continue
        raise RuntimeError(f"Radio 对话框找不到选项: {option_text!r}")

    # ── 选时间 ──────────────────────────────────────────────────────────────
    async def select_time(self, time_str: str):
        log.info(f"  设置 Time = {time_str}")
        trigger = self.page.locator('.inputPopupSelectDiv').filter(has_text="Time").first
        await trigger.wait_for(state="visible", timeout=10_000)
        await trigger.click()
        await asyncio.sleep(0.8)
        await self.snap("09_time_popup")
        await self._pick_dialog_radio(time_str)
        log.info("  ✅ Time 已选")

    # ── 选时长 ──────────────────────────────────────────────────────────────
    async def select_duration(self, dur_candidates):
        if isinstance(dur_candidates, str):
            dur_candidates = [dur_candidates]

        log.info(f"  设置 Duration，候选: {dur_candidates}")
        trigger = self.page.locator('.inputPopupSelectDiv').filter(has_text="Duration").first
        await trigger.wait_for(state="visible", timeout=10_000)
        await trigger.click()
        await asyncio.sleep(0.8)
        await self.snap("10_dur_popup")

        items = self.page.locator('div.my-2')
        cnt = await items.count()
        all_opts = []
        for i in range(cnt):
            try: all_opts.append((await items.nth(i).inner_text()).strip())
            except Exception: pass
        log.info(f"  Duration 弹窗选项: {all_opts}")

        for dur_str in dur_candidates:
            try:
                await self._pick_dialog_radio(dur_str)
                log.info(f"  ✅ Duration 已选: {dur_str}")
                return
            except RuntimeError:
                log.warning(f"  ⚠ Duration 候选 {dur_str!r} 未找到，尝试下一个")
                continue

        if all_opts:
            await self._pick_dialog_radio(all_opts[0])
            log.info(f"  ✅ Duration 兜底选择: {all_opts[0]}")
        else:
            raise RuntimeError(f"Duration 弹窗无可用选项，候选: {dur_candidates}")

    # ── CHECK AVAILABLE SLOTS ─────────────────────────────────────────────
    async def check_available_slots(self):
        log.info("▶ 点击 CHECK AVAILABLE SLOTS...")
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
        await self.check_available_slots()

        await self._click_area_and_book(label)
        log.info(f"🎉 {label} 预约完成！")
        return TARGET_AREA

    # ── 点击区域结果卡片并完成预约 ──────────────────────────────────────
    async def _click_area_and_book(self, label: str):
        s = safe(label)
        await asyncio.sleep(1.5)
        await self.snap(f"11_slots_{s}")

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
            raise RuntimeError(f"❌ 搜索结果页未找到目标区域: {TARGET_AREA}")

        await asyncio.sleep(1.0)
        await self.snap(f"12_seat_selection_{s}")

        booked_seat = await self._pick_seat_by_priority(s)
        log.info(f"  🪑 最终选座: {booked_seat}")

        await self.page.wait_for_selector('button:has-text("BOOK")', timeout=15_000)
        await self.snap(f"13_booking_details_{s}")
        log.info("  📄 已进入 Booking Details 页")

        await self.page.locator('button:has-text("BOOK")').first.click()
        log.info("  ✔ 已点击 BOOK 按钮")
        await asyncio.sleep(1.5)
        await self.snap(f"14_after_book_{s}")

        await self._handle_preferred_seat_dialog(s)

    # ── 处理 "Preferred Seat" 弹窗 ──────────────────────────────────────────
    async def _handle_preferred_seat_dialog(self, label_safe: str):
        try:
            await self.page.wait_for_selector('text="Preferred Seat"', timeout=6_000)
            log.info("  📋 检测到 Preferred Seat 弹窗")
        except PlaywrightTimeout:
            log.info("  ℹ Preferred Seat 弹窗未出现，检查其他确认弹窗...")
            await self._click_confirm_dialog(label_safe)
            return

        await self.snap(f"14b_preferred_seat_dialog_{label_safe}")
        preferred_seat = await self._pick_preferred_seat_in_dialog()
        log.info(f"  🪑 Preferred Seat 弹窗选座: {preferred_seat}")
        await self.snap(f"14c_preferred_seat_selected_{label_safe}")

        confirm_btn = None
        for sel in ['button:has-text("CONFIRM")', 'button:has-text("Confirm")', '[role="button"]:has-text("CONFIRM")']:
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
            await self.page.get_by_text("CONFIRM", exact=True).first.click()
            log.info("  ✔ Preferred Seat 弹窗：已点击 CONFIRM（兜底）")

        await asyncio.sleep(1.5)
        await self.snap(f"15_confirmed_{label_safe}")

    async def _pick_preferred_seat_in_dialog(self) -> str:
        await asyncio.sleep(0.5)
        available_in_dialog: dict[str, object] = {}
        # 🔒 核心修复：严格只匹配 S+数字 格式，过滤所有图书馆/区域/时间干扰项
        seat_re = re.compile(r"^S\d+$", re.IGNORECASE)

        for sel in [
            '.v-dialog label', '.v-dialog [role="radio"]',
            '.v-dialog .v-radio', '.v-dialog div.my-2',
            '[role="dialog"] label', '[role="dialog"] div.my-2',
        ]:
            els = self.page.locator(sel)
            cnt = await els.count()
            if cnt == 0: continue
            for i in range(cnt):
                el = els.nth(i)
                try:
                    txt = (await el.inner_text()).strip()
                    if not txt: continue
                    if not seat_re.match(txt):
                        continue  # 跳过非座位号
                    available_in_dialog[txt.upper()] = el
                except Exception:
                    continue
            if available_in_dialog:
                break

        if not available_in_dialog:
            log.info("  ℹ Preferred Seat 弹窗无有效座位选项，保持默认 'No preferred seat'")
            return "AUTO"

        log.info(f"  📋 Preferred Seat 弹窗有效座位: {sorted(available_in_dialog.keys())}")

        tier3 = [sid for sid in available_in_dialog if sid not in _TIER3_EXCLUDE]
        for tier_label, seats in [
            ("第一序列(S74-S86)", _TIER1),
            ("第二序列(S1-S16)", _TIER2),
            ("其他座位", tier3),
        ]:
            candidates = [s for s in seats if s in available_in_dialog]
            log.info(f"  🔍 弹窗尝试 {tier_label}，候选: {candidates}")
            for seat_id in candidates:
                el = available_in_dialog[seat_id]
                try:
                    await el.scroll_into_view_if_needed()
                    await asyncio.sleep(0.2)
                    await el.click()
                    log.info(f"  ✅ Preferred Seat 弹窗已选: {seat_id}（{tier_label}）")
                    await asyncio.sleep(0.3)
                    return seat_id
                except Exception as e:
                    log.warning(f"  ⚠ 弹窗点击 {seat_id} 失败: {e}")
                    continue

        log.warning("  ⚠ Preferred Seat 弹窗所有优先级座位均不可用，保持默认")
        return "AUTO"

    async def _click_confirm_dialog(self, label_safe: str):
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
        async def _get_available_seats() -> dict[str, object]:
            available: dict[str, object] = {}
            for sel in [
                '[class*="seat"]:not([class*="disabled"]):not([class*="unavailable"])',
                '[class*="seat"]:not([disabled])',
                'button[class*="seat"]',
                '[data-seat]',
            ]:
                els = self.page.locator(sel)
                cnt = await els.count()
                if cnt == 0: continue
                log.info(f"  🔎 selector={sel!r} 匹配到 {cnt} 个元素")
                for i in range(cnt):
                    el = els.nth(i)
                    try:
                        seat_id = (
                            await el.get_attribute("data-seat")
                            or await el.get_attribute("aria-label")
                            or (await el.inner_text()).strip()
                        )
                        if not seat_id: continue
                        seat_id = seat_id.strip().upper()
                        # 🔒 核心修复：仅排除明确禁用状态，保留 booked/occupied 供尝试
                        cls = (await el.get_attribute("class")) or " "
                        if any(k in cls.lower() for k in ("disabled", "unavailable")):
                            continue
                        available[seat_id] = el
                    except Exception:
                        continue
                if available: break
            return available

        available = await _get_available_seats()
        log.info(f"  📋 可用座位数: {len(available)}  全部: {sorted(available.keys())}")

        if not available:
            log.warning("  ⚠ 未检测到可用座位，尝试直接进入预约详情页（系统自动分配）")
            return "AUTO"

        tier3 = [sid for sid in available if sid not in _TIER3_EXCLUDE]

        for tier_label, seats in [
            ("第一序列(S74-S86)", _TIER1),
            ("第二序列(S1-S16)", _TIER2),
            ("其他座位", tier3),
        ]:
            log.info(f"  🔍 尝试 {tier_label}，候选: {[s for s in seats if s in available]}")
            for seat_id in seats:
                if seat_id not in available: continue
                el = available[seat_id]
                try:
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

# ══════════════════════════════════════════════════════════════════════════
async def main():
    log.info(f"=== NLB v5.1 | {datetime.now(SGT).strftime('%Y-%m-%d %H:%M:%S')} SGT ===")
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
                    await booker.snap(f"ERR_{safe(label)}")
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