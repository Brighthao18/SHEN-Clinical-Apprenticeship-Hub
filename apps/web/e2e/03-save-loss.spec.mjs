import { test, expect } from "@playwright/test";

import { draftNoteId, login } from "./helpers.mjs";

const REVISIONS_PATTERN = "**/api/backend/api/v1/notes/*/revisions";

test("保存响应丢失：本机待核对记录保留，重试返回同一版本且不重复入库", async ({ page }) => {
  await login(page);
  const noteId = draftNoteId();
  const historyResponse = await page.request.get(`/api/backend/api/v1/notes/${noteId}`);
  const before = await historyResponse.json();
  const beforeCount = before.versions.length;

  await page.goto(`/notes/${noteId}`);
  const content = page.getByRole("textbox", { name: "笔记内容" });
  await expect(content).toBeEnabled();
  const typed = "自动化验收：响应丢失前保存的正文内容。";
  await content.fill(typed);

  // 请求真实到达服务端（落库），但响应被丢弃——客户端看到的是“结果不明”。
  await page.route(REVISIONS_PATTERN, async (route) => {
    await route.fetch();
    await route.abort("connectionreset");
  });
  await page.getByRole("button", { name: "保存为草稿" }).click();

  const banner = page.getByText("次保存的入库结果尚未确认");
  await expect(banner).toBeVisible();

  // 服务端已入库：恰好 +1 个版本。
  const mid = await (await page.request.get(`/api/backend/api/v1/notes/${noteId}`)).json();
  expect(mid.versions.length).toBe(beforeCount + 1);

  // 恢复网络路径后用原内容重试：同一 request_id 由服务端幂等收敛。
  await page.unroute(REVISIONS_PATTERN);
  await page.getByRole("button", { name: "用原内容重试" }).click();
  await expect(page.getByText("已补交上次未完成的保存")).toBeVisible();
  await expect(banner).toBeHidden();

  const after = await (await page.request.get(`/api/backend/api/v1/notes/${noteId}`)).json();
  expect(after.versions.length).toBe(beforeCount + 1); // 不产生重复版本
  expect(after.versions.at(-1).content).toContain("响应丢失前保存的正文内容");
});
