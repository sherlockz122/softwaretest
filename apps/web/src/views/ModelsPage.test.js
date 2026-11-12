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
    features: [
      "la",
      "ld",
      "nf",
      "nd",
      "ns",
      "ent",
      "ndev",
      "age",
      "nuc",
      "aexp",
      "arexp",
      "asexp",
    ],
    forms: {
      la: "standardize",
      ld: "standardize",
      nf: "standardize",
      nd: "standardize",
      ns: "standardize",
      ent: "standardize",
      ndev: "standardize",
      age: "standardize",
      nuc: "standardize",
      aexp: "standardize",
      arexp: "standardize",
      asexp: "standardize",
    },
  });
  expect(router.currentRoute.value.path).toBe("/model-runs/new");
  wrapper.unmount();
});
test("training page separates itself from the historical risk list", async () => {
  vi.spyOn(client, "request").mockImplementation(async (path) => {
    if (path.startsWith("/datasets?")) return datasetList;
    return modelList;
  });
  const { wrapper } = await view(ModelsPage);
  expect(wrapper.text()).toContain("训练和预测是分开的两件事");
  expect(wrapper.find('a[href="/risk"]').exists()).toBe(true);
  wrapper.unmount();
});

test("chosen dataset shows partition sample counts and two-way note", async () => {
  vi.spyOn(client, "request").mockImplementation(async (path) => {
    if (path.startsWith("/datasets?"))
      return {
        items: [
          {
            id: "two-way",
            name: "two-way research",
            ready: true,
            total: 48,
            summary: {
              partitions: {
                train: { clean: 20, buggy: 18 },
                test: { clean: 5, buggy: 5 },
              },
            },
          },
        ],
        total: 1,
      };
    return modelList;
  });
  const { wrapper } = await view(ModelsPage);
  await wrapper.findAll("select")[0].setValue("two-way");
  await flushPromises();
  expect(wrapper.text()).toContain("训练区 38 条样本");
  expect(wrapper.text()).toContain("测试区 10 条样本");
  expect(wrapper.text()).toContain("没有验证区，说明当时只分了训练和测试两份");
  wrapper.unmount();
});

