import { mount, flushPromises } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, expect, test, vi } from "vitest";
import { ApiError, client } from "../api";
import DatasetsPage from "./DatasetsPage.vue";
import DatasetPage from "./DatasetPage.vue";

const asset = {
  asset_id: "a".repeat(64),
  source_hash: "b".repeat(64),
  rows: 40,
};
const listing = {
  assets: [asset],
  items: [],
  total: 0,
  page: 1,
  page_size: 10,
};
afterEach(() => {
  vi.restoreAllMocks();
  client.state.user = null;
});
async function view(component, role = "Member", path = "/datasets") {
  client.state.user = { id: "test-member", role };
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/datasets", component: DatasetsPage },
      { path: "/datasets/:id", component: DatasetPage },
      { path: "/tasks/:id", component: { template: "<div/>" } },
    ],
  });
  await router.push(path);
  await router.isReady();
  const wrapper = mount(component, { global: { plugins: [router] } });
  await flushPromises();
  return { wrapper, router };
}

test("Viewer reads dataset list with an explicit reason and no creation form", async () => {
  vi.spyOn(client, "request").mockResolvedValue(listing);
  const { wrapper } = await view(DatasetsPage, "Viewer");
  expect(wrapper.find("form").exists()).toBe(false);
  expect(wrapper.text()).toContain("Member 或 Admin");
  wrapper.unmount();
});

test("uncertain create retains request identity and binds preview source hash", async () => {
  let posts = 0;
  const send = vi
    .spyOn(client, "request")
    .mockImplementation(async (path, options) => {
      if (options?.method !== "POST") return listing;
      if (++posts === 1) throw new ApiError(0, "NETWORK_UNAVAILABLE");
      return { dataset_id: "created", task_id: "worker" };
    });
  const { wrapper, router } = await view(DatasetsPage);
  await wrapper.get("input").setValue("research");
  await wrapper.findAll("select")[0].setValue(asset.asset_id);
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  const attempts = send.mock.calls.filter(([, o]) => o?.method === "POST");
  expect(attempts[0][1].headers["Idempotency-Key"]).toBe(
    attempts[1][1].headers["Idempotency-Key"],
  );
  expect(JSON.parse(attempts[1][1].body)).toMatchObject({
    source_hash: asset.source_hash,
    purpose: "published_binary_12_v1",
  });
  expect(router.currentRoute.value.path).toBe("/datasets/created");
  wrapper.unmount();
});

test("unpublished dataset hides members and export with a task link", async () => {
  vi.spyOn(client, "request").mockResolvedValue({
    id: "pending",
    name: "partial",
    ready: false,
    processed: 10,
    total: 40,
    task_id: "work",
    status: "running",
    members: [],
  });
  const { wrapper } = await view(DatasetPage, "Viewer", "/datasets/pending");
  expect(wrapper.text()).toContain("没完成的不能下载");
  expect(wrapper.findAll("button").some((b) => b.text().includes("下载"))).toBe(
    false,
  );
  expect(wrapper.find('a[href="/tasks/work"]').exists()).toBe(true);
  wrapper.unmount();
});

test("ready research shows release limitations and both-class partition support", async () => {
  vi.spyOn(client, "request").mockResolvedValue({
    id: "ready",
    name: "research",
    ready: true,
    processed: 40,
    total: 40,
    task_id: "work",
    status: "succeeded",
    asset_id: asset.asset_id,
    policy: { split_strategy: "time_ordered" },
    summary: {
      partitions: {
        train: { clean: 14, buggy: 14 },
        validation: { clean: 3, buggy: 3 },
        test: { clean: 3, buggy: 3 },
      },
      source_acquisition: {},
      repositories: {},
    },
    members: [],
  });
  const { wrapper } = await view(DatasetPage, "Viewer", "/datasets/ready");
  expect(wrapper.text()).toContain("按历史时间点回放");
  expect(wrapper.text()).toContain("原数据里的 fix 标志不参与计算");
  expect(
    wrapper.findAll("button").filter((b) => b.text().includes("下载")),
  ).toHaveLength(3);
  wrapper.unmount();
});
