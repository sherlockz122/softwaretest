import { mount, flushPromises } from "@vue/test-utils";
import { afterEach, expect, test, vi } from "vitest";
import { client, ApiError } from "../api";
import FixPanel from "./FixPanel.vue";
const repo = {
  id: "repo",
  head_sha: "a".repeat(40),
  parse_status: "parsed",
  sync_status: "synced",
  fix_status: "pending",
  fix_run: null,
};
afterEach(() => vi.restoreAllMocks());
test("Viewer can query evidence without detection or review controls", async () => {
  vi.spyOn(client, "request").mockResolvedValue({ items: [], total: 0 });
  const view = mount(FixPanel, {
    props: { repo, canWrite: false, reload: async () => {} },
  });
  await flushPromises();
  expect(view.text()).toContain("Viewer");
  expect(view.find("form").exists()).toBe(false);
  expect(view.text()).not.toContain("确认修复");
  view.unmount();
});
test("uncertain detection retains key and blocks concurrent submission", async () => {
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
  const view = mount(FixPanel, {
    props: { repo, canWrite: true, reload: async () => {} },
  });
  await flushPromises();
  await view.get("form").trigger("submit");
  await view.get("form").trigger("submit");
  expect(send.mock.calls.filter((c) => c[1]?.method === "POST")).toHaveLength(
    1,
  );
  reject(new ApiError(0, "NETWORK_UNAVAILABLE"));
  await flushPromises();
  await view.get("form").trigger("submit");
  const requests = send.mock.calls.filter((c) => c[1]?.method === "POST");
  expect(requests[0][1].headers["Idempotency-Key"]).toBe(
    requests[1][1].headers["Idempotency-Key"],
  );
  reject(new ApiError(0, "NETWORK_UNAVAILABLE"));
  await flushPromises();
  view.unmount();
});
test("review conflict refreshes revision before a deliberate second decision", async () => {
  const row = {
    id: "assessment",
    sha: "b".repeat(40),
    candidate: true,
    review_status: "unreviewed",
    review_revision: 0,
    content_complete: true,
    evidence: [],
    review_history: [],
  };
  const run = {
    root_task_id: "run",
    head_sha: repo.head_sha,
    rule_version: "fix-evidence-v1",
    include_medium: true,
    processed: 1,
    total: 1,
    created_at: "2026-10-10T00:00:00Z",
  };
  let writes = 0;
  const send = vi
    .spyOn(client, "request")
    .mockImplementation((path, options) => {
      if (options?.method === "PATCH") {
        writes++;
        if (writes === 1) {
          row.review_revision = 1;
          row.review_status = "rejected";
          return Promise.reject(new ApiError(409, "FIX_REVIEW_CONFLICT"));
        }
        return Promise.resolve(row);
      }
      return Promise.resolve(
        path.includes("/evidence?")
          ? { run, items: [structuredClone(row)], total: 1 }
          : { items: [run], total: 1 },
      );
    });
  const view = mount(FixPanel, {
    props: {
      repo: { ...repo, fix_run: run },
      canWrite: true,
      reload: async () => {},
    },
  });
  await flushPromises();
  await view.get('input:not([type="checkbox"])').setValue("review rationale");
  await view
    .findAll("button")
    .find((b) => b.text() === "确认修复")
    .trigger("click");
  await flushPromises();
  expect(view.text()).toContain("复核记录已被更新");
  expect(view.text()).toContain("人工拒绝");
  await view
    .findAll("button")
    .find((b) => b.text() === "确认修复")
    .trigger("click");
  await flushPromises();
  const requests = send.mock.calls.filter((c) => c[1]?.method === "PATCH");
  expect(JSON.parse(requests[0][1].body).expected_revision).toBe(0);
  expect(JSON.parse(requests[1][1].body).expected_revision).toBe(1);
  view.unmount();
});

test("late run query cannot start an evidence query for a different repository", async () => {
  let finishOld;
  const send = vi.spyOn(client, "request").mockImplementation((path) => {
    if (path.startsWith("/repositories/repo/"))
      return new Promise((resolve) => {
        finishOld = resolve;
      });
    return Promise.resolve({
      items: [],
      total: 0,
      run: { rule_version: "fix-evidence-v1", history_coverage: "full" },
    });
  });
  const view = mount(FixPanel, { props: { repo, reload: async () => {} } });
  await view.setProps({
    repo: { ...repo, id: "repo2", fix_run: { root_task_id: "new-run" } },
  });
  await flushPromises();
  finishOld({ items: [], total: 0 });
  await flushPromises();
  expect(
    send.mock.calls.some((c) =>
      c[0].includes("/repositories/repo/fix-runs/new-run/"),
    ),
  ).toBe(false);
  view.unmount();
});
