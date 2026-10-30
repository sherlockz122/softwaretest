import { mount, flushPromises } from "@vue/test-utils";
import { afterEach, expect, test, vi } from "vitest";
import { client, ApiError } from "../api";
import SZZPanel from "./SZZPanel.vue";
const repo = {
  id: "repo",
  head_sha: "a".repeat(40),
  fix_status: "detected",
  szz_status: "pending",
  fix_run: { root_task_id: "fix", head_sha: "a".repeat(40) },
};
afterEach(() => vi.restoreAllMocks());
test("line evidence and paginated results retain unknown boundaries", async () => {
  const run = {
    root_task_id: "szz",
    fix_root_task_id: "fix",
    history_coverage: "recent_window",
    algorithm_version: "baseline-szz-v1",
    label_version: "a".repeat(64),
    as_of: "2026-10-10T00:00:00Z",
  };
  const send = vi.spyOn(client, "request").mockImplementation((path) =>
    Promise.resolve(
      path.includes("/repositories/")
        ? { items: [run], total: 1 }
        : path.includes("/results")
          ? {
              run,
              items: [
                {
                  id: "one",
                  sha: "f".repeat(40),
                  input: { review_revision: 2, review_status: "confirmed" },
                  result: {
                    status: "partial",
                    reasons: ["outside_parsed_history"],
                  },
                  links_count: 1,
                },
              ],
              total: 11,
              counts: { traced: 1, total: 11 },
            }
          : {
              run,
              items: [
                {
                  path: "source.py",
                  fixed_line: 2,
                  fix_sha: "f".repeat(40),
                  blamed_sha: "b".repeat(40),
                  blamed_line: 3,
                  origin_path: "old.py",
                  eligible: false,
                  reason: "outside_parsed_history",
                  label_available_at: run.as_of,
                },
              ],
              total: 11,
            },
    ),
  );
  const view = mount(SZZPanel, {
    props: { repo, canWrite: false, reload: async () => {} },
  });
  await flushPromises();
  expect(view.text()).toContain("只有最近一段历史");
  expect(view.text()).toContain("追溯时的人工复核：已确认是修复");
  const open = view
    .findAll("button")
    .find((button) => button.text().includes("查看 1 条代码行线索"));
  expect(open).toBeTruthy();
  await open.trigger("click");
  await flushPromises();
  expect(view.text()).toContain("source.py 第 2 行");
  expect(view.text()).toContain("第 3 行");
  expect(view.text()).toContain("来源提交不在已整理的提交记录里");
  expect(send.mock.calls.some(([path]) => path.includes("item_id=one"))).toBe(
    true,
  );
  view.unmount();
});
test("Viewer reads SZZ boundaries without startup", async () => {
  vi.spyOn(client, "request").mockResolvedValue({ items: [], total: 0 });
  const view = mount(SZZPanel, {
    props: { repo, canWrite: false, reload: async () => {} },
  });
  await flushPromises();
  expect(view.text()).toContain("Viewer");
  expect(view.find("button.primary").exists()).toBe(false);
  expect(view.text()).toContain("不会当成「肯定不是缺陷」");
  view.unmount();
});
test("uncertain SZZ startup reuses key and prevents concurrent writes", async () => {
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
  const view = mount(SZZPanel, {
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
  expect(calls[0][1].headers["Idempotency-Key"]).toBe(
    calls[1][1].headers["Idempotency-Key"],
  );
  reject(new ApiError(0, "NETWORK_ERROR"));
  await flushPromises();
  view.unmount();
});
test("late old repository results cannot appear after switching", async () => {
  let resolveOld;
  const run = {
    root_task_id: "old-run",
    processed: 1,
    total: 1,
    created_at: "2026-10-10T00:00:00Z",
  };
  vi.spyOn(client, "request").mockImplementation((path) => {
    if (path.includes("/repositories/repo/"))
      return Promise.resolve({ items: [run], total: 1 });
    if (path.includes("/old-run/results"))
      return new Promise((resolve) => {
        resolveOld = resolve;
      });
    return Promise.resolve({ items: [], total: 0 });
  });
  const view = mount(SZZPanel, {
    props: { repo, canWrite: true, reload: async () => {} },
  });
  await flushPromises();
  await view.setProps({ repo: { ...repo, id: "new-repo" } });
  await flushPromises();
  resolveOld({
    run,
    items: [
      {
        id: "old",
        sha: "old-sha",
        input: { review_revision: 0 },
        result: { status: "unknown", reasons: [] },
        links_count: 0,
      },
    ],
    total: 1,
  });
  await flushPromises();
  expect(view.text()).not.toContain("old-sha");
  view.unmount();
});
