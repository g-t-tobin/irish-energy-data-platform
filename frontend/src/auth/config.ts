export type AuthConfig = {
  region: string;
  userPoolId: string;
  clientId: string;
  domain: string;
  redirectSignIn: string;
  redirectSignOut: string;
  scopes: string[];
};

export type AuthConfigResult =
  | { ok: true; config: AuthConfig }
  | { ok: false; problems: string[] };

type Env = Record<string, unknown>;

const POOL_RE = /^(?<region>[a-z]{2}(?:-[a-z]+)+-\d)_[A-Za-z0-9]+$/;
const CLIENT_RE = /^[A-Za-z0-9]{10,128}$/;
const DOMAIN_RE = /^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,}$/;
const SCOPE_RE = /^[A-Za-z0-9_.:/-]+$/;

function text(env: Env, name: string): string {
  const value = env[name];
  return typeof value === "string" ? value.trim() : "";
}

// Cognito matches redirect URIs exactly; plain http is only for local development.
function isAllowedRedirect(value: string): boolean {
  try {
    const url = new URL(value);
    if (url.hash || url.username || url.password) return false;
    if (url.protocol === "https:") return true;
    return (
      url.protocol === "http:" &&
      (url.hostname === "localhost" || url.hostname === "127.0.0.1")
    );
  } catch {
    return false;
  }
}

/** Validates frontend env vars. Problems name variables, never their values. */
export function readAuthConfig(env: Env): AuthConfigResult {
  const problems: string[] = [];
  const read = (name: string, valid: (value: string) => boolean) => {
    const value = text(env, name);
    if (!value) {
      problems.push(`${name} is not set`);
      return "";
    }
    if (!valid(value)) {
      problems.push(`${name} is invalid`);
      return "";
    }
    return value;
  };

  const userPoolId = read("VITE_COGNITO_USER_POOL_ID", (v) => POOL_RE.test(v));
  const clientId = read("VITE_COGNITO_APP_CLIENT_ID", (v) => CLIENT_RE.test(v));
  const domain = read("VITE_COGNITO_DOMAIN", (v) => DOMAIN_RE.test(v));
  const redirectSignIn = read("VITE_COGNITO_REDIRECT_SIGN_IN", isAllowedRedirect);
  const redirectSignOut = read("VITE_COGNITO_REDIRECT_SIGN_OUT", isAllowedRedirect);

  const rawScopes = text(env, "VITE_COGNITO_SCOPES");
  const scopes = rawScopes ? rawScopes.split(/[\s,]+/).filter(Boolean) : ["openid"];
  if (!scopes.every((scope) => SCOPE_RE.test(scope))) {
    problems.push("VITE_COGNITO_SCOPES is invalid");
  }

  if (problems.length > 0) return { ok: false, problems };
  const region = POOL_RE.exec(userPoolId)?.groups?.region ?? "";
  return {
    ok: true,
    config: { region, userPoolId, clientId, domain, redirectSignIn, redirectSignOut, scopes },
  };
}
