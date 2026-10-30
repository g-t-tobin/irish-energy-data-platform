import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import { readAuthConfig } from "./config";
import { AuthProvider } from "./AuthProvider";
import { fakeClient, json, session, validConfig } from "../test/fakes";

afterEach(() => {
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
});

function stubApi() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) =>
      new URL(url).pathname === "/api/runs"
        ? json({ runs: [] })
        : json({ source: "gold/eirgrid/daily_demand", expected_interval_minutes: 15, days: [] }),
    ),
  );
}

function renderApp(fake = fakeClient()) {
  return render(
    <AuthProvider
      config={validConfig()}
      apiBaseUrl="http://api.test"
      createClient={() => fake.client}
    >
      <App />
    </AuthProvider>,
  );
}

describe("missing configuration", () => {
  it("fails closed with variable names, never creating a client or calling the API", () => {
    const createClient = vi.fn();
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    render(
      <AuthProvider
        config={readAuthConfig({ VITE_COGNITO_APP_CLIENT_ID: "abcdefghij1234567890" })}
        apiBaseUrl="http://api.test"
        createClient={createClient}
      >
        <App />
      </AuthProvider>,
    );

    expect(screen.getByRole("alert").textContent).toContain("Authentication is not configured");
    expect(screen.getByText("VITE_COGNITO_USER_POOL_ID is not set")).toBeTruthy();
    expect(screen.queryByText("VITE_COGNITO_APP_CLIENT_ID is not set")).toBeNull();
    expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull();
    expect(createClient).not.toHaveBeenCalled();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("signed-out state", () => {
  it("shows sign-in and makes no API request", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    renderApp(fakeClient(null));

    expect(await screen.findByRole("button", { name: "Sign in" })).toBeTruthy();
    expect(screen.queryByText("Energy data overview")).toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("starts the hosted UI sign-in when the button is used", async () => {
    const fake = fakeClient(null);
    renderApp(fake);

    await userEvent.click(await screen.findByRole("button", { name: "Sign in" }));

    expect(fake.client.signIn).toHaveBeenCalledOnce();
  });

  it("shows a generic message when sign-in cannot start", async () => {
    const fake = fakeClient(null);
    fake.client.signIn.mockRejectedValue(new Error("token=abc.def.ghi"));
    renderApp(fake);

    await userEvent.click(await screen.findByRole("button", { name: "Sign in" }));

    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("Unable to start sign-in. Try again.");
  });
});

describe("signed-in state", () => {
  it("shows the dashboard with the user and role, but never the token", async () => {
    stubApi();

    const { container } = renderApp(fakeClient(session(["viewer"])));

    expect(await screen.findByText("Energy data overview")).toBeTruthy();
    expect(screen.getByText("alice · viewer")).toBeTruthy();
    expect(container.textContent).not.toContain("access-token-value");
  });

  it("signs out and returns to the sign-in screen", async () => {
    stubApi();
    const fake = fakeClient(session(["viewer"]));
    renderApp(fake);

    await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));

    expect(fake.client.signOut).toHaveBeenCalledOnce();
    expect(await screen.findByRole("button", { name: "Sign in" })).toBeTruthy();
    expect(screen.queryByText("Energy data overview")).toBeNull();
  });

  it("still returns to sign-in if sign-out fails", async () => {
    stubApi();
    const fake = fakeClient(session(["viewer"]));
    fake.client.signOut.mockRejectedValue(new Error("network"));
    renderApp(fake);

    await userEvent.click(await screen.findByRole("button", { name: "Sign out" }));

    expect(await screen.findByRole("button", { name: "Sign in" })).toBeTruthy();
  });

  it("reacts to sign-out events from elsewhere", async () => {
    stubApi();
    const fake = fakeClient(session());
    renderApp(fake);
    await screen.findByText("Energy data overview");

    fake.setSession(null);
    fake.emit({ type: "signedOut" });

    expect(await screen.findByRole("button", { name: "Sign in" })).toBeTruthy();
  });
});

describe("failed and expired sessions", () => {
  it("shows a generic notice after a failed sign-in", async () => {
    const fake = fakeClient(null);
    renderApp(fake);
    await screen.findByRole("button", { name: "Sign in" });

    fake.emit({ type: "signInFailed" });

    expect((await screen.findByRole("alert")).textContent).toBe("Sign-in failed. Try again.");
  });

  it("shows an expiry notice when token refresh fails", async () => {
    stubApi();
    const fake = fakeClient();
    renderApp(fake);
    await screen.findByText("Energy data overview");

    fake.emit({ type: "tokenRefreshFailed" });

    expect((await screen.findByRole("alert")).textContent).toBe(
      "Your session expired. Sign in again.",
    );
  });
});

describe("returning from the hosted UI", () => {
  it("waits for the code exchange event instead of flashing the sign-in screen", async () => {
    stubApi();
    window.history.replaceState({}, "", "/?code=abc&state=xyz");
    const fake = fakeClient(null);
    renderApp(fake);

    expect(screen.getByText("Checking your session…")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Sign in" })).toBeNull();
    expect(fake.client.getSession).not.toHaveBeenCalled();

    fake.setSession(session());
    fake.emit({ type: "signedIn" });

    expect(await screen.findByText("Energy data overview")).toBeTruthy();
  });

  it("reports a failed redirect", async () => {
    window.history.replaceState({}, "", "/?error=access_denied");
    const fake = fakeClient(null);
    renderApp(fake);

    fake.emit({ type: "signInFailed" });

    await waitFor(() =>
      expect(screen.getByRole("alert").textContent).toBe("Sign-in failed. Try again."),
    );
  });

  it("falls back to the session check if no event arrives", async () => {
    vi.useFakeTimers();
    try {
      window.history.replaceState({}, "", "/?code=abc&state=xyz");
      const fake = fakeClient(null);
      renderApp(fake);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(15_000);
      });

      expect(fake.client.getSession).toHaveBeenCalled();
      expect(screen.getByRole("alert").textContent).toBe("Sign-in did not complete. Try again.");
    } finally {
      vi.useRealTimers();
    }
  });
});
