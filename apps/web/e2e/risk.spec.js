import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
const root = path.resolve("../..");
const config = Object.fromEntries(
  fs
    .readFileSync(process.env.DG_E2E_ENV_FILE || path.join(root, ".env"), "utf8")
    .split(/\r?\n/)
    .filter((line) => line && !line.startsWith("#"))
    .map((line) => {
      const split = line.indexOf("=");
      return [line.slice(0, split), line.slice(split + 1)];
    }),
);
async function login(page) {
  await page.goto("/login");
  await page.getByLabel("用户名", { exact: true }).fill(config.DG_BOOTSTRAP_USERNAME);
  await page.getByLabel("密码", { exact: true }).fill(config.DG_BOOTSTRAP_PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("heading", { name: "任务中心", exact: true })).toBeVisible();
}
const probabilityOf = async (row) => {
  const cells = row.locator("td");
  const text = await cells.nth(1).innerText();
  return parseFloat(text.replace("%", ""));
};
test.afterEach(async ({ page }) => {
  if (await page.getByRole("button", { name: "退出登录" }).count())
    await page.getByRole("button", { name: "退出登录" }).click();
});
test("risk panel sorts, filters, paginates and surfaces actionable errors", async ({
  page,
}) => {
  test.setTimeout(240000);
  await login(page);
  await page.getByRole("link", { name: "数据集", exact: true }).click();
  await page.getByLabel("起个名字").fill("风险面板复核数据");
  await page.getByLabel("用哪份公开数据").selectOption({ index: 1 });
  await page.getByRole("button", { name: "生成研究数据集" }).click();
  await expect(page.getByRole("heading", { name: "数据集已可用" })).toBeVisible({
    timeout: 60000,
  });
  const id = page.url().split("/").at(-1);
  await page.getByRole("link", { name: "模型研究", exact: true }).click();
  await page.getByLabel("用哪个数据集").selectOption(id);
  await page.getByRole("button", { name: "开始训练" }).click();
  await expect(page.getByRole("heading", { name: "模型已生成" })).toBeVisible({
    timeout: 60000,
  });

  // 1. 面板可见，排序按概率降序（第一行不小于第二行）
  const panel = page.getByRole("heading", { name: "每条提交的风险排序" });
  await expect(panel).toBeVisible();
  // 页面上还有指标表等多个表格，风险表用提交号单元格（class=sha）定位
  const rows = page.locator("tbody tr:has(td.sha)");
  await expect(rows.first()).toBeVisible();
  const first = await probabilityOf(rows.nth(0));
  const second = await probabilityOf(rows.nth(1));
  expect(first).toBeGreaterThanOrEqual(second);

  // 2. 分区过滤：只看测试后所有行都是测试区
  await page.getByLabel("看哪份数据").selectOption("test");
  await expect(rows.first()).toBeVisible();
  const partitions = await page
    .locator("tbody tr:has(td.sha) td:nth-child(5)")
    .allInnerTexts();
  expect(partitions.length).toBeGreaterThan(0);
  expect(partitions.every((text) => text.includes("测试"))).toBe(true);

  // 3. 翻页：默认只看验证与测试，总条数超过一页
  await page.getByLabel("看哪份数据").selectOption("");
  await expect(rows.first()).toBeVisible();
  const pageText = await page.getByText(/共 \d+ 条，第 \d+ \/ \d+ 页/).innerText();
  const pages = Number(pageText.match(/第 \d+ \/ (\d+) 页/)[1]);
  if (pages > 1) {
    const next = page.getByRole("button", { name: "下一页" });
    await expect(next).toBeEnabled();
    await next.click();
    await expect(page.getByText(/共 \d+ 条，第 2 \/ \d+ 页/)).toBeVisible();
    const previous = page.getByRole("button", { name: "上一页" });
    await expect(previous).toBeEnabled();
    await previous.click();
  }

  // 4. 错误提示：模拟工件漂移响应，面板给出可行动提示而不是空表
  // 只拦截一次：之后的请求放行，恢复后无需依赖 unroute 的匹配语义
  let failOnce = true;
  await page.route(
    (url) => url.pathname.endsWith("/risk-items"),
    (route) => {
      if (failOnce) {
        failOnce = false;
        return route.fulfill({
          status: 409,
          contentType: "application/json",
          body: JSON.stringify({
            code: "MODEL_ARTIFACT_DRIFT",
            message: "模型工件校验失败，已停止下载。",
            request_id: "browser-review",
          }),
        });
      }
      return route.continue();
    },
  );
  await page.getByLabel("看哪份数据").selectOption("test");
  await expect(
    page.getByText("模型工件校验失败，已停止下载。", { exact: false }),
  ).toBeVisible();
  await page.getByLabel("看哪份数据").selectOption("");
  await expect(rows.first()).toBeVisible();
});
