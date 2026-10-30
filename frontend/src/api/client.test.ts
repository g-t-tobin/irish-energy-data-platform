import { afterEach, describe, expect, it, vi } from "vitest";
import type { Session } from "../auth/types";
import { fakeClient, json, session } from "../test/fakes";
import { ApiError, createApiClient, describeApiError } from "./client";

afterEach(() => vi.unstubAllGlobals());

function setup(
  responses: Array<Response | Error | DOMException>,
  initial: Session | null = session(),
) {
  const fetchMock = vi.fn(async () => {
    const next = responses.shift();
    if (next === undefined) return json({});
    if (next instanceof Response) return next;
    throw next;
  });
  vi.stubGlobal("fetch", fetchMock);
  const auth = fakeClient(initial);
  const onSessionExpired = vi.fn();
  const api = createApiClient({
    baseUrl: "http://api.test",
    auth: auth.client,
    onSessionExpired,
  });
  return { api, auth, fetchMock, onSessionExpired };
}

async function failure(promise: Promise<unknown>) {
  try {
    await promise;
  } catch (error) {
    return error as ApiError;
  }
  throw new Error("expected the request to fail");
}

describe("createApiClient", () => {
  it("sends GET with the access token as a bearer credential", async () => {
    const { api, fetchMock } = setup([json({ runs: [] })]);

    expect(await api.getJson("/api/runs")).toEqual({ runs: [] });

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("http://api.test/api/runs");
    expect(init.method).toBe("GET");
    expect((init.headers as Record<string, string>).Authorization).toBe(
      "Bearer access-token-value",
    );
  });

  it("exposes no way to send anything but GET", () => {
    const { api } = setup([]);

    expect(Object.keys(api)).toEqual(["getJson"]);
  });

  it("treats a missing session as expired and sends nothing", async () => {
    const { api, fetchMock, onSessionExpired } = setup([], null);

    const error = await failure(api.getJson("/api/runs"));

    expect(error.kind).toBe("unauthenticated");
    expect(fetchMock).not.toHaveBeenCalled();
    expect(onSessionExpired).toHaveBeenCalledOnce();
  });

  it("refreshes once after a 401 and retries with the new token", async () => {
    const { api, auth, fetchMock, onSessionExpired } = setup([
      json({}, { status: 401 }),
      json({ runs: [1] }),
    ]);
    auth.client.getSession.mockImplementation(async (options) =>
      session(["viewer"], options?.forceRefresh ? "refreshed-token" : "old-token"),
    );

    expect(await api.getJson("/api/runs")).toEqual({ runs: [1] });

    expect(auth.client.getSession).toHaveBeenNthCalledWith(1, { forceRefresh: false });
    expect(auth.client.getSession).toHaveBeenNthCalledWith(2, { forceRefresh: true });
    const retry = fetchMock.mock.calls[1] as unknown as [string, RequestInit];
    expect((retry[1].headers as Record<string, string>).Authorization).toBe(
      "Bearer refreshed-token",
    );
    expect(onSessionExpired).not.toHaveBeenCalled();
  });

  it("gives up after a second 401 and asks for sign-in", async () => {
    const { api, fetchMock, onSessionExpired } = setup([
      json({}, { status: 401 }),
      json({}, { status: 401 }),
    ]);

    const error = await failure(api.getJson("/api/runs"));

    expect(error.kind).toBe("unauthenticated");
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(onSessionExpired).toHaveBeenCalledOnce();
  });

  it("expires the session when the refresh finds no session", async () => {
    const { api, auth, onSessionExpired } = setup([json({}, { status: 401 })]);
    auth.client.getSession
      .mockResolvedValueOnce(session())
      .mockResolvedValueOnce(null);

    expect((await failure(api.getJson("/api/runs"))).kind).toBe("unauthenticated");
    expect(onSessionExpired).toHaveBeenCalledOnce();
  });

  it.each([
    [403, "forbidden"],
    [503, "unavailable"],
    [500, "http"],
    [404, "http"],
  ])("maps HTTP %i to %s without retrying", async (status, kind) => {
    const { api, fetchMock, onSessionExpired } = setup([json({ detail: "x" }, { status })]);

    const error = await failure(api.getJson("/api/runs"));

    expect(error.kind).toBe(kind);
    expect(error.status).toBe(status);
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(onSessionExpired).not.toHaveBeenCalled();
  });

  it("reads Retry-After on 429", async () => {
    const { api } = setup([json({}, { status: 429, headers: { "Retry-After": "12" } })]);

    const error = await failure(api.getJson("/api/runs"));

    expect(error.kind).toBe("rate_limited");
    expect(error.retryAfterSeconds).toBe(12);
  });

  it("tolerates a missing Retry-After header", async () => {
    const { api } = setup([json({}, { status: 429 })]);

    expect((await failure(api.getJson("/api/runs"))).retryAfterSeconds).toBeUndefined();
  });

  it("maps network failures and malformed bodies", async () => {
    const network = setup([new TypeError("failed to fetch")]);
    expect((await failure(network.api.getJson("/x"))).kind).toBe("network");

    const malformed = setup([new Response("not json", { status: 200 })]);
    expect((await failure(malformed.api.getJson("/x"))).kind).toBe("invalid_response");
  });

  it("lets aborts through untouched", async () => {
    const controller = new AbortController();
    controller.abort();
    const abort = new DOMException("aborted", "AbortError");
    const { api } = setup([abort]);

    await expect(api.getJson("/x", { signal: controller.signal })).rejects.toBe(abort);
  });
});

describe("describeApiError", () => {
  it.each([
    [new ApiError("forbidden", 403), "does not have access"],
    [new ApiError("rate_limited", 429, 7), "Try again in 7 seconds"],
    [new ApiError("rate_limited", 429), "Try again shortly"],
    [new ApiError("unavailable", 503), "temporarily unavailable"],
    [new ApiError("network"), "Unable to reach the API"],
    [new ApiError("unauthenticated", 401), "Sign in again"],
    [new ApiError("http", 500), "HTTP 500"],
    [new Error("token=abc.def.ghi"), "Something went wrong"],
  ])("describes %j safely", (error, text) => {
    const message = describeApiError(error);

    expect(message).toContain(text);
    expect(message).not.toContain("abc.def.ghi");
  });
});
