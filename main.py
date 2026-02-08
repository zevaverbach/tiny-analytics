import hashlib
import re
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from fastapi import Cookie, Depends, FastAPI, Form, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from itsdangerous import BadSignature, URLSafeSerializer
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict

# ---------------------------------------------------------------------------
# Bot detection
# ---------------------------------------------------------------------------

BOT_PATTERNS = re.compile(
    r"bot|crawler|spider|scraper|curl|wget|python-requests|httpx|aiohttp|"
    r"googlebot|bingbot|yandex|baidu|duckduckbot|slurp|facebookexternalhit|"
    r"twitterbot|linkedinbot|embedly|quora|pinterest|redditbot|applebot|"
    r"semrushbot|ahrefsbot|mj12bot|dotbot|petalbot|bytespider|gptbot|"
    r"claudebot|anthropic|openai|headless|phantom|selenium|puppeteer|playwright",
    re.IGNORECASE,
)


def is_bot(user_agent: str) -> bool:
    """Return True if user-agent looks like a bot."""
    if not user_agent:
        return True  # No UA is suspicious
    return bool(BOT_PATTERNS.search(user_agent))


# ---------------------------------------------------------------------------
# Geo lookup (Cloudflare)
# ---------------------------------------------------------------------------

def get_country(request: Request) -> str | None:
    """Get country code from Cloudflare header."""
    cf_country = request.headers.get("cf-ipcountry")
    if cf_country and cf_country != "XX":  # XX = unknown in CF
        return cf_country
    return None

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")
    tinytrack_password: str = "changeme"
    tinytrack_secret_key: str = "change-this-to-a-random-string"
    tinytrack_allowed_origins: list[str] = []  # e.g. ["https://example.com", "https://blog.example.com"]
    tinytrack_db_path: str = str(Path(__file__).parent / "tinytrack.db")

settings = Settings()
signer = URLSafeSerializer(settings.tinytrack_secret_key, salt="tinytrack")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(settings.tinytrack_db_path)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pageviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            url TEXT NOT NULL,
            referrer TEXT,
            visitor_hash TEXT NOT NULL,
            user_agent TEXT,
            country TEXT
        )
    """)
    # Migration: add country column if missing (must happen before index creation)
    cursor = conn.execute("PRAGMA table_info(pageviews)")
    columns = [row[1] for row in cursor.fetchall()]
    if "country" not in columns:
        conn.execute("ALTER TABLE pageviews ADD COLUMN country TEXT")
        conn.commit()
    # Create indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON pageviews(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_visitor ON pageviews(visitor_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_country ON pageviews(country)")
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield

app = FastAPI(title="tinytrack", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.tinytrack_allowed_origins or ["*"],
    allow_methods=["POST"],
    allow_headers=["Content-Type"],
)

# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Hit(BaseModel):
    url: str
    referrer: str | None = None

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def require_auth(session: Annotated[str | None, Cookie(alias="tt_session")] = None):
    if not session:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    try:
        signer.loads(session)
    except BadSignature:
        raise HTTPException(status_code=303, headers={"Location": "/login"})


# ---------------------------------------------------------------------------
# Tracking endpoint  (called by the snippet)
# ---------------------------------------------------------------------------

def get_real_ip(request: Request) -> str:
    """Extract real client IP, handling Cloudflare/proxy headers."""
    # Cloudflare-specific header
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip
    # Standard proxy header
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    # Direct connection
    return request.client.host if request.client else "unknown"


@app.post("/t", status_code=204)
async def track(hit: Hit, request: Request):
    if settings.tinytrack_allowed_origins:
        origin = request.headers.get("origin") or request.headers.get("referer", "")
        # Strip path from referer to get just the origin
        parsed = urlparse(origin)
        request_origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else ""
        if request_origin not in settings.tinytrack_allowed_origins:
            return Response(status_code=403)

    ip = get_real_ip(request)
    ua = request.headers.get("user-agent", "")
    visitor_hash = hashlib.sha256(f"{ip}:{ua}".encode()).hexdigest()[:16]
    country = get_country(request)

    conn = get_db()
    conn.execute(
        "INSERT INTO pageviews (ts, url, referrer, visitor_hash, user_agent, country) VALUES (?, ?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), hit.url, hit.referrer, visitor_hash, ua, country),
    )
    conn.commit()
    conn.close()
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Snippet endpoint
# ---------------------------------------------------------------------------

@app.get("/snippet.js")
async def snippet(request: Request):
    origin = f"{request.url.scheme}://{request.url.netloc}"
    js = f"""\
