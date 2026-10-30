import { describe, expect, it } from "vitest";
import { validEnv } from "../test/fakes";
import { readAuthConfig } from "./config";

describe("readAuthConfig", () => {
  it("accepts a complete configuration and derives the region", () => {
    const result = readAuthConfig(validEnv);

    expect(result).toEqual({
      ok: true,
      config: {
        region: "eu-west-1",
        userPoolId: "eu-west-1_AbC123xyz",
        clientId: "abcdefghij1234567890",
        domain: "example-prefix.auth.eu-west-1.amazoncognito.com",
        redirectSignIn: "http://localhost:5173/",
        redirectSignOut: "http://localhost:5173/",
        scopes: ["openid"],
      },
    });
  });

  it("reports every missing variable by name only", () => {
    const result = readAuthConfig({});

    expect(result).toEqual({
      ok: false,
      problems: [
        "VITE_COGNITO_USER_POOL_ID is not set",
        "VITE_COGNITO_APP_CLIENT_ID is not set",
        "VITE_COGNITO_DOMAIN is not set",
        "VITE_COGNITO_REDIRECT_SIGN_IN is not set",
        "VITE_COGNITO_REDIRECT_SIGN_OUT is not set",
      ],
    });
  });

  it("treats blank values as missing", () => {
    const result = readAuthConfig({ ...validEnv, VITE_COGNITO_DOMAIN: "   " });

    expect(result).toEqual({ ok: false, problems: ["VITE_COGNITO_DOMAIN is not set"] });
  });

  it.each([
    ["VITE_COGNITO_USER_POOL_ID", "not-a-pool"],
    ["VITE_COGNITO_APP_CLIENT_ID", "short"],
    ["VITE_COGNITO_DOMAIN", "https://example.auth.eu-west-1.amazoncognito.com"],
    ["VITE_COGNITO_DOMAIN", "no_dots"],
    ["VITE_COGNITO_REDIRECT_SIGN_IN", "http://app.example.com/"],
    ["VITE_COGNITO_REDIRECT_SIGN_IN", "not a url"],
    ["VITE_COGNITO_REDIRECT_SIGN_OUT", "https://app.example.com/#frag"],
    ["VITE_COGNITO_SCOPES", "openid bad scope!"],
  ])("rejects an invalid %s without echoing the value", (name, value) => {
    const result = readAuthConfig({ ...validEnv, [name]: value });

    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.problems).toEqual([`${name} is invalid`]);
      expect(JSON.stringify(result)).not.toContain(value);
    }
  });

  it("allows https redirects and parses scopes", () => {
    const result = readAuthConfig({
      ...validEnv,
      VITE_COGNITO_REDIRECT_SIGN_IN: "https://app.example.com/",
      VITE_COGNITO_REDIRECT_SIGN_OUT: "https://app.example.com/signed-out",
      VITE_COGNITO_SCOPES: "openid, email",
    });

    expect(result.ok && result.config.scopes).toEqual(["openid", "email"]);
  });

  it("has no setting for a client secret", () => {
    const result = readAuthConfig({ ...validEnv, VITE_COGNITO_CLIENT_SECRET: "should-be-ignored" });

    expect(JSON.stringify(result)).not.toContain("should-be-ignored");
  });
});
