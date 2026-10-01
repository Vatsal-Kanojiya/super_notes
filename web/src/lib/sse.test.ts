import { describe, expect, it } from 'vitest'
import { SseParser } from './sse'

describe('SseParser', () => {
  it('parses one event', () => {
    expect(new SseParser().feed('event: delta\ndata: {"a":1}\n\n')).toEqual([{ event: 'delta', data: '{"a":1}' }])
  })

  it('handles chunks split anywhere, even mid-field', () => {
    const wire = 'event: delta\ndata: {"text":"hi"}\n\nevent: done\ndata: {}\n\n'
    for (let cut = 1; cut < wire.length; cut++) {
      const p = new SseParser()
      const got = [...p.feed(wire.slice(0, cut)), ...p.feed(wire.slice(cut))]
      expect(got.map((e) => e.event)).toEqual(['delta', 'done'])
    }
  })

  it('joins multi-line data with a newline', () => {
    expect(new SseParser().feed('data: a\ndata: b\ndata\n\n')).toEqual([{ event: 'message', data: 'a\nb\n' }])
  })

  it('ignores comments and heartbeats, which end no event', () => {
    const p = new SseParser()
    expect(p.feed(': keep-alive\n\n')).toEqual([])
    expect(p.feed(': hi\nevent: x\ndata: 1\n\n')).toEqual([{ event: 'x', data: '1' }])
  })

  it('accepts CRLF and CR line ends, including a CRLF split across chunks', () => {
    const p = new SseParser()
    expect(p.feed('data: 1\r\n\r\ndata: 2\r')).toEqual([{ event: 'message', data: '1' }])
    expect(p.feed('\n\r\ndata: 3\r\r')).toEqual([{ event: 'message', data: '2' }, { event: 'message', data: '3' }])
  })

  it('keeps one leading space only and ignores id and retry', () => {
    expect(new SseParser().feed('id: 7\nretry: 5\ndata:  x\n\n')).toEqual([{ event: 'message', data: ' x' }])
  })

  it('does not emit an unfinished event', () => {
    expect(new SseParser().feed('data: 1\n')).toEqual([])
  })
})
