import { mount, flushPromises } from "@vue/test-utils";
import { createMemoryHistory, createRouter } from "vue-router";
import { afterEach, expect, test, vi } from "vitest";
import { ApiError, client } from "../api";
import ModelsPage from "./ModelsPage.vue";
import ModelPage from "./ModelPage.vue";
afterEach(() => {
  vi.restoreAllMocks();
  client.state.user = null;
});
async function view(component, role = "Member", path = "/models") {
  client.state.user = { id: "member", role };
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: "/models", component: ModelsPage },
      { path: "/model-runs/:id", component: ModelPage },
      { path: "/tasks/:id", component: { template: "<div/>" } },
      { path: "/datasets/:id", component: { template: "<div/>" } },
    ],
  });
  await router.push(path);
  await router.isReady();
  const wrapper = mount(component, { global: { plugins: [router] } });
  await flushPromises();
  return { wrapper, router };
}
const modelList = { items: [], total: 0, plugins: {} };
const datasetList = {
  items: [
    { id: "fixed", name: "fixed research", ready: true, total: 80 },
    { id: "failed", name: "not ready", ready: false },
  ],
  total: 2,
};
test("Viewer reads models without training or release controls", async () => {
  vi.spyOn(client, "request").mockResolvedValue(modelList);
  const { wrapper } = await view(ModelsPage, "Viewer");
  expect(wrapper.find("form").exists()).toBe(false);
  expect(wrapper.text()).toContain("Member 或 Admin");
  expect(wrapper.text()).toContain("还没有训练过模型");
  wrapper.unmount();
});
test("uncertain training reuses operation key and immutable dataset ID", async () => {
  let posts = 0;
  const send = vi
    .spyOn(client, "request")
    .mockImplementation(async (path, options) => {
      if (options?.method === "POST") {
        if (++posts === 1) throw new ApiError(0, "NETWORK_UNAVAILABLE");
        return { model_run_id: "new" };
      }
      if (path.startsWith("/datasets?")) return datasetList;
      if (path.startsWith("/models?")) return modelList;
      return {
        id: "new",
        plugin: "lr",
        seed: 42,
        status: "queued",
        task_id: "worker",
      };
    });
  const { wrapper, router } = await view(ModelsPage);
  expect(
    wrapper.find('option[value="failed"]').attributes("disabled"),
  ).toBeDefined();
  await wrapper.findAll("select")[0].setValue("fixed");
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  const attempts = send.mock.calls.filter(
    ([, options]) => options?.method === "POST",
  );
  expect(attempts[0][1].headers["Idempotency-Key"]).toBe(
    attempts[1][1].headers["Idempotency-Key"],
  );
  expect(JSON.parse(attempts[1][1].body)).toEqual({
    dataset_id: "fixed",
    plugin: "lr",
    seed: 42,
    params: { c: 1, max_iter: 500 },
  });
  expect(router.currentRoute.value.path).toBe("/model-runs/new");
  wrapper.unmount();
});
test("failed model hides artifacts and links cancellation/retry task", async () => {
  vi.spyOn(client, "request").mockResolvedValue({
    id: "failed",
    dataset_id: "fixed",
    plugin: "rf",
    seed: 42,
    status: "failed",
    registered: false,
    task_id: "worker",
  });
  const { wrapper } = await view(ModelPage, "Viewer", "/model-runs/failed");
  expect(wrapper.text()).toContain("还没有可用的模型");
  expect(wrapper.find('a[href="/tasks/worker"]').exists()).toBe(true);
  expect(wrapper.findAll("button").some((b) => b.text().includes("下载"))).toBe(
    false,
  );
  wrapper.unmount();
});
