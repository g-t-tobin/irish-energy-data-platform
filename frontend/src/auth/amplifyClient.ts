import { Amplify } from "aws-amplify";
import "aws-amplify/auth/enable-oauth-listener";
import { fetchAuthSession, signInWithRedirect, signOut } from "aws-amplify/auth";
import { cognitoUserPoolsTokenProvider } from "aws-amplify/auth/cognito";
import { Hub } from "aws-amplify/utils";
import type { AuthConfig } from "./config";
import type { AuthClient, AuthEvent, Session } from "./types";

// Tokens live in memory only; Amplify's default would be localStorage.
class MemoryStorage {
  private items = new Map<string, string>();
  async setItem(key: string, value: string) {
    this.items.set(key, value);
  }
  async getItem(key: string) {
    return this.items.get(key) ?? null;
  }
  async removeItem(key: string) {
    this.items.delete(key);
  }
  async clear() {
    this.items.clear();
  }
}

const memoryStorage = new MemoryStorage();

const EVENT_MAP: Record<string, AuthEvent["type"] | undefined> = {
  signedIn: "signedIn",
  signedOut: "signedOut",
  signInWithRedirect_failure: "signInFailed",
  tokenRefresh_failure: "tokenRefreshFailed",
};

export function createAmplifyClient(config: AuthConfig): AuthClient {
  Amplify.configure({
    Auth: {
      Cognito: {
        userPoolId: config.userPoolId,
        userPoolClientId: config.clientId,
        loginWith: {
          oauth: {
            domain: config.domain,
            scopes: config.scopes,
            redirectSignIn: [config.redirectSignIn],
            redirectSignOut: [config.redirectSignOut],
            responseType: "code",
          },
        },
      },
    },
  });
  // configure() resets the token store to localStorage, so swap it afterwards.
  cognitoUserPoolsTokenProvider.setKeyValueStorage(memoryStorage);

  return {
    async getSession(options) {
      try {
        const { tokens } = await fetchAuthSession({
          forceRefresh: options?.forceRefresh ?? false,
        });
        const access = tokens?.accessToken;
        if (!access) return null;
        const claims = access.payload as Record<string, unknown>;
        const rawGroups = claims["cognito:groups"];
        const groups = Array.isArray(rawGroups)
          ? rawGroups.filter((g): g is string => typeof g === "string")
          : [];
        const session: Session = {
          accessToken: access.toString(),
          username: String(claims["username"] ?? claims["sub"] ?? ""),
          groups,
        };
        return session;
      } catch {
        // Details can include token material; callers only need "no session".
        return null;
      }
    },
    async signIn() {
      await signInWithRedirect();
    },
    async signOut() {
      await signOut();
    },
    subscribe(listener) {
      return Hub.listen("auth", ({ payload }) => {
        const type = EVENT_MAP[payload.event];
        if (type) listener({ type });
      });
    },
  };
}
