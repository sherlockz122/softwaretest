import { mount, flushPromises } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, expect, test, vi } from "vitest";
import { ApiError, client } from "../api";
import SemanticAssetsPage from "./SemanticAssetsPage.vue";
import SemanticAssetPage from "./SemanticAssetPage.vue";
import LoginPage from "./LoginPage.vue";

afterEach(() => {
  vi.restoreAllMocks();
  client.state.user = null;
});
const id = "a".repeat(64);
const summary = {
  id,
  accepted: 660,
  shape: [660, 3076],
  producer_version: "frozen-codebert-population-v5",
  partitions: { train: 488, validation: 172 },
  limitations: ["不用于产品发布"],
  items: [],
  total: 172,
};
async function view(component, path) {
  client.state.user = { id: "viewer", role: "Viewer" };
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/semantic-assets", component: SemanticAssetsPage },
      { path: "/semantic-assets/:id", component: SemanticAssetPage },
    ],
  });
  await router.push(path);
  await router.isReady();
  const wrapper = mount(component, { global: { plugins: [router] } });
  await flushPromises();
  return { wrapper, router };
}
test("Viewer sees empty catalog and can retry a safe failure without write controls", async () => {
  const request = vi
    .spyOn(client, "request")
    .mockRejectedValueOnce(new ApiError(409, "SEMANTIC_ASSET_UNAVAILABLE"))
    .mockResolvedValue({ items: [], total: 0 });
  const { wrapper } = await view(SemanticAssetsPage, "/semantic-assets");
  expect(wrapper.find("form").exists()).toBe(false);
  await wrapper.get("button").trigger("click");
  await flushPromises();
  expect(wrapper.text()).toContain("尚无语义资产");
  expect(request.mock.calls.every(([, options]) => !options.method)).toBe(true);
  wrapper.unmount();
});
test("catalog shows research restrictions and stable asset navigation", async () => {
  vi.spyOn(client, "request").mockResolvedValue({ items: [summary], total: 1 });
  const { wrapper } = await view(SemanticAssetsPage, "/semantic-assets");
  expect(wrapper.get("a").attributes("href")).toBe(`/semantic-assets/${id}`);
  expect(wrapper.text()).toContain("当前不能用于历史或候选预测");
  expect(
    wrapper.findAll("button").some((b) => /训练|发布/.test(b.text())),
  ).toBe(false);
  wrapper.unmount();
});
test("partition resets pagination and rejected detail hides previous members", async () => {
  const request = vi
    .spyOn(client, "request")
    .mockResolvedValue({ ...summary, total: 660 });
  const { wrapper } = await view(SemanticAssetPage, `/semantic-assets/${id}`);
  await wrapper
    .findAll("button")
    .find((b) => b.text() === "下一页")
    .trigger("click");
  await flushPromises();
  expect(request.mock.calls.at(-1)[0]).toContain("offset=20&limit=20");
  await wrapper.get("select").setValue("validation");
  await flushPromises();
  expect(request.mock.calls.at(-1)[0]).toContain(
    "offset=0&limit=20&partition=validation",
  );
  request.mockRejectedValueOnce(new ApiError(409, "SEMANTIC_ASSET_DRIFT"));
  await wrapper
    .findAll("button")
    .find((b) => b.text() === "刷新资产详情")
    .trigger("click");
  await flushPromises();
  expect(wrapper.find("table").exists()).toBe(false);
  expect(wrapper.text()).not.toContain("完整性核验通过");
  wrapper.unmount();
});
test("late response after asset ID change cannot replace the latest asset", async () => {
  let finishOld;
  vi.spyOn(client, "request")
    .mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          finishOld = resolve;
        }),
    )
    .mockResolvedValue({ ...summary, id: "b".repeat(64) });
  const { wrapper, router } = await view(
    SemanticAssetPage,
    `/semantic-assets/${id}`,
  );
  await router.push(`/semantic-assets/${"b".repeat(64)}`);
  await flushPromises();
  finishOld(summary);
  await flushPromises();
  expect(wrapper.text()).toContain("b".repeat(64));
  expect(wrapper.text()).not.toContain(id);
  wrapper.unmount();
});

test.each([
  ["/semantic-assets", "/semantic-assets"],
  [`/semantic-assets/${id}`, `/semantic-assets/${id}`],
  ["/tasks/kept", "/tasks/kept"],
  ["/model-runs/kept", "/model-runs/kept"],
  ["//outside.example", "/tasks"],
  ["https://outside.example", "/tasks"],
  ["/semantic-assets-evil", "/tasks"],
  [["/semantic-assets", "//outside.example"], "/tasks"],
])("login return path remains internal: %j", async (next, expected) => {
  vi.spyOn(client, "login").mockResolvedValue(undefined);
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/login", component: LoginPage },
      { path: "/:pathMatch(.*)*", component: { template: "<div/>" } },
    ],
  });
  await router.push({ path: "/login", query: { next } });
  await router.isReady();
  const wrapper = mount(LoginPage, { global: { plugins: [router] } });
  await wrapper.get("#username").setValue("viewer");
  await wrapper.get("#password").setValue("fixture-password");
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  expect(router.currentRoute.value.fullPath).toBe(expected);
  wrapper.unmount();
});
