# Tonie Selector

Python web interface to manage kids audiobooks from folders and upload selected albums to a configured Tonie figure.

## Features (MVP)

- Folder-first library model: `Series/Album`
- Inbox processing: drop new downloads in `data/inbox` and run ingest
- Duplicate detection for existing series/album combinations
- Automatic library sync on startup and `/scan`, including DB purge for removed album folders
- Poster logic:
  - use image from album folder if available
  - else extract embedded artwork from first audio file
- Album selection with 60-minute cap
- Upload flow with configurable figure id and pluggable `my-tonies` API client

## Project structure

```
app/
  services/
  static/
  templates/
data/
  inbox/
  library/
  processed/
  rejected/
config/
```

## Quick start (Docker)

1. Copy env file:

   ```bash
   cp .env.example .env
   ```

2. Create required directories:

   ```bash
   mkdir -p data/inbox data/library data/processed data/rejected data/posters config
   ```

3. Start app:

   ```bash
   docker compose up --build
   ```

4. Open:

   - `http://localhost:8000`

## Local run (without Docker)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
mkdir -p data/inbox data/library data/processed data/rejected data/posters config
uvicorn app.main:app --reload
```

## Inbox conventions

Drop files into:

`data/inbox/<Series>/<Album>/*.{mp3,m4a,flac,ogg,wav}`

For flat drops directly in `data/inbox`, ingest now groups files by embedded audio tags first:

- Series from `Performer/Artist`
- Album from `Album`

If tags are missing, filename parsing is used as fallback.

Then press **Process Inbox** in the UI (or `POST /scan`).

## Notes on `my-tonies` integration

The client in `app/services/my_tonies.py` is intentionally minimal. Set `MY_TONIES_MOCK_UPLOAD=false` and configure:

- `MY_TONIES_BASE_URL`
- `MY_TONIES_GRAPHQL_URL` (default: `https://api.prod.tcs.toys/v2/graphql`) for figurine fetch
- either `MY_TONIES_API_TOKEN`
- or username/password login via:
   - `MY_TONIES_USERNAME`
   - `MY_TONIES_PASSWORD`
   - optional OIDC parameters (`MY_TONIES_AUTH_BASE_URL`, `MY_TONIES_CLIENT_ID`, `MY_TONIES_REDIRECT_URI`, `MY_TONIES_SCOPE`, `MY_TONIES_UI_LOCALES`)
- optional fallback selector values: `FIGURE_OPTIONS=id1:Kitchen Tonie,id2:Bedroom Tonie`

For username/password auth, the app performs the same OIDC browser login flow (PKCE + login form submit + auth code exchange) and reuses the access token until expiry.
Upload flow uses Tonies v2 API endpoints:

- `PATCH /households/{householdId}/creativetonies/{figureId}` with `{"chapters": []}` to clear existing content
- `POST /file` to obtain a signed S3 form and `fileId`
- multipart form upload to the returned S3 URL using returned form fields + file body
- final `PATCH /households/{householdId}/creativetonies/{figureId}` to assign uploaded chapters

The final chapter payload shape can vary by backend version; the client currently tries both `{"chapters": ["fileId", ...]}` and `{"chapters": [{"file": "fileId"}, ...]}`.

When API credentials are available, the web UI fetches available figures via GraphQL (`households -> creativeTonies`) and shows a dropdown.
If fetching fails (or credentials are missing), the UI falls back to `FIGURE_OPTIONS` (or manual figure id input).
