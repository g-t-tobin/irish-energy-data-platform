export type Role = "operator" | "viewer" | null;

/** Access-token session. The token is only ever handed to the API client. */
export type Session = {
  accessToken: string;
  username: string;
  groups: string[];
};

export type AuthEvent =
  | { type: "signedIn" }
  | { type: "signedOut" }
  | { type: "signInFailed" }
  | { type: "tokenRefreshFailed" };

export interface AuthClient {
  getSession(options?: { forceRefresh?: boolean }): Promise<Session | null>;
  signIn(): Promise<void>;
  signOut(): Promise<void>;
  subscribe(listener: (event: AuthEvent) => void): () => void;
}

// Presentation only: the API authorizes from the verified token, not from this.
export function roleFromGroups(groups: string[]): Role {
  if (groups.includes("operator")) return "operator";
  if (groups.includes("viewer")) return "viewer";
  return null;
}
