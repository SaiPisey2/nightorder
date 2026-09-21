// Same-origin API client. Project API key travels in X-API-Key.

export type Session = { project: string; key: string; actor: string };

export function loadSession(): Session | null {
  const raw = localStorage.getItem("bosun.session");
  return raw ? JSON.parse(raw) : null;
}

export function saveSession(s: Session | null) {
  if (s) localStorage.setItem("bosun.session", JSON.stringify(s));
  else localStorage.removeItem("bosun.session");
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request(path: string, opts: RequestInit = {}, key?: string): Promise<any> {
  const headers: Record<string, string> = { ...(opts.headers as any) };
  if (key) headers["X-API-Key"] = key;
  const resp = await fetch(path, { ...opts, headers });
  const text = await resp.text();
  let body: any = text;
  try {
    body = text ? JSON.parse(text) : null;
  } catch {
    /* non-JSON (e.g. gate-action html) */
  }
  if (!resp.ok) {
    const detail = body && body.detail ? JSON.stringify(body.detail) : text;
    throw new ApiError(resp.status, detail || resp.statusText);
  }
  return body;
}

export const api = {
  get: (path: string, key?: string) => request(path, {}, key),
  del: (path: string, key?: string) => request(path, { method: "DELETE" }, key),
  post: (path: string, body: any, key?: string) =>
    request(path, { method: "POST", body: JSON.stringify(body), headers: { "content-type": "application/json" } }, key),
  postYaml: (path: string, yaml: string, key?: string) =>
    request(path, { method: "POST", body: yaml, headers: { "content-type": "application/yaml" } }, key),
};
