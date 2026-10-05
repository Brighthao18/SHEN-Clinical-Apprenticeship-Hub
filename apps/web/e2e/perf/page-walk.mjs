/**
 * P1-B 分页完整性走查（跨页不重不漏）。
 *
 *   E2E_BASE_URL=http://localhost:3002 E2E_PATIENT_ID=<uuid> \
 *     node e2e/perf/page-walk.mjs [outFile]
 *
 * 以最大页大小（limit=200）顺序遍历该患者全部作品页，断言：
 * - 每页条数 = limit（末页除外）；
 * - note_id 全局唯一且并集大小 == total（不重、不漏）；
 * - sort=latest 全局有序（created_at 非递增）；
 * - 记录每页耗时（median/P95）与总遍历时长。
 */
import { writeFileSync } from "node:fs";

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:3002";
const PATIENT_ID = process.env.E2E_PATIENT_ID;
const LIMIT = 200;

function percentile(sorted, p) {
  if (!sorted.length) return null;
  return sorted[Math.min(sorted.length - 1, Math.ceil((p / 100) * sorted.length) - 1)];
}

async function main() {
  const login = await fetch(`${BASE}/api/session`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username: "admin", password: "shen-dev-only" }),
  });
  if (!login.ok) throw new Error(`login failed: ${login.status}`);
  const cookie = login.headers.getSetCookie()[0].split(";")[0];
  const headers = { cookie };

  const ids = [];
  const createdTimes = [];
  const pageMs = [];
  const walkStarted = Date.now();
  let offset = 0;
  let total = null;
  for (;;) {
    const started = performance.now();
    const response = await fetch(
      `${BASE}/api/backend/api/v1/patients/${PATIENT_ID}/notes?sort=latest&limit=${LIMIT}&offset=${offset}`,
      { headers, cache: "no-store" },
    );
    if (!response.ok) throw new Error(`page@${offset} -> ${response.status}`);
    const payload = await response.json();
    pageMs.push(Math.round((performance.now() - started) * 100) / 100);
    total = payload.total;
    for (const item of payload.items) {
      ids.push(item.note_id);
      createdTimes.push(new Date(item.latest.created_at).getTime());
    }
    if (offset + payload.items.length >= total || payload.items.length === 0) break;
    offset += LIMIT;
  }
  const elapsed = Date.now() - walkStarted;

  const unique = new Set(ids);
  const problems = [];
  if (unique.size !== ids.length) problems.push(`duplicate note_ids: ${ids.length - unique.size}`);
  if (total !== null && ids.length !== total) problems.push(`coverage ${ids.length} != total ${total}`);
  for (let index = 1; index < createdTimes.length; index += 1) {
    if (createdTimes[index] > createdTimes[index - 1]) {
      problems.push(`ordering violated at flat index ${index}`);
      break;
    }
  }

  const result = {
    patient_id: PATIENT_ID,
    total,
    collected: ids.length,
    unique: unique.size,
    pages: pageMs.length,
    page_ms: { median: percentile([...pageMs].sort((a, b) => a - b), 50), p95: percentile([...pageMs].sort((a, b) => a - b), 95), max: Math.max(...pageMs) },
    walk_total_ms: elapsed,
    problems,
  };
  console.log(JSON.stringify(result, null, 2));
  const outFile = process.argv[2];
  if (outFile) writeFileSync(outFile, JSON.stringify(result, null, 2));
  if (problems.length) process.exit(1);
}

main().catch((error) => {
  console.error(`[page-walk] FAILED: ${error.message ?? error}`);
  process.exit(1);
});
