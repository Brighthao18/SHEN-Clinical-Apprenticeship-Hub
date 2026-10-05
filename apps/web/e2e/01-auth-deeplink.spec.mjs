import { test, expect } from "@playwright/test";

import { envRequired } from "./helpers.mjs";

const patientId = () => envRequired("E2E_PATIENT_ID");
const olderEncounterId = () => envRequired("E2E_ENCOUNTER_OLDER_ID");

test("未登录深链被闸门重定向，登录后回到原位置（含 encounter 参数）", async ({ page }) => {
  const target = `/patients/${patientId()}?encounter=${olderEncounterId()}`;
  await page.goto(target);
  await expect(page).toHaveURL(/\/login\?next=/);
  await page.getByRole("textbox", { name: "用户名" }).fill("admin");
  await page.getByRole("textbox", { name: "密码" }).fill("shen-dev-only");
  await page.getByRole("button", { name: "进入我的师承工作台" }).click();
  await page.waitForURL(`**/patients/${patientId()}?encounter=${olderEncounterId()}`);
  // 回跳后落在深链指定的就诊上（较早就诊），不是患者默认就诊。
  await expect(page.getByRole("heading", { name: "2026/8/1", exact: true })).toBeVisible();
});