(function(){{
  var d={{url:location.href,referrer:document.referrer||null}};
  fetch("{origin}/t",{{method:"POST",headers:{{"Content-Type":"application/json"}},body:JSON.stringify(d),keepalive:true}});
}})();"""
    return Response(content=js, media_type="application/javascript")


# ---------------------------------------------------------------------------
# Auth pages
# ---------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return """<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>tinytrack - login</title>
<style>
  body{font-family:system-ui;max-width:360px;margin:80px auto;padding:0 1rem}
  input,button{display:block;width:100%;padding:.6rem;margin:.5rem 0;box-sizing:border-box;font-size:1rem;border:1px solid #ccc;border-radius:4px}
  button{background:#111;color:#fff;border:none;cursor:pointer}
  button:hover{background:#333}
</style></head><body>
<h2>tinytrack</h2>
<form method="post" action="/login">
  <input type="password" name="password" placeholder="Password" required autofocus>
  <button type="submit">Log in</button>
</form></body></html>"""


@app.post("/login")
async def login(password: Annotated[str, Form()]):
    if password != settings.tinytrack_password:
        raise HTTPException(status_code=401, detail="Wrong password")
    token = signer.dumps("authenticated")
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie("tt_session", token, httponly=True, samesite="lax", max_age=86400 * 30)
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie("tt_session")
    return resp


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

def bucket_timestamp(ts_str: str, interval: str) -> str:
    """Bucket a timestamp string into the specified interval."""
    # Parse ISO timestamp
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    if interval == "15m":
        minute = (dt.minute // 15) * 15
        return dt.strftime(f"%Y-%m-%d %H:{minute:02d}")
    elif interval == "1h":
        return dt.strftime("%Y-%m-%d %H:00")
    else:  # 1d
        return dt.strftime("%Y-%m-%d")


@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def dashboard(request: Request, hours: int = 24, interval: str = "1h"):
    # Validate interval
    if interval not in ("15m", "1h", "1d"):
        interval = "1h"

    conn = get_db()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    # total unique visitors
    row = conn.execute(
        "SELECT COUNT(DISTINCT visitor_hash) as cnt FROM pageviews WHERE ts >= ?", (since,)
    ).fetchone()
    total_uniques = row["cnt"]

    # total pageviews
    row = conn.execute(
        "SELECT COUNT(*) as cnt FROM pageviews WHERE ts >= ?", (since,)
    ).fetchone()
    total_views = row["cnt"]

    # Get all pageviews for bucketing
    rows = conn.execute("""
        SELECT ts, visitor_hash, user_agent, country
        FROM pageviews WHERE ts >= ?
    """, (since,)).fetchall()

    # Top countries
    country_counts: dict[str, int] = {}
    for r in rows:
        c = r["country"] or "Unknown"
        country_counts[c] = country_counts.get(c, 0) + 1
    top_countries = sorted(country_counts.items(), key=lambda x: -x[1])[:5]

    conn.close()

    # Bucket by interval, separating humans and bots
    bucket_humans: dict[str, set[str]] = {}
    bucket_bots: dict[str, set[str]] = {}
    for r in rows:
        bucket = bucket_timestamp(r["ts"], interval)
        vh = r["visitor_hash"]
        ua = r["user_agent"] or ""
        if is_bot(ua):
            bucket_bots.setdefault(bucket, set()).add(vh)
        else:
            bucket_humans.setdefault(bucket, set()).add(vh)

    all_buckets = sorted(set(bucket_humans.keys()) | set(bucket_bots.keys()))
    human_values = [len(bucket_humans.get(b, set())) for b in all_buckets]
    bot_values = [len(bucket_bots.get(b, set())) for b in all_buckets]

    # Format labels based on interval
    if interval == "1d":
        labels = all_buckets  # Already YYYY-MM-DD
    else:
        # Show just time for sub-day intervals
        labels = [b.split(" ")[1] if " " in b else b for b in all_buckets]

    origin = f"{request.url.scheme}://{request.url.netloc}"

    # Build country stats HTML
    country_html = "".join(
        f'<span class="country">{c} <b>{n}</b></span>' for c, n in top_countries
    ) if top_countries else '<span class="country">No data yet</span>'

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>tinytrack</title>
<style>
  body{{font-family:system-ui;max-width:900px;margin:2rem auto;padding:0 1rem;color:#222}}
  .stats{{display:flex;gap:1rem;margin:1.5rem 0;flex-wrap:wrap}}
  .stat{{background:#f5f5f5;padding:1rem 1.2rem;border-radius:8px;flex:1;min-width:120px}}
  .stat h3{{margin:0 0 .3rem;font-size:.75rem;color:#666;text-transform:uppercase;letter-spacing:.05em}}
  .stat .num{{font-size:1.8rem;font-weight:700}}
  .snippet-box{{background:#f5f5f5;padding:1rem;border-radius:8px;margin:1.5rem 0;font-family:monospace;font-size:.85rem;word-break:break-all}}
  canvas{{max-height:300px}}
  nav{{display:flex;justify-content:space-between;align-items:center}}
  nav a{{color:#666;font-size:.85rem}}
  nav .links{{display:flex;gap:1rem}}
  .controls{{margin:1rem 0;display:flex;gap:2rem;flex-wrap:wrap}}
  .control-group{{display:flex;gap:.5rem;align-items:center}}
  .control-group label{{font-size:.8rem;color:#666;text-transform:uppercase}}
  .control-group a{{padding:.3rem .7rem;border-radius:4px;text-decoration:none;color:#666;font-size:.85rem;border:1px solid #ddd}}
  .control-group a.active{{background:#111;color:#fff;border-color:#111}}
  .countries{{margin:1rem 0}}
  .countries h3{{font-size:.85rem;color:#666;margin:0 0 .5rem}}
  .country{{display:inline-block;background:#f5f5f5;padding:.3rem .6rem;border-radius:4px;margin:.2rem;font-size:.85rem}}
  .country b{{margin-left:.3rem}}
</style>
</head><body>
<nav><h1>tinytrack</h1><div class="links"><a href="/logs">logs</a><a href="/logout">log out</a></div></nav>

<div class="controls">
  <div class="control-group">
    <label>Period:</label>
    <a href="/?hours=6&interval={interval}" {"class='active'" if hours==6 else ""}>6h</a>
    <a href="/?hours=24&interval={interval}" {"class='active'" if hours==24 else ""}>24h</a>
    <a href="/?hours=168&interval={interval}" {"class='active'" if hours==168 else ""}>7d</a>
    <a href="/?hours=720&interval={interval}" {"class='active'" if hours==720 else ""}>30d</a>
  </div>
  <div class="control-group">
    <label>Interval:</label>
    <a href="/?hours={hours}&interval=15m" {"class='active'" if interval=="15m" else ""}>15m</a>
    <a href="/?hours={hours}&interval=1h" {"class='active'" if interval=="1h" else ""}>1h</a>
    <a href="/?hours={hours}&interval=1d" {"class='active'" if interval=="1d" else ""}>1d</a>
  </div>
</div>

<div class="stats">
  <div class="stat"><h3>Unique Visitors</h3><div class="num">{total_uniques:,}</div></div>
  <div class="stat"><h3>Page Views</h3><div class="num">{total_views:,}</div></div>
</div>

<canvas id="chart"></canvas>

<div class="countries">
  <h3>Top Countries</h3>
  {country_html}
</div>

<h3>Embed this on your site</h3>
<div class="snippet-box">&lt;script src="{origin}/snippet.js" defer&gt;&lt;/script&gt;</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script>
new Chart(document.getElementById('chart'),{{
  type:'line',
  data:{{
    labels:{labels},
    datasets:[
      {{label:'Humans',data:{human_values},borderColor:'#111',backgroundColor:'rgba(17,17,17,0.1)',fill:true,tension:0.3,pointRadius:2}},
      {{label:'Bots',data:{bot_values},borderColor:'#e74c3c',backgroundColor:'rgba(231,76,60,0.1)',fill:true,tension:0.3,pointRadius:2}}
    ]
  }},
  options:{{
    responsive:true,
    interaction:{{intersect:false,mode:'index'}},
    plugins:{{legend:{{display:true,position:'bottom'}}}},
    scales:{{y:{{beginAtZero:true,ticks:{{precision:0}}}},x:{{ticks:{{maxRotation:45,autoSkip:true,maxTicksLimit:24}}}}}}
  }}
}});
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# Logs page
# ---------------------------------------------------------------------------

@app.get("/logs", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def logs_page(request: Request, limit: int = 100, offset: int = 0, filter: str = "all"):
    conn = get_db()

    # Get total count
    total = conn.execute("SELECT COUNT(*) as cnt FROM pageviews").fetchone()["cnt"]

    # Get paginated logs
    rows = conn.execute("""
        SELECT id, ts, url, referrer, visitor_hash, user_agent, country
        FROM pageviews
        ORDER BY ts DESC
        LIMIT ? OFFSET ?
    """, (limit, offset)).fetchall()
    conn.close()

    # Build table rows with bot detection
    table_rows = []
    for r in rows:
        ua = r["user_agent"] or ""
        is_bot_hit = is_bot(ua)

        # Skip based on filter
        if filter == "humans" and is_bot_hit:
            continue
        if filter == "bots" and not is_bot_hit:
            continue

        badge = '<span class="badge bot">bot</span>' if is_bot_hit else '<span class="badge human">human</span>'
        ts_short = r["ts"][:19].replace("T", " ")  # Trim to YYYY-MM-DD HH:MM:SS
        url_short = r["url"][:60] + "..." if len(r["url"]) > 60 else r["url"]
        ref_short = (r["referrer"] or "—")[:40]
        ua_short = ua[:50] + "..." if len(ua) > 50 else (ua or "—")
        country = r["country"] or "—"

        table_rows.append(f"""
        <tr>
            <td>{ts_short}</td>
            <td title="{r["url"]}">{url_short}</td>
            <td>{ref_short}</td>
            <td>{country}</td>
            <td><code>{r["visitor_hash"][:8]}</code></td>
            <td title="{ua}">{ua_short}</td>
            <td>{badge}</td>
        </tr>""")

    rows_html = "".join(table_rows) if table_rows else '<tr><td colspan="7">No hits found</td></tr>'

    prev_offset = max(0, offset - limit)
    next_offset = offset + limit

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>tinytrack - logs</title>
<style>
  body{{font-family:system-ui;max-width:1200px;margin:2rem auto;padding:0 1rem;color:#222}}
  nav{{display:flex;justify-content:space-between;align-items:center}}
  nav a{{color:#666;font-size:.85rem}}
  nav .links{{display:flex;gap:1rem}}
  table{{width:100%;border-collapse:collapse;margin:1rem 0;font-size:.85rem}}
  th,td{{text-align:left;padding:.6rem .5rem;border-bottom:1px solid #eee}}
  th{{background:#f5f5f5;font-weight:600;text-transform:uppercase;font-size:.75rem;letter-spacing:.05em}}
  tr:hover{{background:#fafafa}}
  code{{background:#f0f0f0;padding:.1rem .3rem;border-radius:3px;font-size:.8rem}}
  .badge{{padding:.2rem .5rem;border-radius:3px;font-size:.7rem;font-weight:600;text-transform:uppercase}}
  .badge.human{{background:#2ecc71;color:#fff}}
  .badge.bot{{background:#e74c3c;color:#fff}}
  .filters{{margin:1rem 0;display:flex;gap:.5rem}}
  .filters a{{padding:.3rem .7rem;border-radius:4px;text-decoration:none;color:#666;font-size:.85rem;border:1px solid #ddd}}
  .filters a.active{{background:#111;color:#fff;border-color:#111}}
  .pagination{{display:flex;gap:1rem;margin:1rem 0;justify-content:center}}
  .pagination a{{padding:.4rem .8rem;border:1px solid #ddd;border-radius:4px;text-decoration:none;color:#666}}
  .pagination a:hover{{background:#f5f5f5}}
  .meta{{color:#666;font-size:.85rem;margin:.5rem 0}}
</style>
</head><body>
<nav><h1>tinytrack / logs</h1><div class="links"><a href="/">dashboard</a><a href="/logout">log out</a></div></nav>

<div class="filters">
  <a href="/logs?filter=all" {"class='active'" if filter=="all" else ""}>all</a>
  <a href="/logs?filter=humans" {"class='active'" if filter=="humans" else ""}>humans</a>
  <a href="/logs?filter=bots" {"class='active'" if filter=="bots" else ""}>bots</a>
</div>

<p class="meta">Showing {offset + 1}–{min(offset + limit, total)} of {total:,} total hits</p>

<table>
  <thead>
    <tr><th>Timestamp</th><th>URL</th><th>Referrer</th><th>Country</th><th>Visitor</th><th>User Agent</th><th>Type</th></tr>
  </thead>
  <tbody>
    {rows_html}
  </tbody>
</table>

<div class="pagination">
  {"<a href='/logs?offset=" + str(prev_offset) + "&filter=" + filter + "'>← Previous</a>" if offset > 0 else ""}
  {"<a href='/logs?offset=" + str(next_offset) + "&filter=" + filter + "'>Next →</a>" if offset + limit < total else ""}
</div>
</body></html>"""
