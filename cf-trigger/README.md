# nlb-trigger — 准点触发器（Cloudflare Worker Cron）

## 它解决什么问题

GitHub Actions 内置 `schedule` 定时器是"尽力而为"，实测会把触发**拖延 3–4.5 小时**
（原本 11:35 SGT 的 cron 实际 14–16 点才跑），导致抢座错过 12:00 开放窗口。

本 Worker 用 Cloudflare Cron Trigger（分钟级准点）在 **12:00 SGT** 调用 GitHub 的
`repository_dispatch` API。GitHub 收到 dispatch 后**几秒内起跑** workflow，抢座逻辑仍
全部在 GitHub Actions 上执行。

```
Cloudflare Cron (UTC 03:56 / SGT 11:56)
   └─ POST /repos/tongzhouliu-sys/nlb/dispatches  {event_type:"scheduled-booking"}
        └─ GitHub Actions: NLB Seat Auto-Booking（几秒起跑 → 装环境 → 抢座）
             └─ 抢座动作 ≈ 12:00，全程 ~12:05–12:12 完成（< 12:30）
```

触发时间在 [`wrangler.toml`](./wrangler.toml) 的 `crons` 里，是唯一可调值。

## 一次性部署

前置：装了 Node.js；有 Cloudflare 账号。

1. **建 GitHub PAT**（fine-grained）
   - Settings → Developer settings → Fine-grained tokens → Generate new token
   - Repository access：**Only select repositories** → `tongzhouliu-sys/nlb`
   - Permissions → Repository permissions → **Contents: Read and write**
     （`repository_dispatch` 要求这个权限）
   - 生成后复制 token（`github_pat_...`）

2. **登录并设置密钥、部署**
   ```bash
   cd cf-trigger
   npx wrangler login
   npx wrangler secret put GH_PAT   # 粘贴上一步的 PAT（只进 Cloudflare secret，不进仓库）
   npx wrangler deploy
   ```

> 不想用 wrangler 也可在 Cloudflare Dashboard → Workers 里手动建 Worker：粘贴
> `src/index.js`，Settings → Variables and Secrets 加 secret `GH_PAT`，
> Settings → Triggers → Cron Triggers 加 `56 3 * * *`。

## 验证

**A. 先单独验 GitHub 侧 dispatch 路径**（改完 workflow 并 push 后）：
```bash
curl -X POST \
  -H "Authorization: Bearer <PAT>" \
  -H "Accept: application/vnd.github+json" \
  -H "X-GitHub-Api-Version: 2022-11-28" \
  -H "User-Agent: manual-test" \
  https://api.github.com/repos/tongzhouliu-sys/nlb/dispatches \
  -d '{"event_type":"scheduled-booking"}'
```
期望：几秒内 Actions 出现一个 `repository_dispatch` 触发的 run，**立即执行**
（duration ~3–4 分钟，无长 sleep），飞书收到成功/失败卡片。

**B. 验 Worker 触发链**（部署后）：
- 直接访问 Worker 的 URL（`https://nlb-trigger.<子域>.workers.dev`）—— 本 Worker 的
  `fetch` 会立即打一次 dispatch，返回 `dispatched`，随后 GitHub 出现新 run。
- 或本地：`npx wrangler dev --test-scheduled`，再 `curl "http://localhost:8787/__scheduled?cron=56+3+*+*+*"`。
- 看 Worker 日志：`npx wrangler tail` 应打印 `dispatch ok (204)`。

**C. 真实当天**：Actions run 起跑时间应为 **~11:56–12:00 SGT**（而非 14–16 点），
飞书报预约成功，NLB 里能查到对应日期/时段的座位。

## 注意
- 取消了 GitHub `schedule` 兜底：若某天 Cloudflare 触发失败，当天不会自动跑。
  飞书有成功/失败通知；`npx wrangler tail` 可查 Worker 是否发出过 dispatch。
- Cloudflare Cron 偶尔延迟 1–2 分钟属正常，距 12:30 有充足余量。
- `GH_PAT` 只存 Cloudflare secret，**切勿提交进仓库**。
