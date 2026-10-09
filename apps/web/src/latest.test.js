import { effectScope } from "vue";
import { expect, test } from "vitest";
import { useLatest } from "./latest";
test("late list response and disposed scope cannot overwrite the latest query", async () => {
  const pending = [];
  const scope = effectScope();
  const state = scope.run(() =>
    useLatest(
      (input, signal) =>
        new Promise((resolve) => pending.push({ input, signal, resolve })),
    ),
  );
  const old = state.load("running");
  const fresh = state.load("failed");
  expect(pending[0].signal.aborted).toBe(true);
  pending[1].resolve("failed-result");
  await fresh;
  pending[0].resolve("running-result");
  await old;
  expect(state.data.value).toBe("failed-result");
  const last = state.load("cancelled");
  expect(state.data.value).toBeNull();
  scope.stop();
  pending[2].resolve("should-not-display");
  await last;
  expect(state.data.value).toBeNull();
});
