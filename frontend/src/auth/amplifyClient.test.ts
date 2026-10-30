import { beforeEach, describe, expect, it, vi } from "vitest";
import { validConfig } from "../test/fakes";

const mocks = vi.hoisted(() => ({
  configure: vi.fn(),
  setKeyValueStorage: vi.fn(),
  fetchAuthSession: vi.fn(),
  signInWithRedirect: vi.fn(),
  signOut: vi.fn(),
  listen: vi.fn(),
}));

vi.mock("aws-amplify", () => ({ Amplify: { configure: mocks.configure } }));
vi.mock("aws-amplify/auth/enable-oauth-listener", () => ({}));
vi.mock("aws-amplify/auth", () => ({
  fetchAuthSession: mocks.fetchAuthSession,
  signInWithRedirect: mocks.signInWithRedirect,
  signOut: mocks.signOut,
}));
vi.mock("aws-amplify/auth/cognito", () => ({
  cognitoUserPoolsTokenProvider: { setKeyValueStorage: mocks.setKeyValueStorage },
}));
vi.mock("aws-amplify/utils", () => ({ Hub: { listen: mocks.listen } }));

const { createAmplifyClient } = await import("./amplifyClient");

function tokens(groups: unknown) {
  return {
    accessToken: {
      toString: () => "access-token-value",
      payload: { username: "alice", sub: "sub-1", "cognito:groups": groups },
    },
    idToken: { toString: () => "id-token-value", payload: { email: "a@example.com" } },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe("createAmplifyClient", () => {
  it("configures the hosted UI authorization-code flow with no client secret", () => {
    createAmplifyClient(validConfig().config);

    const config = mocks.configure.mock.calls[0][0];
    const cognito = config.Auth.Cognito;
    expect(cognito.userPoolId).toBe("eu-west-1_AbC123xyz");
    expect(cognito.userPoolClientId).toBe("abcdefghij1234567890");
    expect(cognito.loginWith.oauth).toEqual({
      domain: "example-prefix.auth.eu-west-1.amazoncognito.com",
      scopes: ["openid"],
      redirectSignIn: ["http://localhost:5173/"],
      redirectSignOut: ["http://localhost:5173/"],
      responseType: "code",
    });
    expect(JSON.stringify(config).toLowerCase()).not.toContain("secret");
  });

  it("keeps tokens in memory, set after configure so it is not reset", async () => {
    createAmplifyClient(validConfig().config);

    expect(mocks.setKeyValueStorage.mock.invocationCallOrder[0]).toBeGreaterThan(
      mocks.configure.mock.invocationCallOrder[0],
    );
    const storage = mocks.setKeyValueStorage.mock.calls[0][0];
    await storage.setItem("token", "secret-value");
    expect(await storage.getItem("token")).toBe("secret-value");
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
    await storage.removeItem("token");
    expect(await storage.getItem("token")).toBeNull();
  });

  it("returns the access token, never the ID token, plus groups for display", async () => {
    mocks.fetchAuthSession.mockResolvedValue({ tokens: tokens(["operator", "viewer"]) });
    const client = createAmplifyClient(validConfig().config);

    const session = await client.getSession();

    expect(session).toEqual({
      accessToken: "access-token-value",
      username: "alice",
      groups: ["operator", "viewer"],
    });
    expect(JSON.stringify(session)).not.toContain("id-token-value");
  });

  it.each([[undefined], ["operator"], [[1, 2]], [{ operator: true }]])(
    "ignores malformed group claim %j",
    async (claim) => {
      mocks.fetchAuthSession.mockResolvedValue({ tokens: tokens(claim) });

      const session = await createAmplifyClient(validConfig().config).getSession();

      expect(session?.groups).toEqual([]);
    },
  );

  it("passes forceRefresh through", async () => {
    mocks.fetchAuthSession.mockResolvedValue({ tokens: tokens([]) });
    const client = createAmplifyClient(validConfig().config);

    await client.getSession({ forceRefresh: true });
    await client.getSession();

    expect(mocks.fetchAuthSession).toHaveBeenNthCalledWith(1, { forceRefresh: true });
    expect(mocks.fetchAuthSession).toHaveBeenNthCalledWith(2, { forceRefresh: false });
  });

  it("returns null without tokens or when Amplify throws", async () => {
    const client = createAmplifyClient(validConfig().config);

    mocks.fetchAuthSession.mockResolvedValueOnce({});
    expect(await client.getSession()).toBeNull();
    mocks.fetchAuthSession.mockRejectedValueOnce(new Error("token=abc.def.ghi"));
    expect(await client.getSession()).toBeNull();
  });

  it("starts the hosted UI redirect and signs out", async () => {
    const client = createAmplifyClient(validConfig().config);

    await client.signIn();
    await client.signOut();

    expect(mocks.signInWithRedirect).toHaveBeenCalledOnce();
    expect(mocks.signOut).toHaveBeenCalledOnce();
  });

  it("maps Hub auth events and ignores others", () => {
    const stop = vi.fn();
    mocks.listen.mockReturnValue(stop);
    const client = createAmplifyClient(validConfig().config);
    const received: string[] = [];

    const unsubscribe = client.subscribe((event) => received.push(event.type));
    const handler = mocks.listen.mock.calls[0][1];
    for (const event of [
      "signedIn",
      "signedOut",
      "signInWithRedirect_failure",
      "tokenRefresh_failure",
      "tokenRefresh",
    ]) {
      handler({ payload: { event } });
    }

    expect(received).toEqual(["signedIn", "signedOut", "signInFailed", "tokenRefreshFailed"]);
    unsubscribe();
    expect(unsubscribe).toBe(stop);
  });
});
