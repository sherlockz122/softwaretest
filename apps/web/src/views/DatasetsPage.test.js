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
  // attachTo：jsdom 对未挂载元素的 getComputedStyle 缓存不失效，
  // 列显隐（v-show）往返后 isVisible() 会读到旧值。
  const wrapper = mount(component, {
    global: { plugins: [router] },
    attachTo: document.body,
  });
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
  await wrapper.findAll("select")[1].setValue(asset.asset_id);
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  const attempts = send.mock.calls.filter(([, o]) => o?.method === "POST");
  expect(attempts[0][1].headers["Idempotency-Key"]).toBe(
    attempts[1][1].headers["Idempotency-Key"],
  );
  expect(JSON.parse(attempts[1][1].body)).toMatchObject({
    source_kind: "asset",
    source_hash: asset.source_hash,
    purpose: "published_binary_12_v1",
  });
  expect(router.currentRoute.value.path).toBe("/datasets/created");
  wrapper.unmount();
});

test("create sends user-chosen ratios and allows zero validation", async () => {
  const posts = [];
  vi.spyOn(client, "request").mockImplementation(async (path, options) => {
    if (options?.method === "POST") {
      posts.push(JSON.parse(options.body));
      return { dataset_id: "created", task_id: "worker" };
    }
    return listing;
  });
  const { wrapper } = await view(DatasetsPage);
  await wrapper.get("input").setValue("two-way");
  await wrapper.findAll("select")[1].setValue(asset.asset_id);
  const numbers = wrapper.findAll('input[type="number"]');
  await numbers[0].setValue(80);
  await numbers[1].setValue(0);
  await numbers[2].setValue(20);
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  expect(posts[0]).toMatchObject({
    train_ratio: 0.8,
    validation_ratio: 0,
    test_ratio: 0.2,
    split_strategy: "time_ordered",
  });
  wrapper.unmount();
});

test("ratios that do not sum to 100 disable submission with a hint", async () => {
  vi.spyOn(client, "request").mockResolvedValue(listing);
  const { wrapper } = await view(DatasetsPage);
  await wrapper.get("input").setValue("research");
  await wrapper.findAll("select")[1].setValue(asset.asset_id);
  const numbers = wrapper.findAll('input[type="number"]');
  await numbers[2].setValue(10);
  await flushPromises();
  const submit = wrapper
    .findAll("button")
    .find((b) => b.text().includes("生成研究数据集"));
  expect(submit.attributes("disabled")).toBeDefined();
  expect(wrapper.text()).toContain("验证比例可以为 0");
  wrapper.unmount();
});

test("two-way dataset hides validation rows and shows auxiliary features", async () => {
  vi.spyOn(client, "request").mockResolvedValue({
    id: "two-way",
    name: "research",
    ready: true,
    processed: 40,
    total: 40,
    task_id: "work",
    status: "succeeded",
    asset_id: asset.asset_id,
    policy: { split_strategy: "time_ordered" },
    summary: {
      features: ["la", "ld"],
      partitions: {
        train: { clean: 16, buggy: 16 },
        test: { clean: 4, buggy: 4 },
      },
      source_acquisition: {},
      repositories: {},
    },
    members: [
      {
        ordinal: 0,
        canonical_repository: "apache/hadoop",
        commit_id: "c".repeat(40),
        partition: "train",
        label: 0,
        event_epoch: 1,
        numeric12: { la: 5.0, ld: 1.0 },
        fix: true,
        lt: null,
        lt_missing_reason: "upstream_source_omits_lt",
      },
    ],
  });
  const { wrapper } = await view(DatasetPage, "Viewer", "/datasets/two-way");
  expect(
    wrapper.findAll("button").filter((b) => b.text().includes("下载")),
  ).toHaveLength(2);
  expect(wrapper.text()).toContain("是修复");
  expect(wrapper.text()).toContain("未提供");
  expect(wrapper.text()).toContain("原始记录数 / 去重后样本数");
  expect(wrapper.findAll("tr").some((t) => t.text().includes("验证"))).toBe(
    false,
  );
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
  expect(wrapper.text()).toContain("修复标志只展示、不参与模型计算");
  expect(
    wrapper.findAll("button").filter((b) => b.text().includes("下载")),
  ).toHaveLength(3);
  wrapper.unmount();
});

const numericColumns = [
  "la", "ld", "nf", "nd", "ns", "ent",
  "ndev", "age", "nuc", "aexp", "arexp", "asexp",
];

