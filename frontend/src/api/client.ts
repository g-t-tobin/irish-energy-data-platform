import type { AuthClient } from "../auth/types";

export type ApiErrorKind =
  | "unauthenticated"
  | "forbidden"
  | "rate_limited"
  | "unavailable"
  | "network"
  | "invalid_response"
  | "http";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status?: number;
  readonly retryAfterSeconds?: number;

  constructor(kind: ApiErrorKind, status?: number, retryAfterSeconds?: number) {
    super(kind);
    this.kind = kind;
    this.status = status;
    this.retryAfterSeconds = retryAfterSeconds;
  }
}

export type ApiClient = {
  getJson(path: string, options?: { signal?: AbortSignal }): Promise<unknown>;
};

type Options = {
  baseUrl: string;
  auth: AuthClient;
  onSessionExpired: () => void;
};

// Read-only by design: this client has no way to send anything but GET.
export function createApiClient({ baseUrl, auth, onSessionExpired }: Options): ApiClient {
  async function send(path: string, forceRefresh: boolean, signal?: AbortSignal) {
    const session = await auth.getSession({ forceRefresh });
    if (!session) return null;
    return fetch(`${baseUrl}${path}`, {
      method: "GET",
      headers: {
        Accept: "application/json",
        // Always the access token; an ID token is never available here.
        Authorization: `Bearer ${session.accessToken}`,
      },
      signal,
    });
  }

  async function attempt(path: string, forceRefresh: boolean, signal?: AbortSignal) {
    try {
      return await send(path, forceRefresh, signal);
    } catch (error) {
      if (signal?.aborted) throw error;
      throw new ApiError("network");
    }
  }

  return {
    async getJson(path, options) {
      const signal = options?.signal;
      let response = await attempt(path, false, signal);
      if (response?.status === 401) {
        // One silent refresh, then give up and ask the user to sign in again.
        response = await attempt(path, true, signal);
      }
      if (!response || response.status === 401) {
        onSessionExpired();
        throw new ApiError("unauthenticated", 401);
      }
      if (response.status === 403) throw new ApiError("forbidden", 403);
      if (response.status === 429) {
        const retry = Number.parseInt(response.headers.get("Retry-After") ?? "", 10);
        throw new ApiError("rate_limited", 429, Number.isFinite(retry) ? retry : undefined);
      }
      if (response.status === 503) throw new ApiError("unavailable", 503);
      if (!response.ok) throw new ApiError("http", response.status);
      try {
        return (await response.json()) as unknown;
      } catch {
        throw new ApiError("invalid_response", response.status);
      }
    },
  };
}

/** User-facing text. Never includes server detail, tokens or URLs. */
export function describeApiError(error: unknown): string {
  if (!(error instanceof ApiError)) return "Something went wrong. Try again.";
  switch (error.kind) {
    case "unauthenticated":
      return "Your session has expired. Sign in again.";
    case "forbidden":
      return "Your account does not have access to this data. Ask an administrator to add you to the viewer group.";
    case "rate_limited":
      return error.retryAfterSeconds
        ? `Too many requests. Try again in ${error.retryAfterSeconds} seconds.`
        : "Too many requests. Try again shortly.";
    case "unavailable":
      return "The service is temporarily unavailable. Try again later.";
    case "network":
      return "Unable to reach the API.";
    case "invalid_response":
      return "The API returned an unexpected response.";
    default:
      return `The API returned an unexpected error (HTTP ${error.status ?? "unknown"}).`;
  }
}
