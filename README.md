# 🪑 NLB Seat Auto-Booking（Railway 部署版）

自动预约新加坡国家图书馆（NLB）座位的机器人。本分支（`nlbrailway`）已改造为 **Railway 一键部署版本**，以 **Scheduled Job（定时任务）** 的形式运行。

| 项目 | 配置 |
|------|------|
| 图书馆 | Punggol Library |
| 区域 | Study Zone · Level 3 |
| 座位优先级 | Tier0 (S74, S86, S1, S17) → Tier1 (S74–S86, S1–S17) → Any available seat 兜底 |
| 预约时段 | 11:00–12:00 · 13:00–14:00 · 15:00–16:00 · 17:00–18:00（每段 1 小时）|
| 预约日期 | 明天（`BOOKING_DATE_OFFSET=1`，可改）|
| 触发时间 | 每天 12:00（新加坡时间 Asia/Singapore，由 Railway Scheduled Job 触发）|

> 程序自身**不实现定时调度**：容器启动即执行一次预约任务，完成后进程退出
> （成功 `exit 0` / 失败 `exit 1`）。定时由 Railway 的 Scheduled Job 负责。

---

## 🚂 Railway 部署

### 1. 创建 Railway 项目并连接 GitHub

1. 登录 [railway.app](https://railway.app)，点击 **New Project**。
2. 选择 **Deploy from GitHub repo**，授权并选择本仓库。
3. **重要**：在 Service → **Settings → Source** 中，把部署分支设为 **`nlbrailway`**。
4. Railway 会自动识别根目录的 `Dockerfile`（`railway.json` 已声明用 Dockerfile 构建），
   在构建阶段执行 `playwright install --with-deps chromium`，安装 Chromium 及全部
   Linux 系统依赖。**首次构建约需 3–6 分钟。**

### 2. 配置环境变量

在 Service → **Variables** 中添加：

| 变量名 | 是否必填 | 说明 |
|--------|:------:|------|
| `NLB_USERNAME` | ✅ 必填 | 你的 myLibrary ID / NRIC / Email |
| `NLB_PASSWORD` | ✅ 必填 | 你的 NLB 密码 |
| `FEISHU_WEBHOOK_URL` | 可选 | 飞书自定义机器人 Webhook 地址；不填则跳过飞书推送 |
| `BOOKING_DATE_OFFSET` | 可选 | 预约哪天：`0`=今天，`1`=明天（默认 `1`）|

> ⚠️ 账号密码只放在 Railway Variables，**切勿写进代码或提交到 Git**。
> `TZ=Asia/Singapore` 已在 Dockerfile 内固定，无需在 Variables 里再设。

### 3. 配置 Scheduled Job（每天 12:00 SGT）

Railway 的 Cron 使用 **UTC**。新加坡时间 12:00 = UTC 04:00。

1. 进入 Service → **Settings → Cron Schedule**。
2. 填入 Cron 表达式：

   ```
   0 4 * * *
   ```

   （每天 UTC 04:00 = SGT 12:00 触发一次）

3. 保存。到点后 Railway 会启动容器运行一次 `python book_seat.py`，跑完即退出。

> 说明：设置 Cron Schedule 后，Railway 会把该 Service 当作定时任务，只在触发时启动
> 容器；`railway.json` 中 `restartPolicyType: NEVER` 确保任务退出后不会被反复重启。

### 4. 手动触发一次（测试）

在 Service 的 **Deployments** 页点击 **Deploy / Redeploy**，即可立即跑一次，
无需等到 12:00，用于验证账号密码、飞书通知是否正常。

---

## 💻 本地运行（调试用）

```bash
# 安装依赖
pip install -r requirements.txt
playwright install chromium

# 设置环境变量
export NLB_USERNAME="your_id"
export NLB_PASSWORD="your_password"
export FEISHU_WEBHOOK_URL="https://open.feishu.cn/..."   # 可选
export BOOKING_DATE_OFFSET=1                              # 可选，默认 1=明天

# 运行
python book_seat.py
```

或用 Docker 完整复现 Railway 环境：

```bash
docker build -t nlb-booking .
docker run --rm \
  -e NLB_USERNAME="your_id" \
  -e NLB_PASSWORD="your_password" \
  -e FEISHU_WEBHOOK_URL="https://open.feishu.cn/..." \
  nlb-booking
```

运行后截图保存在容器内 `screenshots/` 目录，方便查看每步骤结果。

---

## 🔧 修改配置

打开 `book_seat.py`，修改顶部配置区：

```python
TARGET_LIBRARY = "Punggol Library"
TARGET_AREA    = "Study Zone, Level 3"

_TIER0 = ["S74", "S86", "S1", "S17"]   # 最优先的 4 个座位

# (显示标签, Time弹窗选项文字, Duration弹窗候选文字列表)
TIME_SLOTS = [
    ("11:00–12:00", "11:00 am", ["1:00", "1 hour", "60 mins", "1 hr", "1 hr 0 min"]),
    ("13:00–14:00", "1:00 pm",  ["1:00", "1 hour", "60 mins", "1 hr", "1 hr 0 min"]),
    ("15:00–16:00", "3:00 pm",  ["1:00", "1 hour", "60 mins", "1 hr", "1 hr 0 min"]),
    ("17:00–18:00", "5:00 pm",  ["1:00", "1 hour", "60 mins", "1 hr", "1 hr 0 min"]),
]
```

修改触发时间：编辑 Railway Service → **Settings → Cron Schedule**（UTC 时区）。

---

## 📁 部署相关文件

| 文件 | 作用 |
|------|------|
| `Dockerfile` | Railway 构建镜像；装 Chromium + 系统依赖，固定 `TZ=Asia/Singapore` |
| `railway.json` | 声明用 Dockerfile 构建、启动命令、退出后不重启 |
| `.dockerignore` | 构建时排除无关 / 敏感文件 |
| `requirements.txt` | Python 依赖（playwright + tzdata）|

---

## ⚠️ 注意事项

- NLB 每人每天有预约次数上限，请勿滥用。
- 座位优先级：Tier0 → Tier1 → 兜底 "Any available seat"，逻辑保持不变。
- NLB 网站更新 UI 后，选择器可能需要调整（查看 `screenshots/` 排查）。
- 本脚本仅供个人学习使用，请遵守 NLB 使用条款。