test("feature choice defaults all checked, plugin switch resets forms only", async () => {
  vi.spyOn(client, "request").mockImplementation(async (path) => {
    if (path.startsWith("/datasets?")) return datasetList;
    return modelList;
  });
  const { wrapper } = await view(ModelsPage);
  const checkboxes = wrapper
    .findAll('input[type="checkbox"]')
    .filter((i) => i.attributes("disabled") === undefined);
  expect(checkboxes).toHaveLength(12);
  expect(checkboxes.every((i) => i.element.checked)).toBe(true);
  const selects = wrapper.findAll("select");
  // 训练表单里第 0 个是数据集、第 1 个是模型；其余 12 个是每个特征的形式
  const featureSelects = selects.slice(2);
  expect(featureSelects).toHaveLength(12);
  expect(featureSelects.every((s) => s.element.value === "standardize")).toBe(
    true,
  );
  await selects[1].setValue("rf");
  await wrapper.findAll("input[type='checkbox']")[0].setValue(false);
  expect(featureSelects.every((s) => s.element.value === "raw")).toBe(true);
  expect(wrapper.findAll('input[type="checkbox"]')[0].element.checked).toBe(
    false,
  );
  expect(wrapper.findAll('input[type="checkbox"]')[1].element.checked).toBe(
    true,
  );
  wrapper.unmount();
});
test("empty feature choice disables submit and subset travels in body", async () => {
  const posts = [];
  vi.spyOn(client, "request").mockImplementation(async (path, options) => {
    if (options?.method === "POST") {
      posts.push(JSON.parse(options.body));
      return { model_run_id: "new" };
    }
    if (path.startsWith("/datasets?")) return datasetList;
    return modelList;
  });
  const { wrapper } = await view(ModelsPage);
  await wrapper.findAll("select")[0].setValue("fixed");
  const submit = wrapper
    .findAll("button")
    .find((b) => b.text().includes("开始训练"));
  for (const box of wrapper.findAll('input[type="checkbox"]')) {
    await box.setValue(false);
  }
  expect(submit.attributes("disabled")).toBeDefined();
  expect(wrapper.text()).toContain("至少要勾选一个指标");
  await wrapper.findAll('input[type="checkbox"]')[0].setValue(true);
  await wrapper.findAll('input[type="checkbox"]')[2].setValue(true);
  await wrapper.findAll("select")[2].setValue("log1p");
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  expect(posts[0]).toMatchObject({
    dataset_id: "fixed",
    plugin: "lr",
    features: ["la", "nf"],
  });
  expect(posts[0].forms).toEqual({ la: "log1p", nf: "standardize" });
  wrapper.unmount();
});
test("Admin publish button surfaces the incompatible-purpose error", async () => {
  const send = vi
    .spyOn(client, "request")
    .mockImplementation(async (path, options) => {
      if (options?.method === "POST")
        throw new ApiError(409, "MODEL_PURPOSE_INCOMPATIBLE");
      return {
        id: "ready",
        dataset_id: "fixed",
        plugin: "lr",
        seed: 42,
        status: "succeeded",
        registered: true,
        task_id: "worker",
        snapshot: {},
      };
    });
  const { wrapper } = await view(ModelPage, "Admin", "/model-runs/ready");
  const button = wrapper
    .findAll("button")
    .find((b) => b.text().includes("发布为候选产品"));
  expect(button).toBeTruthy();
  await button.trigger("click");
  await flushPromises();
  expect(wrapper.text()).toContain("研究模型尚不具备候选产品发布资格");
  expect(wrapper.text()).toContain("MODEL_PURPOSE_INCOMPATIBLE");
  expect(send.mock.calls.some(([path, options]) =>
    path === "/model-runs/ready/publish" && options?.method === "POST",
  )).toBe(true);
  wrapper.unmount();
});
test("Member model page hides the publish button", async () => {
  vi.spyOn(client, "request").mockResolvedValue({
    id: "ready",
    dataset_id: "fixed",
    plugin: "lr",
    seed: 42,
    status: "succeeded",
    registered: true,
    task_id: "worker",
    snapshot: {},
  });
  const { wrapper } = await view(ModelPage, "Member", "/model-runs/ready");
  expect(
    wrapper.findAll("button").some((b) => b.text().includes("发布为候选产品")),
  ).toBe(false);
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
test("model detail shows the training feature contract", async () => {
  vi.spyOn(client, "request").mockImplementation(async (path) => {
    if (path.includes("/risk"))
      return {
        items: [],
        total: 0,
        page: 1,
        page_size: 20,
        partitions: ["validation", "test"],
        threshold: 0.5,
        in_sample: false,
      };
    return {
    id: "ready",
    dataset_id: "fixed",
    plugin: "lr",
    seed: 42,
    status: "succeeded",
    registered: true,
    task_id: "worker",
    snapshot: {},
    report: {
      provenance: { version: "research-lr-rf-v4", code_sha: "f".repeat(64) },
      threshold: 0.5,
      calibration: { calibration_rows: 4, threshold_rows: 4 },
      features: ["la", "age"],
      forms: { la: "log1p", age: "standardize" },
      request: { features: ["la", "age"] },
      metrics: {
        validation_threshold: {
          support: { clean: 1, buggy: 1 },
          effort: { pofb20: 0.5, ifa: 0, popt: 0.75 },
        },
        test: {
          support: { clean: 1, buggy: 1 },
          effort: { pofb20: 0.5, ifa: 0, popt: 0.75 },
        },
      },
      baseline: {
        metrics: {
          support: { clean: 1, buggy: 1 },
          effort: { pofb20: 0.5, ifa: 0, popt: 0.75 },
        },
      },
    },
    };
  });
  const { wrapper } = await view(ModelPage, "Viewer", "/model-runs/ready");
  expect(wrapper.text()).toContain("这次训练用了 2 个指标");
  expect(wrapper.text()).toContain("新增行数（取对数）");
  expect(wrapper.text()).toContain("文件平均年龄（天）（标准化）");
  wrapper.unmount();
});
test("model without validation shows train-internal threshold summary", async () => {
  vi.spyOn(client, "request").mockImplementation(async (path) => {
    if (path.includes("/risk"))
      return {
        items: [],
        total: 0,
        page: 1,
        page_size: 20,
        partitions: ["test"],
        threshold: 0.5,
        in_sample: false,
      };
    return {
      id: "no-validation",
      dataset_id: "fixed",
      plugin: "lr",
      seed: 42,
      status: "succeeded",
      registered: true,
      task_id: "worker",
      snapshot: {},
      report: {
        provenance: { version: "research-lr-rf-v4", code_sha: "f".repeat(64) },
        threshold: 0.5,
        calibration_partition: "train",
        calibration: { calibration_rows: 20, threshold_rows: 20 },
        features: ["la"],
        forms: { la: "standardize" },
        request: { features: ["la"] },
        metrics: {
          train_threshold: {
            support: { clean: 1, buggy: 1 },
            effort: { pofb20: 0.5, ifa: 0, popt: 0.75 },
          },
          test: {
            support: { clean: 1, buggy: 1 },
            effort: { pofb20: 0.5, ifa: 0, popt: 0.75 },
          },
        },
        baseline: {
          metrics: {
            support: { clean: 1, buggy: 1 },
            effort: { pofb20: 0.5, ifa: 0, popt: 0.75 },
          },
        },
      },
    };
  });
  const { wrapper } = await view(ModelPage, "Viewer", "/model-runs/no-validation");
  expect(wrapper.text()).toContain("没有验证区，用训练区的前后两段");
  expect(wrapper.text()).toContain("训练区后段（没有验证区，用训练区后段定门槛，属样本内）");
  expect(wrapper.text()).toContain("测试区（最终评价）");
  wrapper.unmount();
});
