# tinytrack

Minimal, privacy-focused web analytics. Self-hosted, no cookies, no tracking IDs.

![Dashboard](docs/dashboard.png)

## Quick Start

```bash
# Clone and setup
git clone https://github.com/zevaverbach/tinytrack.git
cd tinytrack
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure
cp .env.example .env
# Edit .env: set TINYTRACK_PASSWORD and TINYTRACK_SECRET_KEY

# Run
uvicorn main:app --host 0.0.0.0 --port 8000
```

Add this to your site:
```html
<script src="https://your-tinytrack-domain/snippet.js" defer></script>
```

That's it. View your dashboard at `https://your-tinytrack-domain/`.

---

## Features

- **Privacy-first**: No cookies, no fingerprinting, no personal data stored
- **Lightweight**: Single ~18KB Python file + templates
- **Bot filtering**: Automatic detection and separation of bot traffic
- **Time on page**: Tracks actual engagement, not just page loads
- **Geo & device breakdown**: See where your visitors come from and what they use
- **Dark mode UI**: Easy on the eyes

## Screenshots

<details>
<summary>Login</summary>

![Login](docs/login.png)
</details>

<details>
<summary>Logs view</summary>

![Logs](docs/logs.jpg)
</details>

## How It Works

1. **Visitor hits your page** → snippet.js fires a POST to `/t`
2. **Server hashes IP+UA** → creates anonymous visitor ID (never stored raw)
3. **On page leave** → beacon sends time-on-page to `/d`
4. **Dashboard** → aggregates and visualizes the data

No cookies. No localStorage. No tracking across sites.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `TINYTRACK_PASSWORD` | `changeme` | Dashboard login password |
| `TINYTRACK_SECRET_KEY` | `change-this...` | Session signing key (generate a random string) |
| `TINYTRACK_ALLOWED_ORIGINS` | `[]` | Restrict tracking to specific domains (empty = allow all) |
| `TINYTRACK_DB_PATH` | `./tinytrack.db` | SQLite database location |

## Deployment

### Systemd

```ini
[Unit]
Description=tinytrack
After=network.target

[Service]
User=www-data
WorkingDirectory=/opt/tinytrack
Environment="PATH=/opt/tinytrack/.venv/bin"
ExecStart=/opt/tinytrack/.venv/bin/uvicorn main:app --host 127.0.0.1 --port 8000
Restart=always

[Install]
WantedBy=multi-user.target
```

### Behind nginx

```nginx
location / {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

### Cloudflare

tinytrack reads `cf-connecting-ip` and `cf-ipcountry` headers automatically for accurate geo and IP data behind Cloudflare.

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/t` | POST | Record a pageview |
| `/d` | POST | Update time-on-page |
| `/snippet.js` | GET | Tracking script |
| `/` | GET | Dashboard (auth required) |
| `/logs` | GET | Raw logs view (auth required) |

## License

MIT
