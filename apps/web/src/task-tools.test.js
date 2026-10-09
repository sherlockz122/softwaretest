import { expect, test } from "vitest";
import { operationKeys } from "./task-tools";
test("uncertain creation reuses key, while changed input and confirmed success get distinct keys", () => {
  let count = 0;
  const keys = operationKeys(() => String(++count));
  const original = keys.get("create", 3);
  expect(keys.get("create", 3)).toBe(original);
  expect(keys.get("create", 4)).not.toBe(original);
  keys.done("create", 3);
  expect(keys.get("create", 3)).not.toBe(original);
});
