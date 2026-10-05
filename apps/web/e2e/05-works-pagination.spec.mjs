import { test, expect } from "@playwright/test";

import { login } from "./helpers.mjs";

const pagePatientId = () => process.env.E2E_PAGE_PATIENT_ID;
const workTitle = (index) => `分页验收作品 ${String(index).padStart(3, "0")}`;

function worksSection(page) {
  return page.getByRole("region", { name: "该患者的师承作品" });
}

async function firstPageLinks(section) {
  return section
    .getByRole("link")
    .filter({ hasText: /分页验收作品 \d{3}/ })
    .evaluateAll((anchors) => anchors.map((a) => new URL(a.href).pathname));
}

test("作品列表分页：total 如实、第 201–205 可见可打开、跨页不重不漏、新建作品可定位", async ({
  page,
}) => {
  const patientId = pagePatientId();
  await login(page);
  // 首个用例运行时数据集恰为 205 件（无新建）：total/页数如实断言。
  const total = 205;
  const pages = Math.ceil(total / 20);
  await page.goto(`/patients/${patientId}`);
  const section = worksSection(page);

  // 第一页（最近更新优先）：20 条 + 分页导航 + total 如实。
  await expect(section.getByText(`共 ${total} 件作品`)).toBeVisible();
  await expect(section.getByText(`第 1 / ${pages} 页`)).toBeVisible();
  await expect(section.getByRole("link").filter({ hasText: /分页验收作品 \d{3}/ })).toHaveCount(20);
  // 创建序号 201–205（数据集尾部，超出旧 200 条上限）在最近更新排序的第一页
  // 顶部，可见。
  for (const index of [205, 204, 203, 202, 201]) {
    await expect(section.getByText(workTitle(index))).toBeVisible();
  }
  const firstLinks = await firstPageLinks(section);
  expect(new Set(firstLinks).size).toBe(20);

  // 打开其中一件（第 205 件）：可打开。
  await section.getByText(workTitle(205)).click();
  await page.waitForURL("**/notes/**");
  await expect(page.getByRole("heading", { name: workTitle(205) })).toBeVisible();

  // 回到列表翻到最后一页：最早创建的作品可见，下一页禁用。
  // 每次点击后等页码标签更新，避免点击快于数据重渲染。
  await page.goto(`/patients/${patientId}`);
  const lastSection = worksSection(page);
  for (let pageNumber = 2; pageNumber <= pages; pageNumber += 1) {
    await lastSection.getByRole("button", { name: "下一页" }).click();
    await expect(lastSection.getByText(`第 ${pageNumber} / ${pages} 页`)).toBeVisible();
  }
  await expect(lastSection.getByRole("button", { name: "下一页" })).toBeDisabled();
  // 最后一页是创建序号最小的作品（含第 1 件）。
  for (const index of [5, 4, 3, 2, 1]) {
    await expect(lastSection.getByText(workTitle(index))).toBeVisible();
  }
  const lastLinks = await firstPageLinks(lastSection);
  // 跨页不重不漏（抽查首末两页互不重复）。
  expect(new Set([...firstLinks, ...lastLinks]).size).toBe(firstLinks.length + lastLinks.length);

  // 新建作品可定位：创建后刷新，出现在第一页顶部并可打开。
  const created = await page.request.post(`/api/backend/api/v1/patients/${patientId}/notes`, {
    data: {
      note_type: "case_record",
      content: "自动化验收：新建的作品应当出现在列表最前。",
      encounter_ids: [process.env.E2E_PAGE_ENCOUNTER_OLDER_ID],
    },
  });
  expect(created.status()).toBe(201);
  const createdId = (await created.json()).note_id;

  await page.goto(`/patients/${patientId}`);
  const refreshed = worksSection(page);
  const newestLink = refreshed.locator(`a[href="/notes/${createdId}"]`);
  await expect(newestLink).toBeVisible();
  await newestLink.click();
  await page.waitForURL(`**/notes/${createdId}`);
  await expect(page.getByText("自动化验收：新建的作品应当出现在列表最前。")).toBeVisible();
});

