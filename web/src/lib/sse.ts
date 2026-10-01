/**
 * An incremental parser for server-sent events (the WHATWG wire format), apart
 * from the network so it is tested on its own. Feed it text as it arrives, in
 * any split; it returns the events completed so far. Comment lines (`: ...`,
 * the server's keep-alive), `id` and `retry` fields are dropped.
 */
export interface SseEvent {
  /** The `event:` name; "message" when the event has none. */
  event: string
  /** The `data:` lines joined with a newline. */
  data: string
}

export class SseParser {
  private buffer = ''
  private event = ''
  private data: string[] = []
  private hasData = false
  private skipLf = false

  feed(chunk: string): SseEvent[] {
    if (this.skipLf && chunk.startsWith('\n')) chunk = chunk.slice(1)
    if (chunk) this.skipLf = false
    this.buffer += chunk
    const out: SseEvent[] = []
    // Lines end in \r\n, \n or \r. A trailing \r may be half of a \r\n: wait for the next chunk.
    for (;;) {
      const m = /\r\n|\n|\r(?!$)/.exec(this.buffer)
      if (!m) {
        if (this.buffer.endsWith('\r')) {
          this.line(this.buffer.slice(0, -1), out)
          this.buffer = ''
          this.skipLf = true
        }
        break
      }
      const line = this.buffer.slice(0, m.index)
      this.buffer = this.buffer.slice(m.index + m[0].length)
      this.line(line, out)
    }
    return out
  }

  private line(line: string, out: SseEvent[]): void {
    if (line === '') {
      if (this.hasData) out.push({ event: this.event || 'message', data: this.data.join('\n') })
      this.event = ''
      this.data = []
      this.hasData = false
      return
    }
    if (line.startsWith(':')) return
    const colon = line.indexOf(':')
    const field = colon < 0 ? line : line.slice(0, colon)
    let value = colon < 0 ? '' : line.slice(colon + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') this.event = value
    else if (field === 'data') {
      this.data.push(value)
      this.hasData = true
    }
  }
}
