import { mount, flushPromises } from "@vue/test-utils";
import { afterEach, expect, test, vi } from "vitest";
import { client } from "../api";
import FixRecommendationsPanel from "./FixRecommendationsPanel.vue";
afterEach(() => {
  vi.restoreAllMocks();
});
const response = {
  items: [
    {
      fix_sha: "a".repeat(40),
      message_summary: "Fix crash in parser",
      touched_files: ["src/Parser.java", "src/Token.java"],
      touched_files_shown: 2,
      evidence_level: "high",
      similarity: {
        file_overlap: 2,
        file_path_overlap: null,
        feature_distance: 0.42,
        evidence_rank: 0,
      },
    },
    {
      fix_sha: "b".repeat(40),
      message_summary: "Closes #18",
      touched_files: [],
      touched_files_shown: 0,
      evidence_level: null,
      similarity: {
        file_overlap: 0,
        file_path_overlap: null,
        feature_distance: null,
        evidence_rank: null,
      },
    },
  ],
  total: 2,
  limit: 20,
  rule_version: "fix-evidence-v1",
  target: { sha: "c".repeat(40), message_summary: "", touched_files: [] },
  scope: "repository_full_history_including_later_fixes",
};
test("panel lists references without exposing scoring details", async () => {
  const send = vi.spyOn(client, "request").mockResolvedValue(response);
  const wrapper = mount(FixRecommendationsPanel, {
    props: { repositoryId: "repo", sha: "c".repeat(40) },
  });
  await flushPromises();
  expect(send.mock.calls[0][0]).toContain("/repositories/repo/fix-recommendations");
  expect(send.mock.calls[0][0]).toContain(`sha=${"c".repeat(40)}`);
  expect(wrapper.text()).toContain("以前修过类似问题的提交");
  expect(wrapper.text()).toContain("共 2 条匹配，最多显示 20 条");
  expect(wrapper.text()).toContain("Fix crash in parser");
  expect(wrapper.text()).toContain("src/Parser.java");
  // 只留三列：修复提交、提交信息摘要、触及文件。
  expect(wrapper.findAll("th").map((cell) => cell.text())).toEqual([
    "修复提交",
    "提交信息摘要",
    "触及文件",
  ]);
  const buttons = wrapper.findAll("button.link");
  expect(buttons).toHaveLength(2);
  await buttons[0].trigger("click");
  expect(wrapper.emitted("select")[0]).toEqual(["a".repeat(40)]);
  wrapper.unmount();
});
test("panel shows explicit empty state without history", async () => {
  vi.spyOn(client, "request").mockResolvedValue({
    items: [],
    total: 0,
    limit: 20,
    rule_version: "fix-evidence-v1",
    target: { sha: "c".repeat(40), message_summary: "", touched_files: [] },
    scope: "repository_full_history_including_later_fixes",
  });
  const wrapper = mount(FixRecommendationsPanel, {
    props: { repositoryId: "repo", sha: "c".repeat(40) },
  });
  await flushPromises();
  expect(wrapper.text()).toContain("该仓库尚无修复历史");
  expect(wrapper.findAll("table")).toHaveLength(0);
  wrapper.unmount();
});
test("panel surfaces the request error message", async () => {
  vi.spyOn(client, "request").mockRejectedValue({
    code: "FIX_RECOMMEND_COMMIT_NOT_FOUND",
    message: "修复参考检索未通过，请核对仓库与提交编号",
  });
  const wrapper = mount(FixRecommendationsPanel, {
    props: { repositoryId: "repo", sha: "0".repeat(40) },
  });
  await flushPromises();
  expect(wrapper.text()).toContain("修复参考检索未通过");
  wrapper.unmount();
});
