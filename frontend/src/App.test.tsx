import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "./App";
import { AuthProvider } from "./auth/AuthProvider";
import { fakeClient, json, session, validConfig } from "./test/fakes";

afterEach(() => vi.unstubAllGlobals());

const analytics = {
  source: "gold/eirgrid/daily_demand",
  expected_interval_minutes: 15,
  days: [
    {
      date: "2026-10-28",
      region: "ROI",
      average_demand_mw: 4427.6,
      minimum_demand_mw: 3544,
      maximum_demand_mw: 5323,
      observations: 96,
      expected_observations: 96,
      is_complete: true,
      quality_status: "complete",
      quality_note: "All expected intervals are present.",
    },
    {
      date: "2026-10-29",
      region: "ROI",
      average_demand_mw: 4380.3,
      minimum_demand_mw: 3541,
      maximum_demand_mw: 5223,
      observations: 84,
      expected_observations: 96,
      is_complete: false,
      quality_status: "partial",
      quality_note: "12 interval(s) missing.",
    },
  ],
};
const runs = {
  runs: [
    {
      run_id: "run-abc",
      status: "failed",
      stage: "bronze",
      days_back: 2,
      created_at: "2026-10-29T10:00:00+00:00",
      error: "The pipeline run failed.",
    },
  ],
};

type Handler = () => Response | Promise<Response>;

function mockApi(handlers: { analytics?: Handler; runs?: Handler }) {
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => {
    void init;
    const path = new URL(url).pathname;
    if (path === "/api/analytics/daily-demand") return (handlers.analytics ?? (() => json(analytics)))();
    if (path === "/api/runs") return (handlers.runs ?? (() => json(runs)))();
    return json({}, { status: 404 });
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function renderApp(fake = fakeClient()) {
  render(
    <AuthProvider config={validConfig()} apiBaseUrl="http://api.test" createClient={() => fake.client}>
      <App />
    </AuthProvider>,
  );
  return fake;
}

function methodsAndHeaders(fetchMock: ReturnType<typeof mockApi>) {
  return fetchMock.mock.calls.map(
    ([, init]) => init as unknown as { method: string; headers: Record<string, string> },
  );
}

describe("authenticated requests", () => {
  it("attaches the access token to both reads and only ever uses GET", async () => {
    const fetchMock = mockApi({});

    renderApp();
    await screen.findByText("run-abc");

    const calls = methodsAndHeaders(fetchMock);
    expect(calls).toHaveLength(2);
    for (const call of calls) {
      expect(call.method).toBe("GET");
      expect(call.headers.Authorization).toBe("Bearer access-token-value");
    }
  });

  it("renders real analytics, run history and per-day quality indicators", async () => {
    mockApi({});

    renderApp();

    expect(await screen.findByText("run-abc")).toBeTruthy();
    expect(screen.getAllByText("4,380 MW").length).toBeGreaterThan(0);
    expect(screen.getByText("84 / 96")).toBeTruthy();
    expect(screen.getByText("◐ Partial")).toBeTruthy();
    expect(screen.getByText("The pipeline run failed.")).toBeTruthy();
  });

  it("shows the empty states", async () => {
    mockApi({
      analytics: () => json({ ...analytics, days: [] }),
      runs: () => json({ runs: [] }),
    });

    renderApp();

    expect(await screen.findByText(/No Gold daily-demand files were found/)).toBeTruthy();
    expect(await screen.findByText(/No pipeline runs have been recorded/)).toBeTruthy();
  });
});

describe("roles (presentation only)", () => {
  it("hides the Run pipeline control from viewers", async () => {
    mockApi({});

    renderApp(fakeClient(session(["viewer"])));

    expect(await screen.findByText("Read-only access")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Run pipeline/ })).toBeNull();
  });

  it("shows operators the control, which stays a local demonstration", async () => {
    const fetchMock = mockApi({});
    renderApp(fakeClient(session(["operator"])));
    const button = await screen.findByRole("button", { name: /Run pipeline/ });
    await screen.findByText("run-abc");
    const callsBefore = fetchMock.mock.calls.length;

    await userEvent.click(button);

    expect(await screen.findByText(/Pipeline execution is disabled/)).toBeTruthy();
    expect(fetchMock.mock.calls.length).toBe(callsBefore);
    expect(methodsAndHeaders(fetchMock).every((call) => call.method === "GET")).toBe(true);
  });

  it("treats a user without a known group as having no role", async () => {
    mockApi({});

    renderApp(fakeClient(session(["Admin"])));

    expect(await screen.findByText("alice · no role")).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Run pipeline/ })).toBeNull();
  });
});

describe("API failure handling", () => {
  it.each([
    [403, {}, "does not have access to this data"],
    [429, { "Retry-After": "9" }, "Try again in 9 seconds"],
    [503, {}, "temporarily unavailable"],
    [500, {}, "unexpected error (HTTP 500)"],
  ])("shows a specific message for HTTP %i in both panels", async (status, headers, text) => {
    const failing = () => json({ detail: "internal-secret-detail" }, { status, headers });
    mockApi({ analytics: failing, runs: failing });

    renderApp();

    const alerts = await screen.findAllByRole("alert");
    expect(alerts).toHaveLength(2);
    for (const alert of alerts) {
      expect(alert.textContent).toContain(text);
      expect(alert.textContent).not.toContain("internal-secret-detail");
      expect(within(alert).getByRole("button", { name: "Retry" })).toBeTruthy();
    }
  });

  it("shows a network failure and recovers on Retry", async () => {
    let healthy = false;
    const analyticsHandler = () => {
      if (!healthy) throw new TypeError("failed to fetch");
      return json(analytics);
    };
    const fetchMock = mockApi({ analytics: analyticsHandler });
    renderApp();
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Unable to reach the API");
    const callsBefore = fetchMock.mock.calls.length;

    healthy = true;
    await userEvent.click(within(alert).getByRole("button", { name: "Retry" }));

    expect(await screen.findByText(/Source: gold\/eirgrid\/daily_demand/)).toBeTruthy();
    expect(fetchMock.mock.calls.length).toBeGreaterThan(callsBefore);
  });

  it("rejects malformed analytics without showing fabricated data", async () => {
    mockApi({ analytics: () => json({ unexpected: true }) });

    renderApp();

    expect((await screen.findByRole("alert")).textContent).toContain("unexpected response");
    expect(screen.queryByText("84 / 96")).toBeNull();
  });
});

describe("401 handling", () => {
  it("refreshes the session once and retries", async () => {
    let first = true;
    mockApi({
      runs: () => {
        if (first) {
          first = false;
          return json({}, { status: 401 });
        }
        return json(runs);
      },
    });
    const fake = fakeClient();

    renderApp(fake);

    expect(await screen.findByText("run-abc")).toBeTruthy();
    expect(fake.client.getSession).toHaveBeenCalledWith({ forceRefresh: true });
  });

  it("returns to sign-in when the session cannot be refreshed", async () => {
    mockApi({ analytics: () => json({}, { status: 401 }), runs: () => json({}, { status: 401 }) });

    renderApp();

    expect((await screen.findByRole("alert")).textContent).toBe(
      "Your session expired. Sign in again.",
    );
    expect(screen.getByRole("button", { name: "Sign in" })).toBeTruthy();
    expect(screen.queryByText("Energy data overview")).toBeNull();
  });
});
