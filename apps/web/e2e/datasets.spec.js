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
test("fresh research dataset survives reload and exports frozen train CSV", async ({
  page,
}, info) => {
  await login(page);
  await page.getByRole("link", { name: "数据集", exact: true }).click();
  await expect(page.getByLabel("公开数据来源").locator("option")).toHaveCount(
    2,
  );
  await page.getByLabel("数据集名称").fill("新环境研究验收");
  await page.getByLabel("公开数据来源").selectOption({ index: 1 });
  await page.getByRole("button", { name: "创建研究数据集" }).click();
  await expect(page).toHaveURL(/\/datasets\/[0-9a-f-]{36}$/);
  await expect(page.getByRole("heading", { name: "限定研究就绪" })).toBeVisible(
    { timeout: 60000 },
  );
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "登录", exact: true }),
  ).toBeVisible();
  await page
    .getByLabel("用户名", { exact: true })
    .fill(config.DG_BOOTSTRAP_USERNAME);
  await page
    .getByLabel("密码", { exact: true })
    .fill(config.DG_BOOTSTRAP_PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "限定研究就绪" }),
  ).toBeVisible();
  await expect(page.getByText("历史在线回放", { exact: false })).toBeVisible();
  const downloading = page.waitForEvent("download");
  await page.getByRole("button", { name: "下载 train CSV" }).click();
  const download = await downloading;
  const file = info.outputPath("research-train.csv");
  await download.saveAs(file);
  const csv = fs.readFileSync(file, "utf8");
  expect(csv.split(/\r?\n/).filter(Boolean)).toHaveLength(29);
  expect(csv.split(/\r?\n/)[0].split(",")).toHaveLength(16);
  expect(csv.split(/\r?\n/)[0].split(",")).not.toContain("fix");
  await page.screenshot({
    path: info.outputPath("dataset-ready.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 360, height: 800 });
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(360);
  await page.getByText("来源、分区和能力审计", { exact: true }).click();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(360);
  await page.screenshot({
    path: info.outputPath("dataset-detail-mobile.png"),
    fullPage: true,
  });
});
test("dataset list and research controls fit a narrow viewport", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 360, height: 800 });
  await login(page);
  await page.getByRole("link", { name: "数据集", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "数据集", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("研究使用原发布的弱监督标签", { exact: false }),
  ).toBeVisible();
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(360);
  const region = page.getByRole("region", { name: "数据集列表", exact: true });
  expect(await region.evaluate((el) => el.scrollWidth > el.clientWidth)).toBe(
    true,
  );
  await region.focus();
  await page.keyboard.press("ArrowRight");
  await expect
    .poll(() => region.evaluate((el) => el.scrollLeft))
    .toBeGreaterThan(0);
  await page.screenshot({
    path: info.outputPath("dataset-mobile.png"),
    fullPage: true,
  });
});
