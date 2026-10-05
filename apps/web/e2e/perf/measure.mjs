/**
 * P1-B 性能基线测量客户端（隔离环境专用）。
 *
 * 用法（runner 已起好 uvicorn:8002 + next start:3002 且完成对应规模种子）：
 *   E2E_BASE_URL=http://localhost:3002 \
 *   PERF_LABEL=L PERF_NOTE_ID=<uuid> E2E_PATIENT_ID=<uuid> \
 *   node e2e/perf/measure.mjs [outFile]
 *
 * 测量内容：
 * - 关键 GET 接口：1 次冷启动 + 35 次热请求（顺序），记录中位数/P95/均值/最大值与响应体大小。
 *   患者作品列表按 UI 实际调用形状测量：首屏（sort=latest&limit=20）与深分页
 *   （offset=200，第 11 页）。
 * - 页面可操作时间（TTI）：生产构建下，从导航提交到页面真实数据渲染完成
 *   （/notes：作品列表可见；/：本周概况与最近病例同时可见——不只看“继续工作”
 *   区域出现，那不代表数据加载完成），各 3 次取中位数/P95。
 * 只计时；结论阈值见方案 P1-B（热列表 P95>500ms 或 TTI P95>1.5s 才调查）。
 */
import { chromium } from "@playwright/test";
import { writeFileSync } from "node:fs";

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:3002";
const LABEL = process.env.PERF_LABEL ?? "unknown";
const NOTE_ID = process.env.PERF_NOTE_ID;
const PATIENT_ID = process.env.E2E_PATIENT_ID;
const WARM = 35;
const TTI_REPS = 3;

function percentile(sorted, p) {
  if (!sorted.length) return null;
  const index = Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1);
  return sorted[index];
}

function stats(samples) {
  const sorted = [...samples].sort((a, b) => a - b);
  const sum = sorted.reduce((a, b) => a + b, 0);
  return {
    cold: sorted.length ? sorted[0] : null,
    median: percentile(sorted, 50),
    p95: percentile(sorted, 95),
    mean: sorted.length ? Math.round((sum / sorted.length) * 100) / 100 : null,
    max: sorted.length ? sorted.at(-1) : null,
    n: sorted.length,
  };
}

async function loginCookie() {
  const response = await fetch(`${BASE}/api/session`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: "admin", password: "shen-dev-only" }),
  });
  if (!response.ok) throw new Error(`login failed: ${response.status}`);
  const cookie = response.headers.getSetCookie()[0].split(";")[0];
  return cookie;
}

async function measureEndpoint(cookie, name, path) {
  const headers = { cookie };
  const timings = [];
  let bytes = 0;
  for (let index = 0; index < WARM + 1; index += 1) {
    const started = performance.now();
    const response = await fetch(`${BASE}${path}`, { headers, cache: "no-store" });
    const body = await response.arrayBuffer();
    const elapsed = Math.round((performance.now() - started) * 100) / 100;
    if (!response.ok) throw new Error(`${name} -> ${response.status}`);
    bytes = body.byteLength;
    timings.push(elapsed);
  }
  const [cold, ...warm] = timings;
  return {
    name,
    path,
    cold_ms: cold,
    warm: stats(warm),
    response_bytes: bytes,
  };
}

async function measureTti(page, path, ready) {
  const samples = [];
  for (let index = 0; index < TTI_REPS; index += 1) {
    const started = Date.now();
    await page.goto(`${BASE}${path}`, { waitUntil: "commit" });
    await ready(page);
    samples.push(Date.now() - started);
  }
  return stats(samples);
}

async function main() {
  const cookie = await loginCookie();
  const endpoints = [
    { name: "library_list_limit20", path: "/api/backend/api/v1/notes?limit=20" },
    { name: "dashboard_in_progress_query", path: "/api/backend/api/v1/notes?finalized=false&limit=3" },
    { name: "dashboard_aggregate", path: "/api/backend/api/v1/dashboard" },
  ];
  if (PATIENT_ID) {
    endpoints.push({
      name: "patient_notes_page1_latest",
      path: `/api/backend/api/v1/patients/${PATIENT_ID}/notes?sort=latest&limit=20`,
    });
    endpoints.push({
      name: "patient_notes_deep_page_offset200",
      path: `/api/backend/api/v1/patients/${PATIENT_ID}/notes?sort=latest&limit=20&offset=200`,
    });
  }
  if (NOTE_ID) {
    endpoints.push({ name: "note_history_detail_5_versions", path: `/api/backend/api/v1/notes/${NOTE_ID}` });
  }

  const results = { label: LABEL, base: BASE, generated_at: new Date().toISOString(), endpoints: [] };
  for (const endpoint of endpoints) {
    const result = await measureEndpoint(cookie, endpoint.name, endpoint.path);
    results.endpoints.push(result);
    console.log(
      `[perf] ${endpoint.name}: cold ${result.cold_ms}ms, warm median ${result.warm.median}ms, ` +
        `p95 ${result.warm.p95}ms, max ${result.warm.max}ms, ${result.response_bytes}B (n=${result.warm.n})`,
    );
  }

  const browser = await chromium.launch({ channel: "chrome", headless: true });
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    await page.goto(`${BASE}/`);
    if (page.url().includes("/login")) {
      await page.getByRole("textbox", { name: "用户名" }).fill("admin");
      await page.getByRole("textbox", { name: "密码" }).fill("shen-dev-only");
      await page.getByRole("button", { name: "进入我的师承工作台" }).click();
    }
    await page.getByRole("heading", { name: "我的师承工作台" }).waitFor();

    results.tti = {};
    results.tti.notes_library = await measureTti(page, "/notes", (p) =>
      p.getByText(/性能作品|就诊作品|分页验收作品|性能基线/).first(),
    );
    // 工作台“完整可操作时间”：本周概况统计条与最近病例（列表或空态）都渲染
    // 完成——“继续工作”区域出现不代表数据加载完成。
    results.tti.dashboard = await measureTti(page, "/", async (p) => {
      await p.getByRole("region", { name: "本周概况" }).waitFor({ state: "visible" });
      await p
        .locator("section", { has: p.getByRole("heading", { name: "最近病例" }) })
        .getByRole("link")
        .or(p.getByText("还没有病例"))
        .first()
        .waitFor({ state: "visible" });
    });
    console.log(
      `[perf] TTI /notes median ${results.tti.notes_library.median}ms p95 ${results.tti.notes_library.p95}ms; ` +
        `/ median ${results.tti.dashboard.median}ms p95 ${results.tti.dashboard.p95}ms`,
    );
  } finally {
    await browser.close();
  }

  const outFile = process.argv[2];
  if (outFile) writeFileSync(outFile, JSON.stringify(results, null, 2));
}

main().catch((error) => {
  console.error(`[perf] FAILED: ${error.message ?? error}`);
  process.exit(1);
});
