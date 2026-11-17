# RevBridge Connect panel

The control panel of a Connect installation: delivery, open and click rates, the list of emails
and the journey of each one, sending domains with their DNS records, API keys, suppressions and
webhooks. English and Portuguese, light and dark themes.

It is a static app (Vite, React, TypeScript, Tailwind) that only talks to the Connect API of the
same installation, on the same origin under `/api`. No CDN, no analytics; the Inter font ships
with the build.

## Development

Requirements: Node 18.18+ (22 recommended) and the Connect API running (`make dev` at the
repository root).

```bash
cd panel
npm install
npm run dev          # http://localhost:5173
```

Vite proxies `/api/*` to `http://127.0.0.1:8001` and strips the prefix, like Caddy does in
production. To use another API: `CONNECT_API_URL=http://10.0.0.5:8001 npm run dev`.

Log in with the user created by `scripts/bootstrap_tenant.py`.

## Build

```bash
npm run typecheck    # tsc --noEmit
npm run build        # typecheck + vite build, output in dist/
npm run preview      # serve dist/ locally
```

`VITE_API_BASE` changes the API prefix at build time (default `/api`); an absolute URL works too.

## Serving

`docker/panel.Dockerfile` (context = repository root) builds the Caddy image with the files in
`/srv/panel`; `config/caddy/panel.caddy` holds the site rules. `try_files {path} /index.html` is
required because the panel uses browser routes (`/emails`, `/keys`…) that are not files.

## Session

`POST /auth/login` returns a JWT. Without "Remember me" it is kept in `sessionStorage` (gone when
the tab closes); with it, in `localStorage`. Any 401 ends the session. The panel signs out when the
token expires.

## Pages and the API calls they make

Paths without the `/api` prefix; every call sends `Authorization: Bearer <JWT>`.

| Page | Calls |
|---|---|
| Login | `POST /auth/login`; with a saved session, `GET /auth/me` |
| Overview | `GET /stats/dashboard`, `GET /stats/timeseries`, `GET /stats/activity`, `GET /volume/allowance` |
| Emails | `GET /stats/messages`, `GET /stats/message/headers`, `GET /status/{id}` |
| API keys | `GET/POST /api-keys`, `POST /api-keys/{api_key_id}/channels`, `PUT /api-keys/{id}`, `DELETE /api-keys/{id}` |
| Domains | `GET /domains/{domain}/dns-info`, `GET /dkim/keys`, `GET/PUT /email-configs`, `PATCH /email-configs/{id}/toggle` |
| Suppressions | `GET/POST /suppressions`, `DELETE /suppressions/{id}` |
| Webhooks | `GET/POST /webhooks`, `PUT /webhooks/{id}`, `PATCH /webhooks/{id}/toggle`, `POST /webhooks/{id}/test`, `DELETE /webhooks/{id}`, `GET /webhooks/{id}/events` |

Times: the API returns timestamps without a zone; Connect's MySQL runs in UTC, so the panel reads
them as UTC and shows them in the viewer's time zone.

## Layout

```
src/
  main.tsx, App.tsx       entry point and routes
  index.css               color tokens (light and dark) and base styles
  i18n/                   pt.ts, en.ts, provider and formatting
  lib/                    API client, types, session, dates, labels
  components/             buttons, fields, tables, side panel, notices, SVG chart
  layout/AppShell.tsx     side menu, mobile header, theme and language switch
  pages/                  one page per menu item
```

Accessibility: labels on every field, visible focus, keyboard navigation (including the chart,
with the arrow keys), a table alternative to the chart, AA contrast in both themes and
`prefers-reduced-motion` respected.
