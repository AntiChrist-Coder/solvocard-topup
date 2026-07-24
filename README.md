# SolvoCard Topup Panel

Local tool to top up SolvoCard balances with automatic retries. Includes a web panel for managing cards, sessions, and live status.

## Requirements

- Python 3.10+ (the included `run.bat` uses `py -3.14`)
- Windows, macOS, or Linux
- A logged-in SolvoCard session (cookies from your browser)

## Quick start

1. Clone or download this folder.
2. Start the panel:

   **Windows**

   ```bat
   run.bat
   ```

   **macOS / Linux**

   ```bash
   python3 -m pip install -r requirements.txt
   python3 app.py
   ```

3. Open **http://127.0.0.1:5050** in your browser.

4. Go to the **Session** tab, paste your cookies, and click **Connect session**.

5. Return to **Overview**, set a topup amount (in **dollars**), and click **Add a topup**.

## Getting your session cookies

You need the same cookies your browser uses on [solvocard.com](https://www.solvocard.com), especially:

- `sb-*-auth-token` (Supabase auth)
- `cf_clearance` (Cloudflare, if present)

**Option A — curl (easiest)**

1. Log in at solvocard.com.
2. Open DevTools → **Network**.
3. Click any request to `solvocard.com`, right-click → **Copy** → **Copy as cURL**.
4. Paste the full curl into the Session tab and click **Connect session**.

**Option B — cookie JSON**

1. Export or copy cookies as JSON, e.g. `{ "sb-...-auth-token": "...", "cf_clearance": "..." }`.
2. Paste into the Session tab and click **Connect session**.

Cookies are saved locally to `panel_state.json` and reused on the next run.

## Using the panel

| Area | What it does |
|------|----------------|
| **Overview** | Card balances, topup amount, **Add a topup** / **Stop** |
| **Transactions** | Recent activity across your cards |
| **Session** | Connect, view, or clear saved cookies |
| **Retry** | Automatic retry pacing (derived from API rate limits) |
| **Active topups** (right) | Running attempts, status, and stop controls |
| **Live log** (bottom) | Real-time log of attempts and results |

### Topups

- Enter the amount in **dollars** (e.g. `25.00` for a $25 topup).
- **Add a topup** saves the amount and starts retrying until the topup succeeds.
- **Stop** cancels retries for that card.
- **Start all** / **Stop all** in the header control every card.
- Retries stop automatically on success or if the session expires (401/403).
- On **429 rate limits**, the tool pauses all topups, learns the wait time from response headers when available, and otherwise backs off automatically (starting at 60s, up to 15 minutes) before retrying.

Suggested defaults: **$5** on the lowest-balance card, **$25** on others.

### Refresh

Use **Refresh** to reload balances and transactions from SolvoCard. Card inputs and buttons are updated in place so you can keep editing while the panel polls in the background.

## Command line

The panel must have a saved session first (connect once in the web UI).

Retry all cards until success:

```bash
python app.py topup
```

Single attempt, no retry loop:

```bash
python app.py topup --once
```

Specific card and amount (amount in **cents**):

```bash
python app.py topup --card YOUR-CARD-UUID --amount 2500
```

Or use the thin wrapper:

```bash
python topup.py
```

Custom host/port for the panel:

```bash
python app.py panel --host 127.0.0.1 --port 5050
```

## Local files

| File | Purpose |
|------|---------|
| `panel_state.json` | Saved cookies, topup amounts, settings |
| `panel_log.json` | Last ~400 log entries |

These files are gitignored and stay on your machine. Do not commit or share them — they contain session credentials.

## Troubleshooting

| Problem | What to try |
|---------|-------------|
| No cards loaded | Re-copy curl from DevTools; include `cf_clearance` and `sb-*-auth-token`. |
| Session expired | Connect again with fresh cookies from the browser. |
| Stale UI | Hard refresh: **Ctrl+Shift+R**. Restart `run.bat` if an old server is still running. |
| Port 5050 in use | Stop the other process or run `python app.py panel --port 5051`. |
| Cloudflare / auth errors | Export a new curl while logged in; cookies expire quickly. |

## Project layout

```
app.py           Flask panel + CLI entry point
client.py        SolvoCard API client (curl_cffi)
worker.py        Background topup retry workers
panel_state.py   Local persistence
templates/       Web UI
run.bat          Windows launcher
topup.py         CLI shortcut → app.py topup
```
