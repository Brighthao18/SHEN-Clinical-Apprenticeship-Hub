import { defineConfig } from "@playwright/test";

// 隔离 e2e：runner（e2e/run.mjs）负责 shen_e2e 重建/种子、uvicorn 8002 与
// next start 3002；设置 E2E_BROWSER_CHANNEL=chrome 使用本机 Chrome；默认
// 使用由 pnpm exec playwright install chromium 安装的测试浏览器。
const phase = process.env.E2E_PHASE ?? "full";

export default defineConfig({
  testDir: "./e2e",
  testMatch: phase === "empty" ? [/a-empty-state\.spec\.mjs/] : [/.*\.spec\.mjs/],
  testIgnore: phase === "empty" ? [] : [/a-empty-state\.spec\.mjs/],
  timeout: 60_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [["list"]],
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:3002",
    ...(process.env.E2E_BROWSER_CHANNEL ? { channel: process.env.E2E_BROWSER_CHANNEL } : {}),
    headless: true,
    viewport: { width: 1280, height: 900 },
    actionTimeout: 15_000,
    navigationTimeout: 30_000,
  },
});
