import { createContext } from "react";
import type { ApiClient } from "../api/client";
import type { Role } from "./types";

export type AuthState =
  | { status: "unconfigured"; problems: string[] }
  | { status: "loading" }
  | { status: "signedOut"; notice?: string }
  | { status: "signedIn"; user: { username: string; role: Role } };

export type AuthContextValue = {
  state: AuthState;
  api: ApiClient | null;
  signIn: () => void;
  signOut: () => void;
};

export const AuthContext = createContext<AuthContextValue | null>(null);
