import { describe, expect, it } from "vitest";
import { streamOf } from "../test/fixtures";
import { parseSse } from "./sse";

async function collect(stream: ReadableStream<Uint8Array>): Promise<unknown[]> {
  const out: unknown[] = [];
  for await (const item of parseSse(stream)) out.push(item);
  return out;
}

describe("parseSse", () => {
  it("parses a complete frame", async () => {
    expect(await collect(streamOf('event: a\ndata: {"x":1}\n\n'))).toEqual([{ x: 1 }]);
  });

  it("parses several frames delivered in one chunk", async () => {
    const body = 'data: {"x":1}\n\ndata: {"x":2}\n\n';
    expect(await collect(streamOf(body))).toEqual([{ x: 1 }, { x: 2 }]);
  });

  it("reassembles a frame split across any chunk boundary", async () => {
    const body = 'event: a\ndata: {"x":1}\n\ndata: {"x":2}\n\n';
    for (let split = 1; split < body.length; split++) {
      const chunks = streamOf(body.slice(0, split), body.slice(split));
      expect(await collect(chunks)).toEqual([{ x: 1 }, { x: 2 }]);
    }
  });

  it("reassembles a multi-byte character split across chunks", async () => {
    const bytes = new TextEncoder().encode('data: {"x":"café"}\n\n');
    const split = bytes.indexOf(0xc3) + 1;
    const chunks = streamOf(bytes.slice(0, split), bytes.slice(split));
    expect(await collect(chunks)).toEqual([{ x: "café" }]);
  });

  it("accepts CRLF line endings", async () => {
    expect(await collect(streamOf('data: {"x":1}\r\n\r\ndata: {"x":2}\r\n\r\n'))).toEqual([
      { x: 1 },
      { x: 2 },
    ]);
  });

  it("does not invent a frame boundary when CRLF is split across chunks", async () => {
    // One frame whose JSON spans two data lines, with the CRLF between them split in two chunks.
    const chunks = streamOf('data: {"x":\r', '\ndata: 1}\r\n\r\n');
    expect(await collect(chunks)).toEqual([{ x: 1 }]);
  });

  it("ignores heartbeat comments", async () => {
    const body = ': heartbeat\n\ndata: {"x":1}\n\n: heartbeat\n\n';
    expect(await collect(streamOf(body))).toEqual([{ x: 1 }]);
  });

  it("drops an unfinished frame when the connection ends", async () => {
    const body = 'data: {"x":1}\n\ndata: {"x":';
    expect(await collect(streamOf(body))).toEqual([{ x: 1 }]);
  });
});
