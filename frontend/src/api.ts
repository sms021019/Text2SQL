/** Typed client for the Text2SQL backend's `/api/v1` endpoints.
 *
 * Mirrors `backend/app/api/v1/query.py` and `schema.py`'s pydantic
 * response models. `askQuestion` never throws on a pipeline error --
 * that surfaces as a non-null `error` field on a 200 response, per
 * `Text2SQLPipeline.run`'s contract -- but it does throw `ApiError` for
 * a non-2xx HTTP status (422 validation, 503 not-ready, etc).
 */

const BASE = import.meta.env.VITE_API_URL ?? "/api";

export interface Timing {
  stage: string;
  ms: number;
}

export interface Usage {
  prompt_tokens: number;
  completion_tokens: number;
  latency_ms: number;
}

export interface QueryResponse {
  sql: string;
  explanation: string;
  columns: string[];
  rows: unknown[][];
  row_count: number;
  truncated: boolean;
  tables: string[];
  repaired: boolean;
  timings: Timing[];
  usage: Usage;
  error: string | null;
  request_id: string;
}

export interface SchemaColumn {
  name: string;
  type: string;
  nullable: boolean;
  comment: string | null;
  is_pk: boolean;
}

export interface SchemaTable {
  name: string;
  comment: string | null;
  columns: SchemaColumn[];
}

export interface SchemaResponse {
  version: string;
  tables: SchemaTable[];
}

/** Thrown for any non-2xx response. `detail` is FastAPI's parsed
 * `{detail}` body when present, else the raw response text. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(`API error ${status}: ${detail}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

async function parseErrorDetail(res: Response): Promise<string> {
  const text = await res.text();
  try {
    const body = JSON.parse(text) as { detail?: unknown };
    if (typeof body.detail === "string") return body.detail;
    if (body.detail !== undefined) return JSON.stringify(body.detail);
  } catch {
    // not JSON -- fall through to raw text
  }
  return text || res.statusText;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, init);
  if (!res.ok) {
    throw new ApiError(res.status, await parseErrorDetail(res));
  }
  return (await res.json()) as T;
}

export function askQuestion(
  question: string,
  signal?: AbortSignal,
): Promise<QueryResponse> {
  return request<QueryResponse>("/v1/query", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ question }),
    signal,
  });
}

export function getSchema(signal?: AbortSignal): Promise<SchemaResponse> {
  return request<SchemaResponse>("/v1/schema", { signal });
}
