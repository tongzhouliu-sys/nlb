# ─────────────────────────────────────────────────────────────────────────
# NLB 座位预约 —— Railway 一键部署镜像（Scheduled Job）
# 容器启动即运行 book_seat.py，任务完成后进程退出（exit 0 成功 / exit 1 失败）。
# ─────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim

# 时区：Railway 容器默认 UTC。这里强制设为新加坡时间（SGT, UTC+8），
# 保证容器系统时钟与代码内 datetime.now(SGT) 一致，避免"明天"日期算错。
ENV TZ=Asia/Singapore \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

# 安装系统时区库并落地时区（供系统时钟使用；ZoneInfo 另有 tzdata pip 包兜底）
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先装 Python 依赖（利用镜像层缓存）
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 关键：安装 Chromium 及其全部 Linux 系统依赖，防止 Railway 容器缺库导致浏览器崩溃
RUN playwright install --with-deps chromium

# 复制项目代码
COPY . .

# 容器启动即执行预约任务；跑完即退出，正好契合 Railway Scheduled Job 模型
CMD ["python", "book_seat.py"]
