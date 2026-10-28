import { mount, flushPromises } from "@vue/test-utils";
import { afterEach, expect, test, vi } from "vitest";
import { client, ApiError } from "../api";
import RiskPanel from "./RiskPanel.vue";
const run = "11111111-1111-1111-1111-111111111111";
const payload = (overrides = {}) => ({
  items: [
    {
      commit_id: "a".repeat(40),
      event_epoch: 1700000000,
      partition: "test",
      probability: 0.812,
      risk_level: "high",
    },
    {
      commit_id: "b".repeat(40),
      event_epoch: 1700000400,
      partition: "validation",
      probability: 0.34,
      risk_level: "low",
    },
  ],
  total: 42,
  page: 1,
  page_size: 20,
  partitions: ["validation", "test"],
  threshold: 0.5,
  in_sample: false,
  ...overrides,
});
afterEach(() => vi.restoreAllMocks());
test("默认按验证与测试分区请求并渲染概率、等级与分区", async () => {
  const send = vi.spyOn(client, "request").mockResolvedValue(payload());
  const view = mount(RiskPanel, { props: { run } });
  await flushPromises();
  const url = send.mock.calls[0][0];
  expect(url).toContain(`/model-runs/${run}/risk-items`);
  expect(url).toContain("page=1");
  expect(url).toContain("page_size=20");
  expect(url).not.toContain("partition=");
  const first = view.findAll("tbody tr")[0].text();
  expect(first).toContain("高");
  expect(first).toContain("81.2%");
  expect(first).toContain("a".repeat(12));
  expect(view.findAll("tbody tr")[1].text()).toContain("低");
  expect(view.text()).toContain("共 42 条");
  expect(view.text()).toContain("第 1 / 3 页");
  expect(view.text()).toContain("50.0%");
  view.unmount();
});
test("翻页带页码且第一页禁用上一页", async () => {
  const send = vi.spyOn(client, "request").mockResolvedValue(payload());
  const view = mount(RiskPanel, { props: { run } });
  await flushPromises();
  const [previous, next] = view.findAll("button");
  expect(previous.attributes("disabled")).toBeDefined();
  await next.trigger("click");
  await flushPromises();
  expect(send.mock.calls.at(-1)[0]).toContain("page=2");
  view.unmount();
});
test("切换到训练分区后请求带 partition 并提示样本内", async () => {
  const send = vi
    .spyOn(client, "request")
    .mockResolvedValue(payload({ in_sample: true, partitions: ["train"] }));
  const view = mount(RiskPanel, { props: { run } });
  await flushPromises();
  await view.get("#risk-partition").setValue("train");
  await flushPromises();
  expect(send.mock.calls.at(-1)[0]).toContain("partition=train");
  expect(view.text()).toContain("这些分数是样本内的");
  view.unmount();
});
test("阈值未登记时不臆造等级与百分比", async () => {
  vi.spyOn(client, "request").mockResolvedValue(
    payload({
      items: [
        {
          commit_id: "c".repeat(40),
          event_epoch: 1700000000,
          partition: "test",
          probability: 0.5,
          risk_level: null,
        },
      ],
      threshold: null,
    }),
  );
  const view = mount(RiskPanel, { props: { run } });
  await flushPromises();
  expect(view.findAll("tbody tr")[0].text()).toContain("—");
  expect(view.text()).toContain("未登记");
  view.unmount();
});
test("没有条目时给出空态而不是空表", async () => {
  vi.spyOn(client, "request").mockResolvedValue(
    payload({ items: [], total: 0 }),
  );
  const view = mount(RiskPanel, { props: { run } });
  await flushPromises();
  expect(view.find("table").exists()).toBe(false);
  expect(view.text()).toContain("没有可列出的提交");
  // 总数与页数仍然给出来，避免看起来像一直在加载。
  expect(view.text()).toContain("共 0 条");
  view.unmount();
});
test("工件漂移时显示错误编号而不是空表", async () => {
  vi.spyOn(client, "request").mockRejectedValue(
    new ApiError(409, "MODEL_ARTIFACT_DRIFT"),
  );
  const view = mount(RiskPanel, { props: { run } });
  await flushPromises();
  const alert = view.get('[role="alert"]').text();
  expect(alert).toContain("模型工件校验失败");
  expect(alert).toContain("MODEL_ARTIFACT_DRIFT");
  expect(view.find("table").exists()).toBe(false);
  view.unmount();
});
