// Production builds are validated at build time (vite.config.ts); this also stops a production
// bundle that somehow lacks the setting from silently calling localhost.
const configured = import.meta.env.VITE_API_BASE_URL;
if (import.meta.env.MODE === "production" && !configured) {
  throw new Error("VITE_API_BASE_URL is required in production builds");
}

export const API_BASE_URL = (configured ?? "http://127.0.0.1:8001").replace(/\/+$/, "");
