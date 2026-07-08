// Cloudflare Worker：Cron Trigger 到点后调用 GitHub repository_dispatch，
// 由 GitHub Actions 里的 NLB Seat Auto-Booking workflow 接手抢座。
//
// 需要的密钥（用 `wrangler secret put GH_PAT` 设置，切勿写进代码/仓库）：
//   GH_PAT —— GitHub fine-grained PAT，仅授权仓库 tongzhouliu-sys/nlb，
//             权限 Contents: Read and write（repository_dispatch 所需）。

const GITHUB_REPO = "tongzhouliu-sys/nlb";
const EVENT_TYPE = "scheduled-booking";

async function triggerBooking(env) {
  const res = await fetch(
    `https://api.github.com/repos/${GITHUB_REPO}/dispatches`,
    {
      method: "POST",
      headers: {
        Authorization: `Bearer ${env.GH_PAT}`,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "nlb-trigger-worker", // GitHub API 强制要求 UA
      },
      body: JSON.stringify({ event_type: EVENT_TYPE }),
    }
  );

  const ok = res.ok; // dispatch 成功返回 204 No Content
  if (ok) {
    console.log(`dispatch ok (${res.status})`);
  } else {
    console.log(`dispatch failed: ${res.status} ${await res.text()}`);
  }
  return ok;
}

export default {
  // Cron 到点触发
  async scheduled(event, env, ctx) {
    ctx.waitUntil(triggerBooking(env));
  },

  // 便于手动测试：浏览器/curl 打这个 Worker 的 URL 也能触发一次
  async fetch(request, env, ctx) {
    const ok = await triggerBooking(env);
    return new Response(ok ? "dispatched\n" : "dispatch failed\n", {
      status: ok ? 200 : 502,
    });
  },
};
