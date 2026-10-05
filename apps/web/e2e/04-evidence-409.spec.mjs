import { test, expect } from "@playwright/test";

import { draftNoteId, login } from "./helpers.mjs";

test("依据预览后资料变化：采用被 409 拒绝并要求重新预览，重开后采用成功", async ({ page }) => {
  await login(page);
  const noteId = draftNoteId();
  const before = await (await page.request.get(`/api/backend/api/v1/notes/${noteId}`)).json();
  const beforeCount = before.versions.length;
  const evidenceEncounterId = before.versions.at(-1).evidence_encounters[0];
  // 依据更新只生成新版本，正文不被自动改写（保留的是采用前的最新正文）。
  const latestBefore = before.versions.at(-1).content;

  await page.goto(`/notes/${noteId}`);
  await page.getByRole("button", { name: "更新依据…" }).click();
  const adopt = page.getByRole("button", { name: "采用所选资料（保存新草稿）" });
  await expect(adopt).toBeEnabled();

  // 预览之后、采用之前：材料发生变化（同一就诊新增一条学生补录）。
  const createNote = await page.request.post(
    `/api/backend/api/v1/encounters/${evidenceEncounterId}/student-notes`,
    {
      data: {
        note_kind: "student_observation",
        content: "自动化验收：预览之后新增的补录内容",
        request_id: crypto.randomUUID(),
      },
    },
  );
  expect(createNote.status()).toBe(201);

  // 采用：服务端按当前数据库重算凭证 → 与预览不匹配 → 409，原依据保持不变。
  page.once("dialog", (dialog) => dialog.accept());
  await adopt.click();
  await expect(page.getByText("所选资料在预览后已发生变化")).toBeVisible();
  await expect(page.getByText("请重新预览并确认后再采用")).toBeVisible();

  const rejected = await (await page.request.get(`/api/backend/api/v1/notes/${noteId}`)).json();
  expect(rejected.versions.length).toBe(beforeCount); // 409 不落库

  // 重新打开「更新依据」（按新资料重新预览）后采用成功。
  await page.getByRole("button", { name: "更新依据…" }).click();
  const adoptAgain = page.getByRole("button", { name: "采用所选资料（保存新草稿）" });
  await expect(adoptAgain).toBeEnabled();
  page.once("dialog", (dialog) => dialog.accept());
  await adoptAgain.click();
  await expect(page.getByText("已按当前资料整理并保存新版本")).toBeVisible();

  const after = await (await page.request.get(`/api/backend/api/v1/notes/${noteId}`)).json();
  expect(after.versions.length).toBe(beforeCount + 1);
  // 依据更新只生成新版本，正文不被自动改写。
  expect(after.versions.at(-1).content).toBe(latestBefore);
});
