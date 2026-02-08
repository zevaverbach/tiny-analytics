import hashlib
import json
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
        return True
    return bool(BOT_PATTERNS.search(user_agent))


# ---------------------------------------------------------------------------
# Device detection
# ---------------------------------------------------------------------------

MOBILE_PATTERNS = re.compile(
    r"Mobile|Android.*Mobile|iPhone|iPod|BlackBerry|IEMobile|Opera Mini|"
    r"Windows Phone|webOS|Symbian|Nokia|Samsung.*Mobile",
    re.IGNORECASE,
)

TABLET_PATTERNS = re.compile(
    r"iPad|Android(?!.*Mobile)|Tablet|PlayBook|Silk|Kindle",
    re.IGNORECASE,
)


def detect_device(user_agent: str) -> str:
    """Detect device type from user agent."""
    if not user_agent:
        return "unknown"
    if TABLET_PATTERNS.search(user_agent):
        return "tablet"
    if MOBILE_PATTERNS.search(user_agent):
        return "mobile"
    return "desktop"


# ---------------------------------------------------------------------------
# Geo lookup (Cloudflare)
# ---------------------------------------------------------------------------

def get_country(request: Request) -> str | None:
    """Get country code from Cloudflare header."""
    cf_country = request.headers.get("cf-ipcountry")
    if cf_country and cf_country != "XX":
        return cf_country
    return None


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env")
    tinytrack_password: str = "changeme"
    tinytrack_secret_key: str = "change-this-to-a-random-string"
    tinytrack_allowed_origins: list[str] = []
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
            country TEXT,
            device TEXT,
            duration_sec INTEGER
        )
    """)
    # Migrations for existing tables
    cursor = conn.execute("PRAGMA table_info(pageviews)")
    columns = [row[1] for row in cursor.fetchall()]
    if "country" not in columns:
        conn.execute("ALTER TABLE pageviews ADD COLUMN country TEXT")
    if "device" not in columns:
        conn.execute("ALTER TABLE pageviews ADD COLUMN device TEXT")
    if "duration_sec" not in columns:
        conn.execute("ALTER TABLE pageviews ADD COLUMN duration_sec INTEGER")
    conn.commit()
    # Create indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ts ON pageviews(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_visitor ON pageviews(visitor_hash)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_country ON pageviews(country)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_device ON pageviews(device)")
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
    sid: str | None = None  # Session ID for duration tracking


class Duration(BaseModel):
    sid: str
    duration: int  # Seconds spent on page


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
# Tracking endpoints
# ---------------------------------------------------------------------------

def get_real_ip(request: Request) -> str:
    """Extract real client IP, handling Cloudflare/proxy headers."""
    cf_ip = request.headers.get("cf-connecting-ip")
    if cf_ip:
        return cf_ip
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.post("/t", status_code=204)
async def track(hit: Hit, request: Request):
    if settings.tinytrack_allowed_origins:
        origin = request.headers.get("origin") or request.headers.get("referer", "")
        parsed = urlparse(origin)
        request_origin = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme else ""
        if request_origin not in settings.tinytrack_allowed_origins:
            return Response(status_code=403)

    ip = get_real_ip(request)
    ua = request.headers.get("user-agent", "")
    visitor_hash = hashlib.sha256(f"{ip}:{ua}".encode()).hexdigest()[:16]
    country = get_country(request)
    device = detect_device(ua)

    # Use provided session ID or generate one
    sid = hit.sid or hashlib.sha256(f"{visitor_hash}:{datetime.now(timezone.utc).isoformat()}".encode()).hexdigest()[:12]

    conn = get_db()
    conn.execute(
        """INSERT INTO pageviews (ts, url, referrer, visitor_hash, user_agent, country, device, duration_sec)
           VALUES (?, ?, ?, ?, ?, ?, ?, NULL)""",
        (datetime.now(timezone.utc).isoformat(), hit.url, hit.referrer, visitor_hash, ua, country, device),
    )
    # Get the row ID for duration updates
    row_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.commit()
    conn.close()
    
    # Return session info for duration tracking
    return Response(
        content=json.dumps({"sid": sid, "rid": row_id}),
        status_code=200,
        media_type="application/json",
    )


@app.post("/d", status_code=204)
async def duration(request: Request):
    """Update duration for a pageview (called on page unload)."""
    try:
        body = await request.body()
        data = json.loads(body)
        rid = data.get("rid")
        duration_sec = data.get("d", 0)
        
        if rid and duration_sec and duration_sec > 0 and duration_sec < 7200:  # Cap at 2 hours
            conn = get_db()
            conn.execute("UPDATE pageviews SET duration_sec = ? WHERE id = ?", (duration_sec, rid))
            conn.commit()
            conn.close()
    except Exception:
        pass  # Silently fail for beacon requests
    return Response(status_code=204)


# ---------------------------------------------------------------------------
# Snippet endpoint
# ---------------------------------------------------------------------------

@app.get("/snippet.js")
async def snippet(request: Request):
    origin = f"{request.url.scheme}://{request.url.netloc}"
    js = f"""\
