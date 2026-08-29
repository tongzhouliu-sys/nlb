# 🪑 NLB Seat Auto-Booking

自动预约新加坡国家图书馆（NLB）座位的 GitHub Actions 机器人。

| 项目 | 配置 |
|------|------|
| 图书馆 | Punggol Regional Library |
| 区域 | Study Zone · Level 3 |
| 座位 | **S86** |
| 时段 | 10:00–11:00 · 14:00–15:00（每段 1 小时）|
| 触发时间 | 每天 12:00（新加坡时间）|

---

## 快速开始

### 1. Fork / Clone 这个仓库

```bash
git clone https://github.com/YOUR_USERNAME/nlb-seat-booking.git
cd nlb-seat-booking
```

### 2. 设置 GitHub Secrets

进入仓库 → **Settings → Secrets and variables → Actions → New repository secret**，添加：

| Secret 名称 | 值 |
|-------------|-----|
| `NLB_USERNAME` | 你的 myLibrary ID / NRIC / Email |
| `NLB_PASSWORD` | 你的 NLB 密码 |
| `FEISHU_WEBHOOK_URL` | （可选）飞书自定义机器人的 Webhook 地址 |

> ⚠️ **永远不要把账号密码写进代码或 commit 里！**

### 3. 启用 GitHub Actions

- 进入仓库 → **Actions** 标签页
- 点击 "I understand my workflows, go ahead and enable them"
- 之后每天 12:00 SGT 会自动运行

### 4. 手动测试

在 Actions 页面点击 **"Run workflow"** 可立即手动触发，可选参数：
- `date_offset = 0`：预约今天
- `date_offset = 1`：预约明天（默认）

---

## 本地运行（调试用）

```bash
# 安装依赖
pip install -r requirements.txt
playwright install chromium

# 设置环境变量
export NLB_USERNAME="your_id"
export NLB_PASSWORD="your_password"

# 运行
python book_seat.py
```

运行后截图保存在 `screenshots/` 目录，方便查看每步骤结果。

### 只补推飞书与 Telegram（不会订座）

```bash
./.venv/bin/python run_local.py --resend
```

补推会重新登录两个账号，只读取 `My Bookings → Upcoming` 中的最终预约结果，
取得具体 `S数字` 座位号后再推送；不会进入订座页面，也不会使用
`Any available seat` 作为推送结果。

本机 `config.env` 同时配置以下两项后，同一份核验结果会并行推送到 Telegram：

```dotenv
TELEGRAM_BOT_TOKEN=BotFather提供的Token
TELEGRAM_CHAT_ID=接收消息的Chat ID
```

### Telegram 菜单补推

结果消息底部带有 `🔄 再推一次任务结果` 按钮，也可以从机器人菜单选择
`/resend`。两种入口都会调用现有的只读补推流程，不会再次订座。

结果消息底部同时提供 `🗓 重新预定`，机器人菜单中对应 `/rebook`。该操作会
重新运行完整订座程序，因此必须在 Telegram 中再次点击确认按钮才会执行；取消
不会进入订座流程。订座、重新预定和只读补推共用进程锁，不会并发运行。

首次注册菜单：

```bash
./.venv/bin/python telegram_menu_bot.py --setup-menu
```

前台测试监听器：

```bash
./.venv/bin/python telegram_menu_bot.py
```

检查菜单、Webhook 和机器人状态：

```bash
./.venv/bin/python telegram_menu_bot.py --check
```

默认只允许 `TELEGRAM_CHAT_ID` 对应的私人用户执行菜单命令。如需将接收群与
操作人分开，可在 `config.env` 增加：

```dotenv
TELEGRAM_ALLOWED_USER_ID=允许操作菜单的Telegram数字用户ID
```

本机常驻监听使用：

```text
launchd/com.davizhuliang.nlb-telegram-menu.plist
```

---

## 修改配置

打开 `book_seat.py`，修改顶部配置区：

```python
TARGET_LIBRARY  = "Punggol Regional Library"  # 换图书馆
TARGET_ZONE     = "Study Zone"                 # 换区域
TARGET_LEVEL    = "Level 3"                    # 换楼层
TARGET_SEAT     = "S86"                        # 换座位号

BOOKING_DATE_OFFSET = 1  # 0=今天, 1=明天

# (显示标签, Time弹窗选项文字, Duration弹窗候选文字列表)
TIME_SLOTS = [
    ("10:00–11:00", "10:00 am", ["1:00", "1 hour", "60 mins", "1 hr", "1 hr 0 min"]),
    ("14:00–15:00", "2:00 pm",  ["1:00", "1 hour", "60 mins", "1 hr", "1 hr 0 min"]),
]
```

修改 Actions 触发时间：编辑 `.github/workflows/book_seat.yml`：
```yaml
cron: "0 4 * * *"   # 04:00 UTC = 12:00 SGT
#      分 时 日 月 周
```

---

## 注意事项

- NLB 每人每天有预约次数上限，请勿滥用
- 如果 S86 已被他人预约，脚本会报错并截图保存
- NLB 网站更新 UI 后，选择器可能需要调整（查看 screenshots 排查）
- 本脚本仅供个人学习使用，请遵守 NLB 使用条款

---

## 排查问题

Actions 运行后可在 **Artifacts** 里下载 `booking-screenshots-xxx.zip`，查看每步骤截图：

| 截图文件 | 含义 |
|---------|------|
| `01_login_page.png` | 登录页加载情况 |
| `03_after_login.png` | 登录后状态 |
| `08_seat_*_selected.png` | 座位选中状态 |
| `ERR_*.png` | 出错时的页面状态 |
