import { createHash } from "node:crypto";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { streamOf } from "../test/fixtures";
import { ApiError, fetchConfig, sha256Hex, startRun } from "./api";
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

describe("startRun errors", () => {
  it("shows the server's explanation for a 400", async () => {
    fetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({ detail: "Simulated failures need LLM_MODE=fake." }),
        {
          status: 400,
        },
      ),
    );
    await expect(startRun(request)).rejects.toThrow(
      "Simulated failures need LLM_MODE=fake.",
    );
  });
});

describe("sha256Hex", () => {
  const vectors: [string, string][] = [
    ["", "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"],
    ["abc", "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"],
    [
      "abcdbcdecdefdefgefghfghighijhijkijkljklmklmnlmnomnopnopq",
      "248d6a61d20638b8e5c026930c3e6039a33ce45964ff2167f6ecedd419db06c1",
    ],
    [
      "a".repeat(1_000),
      "41edece42d63e8d9bf515a9ba6932e1c20cbc9f5a5d134645adb5db1b9737ea3",
    ],
    [
      "héllo wörld ✓ 日本語 🚀",
      "74ee4ee1973a9f0d651d9b77b77ae6c5342d2f4a0488df5e902f3e006341482a",
    ],
  ];

  it.each(vectors)(
    "hashes %j correctly without crypto.subtle",
    async (text, expected) => {
      vi.stubGlobal("crypto", {});
      expect(await sha256Hex(text)).toBe(expected);
    },
  );

  it.each(vectors)(
    "gives the same digest with and without crypto.subtle for %j",
    async (text) => {
      const withSubtle = await sha256Hex(text);
      vi.stubGlobal("crypto", {});
      expect(await sha256Hex(text)).toBe(withSubtle);
    },
  );
});

describe("startRun without crypto.subtle (plain HTTP on a LAN address)", () => {
  it("still sends the correct x-amz-content-sha256 header", async () => {
    vi.stubGlobal("crypto", {});
    fetchMock.mockResolvedValue(new Response(streamOf(""), { status: 200 }));
    await startRun(request);
    const expected = createHash("sha256")
      .update(JSON.stringify(request))
      .digest("hex");
    expect(headersOfLastCall().get("x-amz-content-sha256")).toBe(expected);
  });
});

describe("startRun", () => {
  it("posts the request and yields the stream's events", async () => {
    const body = streamOf(
      'event: synthesis_started\ndata: {"type":"synthesis_started"}\n\n',
    );
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
    const digest = await crypto.subtle.digest(
      "SHA-256",
      new TextEncoder().encode(body),
    );
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
    const detail =
      status === 429 ? { detail: "Too many runs in progress." } : {};
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify(detail), { status }),
    );
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
