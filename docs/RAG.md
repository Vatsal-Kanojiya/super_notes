# Retrieval-augmented answers

How a note becomes something a question can be answered from: chunking, embedding, indexing,
retrieval, asking, and how it is measured. Plan §6. The decisions behind each part are in
[DECISIONS.md](DECISIONS.md) (D33–D40 for the first two sections).

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

*To be written with `NoteChunk` and `index_note` (Phase 3b).*

## Retrieval

*Phase 4.*

## Asking

*Phase 5.*

## Evaluation

*Phase 4: recall@k and MRR for vector, keyword and hybrid retrieval, with the real provider.*

## Known limits

*Filled as phases land.* So far:

- Token counts are approximated by characters (D33). Scripts that pack more or fewer characters per
  token (CJK, code) make chunks proportionally smaller or larger in tokens.
- The sentence splitter knows `.`, `!` and `?` only; "e.g. this" splits after "e.g.".
