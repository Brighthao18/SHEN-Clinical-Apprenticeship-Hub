import { test, expect } from "@playwright/test";

import { login } from "./helpers.mjs";

// 阶段 empty（种子只有 20 篇定稿、无任何草稿、全新浏览器上下文无本机记录）：
// “只有定稿时为空态”——继续工作必须给出空态说明，而不是留白或报错。
test("全部作品已定稿且无本机记录时，继续工作显示诚实空态", async ({ page }) => {
  await login(page);
  const region = page.getByRole("region", { name: "继续工作" });
  await expect(region.getByText("没有进行中的工作")).toBeVisible();
  await expect(region.getByText("编辑中", { exact: true })).toHaveCount(0);
  await expect(region.getByText("待核对", { exact: true })).toHaveCount(0);
  await expect(region.getByText("进行中的作品", { exact: true })).toHaveCount(0);
});
