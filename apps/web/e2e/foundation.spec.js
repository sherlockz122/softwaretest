import { test, expect } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";
import { execFileSync } from "node:child_process";
import { randomUUID } from "node:crypto";
const root = path.resolve("../..");
const configFile = process.env.DG_E2E_ENV_FILE || path.join(root, ".env");
const config = Object.fromEntries(
  fs
    .readFileSync(configFile, "utf8")
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
async function create(page, duration = 0) {
  await page.getByLabel("诊断时长（秒）").fill(String(duration));
  await page.getByRole("button", { name: "创建诊断任务" }).click();
  await expect(page).toHaveURL(/\/tasks\/[0-9a-f-]{36}$/);
  return page.url().split("/").at(-1);
}
async function shot(page, testInfo, name) {
  const file = testInfo.outputPath(name + ".png");
  await page.screenshot({
    path: file,
    fullPage: true,
    mask: [page.locator("input[type=password]")],
  });
  await testInfo.attach(name, { path: file, contentType: "image/png" });
}
test.afterEach(async ({ page }) => {
  if (await page.getByRole("button", { name: "退出登录" }).count())
    await page.getByRole("button", { name: "退出登录" }).click();
});

test("real login, create, Worker success, persisted reload and logout", async ({
  page,
}, info) => {
  await login(page);
  const id = await create(page, 3);
  await expect(page.getByTestId("task-status")).toHaveText("已完成", {
    timeout: 45000,
  });
  await expect(page.getByRole("heading", { name: "执行结果" })).toBeVisible();
  await shot(page, info, "task-success");
  await page.getByRole("link", { name: "← 返回任务中心" }).click();
  await expect(page.getByRole("link", { name: id, exact: true })).toBeVisible();
  await page.getByRole("link", { name: id, exact: true }).click();
  expect(
    await page.evaluate(() => [localStorage.length, sessionStorage.length]),
  ).toEqual([0, 0]);
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
  await expect(page).toHaveURL(new RegExp("/tasks/" + id + "$"));
  await expect(page.getByTestId("task-status")).toHaveText("已完成");
  await page.getByRole("button", { name: "退出登录" }).click();
  await expect(
    page.getByRole("heading", { name: "登录", exact: true }),
  ).toBeVisible();
  await page.goto("/tasks");
  await expect(
    page.getByRole("heading", { name: "登录", exact: true }),
  ).toBeVisible();
});

test("running cancellation and manual successor use real Worker states", async ({
  page,
}, info) => {
  await login(page);
  const parent = await create(page, 30);
  await expect(page.getByTestId("task-status")).toHaveText("运行中");
  await page.getByRole("button", { name: "取消任务" }).click();
  await expect(page.getByTestId("task-status")).toHaveText("已取消", {
    timeout: 30000,
  });
  await page.getByRole("button", { name: "重试任务" }).click();
  await expect(page).not.toHaveURL(new RegExp("/tasks/" + parent + "$"));
  await expect(page.getByRole("link", { name: parent })).toBeVisible();
  await page.getByRole("button", { name: "取消任务" }).click();
  await expect(page.getByTestId("task-status")).toHaveText("已取消", {
    timeout: 30000,
  });
  await shot(page, info, "cancelled-successor");
});

test("lost create response retains key and replays the one persisted task", async ({
  page,
}) => {
  await login(page);
  let first = true;
  let accepted;
  const keys = [];
  await page.route("**/api/v1/tasks/diagnostic", async (route) => {
    keys.push(route.request().headers()["idempotency-key"]);
    const response = await route.fetch();
    const body = await response.json();
    if (first) {
      first = false;
      accepted = body.task_id;
      await route.abort("failed");
    } else {
      expect(body.task_id).toBe(accepted);
      await route.fulfill({ response });
    }
  });
  await page.getByRole("button", { name: "创建诊断任务" }).click();
  await expect(page.getByRole("alert")).toContainText("连接失败");
  await page.getByRole("button", { name: "创建诊断任务" }).click();
  await expect(page).toHaveURL(new RegExp("/tasks/" + accepted + "$"));
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
  await expect(page.getByTestId("task-status")).toHaveText("已完成", {
    timeout: 45000,
  });
});

test("transient query errors recover, empty filters and pagination are actionable", async ({
  page,
}, info) => {
  await login(page);
  await page.route("**/api/v1/tasks?**", (route) =>
    route.fulfill({
      status: 503,
      json: { code: "SYSTEM_DEPENDENCY_UNAVAILABLE", request_id: randomUUID() },
    }),
  );
  await page.getByRole("button", { name: "刷新列表" }).click();
  await expect(page.getByRole("alert")).toContainText("服务暂不可用");
  await shot(page, info, "recoverable-error");
  await page.unroute("**/api/v1/tasks?**");
  await page.getByRole("button", { name: "刷新列表" }).click();
  await expect(page.getByRole("alert")).toHaveCount(0);
  await page.getByLabel("状态筛选").selectOption("cancel_requested");
  await expect(page.getByText(/暂无符合条件的任务/)).toBeVisible();
  await expect(page.getByRole("button", { name: "下一页" })).toBeDisabled();
  await shot(page, info, "empty-filter");
});

test("revoked session redirects to login and clears task data", async ({
  page,
}) => {
  await login(page);
  await page.route("**/api/v1/tasks?**", (route) =>
    route.fulfill({ status: 401, json: { code: "AUTH_REQUIRED" } }),
  );
  await page.route("**/api/v1/auth/refresh", async (route) => {
    const csrf = route.request().headers()["x-csrf-token"];
    await page.request.post("/api/v1/auth/logout", {
      headers: { "X-CSRF-Token": csrf },
    });
    await route.continue();
  });
  await page.getByRole("button", { name: "刷新列表" }).click();
  await expect(
    page.getByRole("heading", { name: "登录", exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("table")).toHaveCount(0);
  await expect(page.getByText("会话已失效，请重新登录。")).toBeVisible();
});

test("expired access refreshes one session and leaves user on the task page", async ({
  page,
}) => {
  await login(page);
  let first = true;
  let refreshes = 0;
  page.on("request", (req) => {
    if (req.url().endsWith("/auth/refresh")) refreshes += 1;
  });
  await page.route("**/api/v1/tasks?**", async (route) => {
    if (first) {
      first = false;
      await route.fulfill({ status: 401, json: { code: "AUTH_REQUIRED" } });
    } else await route.continue();
  });
  await page.getByRole("button", { name: "刷新列表" }).click();
  await expect(
    page.getByRole("heading", { name: "任务中心", exact: true }),
  ).toBeVisible();
  await expect.poll(() => refreshes).toBe(1);
  await expect(page.getByRole("alert")).toHaveCount(0);
});

test("actual queue deadline failure appears with safe error and can be retried", async ({
  page,
}, info) => {
  const project = process.env.DG_E2E_PROJECT;
  expect(project).toMatch(/^dg-fresh-[0-9a-f]{12}$/);
  const docker = process.env.DG_DOCKER_EXE || "docker";
  const cli = (args) =>
    execFileSync(
      docker,
      [
        "compose",
        "-p",
        project,
        "--env-file",
        configFile,
        "-f",
        path.join(root, "compose.yaml"),
        ...args,
      ],
      { cwd: root, stdio: "pipe", timeout: 60000 },
    );
  cli(["--profile", "full", "stop", "worker"]);
  try {
    await login(page);
    const task = await create(page, 0);
    const python =
      process.env.DG_PYTHON_EXE ||
      path.join(
        root,
        ".venv",
        process.platform === "win32" ? "Scripts/python.exe" : "bin/python",
      );
    execFileSync(
      python,
      ["-m", "tests.browser_fault", "--config", configFile, "--task", task],
      { cwd: root, stdio: "pipe", timeout: 30000 },
    );
    await expect(page.getByTestId("task-status")).toHaveText("失败", {
      timeout: 35000,
    });
    await expect(page.getByRole("alert")).toContainText("TASK_QUEUE_TIMEOUT");
    await shot(page, info, "queue-timeout");
    await page.getByRole("button", { name: "重试任务" }).click();
    await expect(page).not.toHaveURL(new RegExp("/tasks/" + task + "$"));
    await page.getByRole("button", { name: "取消任务" }).click();
    await expect(page.getByTestId("task-status")).toHaveText("已取消");
  } finally {
    cli(["--profile", "full", "up", "-d", "--wait", "worker"]);
  }
});

test("mobile keyboard login and task details avoid page overflow", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/login");
  await page
    .getByLabel("用户名", { exact: true })
    .fill(config.DG_BOOTSTRAP_USERNAME);
  await page
    .getByLabel("密码", { exact: true })
    .fill(config.DG_BOOTSTRAP_PASSWORD);
  await page.getByLabel("密码", { exact: true }).press("Enter");
  await expect(
    page.getByRole("heading", { name: "任务中心", exact: true }),
  ).toBeVisible();
  await create(page, 0);
  await expect(page.getByTestId("task-status")).toHaveText("已完成", {
    timeout: 45000,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await shot(page, info, "mobile-task");
});

test("pagination and Viewer controls follow API data and route query", async ({
  page,
}) => {
  await page.route("**/api/v1/auth/login", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.user.role = "Viewer"; // UI-only role fixture; server RBAC has separate real tests.
    await route.fulfill({ response, json: data });
  });
  await page.route("**/api/v1/tasks?**", (route) => {
    const query = new URL(route.request().url()).searchParams;
    const pageNumber = Number(query.get("page"));
    const id =
      pageNumber === 1
        ? "00000000-0000-4000-8000-000000000001"
        : "00000000-0000-4000-8000-000000000002";
    return route.fulfill({
      json: {
        total: 11,
        page: pageNumber,
        page_size: 10,
        items: [
          {
            id,
            type: "diagnostic",
            status: "succeeded",
            progress: 100,
            heartbeat_at: null,
            created_at: null,
          },
        ],
      },
    });
  });
  await login(page);
  await expect(page.getByRole("button", { name: "创建诊断任务" })).toHaveCount(
    0,
  );
  await expect(page.getByRole("button", { name: "下一页" })).toBeEnabled();
  await page.getByRole("button", { name: "下一页" }).click();
  await expect(page).toHaveURL(/page=2/);
  await expect(
    page.getByRole("link", { name: "00000000-0000-4000-8000-000000000002" }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "下一页" })).toBeDisabled();
  await page.getByRole("button", { name: "上一页" }).click();
  await expect(
    page.getByRole("link", { name: "00000000-0000-4000-8000-000000000001" }),
  ).toBeVisible();
});

test("public repository lost-response replay, real Worker clone and persisted metadata", async ({
  page,
}, info) => {
  test.setTimeout(120000);
  await login(page);
  await page.getByRole("link", { name: "仓库目录", exact: true }).click();
  await expect(page.getByText("尚未添加仓库。")).toBeVisible();
  let first = true;
  let accepted;
  const keys = [];
  await page.route("**/api/v1/repositories", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    keys.push(route.request().headers()["idempotency-key"]);
    const response = await route.fetch();
    const body = await response.json();
    expect(response.status(), body.code || "repository acceptance").toBe(202);
    if (first) {
      first = false;
      accepted = body.repository_id;
      await route.abort("failed");
    } else {
      expect(body.repository_id).toBe(accepted);
      await route.fulfill({ response });
    }
  });
  await page
    .getByLabel("仓库 HTTPS 地址")
    .fill("https://github.com/octocat/Hello-World.git");
  await page.getByRole("button", { name: "添加仓库", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("连接失败");
  await page.getByRole("button", { name: "添加仓库", exact: true }).click();
  await expect(page).toHaveURL(new RegExp("/repositories/" + accepted + "$"));
  expect(keys[0]).toBe(keys[1]);
  await expect(page.getByTestId("repository-status")).toHaveText("已克隆", {
    timeout: 60000,
  });
  await expect(page.getByTestId("repository-head")).toHaveText(
    /^[0-9a-f]{40}$/,
  );
  await shot(page, info, "repository-cloned");
  await page.getByRole("link", { name: "查看当前任务" }).click();
  await expect(page.getByTestId("task-status")).toHaveText("已完成");
  await page.getByRole("link", { name: "查看仓库详情" }).click();
  let firstParse = true;
  let parseTask;
  const parseKeys = [];
  let parseHeaders;
  await page.route("**/api/v1/repositories/*/parse", async (route) => {
    parseKeys.push(route.request().headers()["idempotency-key"]);
    parseHeaders = route.request().headers();
    const response = await route.fetch();
    expect(response.status()).toBe(202);
    const body = await response.json();
    if (firstParse) {
      firstParse = false;
      parseTask = body.task_id;
      await route.abort("failed");
    } else {
      expect(body.task_id).toBe(parseTask);
      await route.fulfill({ response });
    }
  });
  await page.getByRole("button", { name: "开始解析提交" }).click();
  await expect.poll(() => firstParse).toBe(false);
  // The poll may discover the accepted task before another click. Use the same
  // browser request/key to prove uncertain-result replay independently of timing.
  const replay = await page.request.post(
    `/api/v1/repositories/${accepted}/parse`,
    {
      headers: parseHeaders,
      data: { commit_limit: null },
    },
  );
  expect(replay.status()).toBe(202);
  expect((await replay.json()).task_id).toBe(parseTask);
  await expect(page.getByTestId("parse-status")).toHaveText("解析完成", {
    timeout: 60000,
  });
  await expect(page.getByTestId("commit-list")).toContainText("个已入库提交");
  await expect(page.getByTestId("commit-list").locator("li")).not.toHaveCount(
    0,
  );
  await shot(page, info, "repository-parsed");
  await page
    .getByRole("button", { name: "查看文件变更", exact: true })
    .first()
    .click();
  await expect(page.getByTestId("file-list").locator("li")).not.toHaveCount(0);
  await shot(page, info, "repository-file-changes");
  let syncTask;
  let syncHeaders;
  await page.route("**/api/v1/repositories/*/sync", async (route) => {
    syncHeaders = route.request().headers();
    const response = await route.fetch();
    expect(response.status()).toBe(202);
    syncTask = (await response.json()).task_id;
    await route.abort("failed");
  });
  await page.getByRole("button", { name: "同步默认分支", exact: true }).click();
  await expect.poll(() => syncTask).toBeTruthy();
  const syncReplay = await page.request.post(
    `/api/v1/repositories/${accepted}/sync`,
    { headers: syncHeaders, data: {} },
  );
  expect(syncReplay.status()).toBe(202);
  expect((await syncReplay.json()).task_id).toBe(syncTask);
  await expect(page.getByTestId("sync-status")).toHaveText("同步完成", {
    timeout: 60000,
  });
  await expect(page.getByTestId("sync-windows")).toContainText("HEAD 未变化");
  await expect(page.getByTestId("sync-windows")).toContainText(
    "已入库 0 / 0 个新提交",
  );
  await shot(page, info, "repository-synced");
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
  await expect(page).toHaveURL(new RegExp("/repositories/" + accepted + "$"));
  await expect(page.getByTestId("repository-status")).toHaveText("已克隆");
  await expect(page.getByTestId("parse-status")).toHaveText("解析完成");
  await expect(page.getByTestId("sync-status")).toHaveText("同步完成");
});

test("unsafe repository address rejected without catalog mutation", async ({
  page,
}, info) => {
  await login(page);
  await page.getByRole("link", { name: "仓库目录", exact: true }).click();
  await page
    .getByLabel("仓库 HTTPS 地址")
    .fill("https://127.0.0.1/team/private");
  await page.getByRole("button", { name: "添加仓库", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("不允许访问");
  await expect(page.getByRole("alert")).toContainText("请求编号");
  await expect(page).toHaveURL(/\/repositories$/);
  await shot(page, info, "repository-unsafe-rejected");
});

test("repository Viewer controls and mobile detail remain readable", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.route("**/api/v1/auth/login", async (route) => {
    const response = await route.fetch();
    const data = await response.json();
    data.user.role = "Viewer"; // UI fixture; backend RBAC uses real users.
    await route.fulfill({ response, json: data });
  });
  await login(page);
  await page.getByRole("link", { name: "仓库目录", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "添加仓库", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByText("Viewer 可查看仓库；添加需要 Member 或 Admin。"),
  ).toBeVisible();
  await page
    .getByRole("link", {
      name: "https://github.com/octocat/hello-world.git",
      exact: true,
    })
    .click();
  await expect(page.getByTestId("repository-status")).toHaveText("已克隆");
  await expect(
    page.getByRole("button", { name: "同步默认分支", exact: true }),
  ).toHaveCount(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await shot(page, info, "mobile-repository");
});

test("history review UI preserves accepted HEAD and blocks another sync", async ({
  page,
}, info) => {
  // UI state fixture only. Actual force-push cases use real TLS Git/MySQL in test_sync.py.
  await login(page);
  let queriesAvailable = false;
  await page.route("**/api/v1/repositories/*/sync-windows?*", (route) =>
    queriesAvailable ? route.continue() : route.abort("failed"),
  );
  await page.route("**/api/v1/repositories/*", async (route) => {
    if (
      !/\/repositories\/[0-9a-f-]+$/.test(
        new URL(route.request().url()).pathname,
      )
    )
      return route.continue();
    const response = await route.fetch();
    const data = await response.json();
    data.sync_status = "requires_review";
    data.history_coverage = "recent_window";
    await route.fulfill({ response, json: data });
  });
  await page.getByRole("link", { name: "仓库目录", exact: true }).click();
  await page
    .getByRole("link", {
      name: "https://github.com/octocat/hello-world.git",
      exact: true,
    })
    .click();
  await expect(page.getByTestId("sync-status")).toHaveText("历史变化待复核");
  await expect(page.getByTestId("sync-window-error")).toBeVisible();
  queriesAvailable = true;
  await page.getByRole("button", { name: "刷新详情", exact: true }).click();
  await expect(page.getByTestId("sync-windows")).toContainText("HEAD 未变化");
  await expect(page.getByTestId("sync-window-error")).toHaveCount(0);
  await expect(page.getByRole("alert")).toContainText(
    "已暂停同步并保留旧 HEAD",
  );
  await expect(
    page.getByText("初次解析只选取最近 N 个提交", { exact: false }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "同步默认分支", exact: true }),
  ).toHaveCount(0);
  await expect(page.getByTestId("repository-head")).toHaveText(
    /^[0-9a-f]{40}$/,
  );
  await shot(page, info, "repository-review-required");
});