test("import form auto-matches header columns and uploads raw bytes with license", async () => {
  const header = ["repo", "sha", "time", "bug", ...numericColumns, "fix"].join(",");
  const file = {
    name: "public.csv",
    slice: () => ({
      text: async () =>
        `${header}\nuser/a,abc,1000,1,${Array(12).fill("1").join(",")},False\n`,
    }),
    arrayBuffer: async () => new TextEncoder().encode("csv-bytes").buffer,
  };
  const sends = [];
  vi.spyOn(client, "request").mockImplementation(async (path, options) => {
    sends.push([path, options]);
    if (options?.method === "POST" && path.startsWith("/assets/import"))
      return { asset_id: "f".repeat(64), source_hash: "e".repeat(64), rows: 1 };
    return listing;
  });
  const { wrapper } = await view(DatasetsPage);
  const input = wrapper.find('input[type="file"]');
  Object.defineProperty(input.element, "files", {
    value: [file],
    configurable: true,
  });
  await input.trigger("change");
  await flushPromises();
  expect(wrapper.text()).not.toContain("还缺映射");
  const submit = wrapper
    .findAll("button")
    .find((b) => b.text().includes("导入为可用数据"));
  expect(submit.attributes("disabled")).toBeUndefined();
  await wrapper.findAll("form")[1].trigger("submit");
  await flushPromises();
  const call = sends.find(
    ([, o]) =>
      o?.method === "POST" &&
      o?.headers?.["Content-Type"] === "application/octet-stream",
  );
  expect(call).toBeTruthy();
  const [path, options] = call;
  const url = new URL(path, "http://test");
  expect(url.pathname).toBe("/assets/import");
  expect(url.searchParams.get("license")).toBe("CC-BY-4.0");
  expect(JSON.parse(url.searchParams.get("columns"))).toMatchObject({
    canonical_repository: "repo",
    commit_id: "sha",
    event_epoch: "time",
    label: "bug",
    la: "la",
    fix: "fix",
  });
  expect(new TextDecoder().decode(options.body)).toBe("csv-bytes");
  expect(wrapper.text()).toContain("导入成功");
  wrapper.unmount();
});

test("import form lists missing mappings and disables submit until complete", async () => {
  const file = {
    name: "partial.csv",
    slice: () => ({ text: async () => "repo,sha,time\nuser/a,abc,1000\n" }),
    arrayBuffer: async () => new ArrayBuffer(0),
  };
  vi.spyOn(client, "request").mockResolvedValue(listing);
  const { wrapper } = await view(DatasetsPage);
  const input = wrapper.find('input[type="file"]');
  Object.defineProperty(input.element, "files", {
    value: [file],
    configurable: true,
  });
  await input.trigger("change");
  await flushPromises();
  expect(wrapper.text()).toContain("还缺映射");
  const submit = wrapper
    .findAll("button")
    .find((b) => b.text().includes("导入为可用数据"));
  expect(submit.attributes("disabled")).toBeDefined();
  wrapper.unmount();
});

test("admin imports from server path after reading its columns", async () => {
  const sends = [];
  vi.spyOn(client, "request").mockImplementation(async (path, options) => {
    sends.push([path, options]);
    if (path.startsWith("/assets/import-columns"))
      return { columns: ["repo", "sha", "time", "bug", ...numericColumns, "fix"] };
    if (path.startsWith("/assets/import-from-path"))
      return { asset_id: "f".repeat(64), rows: 2 };
    return listing;
  });
  const { wrapper } = await view(DatasetsPage, "Admin");
  const pathInput = wrapper
    .findAll("input")
    .find((i) => i.attributes("placeholder")?.includes("public.csv"));
  await pathInput.setValue("D:\\data\\public.csv");
  const read = wrapper
    .findAll("button")
    .find((b) => b.text().includes("读取文件列"));
  await read.trigger("click");
  await flushPromises();
  expect(wrapper.text()).not.toContain("还缺映射");
  await wrapper.findAll("form")[1].trigger("submit");
  await flushPromises();
  const call = sends.find(([p]) => p.startsWith("/assets/import-from-path"));
  expect(call).toBeTruthy();
  expect(JSON.parse(call[1].body)).toMatchObject({
    path: "D:\\data\\public.csv",
    license: "CC-BY-4.0",
    columns: { label: "bug", fix: "fix" },
  });
  wrapper.unmount();
});

test("imported dataset detail shows source kind, license and column mapping", async () => {
  vi.spyOn(client, "request").mockResolvedValue({
    id: "imported",
    name: "imported",
    ready: true,
    processed: 40,
    total: 40,
    task_id: "work",
    status: "succeeded",
    asset_id: asset.asset_id,
    policy: { split_strategy: "time_ordered" },
    summary: {
      features: numericColumns,
      partitions: {
        train: { clean: 14, buggy: 14 },
        test: { clean: 3, buggy: 3 },
      },
      source_acquisition: {
        source_kind: "user_import",
        import_license_declaration: "CC0-1.0",
        import_columns: { canonical_repository: "repo", label: "bug" },
      },
      repositories: {},
    },
    members: [
      {
        ordinal: 0,
        canonical_repository: "user/alpha",
        commit_id: "c".repeat(40),
        partition: "train",
        label: 0,
        event_epoch: 1,
        numeric12: { la: 5.0 },
        fix: null,
        fix_missing_reason: "import_source_omits_fix",
        lt: null,
        lt_missing_reason: "upstream_source_omits_lt",
      },
    ],
  });
  const { wrapper } = await view(DatasetPage, "Viewer", "/datasets/imported");
  expect(wrapper.text()).toContain("导入文件");
  expect(wrapper.text()).toContain("CC0-1.0");
  expect(wrapper.text()).toContain("canonical_repository ← repo");
  expect(wrapper.text()).toContain("与首次保存时间一致（导入即获取）");
  expect(
    wrapper.find('[title*="导入文件没提供修复标志"]').exists(),
  ).toBe(true);
  wrapper.unmount();
});

