/**
 * Workspace auth wiring shared by proxy.ts (the primary gate) and the
 * workspace layout (the fallback check): the session cookie name and the
 * request header through which the proxy hands the ORIGINAL in-site path to
 * server components, so even the fallback login redirect can send the user
 * back to exactly where they were (same `next=` contract as the 401 path
 * in lib/api.ts).
 */
export const SHEN_SESSION_COOKIE = "shen_session";

export const SHEN_ORIGINAL_PATH_HEADER = "x-shen-original-path";
