# Backlog

Out of V1's scope, or parked during the build. Nothing here is started without the owner's go.

## V2 candidates (from the plan)

- **Multi-turn chat with conversation memory** — the long-term goal. V1's Ask is single-turn.
- **Streaming answers** (and websockets). V1 polls.
- **AI "format my note" button.**
- **Reminders** and **calendar**.
- **Uploads and summaries.**
- Sharing, payments (the plan is admin-edited in V1), offline-first storage, desktop packaging.

## Parked during the build

Things that came up and were left for a decision. Each names what it is waiting on.

- **Real embedding and chat providers** — waiting on which keys the owner has (OpenAI, Gemini or
  Claude). The fake providers carry the build until then; the registry takes a new one without
  touching callers.
- **Relevance floor value and score** (D58) — `ASK_RELEVANCE_FLOOR` is 0.0 (only an empty
  retrieval short-circuits). Waiting on Phase 4's `search()` score shape and the eval numbers:
  the floor should compare the vector leg's cosine similarity, not the fused RRF score.
- **Live provider tests run whenever a key is set** — including a key in `.env`, since
  django-environ loads it into the environment. Each costs a fraction of a cent, but it breaks
  the spirit of D11. Waiting on the owner: add a `RUN_LIVE_TESTS=1` gate on top of the key?
- **Localised no-answer text** — `ASK_NO_ANSWER_TEXT` is fixed English while model answers follow
  the question's language. V1 accepts it.
- **Refusal fallbacks for Claude 5.5-class models** — if `CHAT_CLAUDE_MODEL` moves to Sonnet or
  Opus 5.5, the claude-api skill recommends the server-side `fallbacks` parameter; not sent today
  (default is Haiku 4.5).

- **Choose the embedding provider** — OpenAI and Gemini are both implemented (D37) and neither has
  been run against a real key. Waiting on the owner's choice and key: run
  `LIVE_PROVIDER_TESTS=1 python manage.py test retrieval.tests.test_embeddings`, set
  `EMBEDDING_PROVIDER`, then consider deleting the unused provider.
- **Real chat provider** — waiting on which keys the owner has (OpenAI, Gemini or Claude). The fake
  carries the build until then; the registry takes a new one without touching callers.
- **Sentence splitting on abbreviations** — the chunker's splitter breaks after "e.g." and "Dr.".
  Only affects blocks longer than `CHUNK_MAX_CHARS` and overlap; revisit if the eval shows it.
- **Static files in production** — no WhiteNoise (D10). Needs a deployment decision.
- **413 without CORS headers** — `MaxUploadSizeMiddleware` runs before `CorsMiddleware`, so a
  cross-origin client sees a network error rather than "too large". Same known gap as the
  reference's roadmap §2.
- **Eval: per-kind breakdown and noise** — `eval_retrieval` should print recall/MRR per question
  kind (that is where vector vs keyword differ), and with 28 answerable questions one question is
  ~3.6 points, so differences under ~5 points should not drive decisions. Waiting on search.
- **Eval: relevance-floor check** — the three `no_answer` questions should report the top score
  against the Ask floor once that floor exists (Phase 5).

- **Deeply nested JSON bodies are a 500** — `json.loads` raises `RecursionError` (not a
  `ValueError`) on a body nested a few thousand levels deep, which DRF's `JSONParser` does not
  catch. Fix: a project `JSONParser` that turns it into a `ParseError` (400), set in
  `DEFAULT_PARSER_CLASSES`. Found in phase 2; platform-wide, so waiting on the owner.
- **Trash / undo for deleted notes** — tombstones keep their content (D28), so a restore endpoint
  is possible; not in V1's scope.
