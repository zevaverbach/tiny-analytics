import hashlib
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
# Settings
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")
    tinytrack_password: str = "changeme"
    tinytrack_secret_key: str = "change-this-to-a-random-string"
    tinytrack_allowed_origins: list[str] = []  # e.g. ["https://example.com", "https://blog.example.com"]

settings = Settings()
signer = URLSafeSerializer(settings.tinytrack_secret_key, salt="tinytrack")

DB_PATH = Path(__file__).parent / "tinytrack.db"

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
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
            user_agent TEXT
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON pageviews(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_visitor ON pageviews(visitor_hash)")
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

@app.post("/t", status_code=204)
async def track(hit: Hit, request: Request):
    if settings.tinytrack_allowed_origins:
        origin = request.headers.get("origin") or request.headers.get("referer", "")
        # Strip path from referer to get just the origin
        parsed = urlparse(origin)
        request_origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else ""
        if request_origin not in settings.tinytrack_allowed_origins:
            return Response(status_code=403)

    ip = request.client.host if request.client else "unknown"
    ua = request.headers.get("user-agent", "")
    visitor_hash = hashlib.sha256(f"{ip}:{ua}".encode()).hexdigest()[:16]

    conn = get_db()
    conn.execute(
        "INSERT INTO pageviews (ts, url, referrer, visitor_hash, user_agent) VALUES (?, ?, ?, ?, ?)",
        (datetime.now(timezone.utc).isoformat(), hit.url, hit.referrer, visitor_hash, ua),
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

@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def dashboard(request: Request, days: int = 30):
    conn = get_db()
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()

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

    # daily unique visitors for chart
    rows = conn.execute("""
        SELECT DATE(ts) as day, COUNT(DISTINCT visitor_hash) as uniques
        FROM pageviews WHERE ts >= ?
        GROUP BY DATE(ts) ORDER BY day
    """, (since,)).fetchall()
    conn.close()

    labels = [r["day"] for r in rows]
    values = [r["uniques"] for r in rows]

    origin = f"{request.url.scheme}://{request.url.netloc}"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>tinytrack</title>
<style>
  body{{font-family:system-ui;max-width:800px;margin:2rem auto;padding:0 1rem;color:#222}}
  .stats{{display:flex;gap:2rem;margin:1.5rem 0}}
  .stat{{background:#f5f5f5;padding:1.2rem 1.5rem;border-radius:8px;flex:1}}
  .stat h3{{margin:0 0 .3rem;font-size:.85rem;color:#666;text-transform:uppercase;letter-spacing:.05em}}
  .stat .num{{font-size:2rem;font-weight:700}}
  .snippet-box{{background:#f5f5f5;padding:1rem;border-radius:8px;margin:1.5rem 0;font-family:monospace;font-size:.85rem;word-break:break-all}}
  canvas{{max-height:300px}}
  nav{{display:flex;justify-content:space-between;align-items:center}}
  nav a{{color:#666;font-size:.85rem}}
  .period{{margin:1rem 0;display:flex;gap:.5rem}}
  .period a{{padding:.3rem .7rem;border-radius:4px;text-decoration:none;color:#666;font-size:.85rem;border:1px solid #ddd}}
  .period a.active{{background:#111;color:#fff;border-color:#111}}
</style>
</head><body>
<nav><h1>tinytrack</h1><a href="/logout">log out</a></nav>

<div class="period">
  <a href="/?days=7" {"class='active'" if days==7 else ""}>7d</a>
  <a href="/?days=30" {"class='active'" if days==30 else ""}>30d</a>
  <a href="/?days=90" {"class='active'" if days==90 else ""}>90d</a>
</div>

<div class="stats">
  <div class="stat"><h3>Unique Visitors</h3><div class="num">{total_uniques:,}</div></div>
  <div class="stat"><h3>Page Views</h3><div class="num">{total_views:,}</div></div>
</div>

<canvas id="chart"></canvas>

<h3>Embed this on your site</h3>
<div class="snippet-box">&lt;script src="{origin}/snippet.js" defer&gt;&lt;/script&gt;</div>

<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<script>
new Chart(document.getElementById('chart'),{{
  type:'bar',
  data:{{
    labels:{labels},
    datasets:[{{label:'Unique visitors',data:{values},backgroundColor:'#111',borderRadius:3}}]
  }},
  options:{{
    responsive:true,
    plugins:{{legend:{{display:false}}}},
    scales:{{y:{{beginAtZero:true,ticks:{{precision:0}}}},x:{{ticks:{{maxRotation:45}}}}}}
  }}
}});
</script>
</body></html>"""
