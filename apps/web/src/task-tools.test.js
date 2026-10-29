import { expect, test } from "vitest";
import {
  fileSize,
  operationKeys,
  shortId,
  taskLine,
  taskTitle,
} from "./task-tools";
test("uncertain creation reuses key, while changed input and confirmed success get distinct keys", () => {
  let count = 0;
  const keys = operationKeys(() => String(++count));
  const original = keys.get("create", 3);
  expect(keys.get("create", 3)).toBe(original);
  expect(keys.get("create", 4)).not.toBe(original);
  keys.done("create", 3);
  expect(keys.get("create", 3)).not.toBe(original);
});
test("titles and result lines use plain language and hide raw identifiers", () => {
  expect(taskTitle({ type: "model.train", result: { plugin: "rf" } })).toBe(
    "训练模型（随机森林）",
  );
  expect(taskTitle({ type: "dataset.build" })).toBe("生成研究数据集");
  expect(
    taskLine({
      type: "dataset.build",
      status: "succeeded",
      result: { rows: 106674 },
    }),
  ).toBe("共 106,674 条样本");
  expect(
    taskLine({
      type: "repository.clone",
      status: "succeeded",
      result: { size_bytes: 750762 },
    }),
  ).toBe("仓库大小约 733 KB");
  expect(
    taskLine({
      type: "repository.fix",
      status: "failed",
      result: { assessed: 5 },
    }),
  ).toBe("");
  expect(shortId("535a70d2-1636-46cd-9f30-acfc163fb437")).toBe("535a70d2");
  expect(fileSize(512)).toBe("512 字节");
  expect(fileSize(undefined)).toBe("");
});
