## Evaluation

> Draft for the "Evaluation" section of `docs/RAG.md`. Code: `retrieval/eval/`.

Retrieval is measured, not assumed. A fixed set of notes and labelled questions is run through
each retrieval mode, and recall@k and MRR are reported. With the fake embedding provider this is
only a smoke test (its vectors carry no meaning); the numbers that count come from a real
provider and are recorded below.

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

### Results

`python manage.py eval_retrieval --k 5` over the fixtures. Provider: _to be filled_.

| Mode | recall@5 | MRR | Answerable | No-answer |
|---|---|---|---|---|
| vector | | | | |
| keyword | | | | |
| hybrid | | | | |
