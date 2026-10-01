# Brief — `v2-feat/2-streaming`: streaming answers (Phase 2)

**Queue:** item 6. Decisions D360–D399.

## Read
- `CLAUDE.md`; `docs/V2_PLAN.md` §5 Phase 2; `docs/DECISIONS.md` D53–D57 (providers), D76, D92
  (`uvicorn` approved), D140–D146, D220–D227, D280–D287 (conversations).
- `assistant/chat/` (Protocol, adapters, fake), `assistant/tasks.py`, `assistant/conversation.py`,
  `assistant/api.py`, `assistant/citations.py`, `config/asgi.py`, `config/settings.py`
  (Redis/Celery), `web/src/stores/chat.ts`, `web/src/lib/thread.ts`, `web/src/components/ChatThread.vue`.

## Design anchors
- Provider Protocol gains an optional `stream(system, user, model, max_output_tokens)` yielding
  text deltas and finally a `ChatResult`; Claude, OpenAI and Gemini over `requests` with
  `stream=True`; `fake` yields word by word; a provider without it falls back to `complete`.
- The answer task publishes `delta` events on Redis channel `ask:<id>` and saves the partial
  text on the row about every 0.5 s; a final `done` (with parsed citations) or `failed` event.
  Polling (V1) keeps working unchanged.
- `GET ask/<id>/stream/`: an async view under ASGI (`uvicorn`), bearer auth, owner-scoped (404),
  catch-up from the row first, then relays events as `text/event-stream`, heartbeat comment
  every 15 s, hard duration cap. `runserver` keeps working for everything else.
- Web: `fetch` with a streaming body reader (not `EventSource`), falls back to polling on any
  error; markers render as they stream, chips become clickable on `done`.

## Sub-tasks (conductor's checklist)
- [x] 1. **Provider streaming + worker publishing** (Opus) — Protocol `stream`, the three
  adapters' SSE parsing (tested against recorded vendor streams, no network), fake, fallback;
  the task streams, publishes deltas/done/failed, throttled partial saves, retries and the
  limits/refund paths unchanged; tests.
- [x] 2. **ASGI stream endpoint** (Opus) — `uvicorn` (approved D92; conductor adds it),
  `config/asgi.py`, the async view (auth, ownership, catch-up, relay, heartbeat, cap, client
  disconnect), README run instructions; tests incl. another user → 404 and a reconnect resuming
  from partial text.
- [ ] 3. **Web streaming** (Sonnet) — stream reader in the chat thread with polling fallback;
  vitest for the SSE parser and state merge; headless check against uvicorn.

Done when the branch checklist in `CLAUDE.md` passes; BUILD_LOG entry "V2 2 — streaming".
