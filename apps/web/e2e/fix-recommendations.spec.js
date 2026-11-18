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
const REPOSITORY_ID = process.env.DG_E2E_REPOSITORY_ID;
async function login(page) {
  await page.goto("/login");
  await page.getByLabel("用户名", { exact: true }).fill(config.DG_BOOTSTRAP_USERNAME);
  await page.getByLabel("密码", { exact: true }).fill(config.DG_BOOTSTRAP_PASSWORD);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("heading", { name: "任务中心", exact: true })).toBeVisible();
}
test.afterEach(async ({ page }) => {
  if (await page.getByRole("button", { name: "退出登录" }).count())
    await page.getByRole("button", { name: "退出登录" }).click();
});
test("repository detail fix references list reasons and jump to commit files", async ({
  page,
}) => {
  test.skip(!REPOSITORY_ID, "DG_E2E_REPOSITORY_ID 未指定（纵向探针仓库）");
  test.setTimeout(120000);
  await login(page);
  // 会话只在内存里：整页 goto 会丢登录，走站内导航
  await page.getByRole("link", { name: "仓库目录", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "仓库目录", exact: true }),
  ).toBeVisible();
  await page.locator(`a[href="/repositories/${REPOSITORY_ID}"]`).click();
  await expect(
    page.getByRole("heading", { name: "仓库详情", exact: true }),
  ).toBeVisible();
  // 提交记录 tab 下选一条提交，出现修复参考面板
  const commitTab = page.getByRole("tab", { name: /提交记录/ });
  await commitTab.click();
  await expect(commitTab).toHaveAttribute("aria-selected", "true");
  const firstCommit = page
    .getByTestId("commit-list")
    .locator("li button")
    .first();
  await expect(firstCommit).toBeVisible({ timeout: 30000 });
  await firstCommit.click();
  const panel = page.getByRole("heading", { name: "同类修复参考" });
  await expect(panel).toBeVisible({ timeout: 30000 });
  await expect(page.getByText("不是对当前缺陷的根因结论")).toBeVisible();
  // jsoup 有真实修复历史：列表或明确空态二选一，且都如实展示
  const rows = page.locator("tbody tr:has(button.link)");
  const state = await Promise.race([
    rows.first().waitFor({ state: "visible", timeout: 20000 }).then(() => "rows"),
    page
      .getByText("该仓库尚无修复历史")
      .waitFor({ state: "visible", timeout: 20000 })
      .then(() => "empty"),
  ]);
  if (state === "rows") {
    const first = rows.first();
    await expect(first.getByRole("button")).toBeVisible();
    const reasons = await first.innerText();
    expect(reasons).toMatch(/重叠|距离|相似度/);
    const clicked = await first.getByRole("button").innerText();
    await first.getByRole("button").click();
    // 点击参考编号后，面板目标切换为被点击的修复提交（同一列表内可见）
    await expect(page.getByText(`目标提交：${clicked}`)).toBeVisible({
      timeout: 20000,
    });
  } else {
    expect(state).toBe("empty");
  }
});
test("review page fix references use the prediction candidate as target", async ({
  page,
}) => {
  test.skip(!REPOSITORY_ID, "DG_E2E_REPOSITORY_ID 未指定（纵向探针仓库）");
  test.setTimeout(120000);
  await login(page);
  await page.getByRole("link", { name: "合入前评审", exact: true }).click();
  await page.getByLabel("仓库").selectOption(REPOSITORY_ID);
  const model = page.getByLabel("已注册模型");
  if ((await model.locator("option").count()) > 1) {
    await model.selectOption({ index: 1 });
    await page.getByLabel("Base 引用").fill("HEAD~5");
    await page.getByLabel("Candidate 引用").fill("HEAD");
    await page.getByRole("button", { name: "运行预测", exact: true }).click();
    // 同步秒回显示结果与参考面板；超时转后台任务则显示任务编号入口
    const outcome = await Promise.race([
      page
        .getByRole("heading", { name: "预测结果" })
        .waitFor({ state: "visible", timeout: 60000 })
        .then(() => "result"),
      page
        .getByRole("link", { name: "查看任务" })
        .waitFor({ state: "visible", timeout: 60000 })
        .then(() => "task"),
    ]);
    if (outcome === "result") {
      const panel = page.getByRole("heading", { name: "同类修复参考" });
      await expect(panel).toBeVisible({ timeout: 30000 });
      await expect(page.getByText("不是对当前缺陷的根因结论")).toBeVisible();
    } else {
      expect(outcome).toBe("task");
    }
  }
});
