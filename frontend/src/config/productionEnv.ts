import { readAuthConfig } from "../auth/config.ts";

export const PRODUCTION_API_ORIGIN = "https://energy-api.ai-autofy-general-server.com";
export const PRODUCTION_SITE_URL = "https://energy.ai-autofy-general-server.com/";

type Env = Record<string, unknown>;

function text(env: Env, name: string): string {
  const value = env[name];
  return typeof value === "string" ? value.trim() : "";
}

function hasExactValue(env: Env, name: string, expected: string, problems: string[]): void {
  const value = text(env, name);
  if (value && value !== expected) problems.push(`${name} must be the production site URL`);
}

/**
 * Problems that make a production build unsafe; empty when the build may proceed.
 * Messages name variables, never their values. The app has no WebSocket URL.
 */
export function validateProductionEnv(env: Env): string[] {
  const problems: string[] = [];

  const api = text(env, "VITE_API_BASE_URL");
  if (!api) {
    problems.push("VITE_API_BASE_URL is not set");
  } else {
    let valid = false;
    try {
      const url = new URL(api);
      valid =
        url.origin === PRODUCTION_API_ORIGIN &&
        url.pathname === "/" &&
        !url.search &&
        !url.hash &&
        !url.username &&
        !url.password;
    } catch {
      valid = false;
    }
    if (!valid) problems.push("VITE_API_BASE_URL must be the production API origin over https");
  }

  const auth = readAuthConfig(env);
  if (!auth.ok) problems.push(...auth.problems);
  hasExactValue(env, "VITE_COGNITO_REDIRECT_SIGN_IN", PRODUCTION_SITE_URL, problems);
  hasExactValue(env, "VITE_COGNITO_REDIRECT_SIGN_OUT", PRODUCTION_SITE_URL, problems);

  return problems;
}
