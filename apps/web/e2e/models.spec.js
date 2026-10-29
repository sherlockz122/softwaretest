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
test.afterEach(async ({ page }) => {
  if (await page.getByRole("button", { name: "退出登录" }).count())
    await page.getByRole("button", { name: "退出登录" }).click();
});
test("new-volume LR/RF research registers and downloads checked artifacts", async ({
  page,
}, info) => {
  test.setTimeout(120000);
  await login(page);
  // Build our own small fixed dataset; do not depend on another test's side effects.
  await page.getByRole("link", { name: "数据集", exact: true }).click();
  await page.getByLabel("起个名字").fill("模型独立验收数据");
  await page.getByLabel("用哪份公开数据").selectOption({ index: 1 });
  await page.getByRole("button", { name: "生成研究数据集" }).click();
  await expect(page.getByRole("heading", { name: "数据集已可用" })).toBeVisible(
    { timeout: 60000 },
  );
  const id = page.url().split("/").at(-1);
  for (const plugin of ["lr", "rf"]) {
    await page.getByRole("link", { name: "模型研究", exact: true }).click();
    await page.getByLabel("用哪个数据集").selectOption(id);
    await page.getByLabel("用哪种模型").selectOption(plugin);
    await page.getByRole("button", { name: "开始训练" }).click();
    await expect(
      page.getByRole("heading", { name: "模型已生成" }),
    ).toBeVisible({ timeout: 45000 });
    await expect(
      page.getByRole("region", { name: "模型评估指标" }),
    ).toBeVisible();
    const downloading = page.waitForEvent("download");
    await page
      .getByRole("button", { name: "下载 评估报告（report.json）", exact: true })
      .click();
    const file = info.outputPath(`${plugin}-report.json`);
    await (await downloading).saveAs(file);
    const report = JSON.parse(fs.readFileSync(file, "utf8"));
    expect(report.request.plugin).toBe(plugin);
    expect(report.metrics.test.support).toEqual({ clean: 3, buggy: 3 });
    expect(report.capabilities.candidate_release).toBe(false);
    await page.setViewportSize({ width: 360, height: 800 });
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(360);
    await page.getByText("完整参数与复现信息（JSON）", { exact: true }).click();
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(360);
    await page.getByText("数据来源与划分记录（JSON）", { exact: true }).click();
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(360);
    const region = page.getByRole("region", {
      name: "模型评估指标",
      exact: true,
    });
    await region.focus();
    await page.keyboard.press("ArrowRight");
    await expect
      .poll(() => region.evaluate((el) => el.scrollLeft))
      .toBeGreaterThan(0);
    await page.screenshot({
      path: info.outputPath(`${plugin}-research-mobile.png`),
      fullPage: true,
    });
  }
});