test("ready dataset renders a 2D feature table with toggles and filters", async () => {
  const calls = [];
  vi.spyOn(client, "request").mockImplementation(async (path) => {
    calls.push(path);
    const url = new URL(path, "http://test");
    const partition = url.searchParams.get("filter_partition");
    const label = url.searchParams.get("filter_label");
    return {
      id: "ready",
      name: "research",
      ready: true,
      processed: 40,
      total: 40,
      member_total: 40,
      task_id: "work",
      status: "succeeded",
      asset_id: asset.asset_id,
      policy: { split_strategy: "time_ordered" },
      summary: {
        features: [
          "la", "ld", "nf", "nd", "ns", "ent",
          "ndev", "age", "nuc", "aexp", "arexp", "asexp",
        ],
        partitions: {
          train: { clean: 14, buggy: 14 },
          validation: { clean: 3, buggy: 3 },
          test: { clean: 3, buggy: 3 },
        },
        source_acquisition: {},
        repositories: {},
      },
      members: [
        {
          ordinal: 0,
          canonical_repository: "apache/hadoop",
          commit_id: "c".repeat(40),
          partition: partition || "train",
          label: label == null ? 0 : Number(label),
          event_epoch: 1,
          numeric12: {
            la: 213.0, ld: 0.4235, nf: 3.0, nd: 1.0, ns: 1.0, ent: 0.9,
            ndev: 2.0, age: 30.0, nuc: 5.0, aexp: 10.0, arexp: 2.0, asexp: 1.0,
          },
          fix: false,
          lt: null,
        },
      ],
    };
  });
  const { wrapper } = await view(DatasetPage, "Viewer", "/datasets/ready");
  expect(wrapper.text()).toContain("新增行数");
  expect(wrapper.text()).toContain("213");
  expect(wrapper.text()).toContain("0.42");
  expect(wrapper.text()).toContain("非修复");
  expect(wrapper.text()).toContain("未提供");
  const checkboxes = wrapper.findAll('input[type="checkbox"]');
  expect(checkboxes).toHaveLength(14);
  await checkboxes[0].setValue(false);
  expect(
    wrapper.findAll("th").some((t) => t.isVisible() && t.text().includes("新增行数")),
  ).toBe(false);
  await checkboxes[0].setValue(true);
  expect(
    wrapper.findAll("th").some((t) => t.isVisible() && t.text().includes("新增行数")),
  ).toBe(true);
  const selects = wrapper.findAll("select");
  await selects[0].setValue("test");
  await selects[1].setValue("1");
  await flushPromises();
  expect(calls.some((p) => p.includes("filter_partition=test"))).toBe(true);
  expect(calls.some((p) => p.includes("filter_label=1"))).toBe(true);
  expect(wrapper.text()).toContain("当前按筛选条件显示");
  wrapper.unmount();
});

test("repository source picks rounds and submits observation body", async () => {
  const posts = [];
  vi.spyOn(client, "request").mockImplementation(async (path, options) => {
    if (options?.method === "POST") {
      posts.push(JSON.parse(options.body));
      return { dataset_id: "obs-ds" };
    }
    if (path.startsWith("/datasets?")) return { items: [], total: 0, assets: [] };
    if (path.startsWith("/repositories?"))
      return {
        items: [{ id: "repo-1", url: "https://example.com/a.git", parse_status: "parsed" }],
        total: 1,
      };
    if (path.includes("feature-runs"))
      return {
        items: [
          {
            root_task_id: "feat-1",
            feature_version: "kamei14-dg-v1",
            processed: 51,
            history_coverage: "full",
          },
        ],
        total: 1,
      };
    if (path.includes("szz-runs"))
      return {
        items: [{ root_task_id: "szz-1", as_of: "2024-06-01T00:00:00Z" }],
        total: 1,
      };
    return { items: [], total: 0, assets: [] };
  });
  const { wrapper } = await view(DatasetsPage);
  await wrapper.findAll("select")[0].setValue("repository");
  await flushPromises();
  expect(wrapper.text()).toContain("选哪个仓库");
  await wrapper.findAll("select")[1].setValue("repo-1");
  await flushPromises();
  await wrapper.findAll("select")[2].setValue("feat-1");
  const checkbox = wrapper.find('input[type="checkbox"][value="szz-1"]');
  checkbox.element.checked = true;
  await checkbox.trigger("change");
  await wrapper.get("form").trigger("submit");
  await flushPromises();
  expect(posts[0]).toMatchObject({
    source_kind: "repository",
    repository_id: "repo-1",
    feature_run_id: "feat-1",
    szz_run_ids: ["szz-1"],
  });
  wrapper.unmount();
});
