import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { streamOf } from "../test/fixtures";
import { ApiError, fetchConfig, startRun } from "./api";
import type { RunRequest } from "./types";

const request: RunRequest = {
  incident: { title: "t", severity: "sev3", description: "d" },
  simulate_failures: [],
};
const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  vi.unstubAllGlobals();
});

function headersOfLastCall(): Headers {
  return new Headers(fetchMock.mock.calls.at(-1)?.[1]?.headers);
}

describe("startRun", () => {
  it("posts the request and yields the stream's events", async () => {
    const body = streamOf('event: synthesis_started\ndata: {"type":"synthesis_started"}\n\n');
    fetchMock.mockResolvedValue(new Response(body, { status: 200 }));

    const events = [];
    for await (const event of await startRun(request)) events.push(event);

    expect(events).toEqual([{ type: "synthesis_started" }]);
    const [url, init] = fetchMock.mock.calls[0] ?? [];
    expect(url).toBe("/api/runs");
    expect(init?.method).toBe("POST");
    expect(JSON.parse(init?.body as string)).toEqual(request);
  });

  it("sends the access token in its own header, never in Authorization", async () => {
    fetchMock.mockResolvedValue(new Response(streamOf(""), { status: 200 }));
    await startRun(request, { accessToken: "tok" });
    expect(headersOfLastCall().get("x-access-token")).toBe("tok");
    expect(headersOfLastCall().get("authorization")).toBeNull();
  });

  it("omits the token header when there is no token", async () => {
    fetchMock.mockResolvedValue(new Response(streamOf(""), { status: 200 }));
    await startRun(request);
    expect(headersOfLastCall().has("x-access-token")).toBe(false);
  });

  it("signs the body hash that CloudFront needs to sign a POST for Lambda", async () => {
    fetchMock.mockResolvedValue(new Response(streamOf(""), { status: 200 }));
    await startRun(request);
    const body = fetchMock.mock.calls[0]?.[1]?.body as string;
    const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(body));
    const expected = [...new Uint8Array(digest)]
      .map((byte) => byte.toString(16).padStart(2, "0"))
      .join("");
    expect(headersOfLastCall().get("x-amz-content-sha256")).toBe(expected);
  });

  it.each([
    [401, "access token"],
    [429, "Too many"],
    [422, "check the incident"],
    [500, "500"],
  ])("explains a %i response", async (status, message) => {
    const detail = status === 429 ? { detail: "Too many runs in progress." } : {};
    fetchMock.mockResolvedValue(new Response(JSON.stringify(detail), { status }));
    const attempt = startRun(request);
    await expect(attempt).rejects.toBeInstanceOf(ApiError);
    await expect(attempt).rejects.toThrow(message);
  });

  it("passes an abort signal through to fetch", async () => {
    fetchMock.mockResolvedValue(new Response(streamOf(""), { status: 200 }));
    const controller = new AbortController();
    await startRun(request, { signal: controller.signal });
    expect(fetchMock.mock.calls[0]?.[1]?.signal).toBe(controller.signal);
  });
});

describe("fetchConfig", () => {
  it("raises when the API is unavailable", async () => {
    fetchMock.mockResolvedValue(new Response("bad gateway", { status: 502 }));
    await expect(fetchConfig()).rejects.toBeInstanceOf(ApiError);
  });
});
