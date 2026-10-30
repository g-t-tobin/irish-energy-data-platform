import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { createApiClient } from "../api/client";
import { createAmplifyClient } from "./amplifyClient";
import type { AuthConfig, AuthConfigResult } from "./config";
import { AuthContext, type AuthState } from "./authContext";
import { roleFromGroups, type AuthClient } from "./types";

const SESSION_EXPIRED = "Your session expired. Sign in again.";
const SIGN_IN_FAILED = "Sign-in failed. Try again.";
const SIGN_IN_INCOMPLETE = "Sign-in did not complete. Try again.";
const CALLBACK_TIMEOUT_MS = 15_000;

type Props = {
  config: AuthConfigResult;
  apiBaseUrl: string;
  createClient?: (config: AuthConfig) => AuthClient;
  children: ReactNode;
};

export function AuthProvider({
  config,
  apiBaseUrl,
  createClient = createAmplifyClient,
  children,
}: Props) {
  // Never created without valid configuration, so there is nothing to bypass.
  const [client] = useState<AuthClient | null>(() =>
    config.ok ? createClient(config.config) : null,
  );
  const [state, setState] = useState<AuthState>(() =>
    config.ok ? { status: "loading" } : { status: "unconfigured", problems: config.problems },
  );
  const [returningFromHostedUi] = useState(() =>
    /[?&](code|error)=/.test(window.location.search),
  );

  useEffect(() => {
    if (!client) return;
    let cancelled = false;

    const refresh = async (notice?: string) => {
      const session = await client.getSession();
      if (cancelled) return;
      setState(
        session
          ? {
              status: "signedIn",
              user: { username: session.username, role: roleFromGroups(session.groups) },
            }
          : { status: "signedOut", notice },
      );
    };

    const unsubscribe = client.subscribe((event) => {
      if (cancelled) return;
      if (event.type === "signedIn") void refresh();
      else if (event.type === "signedOut") setState({ status: "signedOut" });
      else if (event.type === "signInFailed") setState({ status: "signedOut", notice: SIGN_IN_FAILED });
      else setState({ status: "signedOut", notice: SESSION_EXPIRED });
    });

    // After the hosted-UI redirect the code exchange finishes asynchronously
    // and raises an event; the timer is only a fallback.
    const timer = returningFromHostedUi
      ? window.setTimeout(() => void refresh(SIGN_IN_INCOMPLETE), CALLBACK_TIMEOUT_MS)
      : undefined;
    if (!returningFromHostedUi) void refresh();

    return () => {
      cancelled = true;
      unsubscribe();
      window.clearTimeout(timer);
    };
  }, [client, returningFromHostedUi]);

  const api = useMemo(
    () =>
      client
        ? createApiClient({
            baseUrl: apiBaseUrl,
            auth: client,
            onSessionExpired: () => setState({ status: "signedOut", notice: SESSION_EXPIRED }),
          })
        : null,
    [client, apiBaseUrl],
  );

  const signIn = useCallback(() => {
    client?.signIn().catch(() => {
      setState({ status: "signedOut", notice: "Unable to start sign-in. Try again." });
    });
  }, [client]);

  const signOut = useCallback(() => {
    void (async () => {
      try {
        await client?.signOut();
      } catch {
        // Local state is cleared either way; the error may carry token data.
      }
      setState({ status: "signedOut" });
    })();
  }, [client]);

  const value = useMemo(
    () => ({ state, api, signIn, signOut }),
    [state, api, signIn, signOut],
  );
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}
