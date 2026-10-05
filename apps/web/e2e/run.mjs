/**
 * 隔离 e2e 一次性编排器（Windows / Git Bash 兼容）：
 *   node e2e/run.mjs            完整流程（含 next build）
 *   SKIP_BUILD=1 node e2e/run.mjs   复用现有 .next 生产构建
 *
 * 隔离环境：专属 Compose 项目（e2e/docker-compose.e2e.yaml，项目名
 * shen-e2e，独立数据卷 e2e_pgdata，端口 127.0.0.1:5433）——不重建、不触碰
 * 现有开发数据库容器。清理只停止/移除本项目创建的容器与卷。
 *
 * 流程：启动隔离 PostgreSQL（healthcheck + 超时）→ 重建 shen_e2e → 种子
 * base（20 篇定稿，无草稿）→ 起 uvicorn:8002 / next start:3002 → Playwright
 * 空态阶段 → 种子 draft + 205 作品分页数据集 → Playwright 全量阶段 → 清理。
 *
 * 安全守卫：数据库 URL 必须指向 shen_e2e；8002/3002 被占用时直接拒绝启动；
 * 所有外部命令带超时。全程只使用合成数据，不触碰 development database。
 */
import { spawn, execSync, execFileSync } from "node:child_process";
import { mkdirSync, openSync, readFileSync } from "node:fs";
import { createServer } from "node:net";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { acquireRunLock, releaseRunLock } from "./lib/run-lock.mjs";

const HERE = dirname(fileURLToPath(import.meta.url));
const WEB_DIR = resolve(HERE, "..");
const API_DIR = resolve(WEB_DIR, "../api");
const ROOT = resolve(WEB_DIR, "../..");
const LOG_DIR = resolve(HERE, ".logs");
const PY = process.env.E2E_PYTHON ?? resolve(ROOT, process.platform === "win32" ? "work/venv/Scripts/python.exe" : "work/venv/bin/python");
const COMPOSE_FILE = resolve(HERE, "docker-compose.e2e.yaml");
const E2E_DATABASE_URL = "postgresql+psycopg://shen:shen-e2e-password@127.0.0.1:5433/shen_e2e";
const BASE_URL = "http://localhost:3002";
const STEP_TIMEOUT_MS = 600_000;

if (!E2E_DATABASE_URL.includes("shen_e2e")) {
  console.error("refusing to run: database URL must target shen_e2e");
  process.exit(1);
}

// 与性能驱动（e2e/perf/run-perf.sh）共用 shen-e2e Compose 项目：目录锁互
// 斥，防止两次隔离运行并发互相清理对方资源。锁的规则统一在共享实现
// e2e/lib/run-lock.mjs：锁已存在即拒绝（含持有者已死的陈旧锁——绝不自动
// 删除，人工核实后清理），释放前核对持有者身份；process.pid 即本轮原生 PID。
const lock = acquireRunLock();
if (!lock.ok) {
  console.error(`refusing to run: ${lock.reason}`);
  process.exit(1);
}

const children = [];
let seedEnv = {};
let composeStarted = false;
const isolatedEnv = {
  APP_ENV: "development", DATABASE_URL: E2E_DATABASE_URL,
  OCR_PROVIDER: "mock", MINERU_API_TOKEN: "", ALLOW_IDENTIFIABLE_MINERU_UPLOAD: "false",
  AI_EXTRACTION_PROVIDER: "mock", AI_PROVIDER_API_KEY: "",
  AI_EXTRACTION_ALLOW_IDENTIFIABLE_INPUT: "false", AI_VERIFICATION_ENABLED: "false",
  AI_VISION_VERIFICATION_ENABLED: "false", AI_VISION_PROVIDER_API_KEY: "",
  ALLOW_IDENTIFIABLE_AI_VISION_UPLOAD: "false", TASKS_EAGER: "true",
  STORAGE_ROOT: resolve(ROOT, "work/e2e-storage"),
};

function sh(cmd, opts = {}) {
  return execSync(cmd, {
    cwd: opts.cwd ?? ROOT,
    stdio: opts.capture ? ["ignore", "pipe", "inherit"] : "inherit",
    encoding: "utf8",
    timeout: opts.timeoutMs ?? STEP_TIMEOUT_MS,
    env: { ...process.env, ...isolatedEnv, ...opts.env },
  });
}

function compose(args, opts = {}) {
  return sh(`docker compose -f "${COMPOSE_FILE}" ${args}`, opts);
}

function spawnBg(name, command, args, opts = {}) {
  const logPath = resolve(LOG_DIR, `${name}.log`);
  const out = openSync(logPath, "a");
  const child = spawn(command, args, {
    cwd: opts.cwd,
    env: { ...process.env, ...isolatedEnv, ...opts.env },
    detached: process.platform !== "win32",
    stdio: ["ignore", out, out],
  });
  children.push({ name, child });
  console.log(`[runner] started ${name} (pid ${child.pid}), log: ${logPath}`);
  return child;
}

async function waitForHttp(url, label, timeoutMs = 90_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url, { signal: AbortSignal.timeout(3_000) });
      if (response.ok || (response.status >= 300 && response.status < 400)) {
        console.log(`[runner] ${label} is up (${response.status})`);
        return;
      }
    } catch {
      // not ready yet
    }
    await new Promise((r) => setTimeout(r, 1_000));
  }
  throw new Error(`${label} did not become ready within ${timeoutMs}ms: ${url}`);
}

