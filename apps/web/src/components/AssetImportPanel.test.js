import { flushPromises, mount } from "@vue/test-utils";
import { afterEach, expect, test, vi } from "vitest";
import { ApiError, client } from "../api";
import AssetImportPanel from "./AssetImportPanel.vue";

const HEADER = [
  "sha",
  "repo",
  "label",
  "when",
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
];
const CSV =
  HEADER.join(",") +
  "\n" +
  [
    "a".repeat(40),
    "apache/demo",
    "True",
    "1041379200",
    ...Array.from({ length: 12 }, (_, index) => String(index + 1)),
  ].join(",") +
  "\n";
const MAPPING = {
  commit_id: "sha",
  project: "repo",
  buggy: "label",
  author_date: "when",
};
HEADER.slice(4).forEach((name) => {
  MAPPING[name] = name;
});

function fileOf(text) {
  return new File([text], "demo.csv", { type: "text/csv" });
}

async function chooseFile(view) {
  const input = view.find('input[type="file"]');
  Object.defineProperty(input.element, "files", { value: [fileOf(CSV)] });
  await input.trigger("change");
  await vi.waitFor(() =>
    expect(view.findAll("fieldset select")).toHaveLength(16),
  );
}

afterEach(() => vi.restoreAllMocks());

test("选文件后按表头给出十六个映射，提交后显示行数与资产标识", async () => {
  const send = vi.spyOn(client, "request").mockResolvedValue({
    asset_id: "c".repeat(64),
    source_hash: "d".repeat(64),
    rows: 1,
    labels: { buggy: 1, clean: 0 },
  });
  const view = mount(AssetImportPanel);
  await chooseFile(view);
  const selects = view.findAll("fieldset select");
  const fields = Object.keys(MAPPING);
  for (const [index, field] of fields.entries()) {
    await selects[index].setValue(MAPPING[field]);
  }
  await view.find("button").trigger("click");
  await flushPromises();
  const [path, options] = send.mock.calls[0];
  expect(path).toBe("/public-assets");
  const body = JSON.parse(options.body);
  expect(body.mapping.commit_id).toBe("sha");
  expect(body.mapping.arexp).toBe("arexp");
  expect(body.license).toBe("CC-BY-4.0");
  expect(view.get('[role="status"]').text()).toContain("已导入 1 条");
  expect(view.get('[role="status"]').text()).toContain("c".repeat(12));
  expect(view.emitted("imported")).toHaveLength(1);
});

test("缺列失败时显示错误码与缺失字段并保留表单", async () => {
  vi.spyOn(client, "request").mockRejectedValue(
    new ApiError(422, "PUBLIC_MAPPING_INCOMPLETE", "req-1", "la"),
  );
  const view = mount(AssetImportPanel);
  await chooseFile(view);
  const selects = view.findAll("fieldset select");
  for (const [index, field] of Object.keys(MAPPING).entries()) {
    await selects[index].setValue(MAPPING[field]);
  }
  await view.find("button").trigger("click");
  await flushPromises();
  const alert = view.get('[role="alert"]').text();
  expect(alert).toContain("PUBLIC_MAPPING_INCOMPLETE");
  expect(alert).toContain("列映射缺少必填字段");
  expect(alert).toContain("la");
  expect(view.find('[role="status"]').exists()).toBe(false);
  expect(view.text()).toContain("demo.csv");
});
