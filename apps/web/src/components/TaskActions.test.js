import { mount, flushPromises } from "@vue/test-utils";
import { afterEach, expect, test, vi } from "vitest";
import { client, ApiError } from "../api";
import TaskActions from "./TaskActions.vue";
afterEach(() => {
  vi.restoreAllMocks();
  client.state.user = null;
});
test("Viewer has an explicit read-only reason and no write controls", () => {
  client.state.user = { role: "Viewer" };
  const view = mount(TaskActions, {
    props: { task: { id: "test", status: "running" } },
  });
  expect(view.find("button").exists()).toBe(false);
  expect(view.text()).toContain("Viewer");
  view.unmount();
});
test("uncertain retry retains idempotency key and prevents a second concurrent request", async () => {
  client.state.user = { role: "Member" };
  let reject;
  const send = vi
    .spyOn(client, "request")
    .mockImplementationOnce(
      () =>
        new Promise((_, fail) => {
          reject = fail;
        }),
    )
    .mockResolvedValueOnce({ task_id: "successor" });
  const view = mount(TaskActions, {
    props: { task: { id: "parent", status: "failed" } },
  });
  await view.get("button").trigger("click");
  expect(view.get("button").attributes("disabled")).toBeDefined();
  await view.get("button").trigger("click");
  expect(send).toHaveBeenCalledTimes(1);
  reject(new ApiError(0, "NETWORK_UNAVAILABLE"));
  await flushPromises();
  await view.get("button").trigger("click");
  await flushPromises();
  expect(send.mock.calls[0][1].headers["Idempotency-Key"]).toBe(
    send.mock.calls[1][1].headers["Idempotency-Key"],
  );
  expect(view.emitted("successor")[0]).toEqual(["successor"]);
  view.unmount();
});
