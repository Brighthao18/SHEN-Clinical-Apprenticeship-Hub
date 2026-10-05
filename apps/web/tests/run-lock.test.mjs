import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, mkdirSync, writeFileSync, readFileSync, rmSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve, relative, isAbsolute } from "node:path";
import { createServer } from "node:net";
import { acquireRunLock, releaseRunLock, DEFAULT_LOCK_DIR } from "../e2e/lib/run-lock.mjs";

function temporary(t) {
  const parent = resolve(tmpdir());
  const folder = mkdtempSync(join(parent, "shen-public-lock-"));
  t.after(() => {
    const rel = relative(parent, folder);
    assert.ok(rel.startsWith("shen-public-lock-") && !isAbsolute(rel) && !rel.includes(".."));
    rmSync(folder, { recursive: true, force: true });
  });
  return join(folder, "lock");
}

test("native live owner excludes a second run, wrong-owner release preserves the lock", (t) => {
  const lock = temporary(t);
  assert.equal(acquireRunLock(lock).ok, true);
  assert.equal(acquireRunLock(lock).ok, false);
  assert.equal(releaseRunLock(lock, process.pid + 1).ok, false);
  assert.equal(readFileSync(join(lock, "owner"), "utf8").trim(), String(process.pid));
  assert.equal(releaseRunLock(lock).ok, true);
  assert.equal(releaseRunLock(lock).ok, true);
});

test("dead and corrupt owners refuse without automatic reclamation", async (t) => {
  const child = spawn(process.execPath, ["-e", ""], { stdio: "ignore" });
  await new Promise((done) => child.once("exit", done));
  const lock = temporary(t);
  mkdirSync(lock);
  for (const owner of [String(child.pid), "not-a-pid"]) {
    writeFileSync(join(lock, "owner"), owner);
    assert.equal(acquireRunLock(lock).ok, false);
    assert.equal(existsSync(lock), true);
    assert.equal(readFileSync(join(lock, "owner"), "utf8"), owner);
  }
});

test("occupied API port fails before Compose and releases the runner's lock", async () => {
  assert.equal(existsSync(DEFAULT_LOCK_DIR), false, "an existing isolated run must finish first");
  const server = createServer();
  await new Promise((done, reject) => { server.once("error", reject); server.listen(8002, "127.0.0.1", done); });
  try {
    const child = spawn(process.execPath, ["e2e/run.mjs"], { cwd: resolve("."), stdio: ["ignore", "pipe", "pipe"] });
    let output = "";
    child.stdout.on("data", (chunk) => { output += chunk; });
    child.stderr.on("data", (chunk) => { output += chunk; });
    const code = await new Promise((done, reject) => { child.once("error", reject); child.once("exit", done); });
    assert.equal(code, 1);
    assert.match(output, /cannot bind isolated port 8002/);
    assert.doesNotMatch(output, /isolated e2e postgres up|Container|Volume .*Removing/);
    assert.equal(existsSync(DEFAULT_LOCK_DIR), false);
  } finally {
    await new Promise((done) => server.close(done));
  }
});
