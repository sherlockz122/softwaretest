import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
const config = Object.fromEntries(
  fs
    .readFileSync(
      process.env.DG_E2E_ENV_FILE || path.resolve("../../.env"),
      "utf8",
    )
    .split(/\r?\n/)
    .filter((line) => line && !line.startsWith("#"))
    .map((line) => {
      const split = line.indexOf("=");
      return [line.slice(0, split), line.slice(split + 1)];
    }),
);
test("new-volume authenticated semantic asset readback, paging and mobile refresh", async ({
  page,
}, info) => {
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
  await page.getByRole("link", { name: "语义资产", exact: true }).click();
  await page.getByRole("link", { name: /660 条研究向量/ }).click();
  await expect(
    page.getByText("成员和向量完整性核验通过", { exact: false }),
  ).toBeVisible();
  await expect(page.getByRole("row")).toHaveCount(21);
  await page.getByLabel("成员分区").selectOption("validation");
  await expect(
    page.getByRole("cell", { name: "489", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "下一页", exact: true }).click();
  await expect(
    page.getByRole("cell", { name: "509", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "刷新资产详情", exact: true }).click();
  await expect(
    page.getByRole("cell", { name: "509", exact: true }),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(() => document.documentElement.scrollWidth),
  ).toBeLessThanOrEqual(390);
  await page.screenshot({
    path: info.outputPath("semantic-asset-mobile.png"),
    fullPage: true,
  });
  await expect(
    page.getByText("当前仅供研究查看", { exact: false }),
  ).toBeVisible();
  const detailPath = new URL(page.url()).pathname;
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "登录", exact: true }),
  ).toBeVisible();
  expect(new URL(page.url()).searchParams.get("next")).toBe(detailPath);
  await page
    .getByLabel("用户名", { exact: true })
    .fill(config.DG_BOOTSTRAP_USERNAME);
  await page
    .getByLabel("密码", { exact: true })
    .fill(config.DG_BOOTSTRAP_PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "语义资产详情", exact: true }),
  ).toBeVisible();
  expect(new URL(page.url()).pathname).toBe(detailPath);
  await expect(page.getByRole("row")).toHaveCount(21);
  await page.getByRole("button", { name: "退出登录" }).click();
});
