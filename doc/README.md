# doc/ — nlb 常青文档

> 建立：2026-08-23 · 本轮深读源码全面修正 · 代码基线 `fc97557`（main，46 commits）
> NLB 座位自动预约机器人：Playwright 驱动 GitHub Actions，每天自动预约新加坡国家图书馆
> （Punggol Regional Library · Study Zone L3）座位的两个时段。

## 一、⚠ 版本对应关系（重要，勿再弄反）

| 文件 | 实际版本 | 角色 |
|---|---|---|
| `book_seat.py` | **v15**（文件头 docstring 自述；1604 行） | **当前生效版本**——workflow `book_seat.yml:71` 运行的就是它（`run: python book_seat.py`） |
| `book_seat_v9.py` | v9（1122 行） | **旧版**，保留作历史参考。文件名带版本号反而易误导——生效判断以 workflow 为准 |

v15 相对 v9 的关键演进（book_seat.py:1-33 docstring 实读）：
- v12 纠正了两个对 NLB UI 的**错误认知**：真实流程是「区域卡片 → Booking Details →
  BOOK → Selection 对话框 → 先点 Select seat 输入框 → Seat 单选列表」，**没有座位图页**，
  也没有 "Preferred Seat" 文字（v9 在等一个永不出现的元素——假成功根因）；
- v12 修复 `:has-text()` 子串匹配导致 "12:00 pm" 误中 "2:00 pm" 的**下午被订成中午** bug
  （改 `^...$` 精确匹配）；
- v15 修复「假失败」：My Bookings 顶部红色 check-in 提醒横幅盖住标签栏 → 点不到
  Upcoming → 误报失败。新增 `_dismiss_banner()` 与 `_goto()` 统一先关横幅。

## 二、文件清单

| 文件 | 内容 |
|---|---|
| `book_seat.py` | **v15 生效版**。含飞书通知（`FEISHU_WEBHOOK_URL`）、`BOOKING_DATE_OFFSET`、`NLB_ACCOUNT_LABEL`、`NLB_DEFER_FEISHU`、结果 JSON（`NLB_RESULTS_FILE`）、Tier0(S74,S86,S1,S17)→Tier1(S74–S86,S1–S17)→Any available seat 兜底的选座优先级、`verify_booking_exists` 终极校验（Bookings 列表查到真实预约才算成功）、失败非零退出码 |
| `book_seat_v9.py` | v9 旧版（勿编辑） |
| `book_seat.yml` | GitHub Actions workflow：cron `0 4 * * *` UTC（=12:00 SGT）+ workflow_dispatch（`date_offset` 输入 → `BOOKING_DATE_OFFSET` env，默认 1=明天）；env 注入 NLB 凭据与飞书 webhook；**运行的是 book_seat.py**；截图工件上传（失败也传） |
| `run_local.py` / `run_scheduled.sh` / `install_local_scheduler.sh` / `launchd/com.davizhuliang.nlb-seat-booking.plist` | 本地 launchd 调度：运行时在 `~/Library/Application Support/NLB Seat Booking/runtime/`（独立 .venv），配置 `config.env`，带 `scheduler.lock` 防并发与 `last-successful-booking-date` 成功标记 |
| `local-runs/` | 本地运行产物 |

## 三、核心逻辑要点（v15 实读）

- 凭据只从环境变量；`FEISHU_WEBHOOK_URL` 未配置时静默跳过通知。
- 选座优先级：Tier0 四个指定座位 → Tier1 序列 → "Any available seat"（用户指定兜底）；
  `normalize_seat_id` 归一化座位 ID 格式。
- 时段：10:00–11:30 与 14:00–15:30（1.5h），Duration 候选多格式按序尝试。
- 成功判据是 `verify_booking_exists`（在 My Bookings 里真实查到），不是「点了 CONFIRM
  没报错」；无法核实（切不到 Upcoming）返回 None 视为成功——宁可不报失败。
- GitHub schedule 尽力而为，可能拖延数小时——已知取舍；Runner 日志统一 TZ=Asia/Singapore。

## 四、维护约定

1. **改动基于 `book_seat.py`（v15）**，修完把版本号与变更说明写进文件头 docstring
   （该文件的版本史都记在 docstring，v9→v15 每版根因都在）。
2. NLB 页面改版是主要故障源：先跑一次手动 workflow，看 screenshots 工件定位选择器。
3. 本地 launchd 与 Actions 二选一，勿双跑造成重复预约。
4. ⚠ 若要让 v9 时代的「座位图诊断」类改动生效，注意 workflow 指向的是 book_seat.py——
   新建 v16 时要么覆盖 book_seat.py，要么同步改 workflow 的 run 行。
