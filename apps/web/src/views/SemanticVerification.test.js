import { mount, flushPromises } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, expect, test, vi } from "vitest";
import { ApiError, client } from "../api";
import SemanticAssetPage from "./SemanticAssetPage.vue";
import TaskPage from "./TaskPage.vue";
import TasksPage from "./TasksPage.vue";

afterEach(() => {
  vi.restoreAllMocks();
  client.state.user = null;
});
const id = "a".repeat(64);
const summary = {
  id,
  shape: [660, 3076],
  accepted: 660,
  producer_version: "frozen-codebert-population-v5",
  partitions: { train: 488, validation: 172 },
  limitations: ["不用于产品发布"],
  items: [],
  total: 660,
};
async function view(component, path, role = "Member") {
  client.state.user = { id: "actor", role };
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/semantic-assets", component: { template: "<div/>" } },
      { path: "/semantic-assets/:id", component: SemanticAssetPage },
      { path: "/tasks/:id", component: TaskPage },
      { path: "/tasks", component: TasksPage },
    ],
  });
  await router.push(path);
  await router.isReady();
  const wrapper = mount(component, { global: { plugins: [router] } });
  await flushPromises();
  return { wrapper, router };
}
const createButton = (wrapper) =>
  wrapper
    .findAll("button")
    .find((b) => /创建完整性校验任务|提交中/.test(b.text()));

test("Viewer can read the asset without a verification create control", async () => {
  vi.spyOn(client, "request").mockResolvedValue(summary);
  const { wrapper } = await view(
    SemanticAssetPage,
    `/semantic-assets/${id}`,
    "Viewer",
  );
  expect(createButton(wrapper)).toBeUndefined();
  expect(wrapper.text()).toContain("结果不改变资产的训练或发布资格");
  wrapper.unmount();
});

test.each(["Member", "Admin"])(
  "%s uncertain response reuses its key and prevents double submit",
  async (role) => {
    let reject;
    let posts = 0;
    const send = vi
      .spyOn(client, "request")
      .mockImplementation((_, options = {}) => {
        if (options.method !== "POST") return Promise.resolve(summary);
        posts++;
        if (posts === 1)
          return new Promise((_, fail) => {
            reject = fail;
          });
        return Promise.resolve({ task_id: "created" });
      });
    const { wrapper, router } = await view(
      SemanticAssetPage,
      `/semantic-assets/${id}`,
      role,
    );
    await createButton(wrapper).trigger("click");
    expect(createButton(wrapper).attributes("disabled")).toBeDefined();
    await createButton(wrapper).trigger("click");
    expect(posts).toBe(1);
    reject(new ApiError(0, "NETWORK_UNAVAILABLE"));
    await flushPromises();
    await createButton(wrapper).trigger("click");
    await flushPromises();
    const requests = send.mock.calls.filter(
      ([, options]) => options.method === "POST",
    );
    expect(requests).toHaveLength(2);
    expect(requests[0][1].headers["Idempotency-Key"]).toBe(
      requests[1][1].headers["Idempotency-Key"],
    );
    expect(requests[0][1].body).toBeUndefined();
    expect(router.currentRoute.value.fullPath).toBe("/tasks/created");
    wrapper.unmount();
  },
);

test("late creation failure after an asset change does not pollute the new page", async () => {
  let reject;
  vi.spyOn(client, "request").mockImplementation((url, options = {}) => {
    if (options.method === "POST")
      return new Promise((_, fail) => {
        reject = fail;
      });
    return Promise.resolve({
      ...summary,
      id: url.includes("b".repeat(64)) ? "b".repeat(64) : id,
    });
  });
  const { wrapper, router } = await view(
    SemanticAssetPage,
    `/semantic-assets/${id}`,
  );
  await createButton(wrapper).trigger("click");
  await router.push(`/semantic-assets/${"b".repeat(64)}`);
  await flushPromises();
  reject(new ApiError(409, "SEMANTIC_TASK_BINDING_CONFLICT"));
  await flushPromises();
  expect(wrapper.text()).toContain("b".repeat(64));
  expect(wrapper.text()).not.toContain("SEMANTIC_TASK_BINDING_CONFLICT");
  expect(router.currentRoute.value.path).toBe(
    `/semantic-assets/${"b".repeat(64)}`,
  );
  wrapper.unmount();
});

test("task type and status filters reset pages and survive pagination", async () => {
  const send = vi
    .spyOn(client, "request")
    .mockResolvedValue({ items: [], total: 25 });
  const { wrapper, router } = await view(
    TasksPage,
    "/tasks?page=3&status=queued",
  );
  await wrapper.get("#task-type").setValue("semantic.verify");
  await flushPromises();
  expect(router.currentRoute.value.query.page).toBeUndefined();
  expect(router.currentRoute.value.query.status).toBe("queued");
  await wrapper.get("#status").setValue("succeeded");
  await flushPromises();
  await wrapper
    .findAll("button")
    .find((b) => b.text() === "下一页")
    .trigger("click");
  await flushPromises();
  const query = new URLSearchParams(send.mock.calls.at(-1)[0].split("?")[1]);
  expect(query.get("type")).toBe("semantic.verify");
  expect(query.get("status")).toBe("succeeded");
  expect(query.get("page")).toBe("2");
  wrapper.unmount();
});

test("task result shows snapshot limits and links to the fixed asset", async () => {
  vi.spyOn(client, "request").mockResolvedValue({
    id: "verified",
    type: "semantic.verify",
    status: "succeeded",
    progress: 100,
    processed: 660,
    total: 660,
    semantic_asset_id: id,
    result: {
      shape: [660, 3076],
      partitions: { train: 488, validation: 172 },
      verified_at: "2026-10-29T00:00:00Z",
      snapshot_only: true,
    },
  });
  const { wrapper } = await view(TaskPage, "/tasks/verified", "Viewer");
  expect(wrapper.text()).toContain("完整性校验通过");
  expect(wrapper.text()).toContain("不授予训练、历史或候选发布资格");
  expect(
    wrapper
      .findAll("a")
      .some((a) => a.attributes("href") === `/semantic-assets/${id}`),
  ).toBe(true);
  expect(
    wrapper
      .findAll("button")
      .some((b) => /创建|取消|重试|发布|训练/.test(b.text())),
  ).toBe(false);
  wrapper.unmount();
});

test("binding conflict directs a new asset task instead of an ineffective retry", async () => {
  vi.spyOn(client, "request").mockResolvedValue({
    id: "outdated",
    type: "semantic.verify",
    status: "failed",
    semantic_asset_id: id,
    error: {
      code: "SEMANTIC_TASK_BINDING_CONFLICT",
      message: "generic retry text",
      request_id: "safe-request",
    },
  });
  const { wrapper } = await view(TaskPage, "/tasks/outdated", "Member");
  expect(wrapper.text()).toContain("从来源资产创建新的校验任务");
  expect(wrapper.text()).not.toContain("generic retry text");
  expect(wrapper.findAll("button").some((b) => b.text() === "重试任务")).toBe(
    false,
  );
  expect(
    wrapper
      .findAll("a")
      .some((a) => a.attributes("href") === `/semantic-assets/${id}`),
  ).toBe(true);
  wrapper.unmount();
});
