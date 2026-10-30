import { vi } from "vitest";
import { readAuthConfig } from "../auth/config";
import type { AuthClient, AuthEvent, Session } from "../auth/types";

export const validEnv = {
  VITE_COGNITO_USER_POOL_ID: "eu-west-1_AbC123xyz",
  VITE_COGNITO_APP_CLIENT_ID: "abcdefghij1234567890",
  VITE_COGNITO_DOMAIN: "example-prefix.auth.eu-west-1.amazoncognito.com",
  VITE_COGNITO_REDIRECT_SIGN_IN: "http://localhost:5173/",
  VITE_COGNITO_REDIRECT_SIGN_OUT: "http://localhost:5173/",
};

export function validConfig() {
  const result = readAuthConfig(validEnv);
  if (!result.ok) throw new Error("test config must be valid");
  return result;
}

export function session(groups: string[] = ["viewer"], accessToken = "access-token-value"): Session {
  return { accessToken, username: "alice", groups };
}

export function fakeClient(initial: Session | null = session()) {
  let current: Session | null = initial;
  const listeners = new Set<(event: AuthEvent) => void>();
  const client = {
    getSession: vi.fn(async (_options?: { forceRefresh?: boolean }): Promise<Session | null> => {
      void _options;
      return current;
    }),
    signIn: vi.fn(async () => undefined),
    signOut: vi.fn(async () => {
      current = null;
    }),
    subscribe: vi.fn((listener: (event: AuthEvent) => void) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    }),
  } satisfies AuthClient;
  return {
    client,
    setSession(next: Session | null) {
      current = next;
    },
    emit(event: AuthEvent) {
      listeners.forEach((listener) => listener(event));
    },
  };
}

export function json(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}
