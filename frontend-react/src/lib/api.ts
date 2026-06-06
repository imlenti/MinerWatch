// Centralised wrapper around fetch for every /api/* call.
//
// Three reasons this lives in one place instead of being inlined into
// each component:
//   1. Auth — when /api/auth/status reports `enabled: true`, an
//      Authorization Bearer or the mw_token cookie is required. The
//      cookie is set automatically by the login form, so credentials
//      "just work" if you've signed in. This wrapper makes sure every
//      call includes them (same-origin) without each caller remembering.
//   2. Error shape — FastAPI returns `{ "detail": "..." }` on 4xx/5xx.
//      We unwrap it and throw a typed Error so React components don't
//      need to .then().catch() the shape themselves.
//   3. JSON conv — POST bodies go in as plain JS objects; we serialise
//      them. GETs that return 204 (e.g. push subscribe) resolve to null
//      rather than throwing on .json().

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = 'ApiError';
  }
}

export interface ApiOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE' | 'PATCH';
  body?: unknown;
  signal?: AbortSignal;
}

// When auth is enabled and a protected call comes back 401 (not signed in,
// or the session expired), bounce the user to the login page instead of
// leaving the UI stuck on silent failures. A module-level guard collapses a
// burst of concurrent 401s (the dashboard fires several queries at once)
// into a single redirect, and we never redirect away from /login itself so a
// wrong-password attempt keeps showing its error inline.
let redirectingToLogin = false;

function redirectToLogin(): void {
  if (redirectingToLogin) return;
  if (window.location.pathname.startsWith('/login')) return;
  redirectingToLogin = true;
  const here = window.location.pathname + window.location.search;
  window.location.href = `/login?next=${encodeURIComponent(here)}`;
}

export async function api<T = unknown>(
  path: string,
  opts: ApiOptions = {},
): Promise<T> {
  const headers: HeadersInit = { 'Content-Type': 'application/json' };
  const init: RequestInit = {
    method: opts.method ?? 'GET',
    headers,
    credentials: 'same-origin',
    signal: opts.signal,
  };
  if (opts.body !== undefined) {
    init.body = JSON.stringify(opts.body);
  }
  const resp = await fetch(path, init);
  if (!resp.ok) {
    if (resp.status === 401) {
      // Auth is on and we're not signed in — send the user to /login,
      // which returns here afterwards via the `next` param.
      redirectToLogin();
    }
    let detail = `${resp.status}`;
    try {
      const j = await resp.json();
      detail = j.detail ?? detail;
    } catch {
      /* response body wasn't JSON — keep the status code as message */
    }
    throw new ApiError(resp.status, detail);
  }
  if (resp.status === 204) {
    return null as unknown as T;
  }
  return (await resp.json()) as T;
}
