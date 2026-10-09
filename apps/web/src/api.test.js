import { expect, test, vi } from "vitest";
import { createClient } from "./api";
const reply = (data, status = 200) =>
  new Response(status === 204 ? null : JSON.stringify(data), { status });
const account = (token = "test-access") => ({
  access_token: token,
  csrf_token: "test-csrf",
  user: { id: "test-user", username: "test", role: "Member" },
});
const deferred = () => {
  let resolve;
  const promise = new Promise((done) => {
    resolve = done;
  });
  return { promise, resolve };
};

test("concurrent 401 requests share one refresh and retry with the rotated token", async () => {
  const gate = deferred();
  const fetch = vi.fn(async (url, options) => {
    if (url.endsWith("/login")) return reply(account());
    if (url.endsWith("/refresh")) {
      await gate.promise;
      return reply(account("rotated"));
    }
    if (options.headers.Authorization === "Bearer test-access")
      return reply({ code: "AUTH_REQUIRED" }, 401);
    return reply({ ok: true });
  });
  const client = createClient(fetch);
  await client.login("test", "fixture");
  const requests = [client.request("/tasks"), client.request("/users/me")];
  await vi.waitFor(() =>
    expect(
      fetch.mock.calls.filter(([url]) => url.endsWith("/refresh")),
    ).toHaveLength(1),
  );
  gate.resolve();
  expect(await Promise.all(requests)).toEqual([{ ok: true }, { ok: true }]);
  expect(
    fetch.mock.calls.filter(([url]) => url.endsWith("/refresh")),
  ).toHaveLength(1);
});

test("logout waits for refresh then uses the rotated CSRF and old response cannot revive data", async () => {
  const refreshGate = deferred();
  const oldGate = deferred();
  const fetch = vi.fn(async (url, options) => {
    if (url.endsWith("/login")) return reply(account());
    if (url.endsWith("/old")) return oldGate.promise;
    if (url.endsWith("/refresh")) {
      await refreshGate.promise;
      return reply({ ...account("new"), csrf_token: "rotated-csrf" });
    }
    if (url.endsWith("/logout")) {
      expect(options.headers["X-CSRF-Token"]).toBe("rotated-csrf");
      return reply(null, 204);
    }
    if (options.headers.Authorization === "Bearer test-access")
      return reply({ code: "AUTH_REQUIRED" }, 401);
    return reply({ ok: true });
  });
  const client = createClient(fetch);
  await client.login("test", "fixture");
  const old = client.request("/old").catch((error) => error.code);
  const rotating = client.request("/tasks").catch((error) => error.code);
  await vi.waitFor(() =>
    expect(fetch.mock.calls.some(([url]) => url.endsWith("/refresh"))).toBe(
      true,
    ),
  );
  const exiting = client.logout();
  refreshGate.resolve();
  await exiting;
  await rotating;
  oldGate.resolve(reply({ old: true }));
  expect(await old).toBe("SESSION_CHANGED");
  expect(client.state.user).toBeNull();
});

test("revoked session clears identity but transient service failure preserves it", async () => {
  let status = 503;
  const fetch = vi.fn(async (url) =>
    url.endsWith("/login")
      ? reply(account())
      : reply(
          {
            code:
              status === 503
                ? "SYSTEM_DEPENDENCY_UNAVAILABLE"
                : "AUTH_REQUIRED",
          },
          status,
        ),
  );
  const client = createClient(fetch);
  await client.login("test", "fixture");
  await expect(client.request("/tasks")).rejects.toHaveProperty("status", 503);
  expect(client.state.user).not.toBeNull();
  status = 401;
  await expect(client.request("/tasks")).rejects.toHaveProperty("status", 401);
  expect(client.state.user).toBeNull();
});

test("network failures expose a safe message rather than fetch internals", async () => {
  const client = createClient(async () => {
    throw new Error("private-driver-details");
  });
  await expect(client.login("test", "fixture")).rejects.toMatchObject({
    code: "NETWORK_UNAVAILABLE",
  });
  expect(client.state.user).toBeNull();
});