test("作品翻页保护：快速双击不重不漏、慢响应不回跳、失败保持当前页并可重试", async ({
  page,
}) => {
  const patientId = pagePatientId();
  await login(page);
  // 第一个用例已新建一篇：total 动态获取（206）。
  const total = await (async () => {
    const response = await page.request.get(
      `/api/backend/api/v1/patients/${patientId}/notes?limit=1`,
    );
    return (await response.json()).total;
  })();
  const pages = Math.ceil(total / 20);
  await page.goto(`/patients/${patientId}`);
  const section = worksSection(page);
  const next = section.getByRole("button", { name: "下一页" });
  await expect(section.getByText(`共 ${total} 件作品`)).toBeVisible();

  // 快速双击 + 第一击响应被延迟（乱序窗口）：加载期间按钮禁用，
  // RequestGeneration 让同页的迟到响应不覆盖、不重复；最终停在第 2 页。
  await page.route("**/api/backend/api/v1/patients/*/notes*offset=20*", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 600));
    await route.continue();
  });
  await next.dblclick();
  await expect(section.getByText(`第 2 / ${pages} 页`)).toBeVisible();
  const page2Links = await section
    .getByRole("link")
    .filter({ hasText: /分页验收作品 \d{3}/ })
    .evaluateAll((anchors) => anchors.map((a) => new URL(a.href).pathname));
  expect(new Set(page2Links).size).toBe(20);

  // 翻到第 3 页的请求失败：保持第 3 页入口的当前态可见错误并可重试，
  // 重试成功后第 3 页条目正确（不漏项、不回跳）。
  await page.unroute("**/api/backend/api/v1/patients/*/notes*offset=20*");
  await page.route("**/api/backend/api/v1/patients/*/notes*offset=40*", (route) => route.abort());
  await next.click();
  await expect(section.getByText(/作品列表翻页失败/)).toBeVisible();
  await expect(section.getByText(`第 2 / ${pages} 页`)).toBeVisible(); // 保持当前页
  await page.unroute("**/api/backend/api/v1/patients/*/notes*offset=40*");
  await section.getByRole("button", { name: "重试本页" }).click();
  await expect(section.getByText(`第 3 / ${pages} 页`)).toBeVisible();
  const page3Links = await section
    .getByRole("link")
    .filter({ hasText: /分页验收作品 \d{3}/ })
    .evaluateAll((anchors) => anchors.map((a) => new URL(a.href).pathname));
  expect(new Set(page3Links).size).toBe(20);
  expect(page3Links.filter((path) => page2Links.includes(path))).toHaveLength(0);
});

test("医案显示更多：防重入、失败可重试、追加按 note_id 去重", async ({ page }) => {
  const patientId = pagePatientId();
  await login(page);
  // 第一个用例已新建一篇医案：total 动态获取（206）。
  const total = await (async () => {
    const response = await page.request.get(
      `/api/backend/api/v1/patients/${patientId}/notes?note_type=case_record&limit=1`,
    );
    return (await response.json()).total;
  })();
  await login(page);
  await page.goto(`/patients/${patientId}`);
  // 医案整理面板收纳在 <details id="case-record-tool"> 里，默认折叠：
  // 展开后 CaseRecordPanel 才挂载并拉取作品列表。
  await page.locator("#case-record-tool summary").click();
  const panel = page.locator("#case-record");
  const loadMore = panel.getByRole("button", { name: /显示更多医案/ });
  await expect(loadMore).toHaveText(new RegExp(`已加载 200 / ${total}`));

  // 快速双击：按钮在加载期间禁用（防重入），只追加一页。
  await page.route("**/api/backend/api/v1/patients/*/notes*offset=200*", async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 500));
    await route.continue();
  });
  const caseSelect = panel.getByRole("combobox", { name: /查看已有医案/ });
  await loadMore.dblclick();
  // 双击的两路并发请求按 note_id 去重合并：全部加载完成后按钮消失（合法）。
  await expect(caseSelect.locator("option")).toHaveCount(total);
  await expect(loadMore).toHaveCount(0);

  // 全部加载后按钮消失（无“没有更多”的悬挂提示与重复入口）。
  await expect(loadMore).toHaveCount(0);
  const optionValues = await caseSelect.locator("option").evaluateAll((options) =>
    options.map((option) => option.value),
  );
  expect(new Set(optionValues).size).toBe(optionValues.length);
});

