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
- **Static files in production** — no WhiteNoise (D10). Needs a deployment decision.
- **413 without CORS headers** — `MaxUploadSizeMiddleware` runs before `CorsMiddleware`, so a
  cross-origin client sees a network error rather than "too large". Same known gap as the
  reference's roadmap §2.
- **Eval: per-kind breakdown and noise** — `eval_retrieval` should print recall/MRR per question
  kind (that is where vector vs keyword differ), and with 28 answerable questions one question is
  ~3.6 points, so differences under ~5 points should not drive decisions. Waiting on search.
- **Eval: relevance-floor check** — the three `no_answer` questions should report the top score
  against the Ask floor once that floor exists (Phase 5).
