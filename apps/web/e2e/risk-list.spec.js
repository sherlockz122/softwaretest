import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const root = path.resolve("../..");
const config = Object.fromEntries(
  fs
    .readFileSync(
      process.env.DG_E2E_ENV_FILE || path.join(root, ".env"),
      "utf8",
    )
    .split(/\r?\n/)
    .filter((line) => line && !line.startsWith("#"))
    .map((line) => {
      const split = line.indexOf("=");
      return [line.slice(0, split), line.slice(split + 1)];
    }),
);
const shot = (name) =>
  path.join(root, "docs/reports/assets", `nfr03-risk-list-${name}.png`);

async function login(page) {
  await page.goto("/login");
  await page
    .getByLabel("用户名", { exact: true })
    .fill(config.DG_BOOTSTRAP_USERNAME);
  await page
    .getByLabel("密码", { exact: true })
    .fill(config.DG_BOOTSTRAP_PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "任务中心", exact: true }),
  ).toBeVisible();
}

async function tableCells(page) {
  const table = page.locator("table").filter({
    has: page.getByRole("columnheader", { name: "提交时间", exact: true }),
  });
  const rows = table.locator("tbody tr");
  const count = await rows.count();
  const values = [];
  for (let index = 0; index < count; index += 1) {
    values.push(await rows.nth(index).locator("td").allInnerTexts());
  }
  return values;
}

test("historical risk list shows levels, times and pages through a real browser", async ({
  page,
}) => {
  const runId = process.env.DG_E2E_RUN_ID;
  expect(
    runId,
    "DG_E2E_RUN_ID must point at a registered model run",
  ).toBeTruthy();
  await login(page);
  await page.getByRole("link", { name: "模型研究", exact: true }).click();
  await page.getByRole("link", { name: runId, exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "历史风险列表", exact: true }),
  ).toBeVisible();

  const summary = page.getByText(/共 \d+ 条，第 \d+ \/ \d+ 页/u);
  await expect(summary).toBeVisible();
  await expect(summary).toContainText("判定阈值");
  await expect(page.getByLabel("分区", { exact: true })).toHaveValue("");

  const first = await tableCells(page);
  expect(first.length).toBeGreaterThan(5);
  for (const cells of first) {
    expect(cells).toHaveLength(5);
    expect(cells[0]).toMatch(/^(高|中|低)$/u);
    expect(cells[1]).toMatch(/^\d+\.\d%$/u);
    expect(cells[2]).toMatch(/^[0-9a-f]{12}$/u);
    expect(cells[3]).not.toBe("");
    expect(cells[4]).toMatch(/^(验证|测试)$/u);
  }
  await page.screenshot({ path: shot("1-page-1"), fullPage: true });

  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(summary).toContainText("第 2 /");
  const rowsOf = (rows) => rows.map((cells) => cells.join("|"));
  await expect
    .poll(async () => rowsOf(await tableCells(page))[0])
    .not.toBe(rowsOf(first)[0]);
  const second = await tableCells(page);
  expect(rowsOf(second)).not.toEqual(rowsOf(first));
  await page.screenshot({ path: shot("2-page-2"), fullPage: true });

  await page.getByLabel("分区", { exact: true }).selectOption("test");
  await expect
    .poll(async () =>
      (await tableCells(page)).every((cells) => cells[4] === "测试"),
    )
    .toBe(true);
  await expect(summary).toContainText("共 900 条");
  await page.screenshot({ path: shot("3-partition-test"), fullPage: true });

  await page.getByLabel("分区", { exact: true }).selectOption("train");
  await expect(page.getByText(/当前包含训练分区/u)).toBeVisible();
  await page.screenshot({ path: shot("4-partition-train"), fullPage: true });
});
