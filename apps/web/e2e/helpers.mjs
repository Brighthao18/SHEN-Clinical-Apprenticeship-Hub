import { expect } from "@playwright/test";

// 断言所用的合成数据文案，与 e2e/fixtures/seed_dashboard.py 保持一致。
export const OLDER_CASE = /咳嗽加重，痰白清稀/;
export const OLDER_HEADING = "2026/8/1";
export const DRAFT_TITLE = "较早的未完成作品";
export const LOCAL_NOTE_TEXT = "自动化验收：本机编辑缓冲";
export const LOCAL_STUDENT_TEXT = "自动化验收：旧补录直达链接";
export const LOCAL_STUDENT_PREVIEW = /自动化验收：旧补录直达/;

export function envRequired(name) {
  const value = process.env[name];
  if (!value) throw new Error(`missing required env ${name} (set by e2e/run.mjs)`);
  return value;
}

/** 登录并等到工作台渲染。未登录访问会被 proxy 闸门重定向到 /login?next=…。 */
export async function login(page) {
  await page.goto("/");
  if (page.url().includes("/login")) {
    await page.getByRole("textbox", { name: "用户名" }).fill("admin");
    await page.getByRole("textbox", { name: "密码" }).fill("shen-dev-only");
    await page.getByRole("button", { name: "进入我的师承工作台" }).click();
  }
  await page.getByRole("heading", { name: "我的师承工作台" }).waitFor();
}

/**
 * 注入两条本机恢复记录（页面脚本运行前写入 localStorage）：
 * 一条作品编辑缓冲（标题依赖服务器列表解析，接口失败时回退为“作品”），
 * 一条带 patientId 的学生补录缓冲（直达深链，不依赖 owner 解析）。
 */
export function addLocalRecords(page) {
  const savedAt = "2026-09-10T12:00:00Z";
  const records = {
    "shen.note-buffer.v2.e2e-local-note.e2e-v1.e2e-session": JSON.stringify({
      noteId: "e2e-local-note",
      versionId: "e2e-v1",
      version: 1,
      text: LOCAL_NOTE_TEXT,
      savedAt,
      sessionId: "e2e-session",
    }),
    [`shen.student-note-buffer.v2.${envRequired("E2E_ENCOUNTER_NEWER_ID")}.e2e-session-student`]:
      JSON.stringify({
        encounterId: envRequired("E2E_ENCOUNTER_NEWER_ID"),
        patientId: envRequired("E2E_PATIENT_ID"),
        noteKind: "teacher_oral",
        content: LOCAL_STUDENT_TEXT,
        savedAt,
        sessionId: "e2e-session-student",
      }),
  };
  return page.addInitScript((entries) => {
    for (const [key, value] of Object.entries(entries)) localStorage.setItem(key, value);
  }, records);
}

/** 在继续工作区断言两条本机记录可见。 */
export async function expectLocalRecordsVisible(page) {
  const region = page.getByRole("region", { name: "继续工作" });
  await expect(region.getByText(LOCAL_STUDENT_PREVIEW)).toBeVisible();
  await expect(region.getByText(/未保存编辑 · 基于 v1/)).toBeVisible();
  return region;
}

/** 种子阶段注入的较早草稿 note id（03/04 用例对它做写操作）。 */
export function draftNoteId() {
  return envRequired("E2E_DRAFT_NOTE_ID");
}
