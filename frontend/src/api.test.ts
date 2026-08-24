import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, askQuestion, getSchema } from "./api";

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("api", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn());
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("askQuestion posts the question and returns the parsed response", async () => {
    const payload = {
      sql: "SELECT 1",
      explanation: "trivial",
      columns: ["?column?"],
      rows: [[1]],
      row_count: 1,
      truncated: false,
      tables: [],
      repaired: false,
      timings: [{ stage: "llm", ms: 12.3 }],
      usage: { prompt_tokens: 10, completion_tokens: 5, latency_ms: 100 },
      error: null,
      request_id: "req-1",
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(payload));
    vi.stubGlobal("fetch", fetchMock);

    const result = await askQuestion("how many rows?");

    expect(result).toEqual(payload);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/v1/query");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string)).toEqual({
      question: "how many rows?",
    });
  });

  it("askQuestion throws ApiError with the parsed detail on a non-2xx response", async () => {
    const fetchMock = vi
      .fn()
      .mockResolvedValue(jsonResponse({ detail: "schema not loaded" }, 503));
    vi.stubGlobal("fetch", fetchMock);

    await expect(askQuestion("anything")).rejects.toMatchObject({
      name: "ApiError",
      status: 503,
      detail: "schema not loaded",
    });
  });

  it("askQuestion throws an ApiError instance", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn().mockResolvedValue(jsonResponse({ detail: "bad" }, 422)),
    );

    await expect(askQuestion("x")).rejects.toBeInstanceOf(ApiError);
  });

  it("getSchema returns the parsed schema", async () => {
    const payload = {
      version: "v1",
      tables: [
        {
          name: "orders",
          comment: null,
          columns: [
            {
              name: "id",
              type: "integer",
              nullable: false,
              comment: null,
              is_pk: true,
            },
          ],
        },
      ],
    };
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse(payload));
    vi.stubGlobal("fetch", fetchMock);

    const result = await getSchema();

    expect(result).toEqual(payload);
    const [url] = fetchMock.mock.calls[0] as [string];
    expect(url).toBe("/api/v1/schema");
  });
});