(function(){{
  var start=Date.now(),rid=null;
  fetch("{origin}/t",{{
    method:"POST",
    headers:{{"Content-Type":"application/json"}},
    body:JSON.stringify({{url:location.href,referrer:document.referrer||null}})
  }}).then(r=>r.json()).then(d=>{{rid=d.rid}}).catch(()=>{{}});
  function send(){{
    if(!rid)return;
    var d=Math.round((Date.now()-start)/1000);
    navigator.sendBeacon("{origin}/d",JSON.stringify({{rid:rid,d:d}}));
  }}
  document.addEventListener("visibilitychange",function(){{if(document.hidden)send()}});
  window.addEventListener("pagehide",send);
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
  :root{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#c9d1d9;--muted:#8b949e;--accent:#58a6ff}
  body{font-family:system-ui;max-width:360px;margin:80px auto;padding:0 1rem;background:var(--bg);color:var(--text)}
  h2{color:var(--text)}
  input,button{display:block;width:100%;padding:.7rem;margin:.5rem 0;box-sizing:border-box;font-size:1rem;border-radius:6px;border:1px solid var(--border);background:var(--card);color:var(--text)}
  input:focus{outline:none;border-color:var(--accent)}
  button{background:var(--accent);color:#fff;border:none;cursor:pointer;font-weight:500}
  button:hover{opacity:.9}
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
# Dashboard helpers
# ---------------------------------------------------------------------------

def bucket_timestamp(ts_str: str, interval: str) -> str:
    """Bucket a timestamp string into the specified interval."""
    dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    if interval == "15m":
        minute = (dt.minute // 15) * 15
        return dt.strftime(f"%Y-%m-%d %H:{minute:02d}")
    elif interval == "1h":
        return dt.strftime("%Y-%m-%d %H:00")
    else:
        return dt.strftime("%Y-%m-%d")


# Country name mapping for display
COUNTRY_NAMES = {
    "US": "United States", "GB": "United Kingdom", "DE": "Germany", "FR": "France",
    "CA": "Canada", "AU": "Australia", "JP": "Japan", "CN": "China", "IN": "India",
    "BR": "Brazil", "NL": "Netherlands", "IT": "Italy", "ES": "Spain", "SE": "Sweden",
    "CH": "Switzerland", "PL": "Poland", "RU": "Russia", "KR": "South Korea",
    "MX": "Mexico", "SG": "Singapore", "HK": "Hong Kong", "TW": "Taiwan",
    "IE": "Ireland", "IL": "Israel", "AT": "Austria", "BE": "Belgium", "DK": "Denmark",
    "NO": "Norway", "FI": "Finland", "NZ": "New Zealand", "PT": "Portugal",
}


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def dashboard(request: Request, hours: int = 24, interval: str = "1h"):
    if interval not in ("15m", "1h", "1d"):
        interval = "1h"

    conn = get_db()
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()

    # Aggregate stats
    stats = conn.execute("""
        SELECT 
            COUNT(DISTINCT visitor_hash) as uniques,
            COUNT(*) as views,
            AVG(CASE WHEN duration_sec IS NOT NULL AND duration_sec > 0 THEN duration_sec END) as avg_duration
        FROM pageviews WHERE ts >= ?
    """, (since,)).fetchone()
    
    total_uniques = stats["uniques"]
    total_views = stats["views"]
    avg_duration = int(stats["avg_duration"] or 0)

    # Get all pageviews
    rows = conn.execute("""
        SELECT ts, visitor_hash, user_agent, country, device, duration_sec
        FROM pageviews WHERE ts >= ?
    """, (since,)).fetchall()
    conn.close()

    # Process data for charts
    bucket_humans: dict[str, set[str]] = {}
    bucket_bots: dict[str, set[str]] = {}
    country_counts: dict[str, int] = {}
    device_counts: dict[str, int] = {"desktop": 0, "mobile": 0, "tablet": 0, "unknown": 0}
    duration_buckets: dict[str, list[int]] = {}

    for r in rows:
        bucket = bucket_timestamp(r["ts"], interval)
        vh = r["visitor_hash"]
        ua = r["user_agent"] or ""
        country = r["country"] or "Unknown"
        device = r["device"] or "unknown"
        duration = r["duration_sec"]

        if is_bot(ua):
            bucket_bots.setdefault(bucket, set()).add(vh)
        else:
            bucket_humans.setdefault(bucket, set()).add(vh)
            country_counts[country] = country_counts.get(country, 0) + 1
            device_counts[device] = device_counts.get(device, 0) + 1
            if duration and duration > 0:
                duration_buckets.setdefault(bucket, []).append(duration)

    # Time series data
    all_buckets = sorted(set(bucket_humans.keys()) | set(bucket_bots.keys()))
    human_values = [len(bucket_humans.get(b, set())) for b in all_buckets]
    bot_values = [len(bucket_bots.get(b, set())) for b in all_buckets]
    duration_values = [
        int(sum(duration_buckets.get(b, [])) / len(duration_buckets.get(b, [1])))
        for b in all_buckets
    ]

    # Top countries for chart (with full names)
    top_countries = sorted(country_counts.items(), key=lambda x: -x[1])[:10]
    country_labels = [COUNTRY_NAMES.get(c, c) for c, _ in top_countries]
    country_values = [v for _, v in top_countries]

    # Format time labels
    if interval == "1d":
        time_labels = all_buckets
    else:
        time_labels = [b.split(" ")[1] if " " in b else b for b in all_buckets]

    origin = f"{request.url.scheme}://{request.url.netloc}"

    # Format average duration
    if avg_duration > 60:
        duration_str = f"{avg_duration // 60}m {avg_duration % 60}s"
    else:
        duration_str = f"{avg_duration}s"

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>tinytrack</title>
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
<style>
  :root{{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#c9d1d9;--muted:#8b949e;--accent:#58a6ff;--green:#3fb950;--red:#f85149;--orange:#d29922}}
  *{{box-sizing:border-box}}
  body{{font-family:system-ui,-apple-system,sans-serif;margin:0;padding:1.5rem;background:var(--bg);color:var(--text);min-height:100vh}}
  .container{{max-width:1200px;margin:0 auto}}
  nav{{display:flex;justify-content:space-between;align-items:center;margin-bottom:1.5rem}}
  nav h1{{font-size:1.4rem;margin:0}}
  nav .links{{display:flex;gap:1rem}}
  nav a{{color:var(--muted);font-size:.85rem;text-decoration:none}}
  nav a:hover{{color:var(--accent)}}
  .controls{{display:flex;gap:1.5rem;flex-wrap:wrap;margin-bottom:1.5rem}}
  .control-group{{display:flex;gap:.4rem;align-items:center}}
  .control-group label{{font-size:.75rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-right:.3rem}}
  .control-group a{{padding:.4rem .7rem;border-radius:6px;text-decoration:none;color:var(--muted);font-size:.8rem;border:1px solid var(--border);background:transparent;transition:all .15s}}
  .control-group a:hover{{border-color:var(--muted)}}
  .control-group a.active{{background:var(--accent);color:#fff;border-color:var(--accent)}}
  .stats{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:1rem;margin-bottom:1.5rem}}
  .stat{{background:var(--card);padding:1rem 1.2rem;border-radius:10px;border:1px solid var(--border)}}
  .stat h3{{margin:0 0 .3rem;font-size:.7rem;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;font-weight:500}}
  .stat .num{{font-size:1.8rem;font-weight:700}}
  .charts{{display:grid;grid-template-columns:repeat(2,1fr);gap:1rem;margin-bottom:1.5rem}}
  @media(max-width:900px){{.charts{{grid-template-columns:1fr}}}}
  .chart-card{{background:var(--card);border-radius:10px;border:1px solid var(--border);padding:1rem}}
  .chart-card h3{{margin:0 0 .8rem;font-size:.85rem;color:var(--muted);font-weight:500}}
  .chart-card.wide{{grid-column:span 2}}
  @media(max-width:900px){{.chart-card.wide{{grid-column:span 1}}}}
  .chart{{height:220px}}
  .chart.tall{{height:280px}}
  .snippet{{background:var(--card);border-radius:10px;border:1px solid var(--border);padding:1rem;margin-top:1.5rem}}
  .snippet h3{{margin:0 0 .8rem;font-size:.85rem;color:var(--muted);font-weight:500}}
  .snippet-box{{position:relative}}
  .snippet-code{{background:var(--bg);padding:.8rem 3rem .8rem .8rem;border-radius:6px;font-family:'SF Mono',Monaco,monospace;font-size:.8rem;word-break:break-all;color:var(--text);border:1px solid var(--border)}}
  .copy-btn{{position:absolute;right:.5rem;top:50%;transform:translateY(-50%);background:var(--border);border:none;color:var(--text);padding:.4rem .6rem;border-radius:4px;cursor:pointer;font-size:.75rem;transition:all .15s}}
  .copy-btn:hover{{background:var(--muted)}}
  .copy-btn.copied{{background:var(--green);color:#fff}}
</style>
</head><body>
<div class="container">
<nav><h1>📊 tinytrack</h1><div class="links"><a href="/logs">logs</a><a href="/logout">log out</a></div></nav>

<div class="controls">
  <div class="control-group">
    <label>Period</label>
    <a href="/?hours=6&interval={interval}" {"class='active'" if hours==6 else ""}>6h</a>
    <a href="/?hours=24&interval={interval}" {"class='active'" if hours==24 else ""}>24h</a>
    <a href="/?hours=168&interval={interval}" {"class='active'" if hours==168 else ""}>7d</a>
    <a href="/?hours=720&interval={interval}" {"class='active'" if hours==720 else ""}>30d</a>
  </div>
  <div class="control-group">
    <label>Interval</label>
    <a href="/?hours={hours}&interval=15m" {"class='active'" if interval=="15m" else ""}>15m</a>
    <a href="/?hours={hours}&interval=1h" {"class='active'" if interval=="1h" else ""}>1h</a>
    <a href="/?hours={hours}&interval=1d" {"class='active'" if interval=="1d" else ""}>1d</a>
  </div>
</div>

<div class="stats">
  <div class="stat"><h3>Unique Visitors</h3><div class="num">{total_uniques:,}</div></div>
  <div class="stat"><h3>Page Views</h3><div class="num">{total_views:,}</div></div>
  <div class="stat"><h3>Avg. Time on Page</h3><div class="num">{duration_str}</div></div>
  <div class="stat"><h3>Desktop</h3><div class="num">{device_counts['desktop']:,}</div></div>
  <div class="stat"><h3>Mobile</h3><div class="num">{device_counts['mobile']:,}</div></div>
</div>

<div class="charts">
  <div class="chart-card wide">
    <h3>Traffic</h3>
    <div id="traffic-chart" class="chart tall"></div>
  </div>
  <div class="chart-card">
    <h3>Countries</h3>
    <div id="geo-chart" class="chart"></div>
  </div>
  <div class="chart-card">
    <h3>Devices</h3>
    <div id="device-chart" class="chart"></div>
  </div>
</div>

<div class="snippet">
  <h3>Embed this on your site</h3>
  <div class="snippet-box">
    <div class="snippet-code" id="snippet-text">&lt;script src="{origin}/snippet.js" defer&gt;&lt;/script&gt;</div>
    <button class="copy-btn" onclick="copySnippet()">Copy</button>
  </div>
</div>
</div>

<script>
const dark = {{bg:'#0d1117',card:'#161b22',border:'#30363d',text:'#c9d1d9',muted:'#8b949e',accent:'#58a6ff',green:'#3fb950',red:'#f85149'}};

// Traffic chart
echarts.init(document.getElementById('traffic-chart')).setOption({{
  tooltip:{{trigger:'axis',backgroundColor:dark.card,borderColor:dark.border,textStyle:{{color:dark.text}}}},
  legend:{{bottom:0,textStyle:{{color:dark.muted}},data:['Humans','Bots']}},
  grid:{{left:40,right:20,top:20,bottom:40}},
  xAxis:{{type:'category',data:{json.dumps(time_labels)},axisLine:{{lineStyle:{{color:dark.border}}}},axisLabel:{{color:dark.muted,rotate:45}}}},
  yAxis:{{type:'value',axisLine:{{show:false}},axisLabel:{{color:dark.muted}},splitLine:{{lineStyle:{{color:dark.border}}}}}},
  series:[
    {{name:'Humans',type:'line',smooth:true,data:{json.dumps(human_values)},areaStyle:{{opacity:0.2}},lineStyle:{{color:dark.accent}},itemStyle:{{color:dark.accent}}}},
    {{name:'Bots',type:'line',smooth:true,data:{json.dumps(bot_values)},areaStyle:{{opacity:0.1}},lineStyle:{{color:dark.red}},itemStyle:{{color:dark.red}}}}
  ]
}});

// Countries chart
echarts.init(document.getElementById('geo-chart')).setOption({{
  tooltip:{{trigger:'axis',backgroundColor:dark.card,borderColor:dark.border,textStyle:{{color:dark.text}}}},
  grid:{{left:100,right:20,top:10,bottom:10}},
  xAxis:{{type:'value',axisLine:{{show:false}},axisLabel:{{color:dark.muted}},splitLine:{{lineStyle:{{color:dark.border}}}}}},
  yAxis:{{type:'category',data:{json.dumps(country_labels[::-1])},axisLine:{{show:false}},axisLabel:{{color:dark.muted}}}},
  series:[{{type:'bar',data:{json.dumps(country_values[::-1])},itemStyle:{{color:dark.green,borderRadius:[0,4,4,0]}}}}]
}});

// Device chart
echarts.init(document.getElementById('device-chart')).setOption({{
  tooltip:{{trigger:'item',backgroundColor:dark.card,borderColor:dark.border,textStyle:{{color:dark.text}}}},
  series:[{{
    type:'pie',
    radius:['45%','70%'],
    center:['50%','50%'],
    avoidLabelOverlap:false,
    itemStyle:{{borderRadius:6,borderColor:dark.bg,borderWidth:2}},
    label:{{show:true,position:'outside',color:dark.muted,formatter:'{{b}}\\n{{c}}'}},
    data:[
      {{value:{device_counts['desktop']},name:'Desktop',itemStyle:{{color:dark.accent}}}},
      {{value:{device_counts['mobile']},name:'Mobile',itemStyle:{{color:dark.green}}}},
      {{value:{device_counts['tablet']},name:'Tablet',itemStyle:{{color:dark.muted}}}}
    ]
  }}]
}});

// Copy button
function copySnippet(){{
  const text=document.getElementById('snippet-text').innerText;
  navigator.clipboard.writeText(text).then(()=>{{
    const btn=document.querySelector('.copy-btn');
    btn.textContent='Copied!';
    btn.classList.add('copied');
    setTimeout(()=>{{btn.textContent='Copy';btn.classList.remove('copied')}},2000);
  }});
}}

// Resize handler
window.addEventListener('resize',()=>{{
  document.querySelectorAll('[id$="-chart"]').forEach(el=>{{
    const chart=echarts.getInstanceByDom(el);
    if(chart)chart.resize();
  }});
}});
</script>
</body></html>"""


# ---------------------------------------------------------------------------
# Logs page
# ---------------------------------------------------------------------------

@app.get("/logs", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def logs_page(request: Request, limit: int = 100, offset: int = 0, filter: str = "all"):
    conn = get_db()
    total = conn.execute("SELECT COUNT(*) as cnt FROM pageviews").fetchone()["cnt"]
    rows = conn.execute("""
        SELECT id, ts, url, referrer, visitor_hash, user_agent, country, device, duration_sec
        FROM pageviews ORDER BY ts DESC LIMIT ? OFFSET ?
    """, (limit, offset)).fetchall()
    conn.close()

    table_rows = []
    for r in rows:
        ua = r["user_agent"] or ""
        is_bot_hit = is_bot(ua)

        if filter == "humans" and is_bot_hit:
            continue
        if filter == "bots" and not is_bot_hit:
            continue

        badge = '<span class="badge bot">bot</span>' if is_bot_hit else '<span class="badge human">human</span>'
        ts_short = r["ts"][:19].replace("T", " ")
        url_short = r["url"][:50] + "..." if len(r["url"]) > 50 else r["url"]
        ref_short = (r["referrer"] or "—")[:30]
        country = r["country"] or "—"
        device = r["device"] or "—"
        duration = f"{r['duration_sec']}s" if r["duration_sec"] else "—"

        table_rows.append(f"""
        <tr>
            <td>{ts_short}</td>
            <td title="{r["url"]}">{url_short}</td>
            <td>{ref_short}</td>
            <td>{country}</td>
            <td>{device}</td>
            <td>{duration}</td>
            <td>{badge}</td>
        </tr>""")

    rows_html = "".join(table_rows) if table_rows else '<tr><td colspan="7">No hits found</td></tr>'
    prev_offset = max(0, offset - limit)
    next_offset = offset + limit

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>tinytrack - logs</title>
<style>
  :root{{--bg:#0d1117;--card:#161b22;--border:#30363d;--text:#c9d1d9;--muted:#8b949e;--accent:#58a6ff;--green:#3fb950;--red:#f85149}}
  body{{font-family:system-ui;margin:0;padding:1.5rem;background:var(--bg);color:var(--text)}}
  .container{{max-width:1400px;margin:0 auto}}
  nav{{display:flex;justify-content:space-between;align-items:center;margin-bottom:1.5rem}}
  nav h1{{font-size:1.4rem;margin:0}}
  nav .links{{display:flex;gap:1rem}}
  nav a{{color:var(--muted);font-size:.85rem;text-decoration:none}}
  nav a:hover{{color:var(--accent)}}
  table{{width:100%;border-collapse:collapse;font-size:.85rem;background:var(--card);border-radius:10px;overflow:hidden}}
  th,td{{text-align:left;padding:.7rem .6rem;border-bottom:1px solid var(--border)}}
  th{{background:var(--bg);font-weight:600;text-transform:uppercase;font-size:.7rem;letter-spacing:.05em;color:var(--muted)}}
  tr:hover td{{background:rgba(88,166,255,0.05)}}
  .badge{{padding:.2rem .5rem;border-radius:4px;font-size:.65rem;font-weight:600;text-transform:uppercase}}
  .badge.human{{background:var(--green);color:#fff}}
  .badge.bot{{background:var(--red);color:#fff}}
  .filters{{margin-bottom:1rem;display:flex;gap:.5rem}}
  .filters a{{padding:.4rem .7rem;border-radius:6px;text-decoration:none;color:var(--muted);font-size:.8rem;border:1px solid var(--border)}}
  .filters a:hover{{border-color:var(--muted)}}
  .filters a.active{{background:var(--accent);color:#fff;border-color:var(--accent)}}
  .pagination{{display:flex;gap:1rem;margin:1.5rem 0;justify-content:center}}
  .pagination a{{padding:.5rem 1rem;border:1px solid var(--border);border-radius:6px;text-decoration:none;color:var(--muted)}}
  .pagination a:hover{{border-color:var(--accent);color:var(--accent)}}
  .meta{{color:var(--muted);font-size:.85rem;margin-bottom:1rem}}
</style>
</head><body>
<div class="container">
<nav><h1>📊 tinytrack / logs</h1><div class="links"><a href="/">dashboard</a><a href="/logout">log out</a></div></nav>

<div class="filters">
  <a href="/logs?filter=all" {"class='active'" if filter=="all" else ""}>all</a>
  <a href="/logs?filter=humans" {"class='active'" if filter=="humans" else ""}>humans</a>
  <a href="/logs?filter=bots" {"class='active'" if filter=="bots" else ""}>bots</a>
</div>

<p class="meta">Showing {offset + 1}–{min(offset + limit, total)} of {total:,} total hits</p>

<table>
  <thead><tr><th>Timestamp</th><th>URL</th><th>Referrer</th><th>Country</th><th>Device</th><th>Duration</th><th>Type</th></tr></thead>
  <tbody>{rows_html}</tbody>
</table>

<div class="pagination">
  {"<a href='/logs?offset=" + str(prev_offset) + "&filter=" + filter + "'>← Previous</a>" if offset > 0 else ""}
  {"<a href='/logs?offset=" + str(next_offset) + "&filter=" + filter + "'>Next →</a>" if offset + limit < total else ""}
</div>
</div>
</body></html>"""
