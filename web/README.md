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
  stores/           auth, notes (sync via notes/changes/), ask (submit + poll), view
  components/       SignIn, NotesList, NotePage/NoteEditor, AskPanel, AppHeader, DevicesList
  lib/              GIS loader, citation splitter, uuid, date formatting
```

No router: `stores/view.ts` holds which of the three screens is showing (DECISIONS D46).
