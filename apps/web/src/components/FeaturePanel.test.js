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
test("v2 default omits later Fix run; explicit v1 selection retains compatibility", async () => {
  const send = vi
    .spyOn(client, "request")
    .mockImplementation((_path, options) =>
      Promise.resolve(
        options?.method === "POST"
          ? { task_id: "new-run" }
          : { items: [], total: 0 },
      ),
    );
  const view = mount(FeaturePanel, {
    props: {
      repo: {
        ...repo,
        fix_run: {
          head_sha: repo.head_sha,
          processed: 3,
          total: 3,
          root_task_id: "fixed",
        },
      },
      canWrite: true,
      reload: async () => {},
    },
  });
  await flushPromises();
  await view.get("button.primary").trigger("click");
  await flushPromises();
  let calls = send.mock.calls.filter(
    ([, options]) => options?.method === "POST",
  );
  expect(JSON.parse(calls[0][1].body)).toEqual({
    feature_version: "kamei14-dg-message-v2",
    purpose: "historical",
    fix_run_id: null,
  });
  await view.get("select").setValue("kamei14-dg-v1");
  await view.get("button.primary").trigger("click");
  await flushPromises();
  calls = send.mock.calls.filter(([, options]) => options?.method === "POST");
  expect(JSON.parse(calls[1][1].body).fix_run_id).toBe("fixed");
  expect(calls[0][1].headers["Idempotency-Key"]).not.toBe(
    calls[1][1].headers["Idempotency-Key"],
  );
  view.unmount();
});

test("Viewer reads v2 source boundary without training or clean claims", async () => {
  const v2run = {
    ...run,
    feature_version: "kamei14-dg-message-v2",
    purpose: "candidate",
  };
  vi.spyOn(client, "request").mockImplementation((path) =>
    Promise.resolve(
      path.includes("/feature-runs")
        ? { items: [v2run], total: 1 }
        : {
            run: v2run,
            quality: {
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
                ].map((name) => [
                  name,
                  {
                    valid: 11,
                    missing: 0,
                    missing_reasons: {},
                    min: 0,
                    p25: 0,
                    median: 0,
                    p75: 0,
                    p95: 0,
                    max: 0,
                  },
                ]),
              ),
              source_validation: {
                version: "native-record-binding-v1",
                feature_population_complete: false,
                label_coverage_complete: false,
                product_training_ready: false,
              },
            },
            items: [],
            total: 0,
          },
    ),
  );
  const view = mount(FeaturePanel, {
    props: { repo, canWrite: false, reload: async () => {} },
  });
  await flushPromises();
  expect(
    view.get('[data-testid="feature-version-semantics"]').text(),
  ).toContain("不代表这条提交不是修复");
  expect(
    view.get('[data-testid="feature-source-validation"]').text(),
  ).toContain("目前都还没有确认");
  expect(view.text()).toContain("待预测的提交（同样规则）");
  view.unmount();
});
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
  expect(view.text()).toContain("只有最近一段历史");
  expect(view.text()).toContain("这些文件以前没有改动记录：1 个");
  const drill = (name, action) =>
    view
      .findAll("tr")
      .find((row) => row.text().includes(`（${name}）`))
      .findAll("button")
      .find((button) => button.text() === action);
  await drill("AGE", "看缺失的提交").trigger("click");
  await flushPromises();
  expect(send.mock.calls.at(-1)[0]).toContain("feature=AGE&missing=true");
  await view
    .findAll("button")
    .find((button) => button.text() === "下一页结果")
    .trigger("click");
  await flushPromises();
  expect(send.mock.calls.at(-1)[0]).toContain("page=2");
  await drill("EXP", "数值从高到低").trigger("click");
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
  expect(view.text()).toContain("这一轮还没算完");
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
