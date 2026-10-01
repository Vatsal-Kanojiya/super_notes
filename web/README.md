# Super Notes — web client

Vue 3 + Vite + TypeScript, Pinia for state, TipTap for the editor. Talks to the Django API
under `/api/v1/` (see the repository's `README.md`).

## Setup

Node 24 (`nvm use 24`).

```bash
cd web
npm ci
cp .env.example .env.local   # then fill in VITE_GOOGLE_CLIENT_ID
```

| Setting | Meaning |
|---|---|
| `VITE_API_BASE_URL` | The API with its version prefix. Default `http://localhost:8000/api/v1`. |
| `VITE_GOOGLE_CLIENT_ID` | Google OAuth *web* client id. Also list it in the backend's `GOOGLE_OAUTH_CLIENT_IDS`. |

Google sign-in is the only way in. For it to work locally:

- add `http://localhost:5173` (and `http://localhost`) as **authorised JavaScript origins** on
  the OAuth client in Google Cloud Console;
- set `CORS_ALLOWED_ORIGINS=http://localhost:5173` in the backend's `.env`.

## Commands

```bash
npm run dev         # dev server on http://localhost:5173 (port is fixed)
npm run typecheck   # vue-tsc
npm run build       # typecheck, then a production build into dist/
npm run preview     # serve dist/ locally
```

## Layout

```
src/
  api/types.ts      every request/response shape (reconcile with docs/openapi.yml here)
  api/client.ts     fetch wrapper: bearer token, single-flight refresh, ApiError
  api/endpoints.ts  one typed function per endpoint
  stores/           auth, notes (sync via notes/changes/), chat (conversations, turn polling), appVersion
  components/       SignIn, NotesList, NotePage/NoteEditor, ChatList, ChatThread, AnswerBody, AppHeader, DevicesList
  lib/              GIS loader, citation splitter, thread logic, uuid, date formatting
```

Routes (vue-router, history mode): `/notes`, `/notes/:id`, `/chat`, `/chat/new`, `/chat/:id` (`/ask` redirects to `/chat/new`), `/settings`, and `/signin`
for signed-out users (DECISIONS D86, D112). Route components are lazy-loaded.

## Hosting

The app is a static site. Serve `dist/` with these rules, or users get stale or broken pages:

| Path | Header |
|---|---|
| `assets/*` (hashed file names) | `Cache-Control: public, max-age=31536000, immutable` |
| `index.html` | `Cache-Control: no-cache` |
| any service worker file (`sw.js`, if one is added) | `Cache-Control: no-cache` |
| any other unknown path | serve `index.html` (SPA fallback), so deep links and refresh work |

Why: `index.html` names the current hashed assets, so it must always be revalidated; the assets
never change under the same name, so they can be cached for a year. If a stale `index.html`
still points at deleted assets, the client reloads once on `vite:preloadError`; that is a safety
net, not a substitute for these headers.

## Versions (D89)

Each build has an id `YYYYMMDDHHMM-<shortsha>` (UTC), injected as `VITE_APP_VERSION` by
`vite.config.ts` (`nogit` replaces the sha without git; set `VITE_APP_VERSION` yourself to
override). The client checks `GET /api/v1/app/version/` on load, on window focus and every 5
minutes, and watches the `X-Client-Min-Version` response header. A newer build shows a "New
version" bar and reloads on its own when no note edit is unsaved; a build below the minimum is
reloaded the same way (after the edit is saved). Builds compare by their timestamp prefix. Set
the server's `CLIENT_LATEST_VERSION` (and, to force an update, `CLIENT_MIN_VERSION`) to the id
of the build you deploy.
