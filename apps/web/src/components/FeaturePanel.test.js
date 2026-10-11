import { mount, flushPromises } from "@vue/test-utils";
import { afterEach, expect, test, vi } from "vitest";
import { client, ApiError } from "../api";
import FeaturePanel from "./FeaturePanel.vue";
const repo = {
  id: "repo",
  head_sha: "a".repeat(40),
  parse_status: "parsed",
  feature_status: "pending",
};
const run = {
  root_task_id: "run",
  processed: 11,
  total: 11,
  complete: true,
  feature_version: "kamei14-dg-v1",
  purpose: "historical",
  history_coverage: "recent_window",
  created_at: "2026-10-11T00:00:00Z",
};
afterEach(() => vi.restoreAllMocks());
test("quality drills down missing or descending values and pages independently", async () => {
  const stats = {
    valid: 10,
    missing: 1,
    missing_reasons: { first_file_change: 1 },
    min: 0,
    p25: 1,
    median: 2,
    p75: 3,
    p95: 4,
    max: 5,
  };
  const quality = {
    count: 11,
    features: Object.fromEntries(
      [
        "NS",
        "ND",
        "NF",
        "Entropy",
        "LA",
        "LD",
        "LT",
        "FIX",
        "NDEV",
        "AGE",
        "NUC",
        "EXP",
        "REXP",
        "SEXP",
      ].map((name) => [name, stats]),
    ),
  };
  const send = vi
    .spyOn(client, "request")
    .mockImplementation((path) =>
      Promise.resolve(
        path.includes("/feature-runs")
          ? { items: [run], total: 11 }
          : { run, quality, items: [], total: 11 },
      ),
    );
  const view = mount(FeaturePanel, {
    props: { repo, canWrite: false, reload: async () => {} },
  });
  await flushPromises();
  expect(view.text()).toContain("有限历史窗口");
  expect(view.text()).toContain("first_file_change: 1");
  await view
    .findAll("button")
    .find((button) => button.text() === "AGE 缺失")
    .trigger("click");
  await flushPromises();
  expect(send.mock.calls.at(-1)[0]).toContain("feature=AGE&missing=true");
  await view
    .findAll("button")
    .find((button) => button.text() === "特征结果下一页")
    .trigger("click");
  await flushPromises();
  expect(send.mock.calls.at(-1)[0]).toContain("page=2");
  await view
    .findAll("button")
    .find((button) => button.text() === "EXP 数值降序")
    .trigger("click");
  await flushPromises();
  expect(send.mock.calls.at(-1)[0]).toContain("feature=EXP&missing=false");
  expect(send.mock.calls.at(-1)[0]).toContain("page=1");
  view.unmount();
});
test("Viewer sees incomplete boundaries and no write controls", async () => {
  vi.spyOn(client, "request").mockImplementation((path) =>
    Promise.resolve(
      path.includes("/feature-runs")
        ? { items: [run], total: 1 }
        : {
            run: { ...run, complete: false },
            quality: null,
            items: [],
            total: 0,
          },
    ),
  );
  const view = mount(FeaturePanel, {
    props: { repo, canWrite: false, reload: async () => {} },
  });
  await flushPromises();
  expect(view.text()).toContain("Viewer");
  expect(view.text()).toContain("本轮未完成");
  expect(view.find("button.primary").exists()).toBe(false);
  view.unmount();
});
test("lost startup acknowledgement retains idempotency key and prevents concurrent POST", async () => {
  let reject;
  const send = vi
    .spyOn(client, "request")
    .mockImplementation((_path, options) =>
      options?.method === "POST"
        ? new Promise((_resolve, fail) => {
            reject = fail;
          })
        : Promise.resolve({ items: [], total: 0 }),
    );
  const view = mount(FeaturePanel, {
    props: { repo, canWrite: true, reload: async () => {} },
  });
  await flushPromises();
  await view.get("button.primary").trigger("click");
  await view.get("button.primary").trigger("click");
  expect(
    send.mock.calls.filter(([, options]) => options?.method === "POST"),
  ).toHaveLength(1);
  reject(new ApiError(0, "NETWORK_ERROR"));
  await flushPromises();
  await view.get("button.primary").trigger("click");
  const calls = send.mock.calls.filter(
    ([, options]) => options?.method === "POST",
  );
  expect(calls[1][1].headers["Idempotency-Key"]).toBe(
    calls[0][1].headers["Idempotency-Key"],
  );
  reject(new ApiError(0, "NETWORK_ERROR"));
  await flushPromises();
  view.unmount();
});
test("late old repository quality cannot appear after switching", async () => {
  let resolveOld;
  vi.spyOn(client, "request").mockImplementation((path) => {
    if (path.includes("/repositories/repo/feature-runs"))
      return Promise.resolve({ items: [run], total: 1 });
    if (path.includes("/repositories/repo/feature-quality"))
      return new Promise((resolve) => {
        resolveOld = resolve;
      });
    return Promise.resolve({ items: [], total: 0 });
  });
  const view = mount(FeaturePanel, {
    props: { repo, canWrite: true, reload: async () => {} },
  });
  await flushPromises();
  await view.setProps({ repo: { ...repo, id: "new-repo" } });
  await flushPromises();
  resolveOld({
    run,
    quality: null,
    items: [{ ordinal: 0, sha: "old-sha", result: null }],
    total: 1,
  });
  await flushPromises();
  expect(view.text()).not.toContain("old-sha");
  view.unmount();
});
