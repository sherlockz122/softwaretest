import { mount, flushPromises } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, expect, test, vi } from "vitest";
import { ApiError, client } from "../api";
import RiskPage from "./RiskPage.vue";
afterEach(() => {
  vi.restoreAllMocks();
  client.state.user = null;
});
async function view(role = "Member", path = "/risk") {
  client.state.user = { id: "member", role };
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/risk", component: RiskPage },
      { path: "/tasks/:id", component: { template: "<div/>" } },
    ],
  });
  await router.push(path);
  await router.isReady();
  const wrapper = mount(RiskPage, { global: { plugins: [router] } });
  await flushPromises();
  return { wrapper, router };
}
const repoList = {
  items: [
    {
      id: "repo-1",
      url: "https://example.com/a.git",
      default_branch: "main",
      parse_status: "parsed",
    },
  ],
  total: 1,
};
const modelList = {
  items: [{ id: "run-1", plugin: "lr", seed: 42, registered: true }],
  total: 1,
};
const queueData = {
  prediction_task_id: "pt-9",
  task_id: "t-9",
  status: "succeeded",
  repository_id: "repo-1",
  model_run_id: "run-1",
  base_ref: "main",
  candidate_kind: "batch",
  feature_version: "kamei14-dg-v1",
  threshold: 0.5,
  empty: false,
  items: [
    {
      prediction_id: "p-2",
      candidate_sha: "a".repeat(40),
      event_time: "2026-10-01T00:00:00.000000Z",
      probability: 0.82,
      risk_level: "high",
      effort: 126,
      risk_density: 0.006508,
      oversized: false,
      top_factors: [
        { feature: "la", value: 12, contribution: 0.31, direction: "increase" },
      ],
    },
    {
      prediction_id: "p-1",
      candidate_sha: "b".repeat(40),
      event_time: "2026-10-02T00:00:00.000000Z",
      probability: 0.57,
      risk_level: "medium",
      effort: 600,
      risk_density: 0.00095,
      oversized: true,
      top_factors: [],
    },
  ],
  total: 2,
  page: 1,
  page_size: 20,
};
test("batch creation submits limits and redirects to the queue", async () => {
  const send = vi
    .spyOn(client, "request")
    .mockImplementation(async (path, options) => {
      if (path.startsWith("/repositories?")) return repoList;
      if (path.startsWith("/models?")) return modelList;
      if (path.startsWith("/prediction-tasks/")) return queueData;
      if (options?.method === "POST")
        return { task_id: "t-9", status: "queued", prediction_task_id: "pt-9" };
      throw new ApiError(0, "NETWORK_UNAVAILABLE");
    });
  const { wrapper, router } = await view();
  await wrapper.findAll("select")[0].setValue("repo-1");
  await wrapper.findAll("select")[1].setValue("run-1");
  await wrapper.find('input[placeholder="例如 main 或 v1.0"]').setValue("main");
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  expect(router.currentRoute.value.query.task).toBe("pt-9");
  const post = send.mock.calls.find(([, options]) => options?.method === "POST");
  expect(JSON.parse(post[1].body)).toEqual({
    repository_id: "repo-1",
    model_run_id: "run-1",
    base_ref: "main",
    window_days: 90,
    max_candidates: 30,
  });
  expect(post[1].headers["Idempotency-Key"]).toBeTruthy();
  wrapper.unmount();
});
test("uncertain network retry reuses the batch operation key", async () => {
  let posts = 0;
  vi.spyOn(client, "request").mockImplementation(async (path, options) => {
    if (path.startsWith("/repositories?")) return repoList;
    if (path.startsWith("/models?")) return modelList;
    if (path.startsWith("/prediction-tasks/")) return queueData;
    if (options?.method === "POST" && ++posts === 1)
      throw new ApiError(0, "NETWORK_UNAVAILABLE");
    return { task_id: "t-9", status: "queued", prediction_task_id: "pt-9" };
  });
  const { wrapper } = await view();
  await wrapper.findAll("select")[0].setValue("repo-1");
  await wrapper.findAll("select")[1].setValue("run-1");
  await wrapper.find('input[placeholder="例如 main 或 v1.0"]').setValue("main");
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  expect(wrapper.text()).toContain("连接失败");
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  const attempts = vi
    .mocked(client.request)
    .mock.calls.filter(([, options]) => options?.method === "POST");
  expect(attempts).toHaveLength(2);
  expect(attempts[0][1].headers["Idempotency-Key"]).toBe(
    attempts[1][1].headers["Idempotency-Key"],
  );
  wrapper.unmount();
});
test("queue table renders ordering columns and disclaimer", async () => {
  const send = vi
    .spyOn(client, "request")
    .mockImplementation(async (path) => {
      if (path.startsWith("/repositories?")) return repoList;
      if (path.startsWith("/models?")) return modelList;
      if (path.startsWith("/prediction-tasks/")) return queueData;
      throw new ApiError(0, "NETWORK_UNAVAILABLE");
    });
  const { wrapper } = await view("Member", "/risk?task=pt-9");
  expect(wrapper.text()).toContain("批量窗口候选");
  expect(wrapper.text()).toContain("判定阈值：0.5000");
  expect(wrapper.text()).toContain("高");
  expect(wrapper.text()).toContain("超大");
  expect(wrapper.text()).toContain("LA↑");
  expect(wrapper.text()).toContain("0.006508");
  expect(wrapper.text()).toContain("不是自动合并或拒绝结论");
  expect(wrapper.text()).toContain("共 2 条");
  expect(send.mock.calls.some(([path]) =>
    path.startsWith("/prediction-tasks/pt-9/results?page=1&page_size=20"),
  )).toBe(true);
  wrapper.unmount();
});
test("level filter reloads the queue with the query parameter", async () => {
  const send = vi
    .spyOn(client, "request")
    .mockImplementation(async (path) => {
      if (path.startsWith("/repositories?")) return repoList;
      if (path.startsWith("/models?")) return modelList;
      if (path.startsWith("/prediction-tasks/")) return queueData;
      throw new ApiError(0, "NETWORK_UNAVAILABLE");
    });
  const { wrapper } = await view("Member", "/risk?task=pt-9");
  await wrapper.findAll("select")[0].setValue("high");
  await flushPromises();
  expect(send.mock.calls.some(([path]) =>
    path.includes("level=high"),
  )).toBe(true);
  wrapper.unmount();
});
test("empty queue and missing results explain themselves", async () => {
  vi.spyOn(client, "request").mockImplementation(async (path) => {
    if (path.startsWith("/repositories?")) return repoList;
    if (path.startsWith("/models?")) return modelList;
    if (path.startsWith("/prediction-tasks/"))
      return { ...queueData, items: [], total: 0, empty: true, threshold: null };
    throw new ApiError(0, "NETWORK_UNAVAILABLE");
  });
  const { wrapper } = await view("Member", "/risk?task=pt-9");
  expect(wrapper.text()).toContain("队列中没有候选记录");
  expect(wrapper.text()).toContain("判定阈值：—");
  wrapper.unmount();
});
test("explanation button opens the factor panel", async () => {
  const send = vi
    .spyOn(client, "request")
    .mockImplementation(async (path) => {
      if (path.startsWith("/repositories?")) return repoList;
      if (path.startsWith("/models?")) return modelList;
      if (path.startsWith("/prediction-tasks/")) return queueData;
      if (path.startsWith("/predictions/")) {
        return {
          candidate_sha: "a".repeat(40),
          probability: 0.82,
          method: "lr-scaled-coefficient-v1",
          factors: [
            { feature: "la", value: 12, contribution: 0.31, direction: "increase" },
          ],
          limitations: ["解释不等于缺陷根因。"],
        };
      }
      throw new ApiError(0, "NETWORK_UNAVAILABLE");
    });
  const { wrapper } = await view("Member", "/risk?task=pt-9");
  await wrapper
    .findAll("button")
    .find((b) => b.text().includes("解释"))
    .trigger("click");
  await flushPromises();
  expect(wrapper.text()).toContain("lr-scaled-coefficient-v1");
  expect(wrapper.text()).toContain("推高风险");
  expect(wrapper.text()).toContain("解释不等于缺陷根因");
  expect(send.mock.calls.some(([path]) =>
    path.startsWith("/predictions/p-2/explanation"),
  )).toBe(true);
  wrapper.unmount();
});
