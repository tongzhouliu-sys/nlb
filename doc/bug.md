# nlb · bug.md — 源码问题登记

> 建立日期：2026-08-23 · 审查基线：`fc97557`（main）
> 本文件登记本轮深度源码审查发现。

## BUG-1 · workflow 运行旧版脚本的风险模式（本轮最重要的发现）

- **事实**：`book_seat.yml:71` 是 `run: python book_seat.py`。历史上 v9 改进写入
  `book_seat_v9.py` 时若 workflow 仍指 `book_seat.py`（当时的旧版），改进**不会生效**。
- **现状核实**：当前 `book_seat.py` 文件头自述为 v15 且包含全部演进（v10-v15），
  即维护者最终是把新版本**写回 book_seat.py 文件名**的——workflow 与代码现在一致。
- **遗留风险**：仓库同时存在 `book_seat.py`(v15) 与 `book_seat_v9.py`(旧)，命名暗示
  「带版本号的是新版」，与事实相反。未来贡献者极易改错文件。
- **建议**：删除或重命名 book_seat_v9.py 为 `archive/book_seat_v9.py.bak`；
  或在两个文件头互相注明「生效版本见 workflow run 行」。
- **严重度**：中（误导性高，当前无功能影响）

## BUG-2 · README 与 workflow 的 date_offset 语义差异

- **事实**：workflow input `date_offset` 描述「0=今天, 1=明天, 默认1」，经
  `BOOKING_DATE_OFFSET` env 传入（book_seat.yml:70）。但根 README 的手动测试说明写
  「date_offset = 0 预约今天」时未提该值只对手动触发有意义——定时触发固定用默认 1。
- **影响**：仅文档语义澄清问题。
- **严重度**：低。

## OBS-3 · v12 修复的历史 bug 模式值得记录（防复发）

`:has-text()` 是 Playwright 子串匹配："12:00 pm" 包含 "2:00 pm"，导致下午时段单被订成
中午时段。此类「时间文本互为子串」陷阱在所有按文本点选的自动化里都存在。
当前代码已用 `^...$` 精确匹配修复；后续新增任何按选项文字点选的逻辑必须沿用精确匹配。

## OBS-4 · 「无法核实 → 视为成功」的取舍

v15 的 verify 在无法确认切到 Upcoming 标签时返回 None 并视为成功（宁可不报失败）。
配合飞书通知意味着用户可能收到成功但实际未预订（概率低，CONFIRM 已脱离禁用态才点击）。
这是刻意取舍：假失败会导致重复预约尝试与人工焦虑；反向风险由「预约确认邮件/短信」
兜底验证。运营者应知晓此语义。

## 复核结论

- Tier 选座序列、normalize_seat_id、verify_booking_exists、非零退出码标红均实读确认；
- launchd 路径、scheduler.lock、last-successful-booking-date 与 doc/README §二一致；
- workflow 截图工件在失败时也上传（便于排查），与 README 描述一致。
