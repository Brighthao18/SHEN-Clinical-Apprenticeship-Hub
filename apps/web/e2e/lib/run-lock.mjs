/**
 * shen-e2e 隔离运行共享目录锁 —— e2e runner（run.mjs）与性能驱动
 * （perf/run-perf.sh）共用的唯一互斥实现，避免两套锁规则漂移。
 *
 * 规则（fail closed）：
 * - 持有者身份一律记录 Windows 原生 PID。Git Bash 的 $$ 是 MSYS PID，
 *   Windows 的 tasklist 查不到，会把活锁误判为陈旧——禁止使用；bash 侧
 *   用 `ps -p $$` 的 WINPID 列取得自身原生 PID 后经 CLI 传入。
 * - 锁已存在即拒绝运行，绝不自动删除：持有者存活 → 拒绝；持有者已死
 *   （陈旧锁）→ 拒绝并提示核实后人工清理——自动回收存在两个进程同时
 *   清理、误删新持有者锁的竞争；持有者身份不可判定（owner 缺失/写入
 *   中/损坏，或存活检查失败）→ 拒绝。
 * - 存活判定取无过滤完整进程表 CSV（退出码恒为 0、与系统语言无关），
 *   按精确 PID 行匹配；输出为空或不含可解析进程行时一律视为不可判定，
 *   绝不当作死亡。
 * - 释放前核对锁内 owner 是否为本轮持有者：不是（锁被替换/已损坏）则
 *   拒绝，防止误删他人的锁。释放按目录递归删除（含 owner 文件）。
 *
 * CLI（供 bash 调用；持有者 PID 必须显式传入，不能取本 CLI 进程的）：
 *   node run-lock.mjs acquire --pid <windows native pid>
 *   node run-lock.mjs release --pid <windows native pid>
 */
import { execSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const LIB_DIR = dirname(fileURLToPath(import.meta.url));
export const DEFAULT_LOCK_DIR = resolve(LIB_DIR, "../.locks/shen-e2e.lock");
const OWNER_FILE = "owner";

function readOwner(lockDir) {
  try {
    const raw = readFileSync(join(lockDir, OWNER_FILE), "utf8").trim();
    return /^\d+$/.test(raw) ? raw : null;
  } catch {
    return null; // 缺失 / 写入中 / 损坏，一律视为不可判定
  }
}

/** "alive" | "dead" | "unknown" —— 检查失败或输出不可解析绝不报告为 dead。 */
function windowsPidState(pid) {
  let text = "";
  try {
    // 不带过滤器地取完整进程表：过滤器无匹配时 tasklist 以非零码退出并
    // 输出本地化提示（中文系统为"信息: 没有运行的任务…"），无法稳定解析；
    // 完整表恒为 CSV、退出码恒为 0，按精确 PID 行判断与系统语言无关。
    text = String(
      execSync("tasklist /FO CSV /NH", {
        stdio: ["ignore", "pipe", "ignore"],
        encoding: "utf8",
        timeout: 15_000,
        maxBuffer: 16 * 1024 * 1024,
      }),
    );
  } catch {
    return "unknown";
  }
  if (new RegExp(`^"[^"]*","${pid}",`, "m").test(text)) return "alive";
  // 空输出或不含任何可解析进程行 = 进程表不可信，不可据此判死。
  if (!text.trim() || !/^"[^"]*","\d+",/m.test(text)) return "unknown";
  return "dead";
}

function nativePidState(pid) {
  if (process.platform === "win32") return windowsPidState(pid);
  if (!/^[1-9]\d*$/.test(pid)) return "unknown";
  try {
    process.kill(Number(pid), 0);
    return "alive";
  } catch (error) {
    return error?.code === "ESRCH" ? "dead" : "unknown";
  }
}

function acquireFresh(lockDir, pid) {
  mkdirSync(dirname(lockDir), { recursive: true });
  mkdirSync(lockDir); // 独占创建；EEXIST 交由调用方走"锁已存在"分支
  try {
    writeFileSync(join(lockDir, OWNER_FILE), `${pid}\n`);
  } catch (error) {
    // owner 写不进去的锁是本轮刚创建的半把锁，原样移除
    try {
      rmSync(lockDir, { recursive: true, force: true });
    } catch {
      // best effort
    }
    throw error;
  }
}

/**
 * 返回 { ok: true } 或 { ok: false, reason }。锁已存在的一切情形（存活、
 * 已死、不可判定）都拒绝，绝不自动删除；陈旧锁由人工核实后清理。
 * @param {string} [lockDir]
 * @param {number|string} [pid] 持有者的 Windows 原生 PID
 */
export function acquireRunLock(lockDir = DEFAULT_LOCK_DIR, pid = process.pid) {
  const me = String(pid);
  try {
    acquireFresh(lockDir, me);
    return { ok: true };
  } catch (error) {
    if (error?.code !== "EEXIST") {
      return { ok: false, reason: `cannot create lock at ${lockDir}: ${error?.message ?? error}` };
    }
  }
  const owner = readOwner(lockDir);
  if (owner === null) {
    return {
      ok: false,
      reason:
        `lock at ${lockDir} has a missing/unreadable owner record (another run may be mid-acquire) — ` +
        "refusing, not removing; clear it manually only after confirming no isolated run is active",
    };
  }
  const state = nativePidState(owner);
  if (state === "alive") {
    return { ok: false, reason: `another isolated run holds ${lockDir} (native pid ${owner})` };
  }
  if (state === "unknown") {
    return {
      ok: false,
      reason:
        `cannot verify liveness of lock owner pid ${owner} at ${lockDir} — refusing, not removing; ` +
        `clear it manually only after confirming that process is gone`,
    };
  }
  return {
    ok: false,
    reason:
      `lock at ${lockDir} is stale: owner pid ${owner} is no longer running — refusing to auto-remove ` +
      `(concurrent reclamation could delete a new holder's lock); verify no isolated run is active, ` +
      `then delete the directory manually: ${lockDir}`,
  };
}

/**
 * 返回 { ok: true } 或 { ok: false, reason }。已不存在视为释放成功（幂等）。
 * @param {string} [lockDir]
 * @param {number|string} [pid] 持有者的 Windows 原生 PID
 */
export function releaseRunLock(lockDir = DEFAULT_LOCK_DIR, pid = process.pid) {
  const me = String(pid);
  if (!existsSync(lockDir)) return { ok: true };
  const owner = readOwner(lockDir);
  if (owner !== me) {
    return {
      ok: false,
      reason: `lock at ${lockDir} is owned by pid ${owner ?? "<missing>"}, not this run (${me}) — refusing to release a foreign lock`,
    };
  }
  try {
    rmSync(lockDir, { recursive: true, force: true });
  } catch (error) {
    return { ok: false, reason: `could not remove lock at ${lockDir}: ${error?.message ?? error}` };
  }
  return { ok: true };
}

function cli() {
  const [action, flag, pidArg] = process.argv.slice(2);
  if ((action !== "acquire" && action !== "release") || flag !== "--pid" || !/^\d+$/.test(pidArg ?? "")) {
    console.error("usage: node run-lock.mjs acquire|release --pid <windows native pid>");
    process.exit(2);
  }
  const result =
    action === "acquire"
      ? acquireRunLock(DEFAULT_LOCK_DIR, pidArg)
      : releaseRunLock(DEFAULT_LOCK_DIR, pidArg);
  if (result.ok) process.exit(0);
  console.error(`[run-lock] REFUSED: ${result.reason}`);
  process.exit(1);
}

if (process.argv[1] && import.meta.url === pathToFileURL(resolve(process.argv[1])).href) cli();
