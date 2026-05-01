# 🪑 NLB Seat Auto-Booking

自动预约新加坡国家图书馆（NLB）座位的 GitHub Actions 机器人。

| 项目 | 配置 |
|------|------|
| 图书馆 | Punggol Regional Library |
| 区域 | Study Zone · Level 3 |
| 座位 | **S86** |
| 时段 | 10:00–11:30 · 14:00–15:30 |
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

---

## 修改配置

打开 `book_seat.py`，修改顶部配置区：

```python
TARGET_LIBRARY  = "Punggol Regional Library"  # 换图书馆
TARGET_ZONE     = "Study Zone"                 # 换区域
TARGET_LEVEL    = "Level 3"                    # 换楼层
TARGET_SEAT     = "S86"                        # 换座位号

BOOKING_DATE_OFFSET = 1  # 0=今天, 1=明天

TIME_SLOTS = [
    ("10:00", "11:30"),   # 时段1
    ("14:00", "15:30"),   # 时段2
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
