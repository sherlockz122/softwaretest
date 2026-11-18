import { mount, flushPromises } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, expect, test, vi } from "vitest";
import { ApiError, client } from "../api";
import ReviewPage from "./ReviewPage.vue";
afterEach(() => {
  vi.restoreAllMocks();
  client.state.user = null;
});
async function view(role = "Member") {
  client.state.user = { id: "member", role };
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/review", component: ReviewPage },
      { path: "/tasks/:id", component: { template: "<div/>" } },
      { path: "/risk", component: { template: "<div/>" } },
      { path: "/model-runs/:id", component: { template: "<div/>" } },
    ],
  });
  await router.push("/review");
  await router.isReady();
  const wrapper = mount(ReviewPage, { global: { plugins: [router] } });
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
  items: [
    { id: "run-1", plugin: "lr", seed: 42, registered: true },
    { id: "run-2", plugin: "rf", seed: 7, registered: false },
  ],
  total: 2,
};
const single = {
  prediction_id: "p-1",
  model_run_id: "run-1",
  probability: 0.82,
  calibration: "validation-chronological-sigmoid-logit-v1",
  risk_level: "high",
  effort: 126,
  risk_density: 0.006508,
  threshold: 0.5,
  feature_version: "kamei14-dg-v1",
  top_factors: [
    { feature: "la", value: 12, contribution: 0.31, direction: "increase" },
    { feature: "ent", value: 3, contribution: 0.12, direction: "increase" },
  ],
  limitations: ["模型结果用于安排审查顺序，不是自动合并或拒绝结论。"],
};
async function fill(wrapper, mode = "ref") {
  await wrapper.findAll("select")[0].setValue("repo-1");
  await wrapper.findAll("select")[1].setValue("run-1");
  await wrapper.find('input[placeholder="例如 main 或 v1.0"]').setValue("v1.0");
  if (mode === "ref") {
    await wrapper
      .find('input[placeholder="例如 feature/x"]')
      .setValue("feature/x");
  } else {
    await wrapper.find('input[value="diff"]').setValue();
    await wrapper
      .find("textarea")
      .setValue("diff --git a/code.py b/code.py\n-old\n+new\n");
  }
}
test("Viewer sees the limits but no prediction form", async () => {
  vi.spyOn(client, "request").mockResolvedValue(repoList);
  const { wrapper } = await view("Viewer");
  expect(wrapper.find("form").exists()).toBe(false);
  expect(wrapper.text()).toContain("Member或Admin");
  expect(wrapper.text()).toContain("不是自动合并或拒绝结论");
  wrapper.unmount();
});
test("sync result shows probability and non-causal statement", async () => {
  const send = vi
    .spyOn(client, "request")
    .mockImplementation(async (path) => {
      if (path.startsWith("/repositories?")) return repoList;
      if (path.startsWith("/models?")) return modelList;
      return single;
    });
  const { wrapper } = await view();
  await fill(wrapper);
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  expect(wrapper.text()).toContain("0.8200");
  expect(wrapper.text()).toContain("高");
  expect(wrapper.text()).toContain("Effort 126");
  expect(wrapper.text()).toContain("0.006508");
  expect(wrapper.text()).toContain("LA↑");
  expect(wrapper.text()).toContain("用于安排审查顺序，不是自动合并或拒绝结论");
  const post = send.mock.calls.find(([, options]) => options?.method === "POST");
  expect(post[1].headers["Idempotency-Key"]).toBeTruthy();
  expect(JSON.parse(post[1].body)).toEqual({
    repository_id: "repo-1",
    model_run_id: "run-1",
    base_ref: "v1.0",
    candidate_ref: "feature/x",
    candidate_diff: null,
  });
  wrapper.unmount();
});
test("uncertain network retry reuses the operation key", async () => {
  let posts = 0;
  const send = vi
    .spyOn(client, "request")
    .mockImplementation(async (path, options) => {
      if (path.startsWith("/repositories?")) return repoList;
      if (path.startsWith("/models?")) return modelList;
      if (options?.method === "POST" && ++posts === 1)
        throw new ApiError(0, "NETWORK_UNAVAILABLE");
      return single;
    });
  const { wrapper } = await view();
  await fill(wrapper);
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  expect(wrapper.text()).toContain("连接失败");
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  const attempts = send.mock.calls.filter(
    ([, options]) => options?.method === "POST",
  );
  expect(attempts).toHaveLength(2);
  expect(attempts[0][1].headers["Idempotency-Key"]).toBe(
    attempts[1][1].headers["Idempotency-Key"],
  );
  expect(wrapper.text()).toContain("预测编号：p-1");
  wrapper.unmount();
});
test("async fallback links to task and risk queue", async () => {
  vi.spyOn(client, "request").mockImplementation(async (path) => {
    if (path.startsWith("/repositories?")) return repoList;
    if (path.startsWith("/models?")) return modelList;
    return { task_id: "t-9", status: "queued", prediction_task_id: "pt-9" };
  });
  const { wrapper } = await view();
  await fill(wrapper, "diff");
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  expect(wrapper.text()).toContain("预测已转入后台任务");
  expect(wrapper.find('a[href="/tasks/t-9"]').exists()).toBe(true);
  expect(wrapper.find('a[href="/risk?task=pt-9"]').exists()).toBe(true);
  wrapper.unmount();
});
test("input limit error shows the mapped message", async () => {
  vi.spyOn(client, "request").mockImplementation(async (path) => {
    if (path.startsWith("/repositories?")) return repoList;
    if (path.startsWith("/models?")) return modelList;
    throw new ApiError(422, "PRED_INPUT_LIMIT");
  });
  const { wrapper } = await view();
  await fill(wrapper);
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  expect(wrapper.text()).toContain("改动超过输入上限");
  expect(wrapper.text()).toContain("PRED_INPUT_LIMIT");
  wrapper.unmount();
});
test("explanation panel lists factors with directions", async () => {
  const send = vi
    .spyOn(client, "request")
    .mockImplementation(async (path, options) => {
      if (path.startsWith("/repositories?")) return repoList;
      if (path.startsWith("/models?")) return modelList;
      if (path.startsWith("/predictions/") && !options?.method) {
        return {
          method: "lr-scaled-coefficient-v1",
          factors: [
            { feature: "ld", value: 40, contribution: -0.2, direction: "decrease" },
            { feature: "la", value: 12, contribution: 0.31, direction: "increase" },
          ],
          limitations: ["解释不等于缺陷根因。"],
        };
      }
      return single;
    });
  const { wrapper } = await view();
  await fill(wrapper);
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  await wrapper
    .findAll("button")
    .find((b) => b.text().includes("查看完整解释"))
    .trigger("click");
  await flushPromises();
  expect(wrapper.text()).toContain("lr-scaled-coefficient-v1");
  expect(wrapper.text()).toContain("LD");
  expect(wrapper.text()).toContain("降低风险");
  expect(wrapper.text()).toContain("解释不等于缺陷根因");
  expect(send.mock.calls.some(([path]) => path === "/predictions/p-1/explanation")).toBe(true);
  wrapper.unmount();
});
test("fix references panel uses the same candidate as the prediction", async () => {
  const calls = [];
  const recommendations = {
    items: [
      {
        fix_sha: "a".repeat(40),
        message_summary: "Fix crash",
        touched_files: ["Parser.java"],
        touched_files_shown: 1,
        evidence_level: "high",
        similarity: {
          file_overlap: 1,
          file_path_overlap: null,
          feature_distance: 0.5,
          evidence_rank: 0,
        },
      },
    ],
    total: 1,
    limit: 20,
    rule_version: "fix-evidence-v1",
    target: { sha: "f".repeat(40), message_summary: "target", touched_files: [] },
    scope: "repository_full_history_including_later_fixes",
  };
  vi.spyOn(client, "request").mockImplementation(async (path, options) => {
    calls.push([path, options]);
    if (path.startsWith("/repositories?")) return repoList;
    if (path.startsWith("/models?")) return modelList;
    if (path.startsWith("/repositories/") && path.includes("fix-recommendations"))
      return recommendations;
    return single;
  });
  const { wrapper } = await view();
  await fill(wrapper);
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  const panelCall = calls.find(
    ([path]) => path.includes("fix-recommendations"),
  );
  expect(panelCall).toBeTruthy();
  // 检索对象与预测提交的候选引用完全一致
  expect(panelCall[0]).toContain("sha=feature%2Fx");
  expect(panelCall[0]).toContain("/repositories/repo-1/fix-recommendations");
  expect(wrapper.text()).toContain("同类修复参考");
  expect(wrapper.text()).toContain("不是对当前缺陷的根因结论");
  expect(wrapper.text()).toContain("目标提交：ffffffff");
  wrapper.unmount();
});
