import { test, expect } from "@playwright/test";

import {
  DRAFT_TITLE,
  OLDER_CASE,
  OLDER_HEADING,
  addLocalRecords,
  expectLocalRecordsVisible,
  login,
} from "./helpers.mjs";

const olderEncounterId = () => process.env.E2E_ENCOUNTER_OLDER_ID;

test("F1：较早的进行中草稿不会被最近的定稿挤出", async ({ page }) => {
  await login(page);
  const region = page.getByRole("region", { name: "继续工作" });
  await expect(region.getByText(DRAFT_TITLE)).toBeVisible();
  await expect(region.getByText("进行中的作品")).toBeVisible();
});

test("F2：最近病例卡片深链到所展示的就诊，刷新后保持", async ({ page }) => {
  await login(page);
  await page.getByRole("link", { name: OLDER_CASE }).click();
  await page.waitForURL(`**/patients/**?encounter=${olderEncounterId()}`);
  await expect(page.getByRole("heading", { name: OLDER_HEADING, exact: true })).toBeVisible();
  await expect(page.getByText("这是最早一次就诊")).toBeVisible();

  await page.reload();
  await expect(page).toHaveURL(new RegExp(`encounter=${olderEncounterId()}`));
  await expect(page.getByRole("heading", { name: OLDER_HEADING, exact: true })).toBeVisible();
});

test("F3a：dashboard 接口失败时错误归属准确，本机恢复入口保持可达，重试恢复", async ({
  page,
}) => {
  await login(page);
  await addLocalRecords(page);
  await page.route("**/api/backend/api/v1/dashboard*", (route) => route.abort());
  await page.goto("/");
  await expect(page.getByText("本周概况与最近病例读取失败")).toBeVisible();
  const region = await expectLocalRecordsVisible(page);
  // 服务器作品列表不受 dashboard 故障影响，正常加载。
  await expect(region.getByText(DRAFT_TITLE)).toBeVisible();

  await page.unroute("**/api/backend/api/v1/dashboard*");
  await page
    .getByRole("button", { name: "重试加载" })
    .first()
    .click();
  await expect(page.getByRole("region", { name: "本周概况" })).toBeVisible();
  await expect(page.getByText("本周概况与最近病例读取失败")).toHaveCount(0);
});

test("F3b：notes 接口持续挂起时本机恢复入口仍可达", async ({ page }) => {
  await login(page);
  await addLocalRecords(page);
  await page.route("**/api/backend/api/v1/notes?*", () => {
    // 永不 fulfill：模拟长时间挂起。
  });
  await page.goto("/");
  const region = await expectLocalRecordsVisible(page);
  await expect(region.getByText("正在读取进行中的作品…")).toBeVisible();
});

test("F3c：notes 接口失败时错误框归属准确，重试后列表恢复", async ({ page }) => {
  await login(page);
  await addLocalRecords(page);
  await page.route("**/api/backend/api/v1/notes?*", (route) => route.abort());
  await page.goto("/");
  const region = await expectLocalRecordsVisible(page);
  await expect(region.getByText("进行中作品读取失败")).toBeVisible();
  await expect(region.getByText("上方本机草稿与待核对记录不受影响")).toBeVisible();
  // 统计区正常（故障只影响作品列表）。
  await expect(page.getByRole("region", { name: "本周概况" })).toBeVisible();

  await page.unroute("**/api/backend/api/v1/notes?*");
  await region.getByRole("button", { name: "重试加载" }).click();
  await expect(region.getByText(DRAFT_TITLE)).toBeVisible();
  await expect(region.getByText("进行中作品读取失败")).toHaveCount(0);
});
