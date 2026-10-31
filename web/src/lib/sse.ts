/**
 * Parses a server-sent event stream from a fetch body.
 *
 * EventSource can't be used here, because it only supports GET and a run starts with a POST.
 * Yields each frame's parsed `data`. Comment frames, which the API uses as heartbeats, are skipped.
 */
export async function* parseSse(stream: ReadableStream<Uint8Array>): AsyncGenerator<unknown> {
  const reader = stream.getReader();
  // Stream mode holds back a multi-byte character split across two chunks.
  const decoder = new TextDecoder();
  let buffer = "";
  // A "\r" ending a chunk may be the first half of a CRLF, so it waits for the next chunk.
  let heldCr = "";
  function* drain(): Generator<unknown> {
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const data = dataOf(buffer.slice(0, boundary));
      buffer = buffer.slice(boundary + 2);
      if (data !== null) yield JSON.parse(data);
      boundary = buffer.indexOf("\n\n");
    }
  }
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) {
        // A CR that ended the stream was a whole line ending, not half of a CRLF.
        buffer += heldCr.replace("\r", "\n");
        yield* drain();
        return;
      }
      // The SSE spec allows CRLF line endings; a proxy may use them.
      let text = heldCr + decoder.decode(value, { stream: true });
      heldCr = text.endsWith("\r") ? "\r" : "";
      if (heldCr) text = text.slice(0, -1);
      buffer += text.replace(/\r\n?/g, "\n");

      yield* drain();
    }
  } finally {
    // Runs when the consumer stops early too, which is what closes the connection and stops the run.
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}

function dataOf(frame: string): string | null {
  const lines = frame
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart());
  return lines.length > 0 ? lines.join("\n") : null;
}
