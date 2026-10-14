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
  await page.getByLabel("数据集名称").fill("模型独立验收数据");
  await page.getByLabel("公开数据来源").selectOption({ index: 1 });
  await page.getByRole("button", { name: "创建研究数据集" }).click();
  await expect(page.getByRole("heading", { name: "限定研究就绪" })).toBeVisible(
    { timeout: 60000 },
  );
  const id = page.url().split("/").at(-1);
  for (const plugin of ["lr", "rf"]) {
    await page.getByRole("link", { name: "模型研究", exact: true }).click();
    await page.getByLabel("已发布数据集").selectOption(id);
    await page.getByLabel("算法").selectOption(plugin);
    await page.getByRole("button", { name: "开始研究训练" }).click();
    await expect(
      page.getByRole("heading", { name: "研究工件已注册" }),
    ).toBeVisible({ timeout: 45000 });
    await expect(
      page.getByRole("region", { name: "模型评估指标" }),
    ).toBeVisible();
    const downloading = page.waitForEvent("download");
    await page
      .getByRole("button", { name: "下载 report.json", exact: true })
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
    await page.getByText("参数、校准、区间与复现信息", { exact: true }).click();
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(360);
    await page.getByText("固定来源与分区审计", { exact: true }).click();
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
