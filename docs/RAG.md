# Retrieval-augmented answers

How a note becomes something a question can be answered from: chunking, embedding, indexing,
retrieval, asking, and how it is measured. Plan §6. The decisions behind each part are in
[DECISIONS.md](DECISIONS.md) (D33–D40 for the first two sections, D61–D65 for indexing, D66–D71 for retrieval, D41–D45 for the
evaluation).

---

## Chunking

`retrieval/chunking.py` — `chunk_note(title, content) -> list[Chunk]`, pure and deterministic.

A `Chunk` is `(ordinal, text, heading_path, embed_text, content_hash)`:

| Field | What it is |
|---|---|
| `ordinal` | 0, 1, 2… in document order |
| `text` | what a person is shown as the cited excerpt |
| `heading_path` | the headings above it, `"Project > Risks"`; empty before the first heading |
| `embed_text` | what the provider is sent: `"<title> > <heading_path>\n\n<text>"` |
| `content_hash` | sha256 of `embed_text` |

**Walking the tree, not the text.** The TipTap JSON is walked node by node so chunks follow the
note's structure:

- `heading(level)` starts a section. A lower level nests (`Project > Risks`), the same or a higher
  level replaces (`Project > Timeline`, then `Personal`). A chunk never spans two sections. A
  heading with nothing under it (not even a subheading) becomes a chunk of its own words, so a note
  of bare headings is still findable.
- `paragraph`, `codeBlock` (verbatim), `blockquote` (`> ` prefix) become blocks. `hardBreak` is a
  newline.
- `bulletList` / `orderedList` / `taskList`: each item's own words are one block, rendered
  `- item`, `3. item` (honouring `start`) or `[x] item` / `[ ] item` (only a real JSON `true` is
  ticked). A nested list's items are blocks of their own, indented two spaces, so one long sub-list
  cannot make its parent item an enormous unsplittable block.
- Unknown node types (tables, future extensions) are read for whatever text they contain.
  `horizontalRule` and images contribute nothing.

**Packing.** Blocks are packed into a chunk until the next one would take it past
`CHUNK_TARGET_CHARS`. Sizes are in characters, taken as ~4 per token (D33):

| Setting | Default | ≈ tokens | Role |
|---|---|---|---|
| `CHUNK_TARGET_CHARS` | 1600 | 400 | pack up to here |
| `CHUNK_MAX_CHARS` | 2000 | 500 | hard ceiling on any chunk's `text` |
| `CHUNK_OVERLAP_CHARS` | 200 | 50 | carried from one chunk into the next |

- **Items are never cut.** Only a single block longer than `CHUNK_MAX_CHARS` is split — a very long
  paragraph, code block or (rarely) list item — and then on the coarsest boundary that works: lines,
  then sentences (`.`, `!`, `?` followed by whitespace), then words, and only a single "word" longer
  than the target is cut by length. The pieces are at most the target, so they pack like any other
  block.
- **Overlap** repeats the tail of the previous chunk at the start of the next: as many *whole*
  blocks as fit the budget; if not even the last block fits and it is a paragraph, its last whole
  sentences (never the whole paragraph). An item or a code block is carried whole or not at all.
  The budget shrinks so that overlap plus the next block never passes the max; if what would be
  carried is everything the chunk holds, the chunk grows instead of being repeated.
- **No overlap across headings**: the next section's chunks would carry text under the wrong path.

**Prefix and hash (D35).** A chunk that says "move it to Friday" is useless without knowing it is
from *Trip plan > Day 2*, so `embed_text` puts the title and heading path first. The hash covers
`embed_text`, so renaming a note or a heading re-embeds its chunks — the vectors really do change.
The indexer (next) reuses a stored vector only when both `content_hash` and the embedding model id
match.

**Bad content never raises.** A note is whatever a client saved. Non-dict nodes, non-list
`content`, non-dict `attrs`, non-string `text`, a heading level of `"2"` or `99`: each contributes
nothing (or is clamped) and the rest of the note is still chunked. Nesting deeper than 32 levels is
ignored rather than hitting Python's recursion limit. Only inconsistent CHUNK_* settings raise
(`ImproperlyConfigured`), because that is a configuration mistake, not content.

