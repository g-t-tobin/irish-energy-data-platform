import { describe, expect, it } from "vitest";
import {
  PRODUCTION_API_ORIGIN,
  PRODUCTION_SITE_URL,
  validateProductionEnv,
} from "./productionEnv.ts";

const valid = {
  VITE_API_BASE_URL: PRODUCTION_API_ORIGIN,
  VITE_COGNITO_USER_POOL_ID: "eu-west-1_TestPool1",
  VITE_COGNITO_APP_CLIENT_ID: "testclientid1234567890",
  VITE_COGNITO_DOMAIN: "example.auth.eu-west-1.amazoncognito.com",
  VITE_COGNITO_REDIRECT_SIGN_IN: PRODUCTION_SITE_URL,
  VITE_COGNITO_REDIRECT_SIGN_OUT: PRODUCTION_SITE_URL,
};

describe("validateProductionEnv", () => {
  it("accepts the production configuration", () => {
    expect(validateProductionEnv(valid)).toEqual([]);
    expect(validateProductionEnv({ ...valid, VITE_API_BASE_URL: `${PRODUCTION_API_ORIGIN}/` })).toEqual([]);
  });

  it("refuses an empty environment, naming every missing variable", () => {
    const problems = validateProductionEnv({});

    expect(problems).toContain("VITE_API_BASE_URL is not set");
    expect(problems.filter((p) => p.endsWith("is not set"))).toHaveLength(6);
  });

  it.each([
    "http://127.0.0.1:8001",
    "http://localhost:8001",
    "http://energy-api.ai-autofy-general-server.com",
    "https://api.ai-autofy-general-server.com",
    "https://play.ai-autofy-general-server.com",
    "https://energy-api.ai-autofy-general-server.com/api",
    "https://energy-api.ai-autofy-general-server.com/?x=1",
    "https://user:pass@energy-api.ai-autofy-general-server.com",
    "wss://energy-api.ai-autofy-general-server.com",
    "not a url",
  ])("refuses the API URL %s", (url) => {
    expect(validateProductionEnv({ ...valid, VITE_API_BASE_URL: url })).toEqual([
      "VITE_API_BASE_URL must be the production API origin over https",
    ]);
  });

  it.each([
    "http://localhost:5173/",
    "http://127.0.0.1:5173/",
    "https://play.ai-autofy-general-server.com/",
    "https://energy.ai-autofy-general-server.com",
  ])("refuses the redirect URL %s", (url) => {
    const problems = validateProductionEnv({ ...valid, VITE_COGNITO_REDIRECT_SIGN_IN: url });

    expect(problems).toHaveLength(1);
    expect(problems[0]).toContain("VITE_COGNITO_REDIRECT_SIGN_IN");
  });

  it("never puts a value in a message", () => {
    const secret = "https://top-secret.example.com";
    const problems = validateProductionEnv({ ...valid, VITE_API_BASE_URL: secret, VITE_COGNITO_DOMAIN: secret });

    expect(problems.join(" ")).not.toContain("top-secret");
  });
});