function assertPortFree(port) {
  return new Promise((done, reject) => {
    const probe = createServer();
    probe.once("error", (error) => reject(new Error(`cannot bind isolated port ${port} (${error.code}); verify its owner before retrying`)));
    probe.listen({ host: "127.0.0.1", port, exclusive: true }, () => probe.close(done));
  });
}

function killTree(child, name) {
  if (!child || child.exitCode !== null) return;
  console.log(`[runner] stopping ${name} (pid ${child.pid})`);
  try {
    if (process.platform === "win32") {
      execSync(`taskkill /PID ${child.pid} /T /F`, { stdio: "ignore", timeout: 15_000 });
    } else {
      process.kill(-child.pid, "SIGTERM");
    }
  } catch {
    // already gone
  }
}

function seed(script, phase, prefix = "E2E") {
  const label = `${script}${phase ? `:${phase}` : ""}`;
  console.log(`[runner] seeding ${label}`);
  const output = execFileSync(PY, ["-", ...(phase ? [phase] : [])], {
    cwd: API_DIR,
    input: readFileSync(resolve(HERE, "fixtures", script)),
    encoding: "utf8", timeout: STEP_TIMEOUT_MS,
    env: { ...process.env, ...isolatedEnv },
  });
  const line = output.split("\n").find((l) => l.startsWith("E2E_SEED_JSON:"));
  if (!line) throw new Error(`seed ${label} did not print E2E_SEED_JSON:\n${output}`);
  const ids = JSON.parse(line.slice("E2E_SEED_JSON:".length));
  // 不同数据集用不同前缀记录（E2E_* = 工作台用例；E2E_PAGE_* = 分页用例），
  // 后种子的 id 不覆盖先种子的。
  seedEnv = {
    ...seedEnv,
    [`${prefix}_PATIENT_ID`]: ids.patient_id,
    [`${prefix}_ENCOUNTER_OLDER_ID`]: ids.encounter_older_id,
    [`${prefix}_ENCOUNTER_NEWER_ID`]: ids.encounter_newer_id,
    ...(ids.draft_note_id ? { E2E_DRAFT_NOTE_ID: ids.draft_note_id } : {}),
    E2E_BASE_URL: BASE_URL,
    E2E_PHASE: "",
  };
  console.log(`[runner] seed ids: ${JSON.stringify(ids)}`);
}

function runPlaywright(phase) {
  console.log(`[runner] playwright phase: ${phase}`);
  sh("corepack pnpm exec playwright test", {
    cwd: WEB_DIR,
    env: { ...seedEnv, E2E_PHASE: phase },
  });
}

async function main() {
  mkdirSync(LOG_DIR, { recursive: true });
  await assertPortFree(8002);
  await assertPortFree(3002);

  console.log("[runner] 1/8 isolated e2e postgres up (dedicated project, port 5433)");
  composeStarted = true;
  compose("up -d --wait --wait-timeout 60");

  console.log("[runner] 2/8 recreate shen_e2e + migrate");
  compose('exec -T db psql -U shen -d shen_e2e -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"');
  sh(`"${PY}" -m alembic upgrade head`, { cwd: API_DIR, env: { DATABASE_URL: E2E_DATABASE_URL } });

  console.log("[runner] 3/8 web production build");
  if (process.env.SKIP_BUILD !== "1") {
    sh("corepack pnpm exec next build", { cwd: WEB_DIR });
  } else {
    console.log("[runner] SKIP_BUILD=1 -> reusing existing .next");
  }

  console.log("[runner] 4/8 start uvicorn:8002");
  spawnBg("uvicorn", PY, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8002"], {
    cwd: API_DIR,
    env: { DATABASE_URL: E2E_DATABASE_URL, APP_ENV: "development" },
  });
  await waitForHttp("http://127.0.0.1:8002/health", "uvicorn");

  console.log("[runner] 5/8 seed base (finals only) + start web:3002");
  seed("seed_dashboard.py", "base");
  spawnBg(
    "next-start",
    process.execPath,
    [resolve(WEB_DIR, "node_modules/next/dist/bin/next"), "start", "--port", "3002"],
    { cwd: WEB_DIR, env: { API_INTERNAL_URL: "http://127.0.0.1:8002" } },
  );
  await waitForHttp(`${BASE_URL}/login`, "next start");

  console.log("[runner] 6/8 Playwright: empty-state phase");
  runPlaywright("empty");

  console.log("[runner] 7/8 seed draft + pagination dataset + Playwright: full phase");
  seed("seed_dashboard.py", "draft");
  seed("seed_pagination.py", undefined, "E2E_PAGE");
  runPlaywright("full");

  console.log("[runner] 8/8 all e2e scenarios passed");
}

async function mainAsync() {
  try {
    await main();
  } finally {
    for (const { name, child } of children) killTree(child, name);
    try {
      // 只清理本项目创建的容器与数据卷（shen-e2e 项目，非开发栈）。
      if (composeStarted) compose("down -v");
    } catch {
      // best effort
    }
  }
}

function releaseLock() {
  // 共享实现先核对本轮持有者身份再删除（含 owner 文件）；非本轮持有则拒绝。
  const result = releaseRunLock();
  if (!result.ok) console.warn(`[runner] lock not released: ${result.reason}`);
}

mainAsync()
  .then(() => {
    releaseLock();
    process.exit(0);
  })
  .catch((error) => {
    console.error(`[runner] FAILED: ${error.message ?? error}`);
    releaseLock();
    process.exit(1);
  });