## Embedding providers

`retrieval/embeddings/`, shaped exactly like the reference's `expenses/extraction/`:

```
embeddings/
  __init__.py      embed_texts(texts), embed_query(text), embedding_model_id()
  errors.py        EmbeddingError, EmbeddingTransientError
  registry.py      name -> import path string, resolved lazily
  providers/
    base.py        EmbeddingProvider Protocol: embed(texts, model, dimensions, task)
    _common.py     l2_normalise, post_json (HTTP + error translation)
    fake.py        hashed bag of words — the default
    openai.py      POST /v1/embeddings
    gemini.py      POST models/{model}:batchEmbedContents
```

**The boundary.** `embed_texts` (for chunks) and `embed_query` (for a search) are the only entry
points. They refuse blank input before any call, send `EMBEDDING_BATCH_SIZE` texts (64) per provider
call, and check that each vector is numeric, finite and exactly `EMBEDDING_DIMENSIONS` (1536) long —
a wrong-sized vector would fail at `INSERT` or, worse, be compared with vectors from another space.
`embedding_model_id()` (`openai/text-embedding-3-small@1536`) is stored beside each vector so the
indexer can tell when a stored vector belongs to another model.

**Errors (D38).** Two classes, deliberately unrelated:

| Failure | Raised | Celery |
|---|---|---|
| 400, 401, 403, 404, unknown provider, missing key, malformed response, wrong size | `EmbeddingError` | gives up |
| 429 (except OpenAI's `insufficient_quota`), 5xx, timeout, connection error | `EmbeddingTransientError` | retries with backoff |

Vendor error messages are quoted (truncated) with the API key redacted — OpenAI's 401 echoes the key
it was sent.

**The fake (D39).** A hashed bag of words: each lowercased `\w+` token is hashed (sha256) to one of
`dimensions` slots and a ±1 sign, counts are summed, the vector is L2-normalised. Deterministic across
processes and machines, no network, and texts sharing words have a higher cosine similarity — so the
search and eval smoke tests rank sensibly with no key. It knows nothing of meaning.

**The real providers (D37).** The owner has not chosen between OpenAI and Gemini, so both exist,
over plain `requests` (already a dependency) rather than two SDKs:

| | OpenAI | Gemini |
|---|---|---|
| Model | `text-embedding-3-small` | `gemini-embedding-001` |
| Key | `OPENAI_API_KEY` (Bearer) | `GEMINI_API_KEY` (`x-goog-api-key` header) |
| Size | `dimensions: 1536` | `outputDimensionality: 1536` |
| Query vs passage | same | `taskType` `RETRIEVAL_QUERY` / `RETRIEVAL_DOCUMENT` |
| Normalised | yes | only at 3072, so the provider normalises |
| Per-request limit | 2048 inputs | 100 inputs |

**Dimensions (D36).** 1536 because both models can produce it and pgvector's HNSW index accepts at
most 2000 dimensions. The vector column's width is fixed by a migration, so changing the model or the
size means a migration plus a full re-index (`reindex_notes --all`, next phase). Switching between
OpenAI and Gemini at the same size needs only the re-index: `embedding_model_id` differs, so every
chunk is re-embedded.

**Tests.** `requests.post` is mocked for request shape, ordering, batching, error translation and
dimension mismatch. One live test per provider runs only with the key **and**
`LIVE_PROVIDER_TESTS=1` (D40); it overrides the test runner's forced `fake`.

## Indexing

Every note write (`notes/services.py`, through `_after_write`) enqueues
`index_note_task(note_id, version)` **on commit**, so a worker never looks for an uncommitted row and a
rolled-back write is never indexed.

**Debounce (D61).** The task runs `INDEX_DEBOUNCE_SECONDS` (20) later. Autosave writes every few
seconds; each write enqueues a task for its own version, and all but the last find the note has moved
on and exit before embedding anything. A delete is enqueued with no delay and removes the chunks.

**Idempotency (D63).** The task may run twice (`acks_late`) or overlap with another. `index_note`
loads the note and exits if it is missing, deleted (after removing its chunks) or at another version
than the task's. It then chunks, embeds what is needed with **no lock held**, and writes in one
transaction that first locks the note row and re-checks the version. If the note moved on meanwhile,
nothing is written and the newer task does the work.

**Reuse (D62).** A stored chunk whose `content_hash` and `embedding_model` match a new chunk keeps its
vector; only its ordinal, text, heading path and `note_version` are updated. Only new hashes are
embedded, in one `embed_texts` call that the boundary splits into batches. One changed paragraph
costs one embedding; reordering sections costs none.

**Model change.** `embedding_model_id()` is stored per chunk, so after changing the provider or
model every chunk counts as new and is re-embedded by the next index of its note. Run
`reindex_notes --all` to do that for every note; `index_status` counts chunks from another model. A
change of `EMBEDDING_DIMENSIONS` also needs a migration (D36).

**Failures (D65).** A transient provider error retries with backoff, up to 5 times. A permanent one is
logged and dropped; the note keeps its old chunks and shows up as behind in `index_status`.

**Commands.**

- `reindex_notes --user EMAIL | --all [--sync]` queues an index task per live note (`--sync` indexes
  inline and prints how many succeeded and failed). Safe to repeat: unchanged chunks are reused.
- `index_status` prints live notes, how many have no chunks, chunks behind the note's version or
  from another embedding model, and orphan chunks of deleted notes.

**Keyword index (D64).** `NoteChunk` has a GIN index on `chunk_search_vector()`
(`retrieval/search_vector.py`: heading path and text, `english`), which phase 4's query imports.

## Retrieval

`retrieval/search.py` — `search(user, query, k=None, *, mode="hybrid") -> list[SearchHit]`, and
`GET /api/v1/search/?q=&k=` over it. D66–D71.

```
query ─┬─ embed_query ── vector leg: top 50 by cosine distance (HNSW) ─┐
       │                                                                ├─ RRF ─ cap 2/note ─ top k ─ read texts
       └──────────────── keyword leg: top 50 by ts_rank (GIN) ──────────┘
```

**Two legs, one SQL query each.** Both start from `_live_chunks(user)`: the owner's chunks of
notes with no `deleted_at`, as a `WHERE` clause, never a Python filter afterwards (D67).

- **Vector:** `CosineDistance` against the query's embedding, chunks from the current
  `embedding_model_id()` only, `ORDER BY` the distance alone so pgvector's HNSW index can serve it.
  `hnsw.ef_search` is raised to 200 for the query (`SET LOCAL`): the default 40 is below the 50
  candidates, and pgvector 0.6 applies the owner filter after the index scan (D68). For a user
  whose chunks are a small share of the table Postgres instead scans that user's rows by the owner
  index and sorts them exactly, which is cheaper and loses nothing.
- **Keyword:** `websearch_to_tsquery('english', q)` against `chunk_search_vector()` (heading path
  and text), the expression the GIN index is built on, ranked by `ts_rank`. Tests EXPLAIN both
  queries and check each index is reachable.

**Fusion (D66).** Reciprocal rank fusion: each chunk scores Σ 1/(60 + rank) over the lists it
appears in (rank from 1). A chunk both legs agree on beats one that is first in a single list.
Ties go to the lower chunk id. `fuse` and `cap_per_note` are pure functions over ids.

**Cap, then cut (D71).** Walking the fused list, a note's third and later chunks are dropped; then
the top k (default 8, max 20) are kept, and one more owner-scoped query reads their titles,
heading paths and texts.

**What a hit says.**

| Field | Meaning |
|---|---|
| `chunk_id`, `note_id`, `title`, `heading_path`, `text` | where it is and what it says |
| `score` | the fused RRF score: orders hits, says nothing about relevance on its own |
| `similarity` | 1 − cosine distance from the vector leg; `null` if only keyword search found it |
| `keyword_rank` | `ts_rank` from the keyword leg; `null` if only vector search found it |

The endpoint adds `snippet` (the first 200 characters, cut at a word). `similarity` is what Ask's
relevance floor will compare against (D58, D69).

**Failure (D70).** If the query cannot be embedded (provider down, rate-limited, bad key), hybrid
search logs a warning and answers from the keyword leg alone, every `similarity` `null`.
`mode="vector"` raises instead. An empty or whitespace query returns `[]` without a query.

**Settings.** `SEARCH_CANDIDATES` (50), `SEARCH_RRF_K` (60), `SEARCH_MAX_CHUNKS_PER_NOTE` (2),
`SEARCH_DEFAULT_K` (8), `SEARCH_MAX_K` (20, fixed), `SEARCH_HNSW_EF_SEARCH` (200). The endpoint is
throttled by the `search` scope (`API_SEARCH_THROTTLE`, 120/hour) because each call may cost an
embedding, and refuses a `q` over 500 characters.

## Asking

*Phase 5.*

## Evaluation

Retrieval is measured, not assumed. A fixed set of notes and labelled questions is run through
each retrieval mode, and recall@k and MRR are reported. With the fake embedding provider this is
only a smoke test (its vectors carry no meaning); the numbers that count come from a real
provider and are recorded below. Code: `retrieval/eval/` and the `eval_retrieval` command.

### The fixtures

`retrieval/eval/fixtures/notes.json` holds 30 notes of the kind one person in India actually keeps:
a work project and its weekly syncs, trip plans, recipes, grocery and packing lists, a doctor's
visit and a medicines schedule, rent and subscriptions, investments, book notes, home repairs, a
car service log, gift ideas, birthdays, learning notes on Django and Postgres. No secrets.

Each note is a **TipTap document**, the same JSON the editor saves (StarterKit plus
TaskList/TaskItem), so the eval runs the real chunker over real structure: headings, bullet and
ordered lists, task lists with checked and unchecked items, a blockquote, code blocks, bold,
italic and inline code. Each has a stable `key` (`n01`...`n30`), a `type` (`text` or `checklist`)
and a `title`. Database ids change on every load; the keys do not.

The set is built to be hard in specific ways:

| Trait | Notes | Why |
|---|---|---|
| Near-duplicates | `n01`/`n02` (two Goa trips, December and February); `n04`/`n05` (Atlas syncs, 8 and 15 September); `n06`/`n07` (groceries, two weeks) | Same shape and vocabulary, different facts: tests picking the *right* one |
| Checklists | `n06`, `n07`, `n08`, `n24`, `n28`, `n29` | Answers live in task items; each has open and done items |
| Long, multi-chunk notes | `n03` (~3,900 chars), `n16`, `n20`, `n21` (2,200–2,900) | Answers sit in one section; tests heading paths and the per-note chunk cap |
| One-liners | `n10`, `n23`, `n30` | A short chunk must still be findable |
| Hinglish | `n09` (recipe), `n24` (weekend chores) | Hindi in Latin script; neither stemming nor an English embedding model is built for it |

`retrieval/eval/fixtures/questions.json` holds 31 questions, each
`{id, question, relevant, kind}`, where `relevant` lists the note keys that answer it. Labels are
strict: a note is relevant only if it contains the answer, not if it is merely on the topic.

| Kind | Count | What it tests |
|---|---|---|
| `keyword` | 5 | Shares rare words with the note ("Honda City", `select_for_update`): keyword search should win |
| `paraphrase` | 6 | Shares no content words ("landlord" for rent, "sunshine vitamin" for vitamin D): only vectors can find it |
| `section` | 5 | The answer is one section of a long note ("the Atlas risks") |
| `near_duplicate` | 5 | Only one of a near-duplicate pair is right ("when is the *second* Goa trip") |
| `checklist` | 3 | The answer is the unchecked items ("what's left to pack?") |
| `hinglish` | 1 | "weekend pe kya kaam baaki hai?" |
| `multi_note` | 3 | Two notes are both needed (Riya's birthday *and* gift ideas) |
| `no_answer` | 3 | Nothing in the notes answers it (passport number, dentist, chocolate cake) |

The loader (`retrieval/eval/loader.py`) validates both files and fails with the file and entry
named on: a duplicate key or id, a question naming an unknown note, a malformed TipTap document,
a node type the editor cannot produce, a checklist with no task item, or a `kind` that disagrees
with its labels (`no_answer` exactly when `relevant` is empty; `multi_note` needs two or more).

### Metrics

Search returns chunks; questions are labelled with notes. Every ranking is first collapsed to
notes in first-seen order, so each note sits at the rank of its best chunk
(`dedupe_to_notes`). Then, for one question with relevant notes *R* and note ranking *L*
(ranks from 1):

- **recall@k** = |R ∩ L[:k]| / |R|: the fraction of relevant notes in the top *k*. One of two
  relevant notes found scores 0.5.
- **reciprocal rank** = 1 / rank of the first relevant note in *L*, or 0 if none is ranked.
- **MRR** and **mean recall@k** are plain means over the *answerable* questions, each question
  weighing the same.

`no_answer` questions have no recall or rank (|R| = 0), so they are excluded from both means and
reported as a separate count. They are what the Ask relevance floor is judged on: for them, the
right outcome is no chunk above the floor.

### Running it

```
python manage.py eval_retrieval [--k 5] [--provider fake|openai|gemini] [--by-kind]
```

It creates a throwaway user, the 30 notes (through `notes/services.py`) and their chunks (with
`index_note`, synchronously) inside one transaction that is always rolled back, so it leaves no
data and is safe against any database; only the embedding calls cost anything. Each question is
searched in all three modes asking for 20 chunks, the hits are mapped back to fixture keys, and
recall@k and MRR are scored on the top k *notes* (with the per-note cap, up to 2k chunks).
`--provider` overrides `EMBEDDING_PROVIDER` for the run; `--by-kind` adds a row per question kind.
It ends with the vector leg's top similarity for each no-answer question, next to the minimum and
median top similarity of the answerable ones: the data for setting `ASK_RELEVANCE_FLOOR`. Vector
mode runs first, so a provider failure stops the command rather than letting hybrid quietly fall
back to keyword-only.

### Results

`python manage.py eval_retrieval --k 5`, 28 answerable questions, 3 no-answer.

**Fake provider (smoke test, not a quality measure).** The fake's vectors are a hashed bag of
words, so its "vector" leg is really a second keyword matcher without stemming; these numbers only
show the pipeline runs end to end.

| Mode | recall@5 | MRR |
|---|---|---|
| vector | 0.625 | 0.587 |
| keyword | 0.179 | 0.179 |
| hybrid | 0.661 | 0.622 |

No-answer top similarity (fake): 0.166, 0.144, 0.369; answerable: min 0.170, median 0.328. The
fake cannot separate them, as expected.

**Real provider: pending an API key.**

| Mode | recall@5 | MRR |
|---|---|---|
| vector | pending an API key | |
| keyword | pending an API key | |
| hybrid | pending an API key | |

Run `python manage.py eval_retrieval --k 5 --by-kind --provider openai` (or `gemini`) with the key
set, fill this table and the per-kind breakdown, and set `ASK_RELEVANCE_FLOOR` from the similarity
lines (D58).

## Known limits

*Filled as phases land.* So far:

- Token counts are approximated by characters (D33). Scripts that pack more or fewer characters per
  token (CJK, code) make chunks proportionally smaller or larger in tokens.
- The sentence splitter knows `.`, `!` and `?` only; "e.g. this" splits after "e.g.".
- The keyword leg uses `websearch_to_tsquery`, which ANDs every word: a whole question ("How often
  does the Honda City need a service?") matches only chunks containing all of its non-stopwords.
  That is why keyword-only recall is low in the smoke run (0.179). An OR of the query's lexemes
  scored 0.875 keyword / 0.804 hybrid in a throwaway experiment with the fake provider; whether to
  switch should be decided on the real-provider numbers.
- pgvector 0.6 filters by owner after the HNSW scan (D68). `ef_search` = 200 leaves room, but an
  owner who is a middling share of a very large table can get fewer than 50 vector candidates.
