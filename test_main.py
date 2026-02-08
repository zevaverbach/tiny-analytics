import hashlib

from fastapi.testclient import TestClient

from main import app, get_db, settings, signer


client = TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _send_hit(url="https://example.com/page", origin=None, referer=None, user_agent=None):
    headers = {"Content-Type": "application/json"}
    if origin:
        headers["Origin"] = origin
    if referer:
        headers["Referer"] = referer
    if user_agent:
        headers["User-Agent"] = user_agent
    return client.post("/t", json={"url": url, "referrer": None}, headers=headers)


def _auth_cookies():
    resp = client.post("/login", data={"password": settings.tinytrack_password}, follow_redirects=False)
    return {"tt_session": resp.cookies["tt_session"]}


def _row_count():
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) FROM pageviews").fetchone()[0]
    conn.close()
    return n


# ---------------------------------------------------------------------------
# Tracking
# ---------------------------------------------------------------------------

class TestTracking:
    def test_records_pageview(self):
        resp = _send_hit()
        assert resp.status_code == 204
        assert _row_count() == 1

    def test_multiple_hits(self):
        _send_hit(url="https://example.com/a")
        _send_hit(url="https://example.com/b")
        _send_hit(url="https://example.com/c")
        assert _row_count() == 3

    def test_stores_correct_url(self):
        _send_hit(url="https://example.com/specific-page")
        conn = get_db()
        row = conn.execute("SELECT url FROM pageviews").fetchone()
        conn.close()
        assert row[0] == "https://example.com/specific-page"

    def test_different_user_agents_get_different_hashes(self):
        _send_hit(user_agent="Mozilla/5.0 Chrome")
        _send_hit(user_agent="Mozilla/5.0 Firefox")
        conn = get_db()
        hashes = [r[0] for r in conn.execute("SELECT DISTINCT visitor_hash FROM pageviews").fetchall()]
        conn.close()
        assert len(hashes) == 2

    def test_same_user_agent_gets_same_hash(self):
        _send_hit(user_agent="Mozilla/5.0 Chrome")
        _send_hit(user_agent="Mozilla/5.0 Chrome")
        conn = get_db()
        hashes = [r[0] for r in conn.execute("SELECT DISTINCT visitor_hash FROM pageviews").fetchall()]
        conn.close()
        assert len(hashes) == 1

    def test_visitor_hash_is_sha256_prefix(self):
        _send_hit(user_agent="TestAgent")
        conn = get_db()
        stored_hash = conn.execute("SELECT visitor_hash FROM pageviews").fetchone()[0]
        conn.close()
        # TestClient uses testclient as the host IP
        expected = hashlib.sha256("testclient:TestAgent".encode()).hexdigest()[:16]
        assert stored_hash == expected


# ---------------------------------------------------------------------------
# Origin validation
# ---------------------------------------------------------------------------

class TestOriginValidation:
    def setup_method(self):
        settings.tinytrack_allowed_origins = ["https://allowed.com"]

    def test_rejects_no_origin(self):
        resp = _send_hit()
        assert resp.status_code == 403

    def test_rejects_wrong_origin(self):
        resp = _send_hit(origin="https://evil.com")
        assert resp.status_code == 403

    def test_accepts_correct_origin(self):
        resp = _send_hit(origin="https://allowed.com")
        assert resp.status_code == 204

    def test_referer_fallback(self):
        resp = _send_hit(referer="https://allowed.com/some/deep/page")
        assert resp.status_code == 204

    def test_nothing_stored_on_reject(self):
        _send_hit(origin="https://evil.com")
        assert _row_count() == 0

    def test_open_when_no_origins_configured(self):
        settings.tinytrack_allowed_origins = []
        resp = _send_hit()
        assert resp.status_code == 204


# ---------------------------------------------------------------------------
# Snippet
# ---------------------------------------------------------------------------

class TestSnippet:
    def test_returns_javascript(self):
        resp = client.get("/snippet.js")
        assert resp.status_code == 200
        assert "application/javascript" in resp.headers["content-type"]

    def test_contains_fetch_call(self):
        resp = client.get("/snippet.js")
        body = resp.text
        assert "fetch(" in body
        assert "/t" in body
        assert "POST" in body

    def test_is_an_iife(self):
        resp = client.get("/snippet.js")
        assert resp.text.startswith("(function(){")
        assert resp.text.rstrip().endswith("})();")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

class TestAuth:
    def test_dashboard_requires_login(self):
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"

    def test_login_page_renders(self):
        resp = client.get("/login")
        assert resp.status_code == 200
        assert "<form" in resp.text
        assert 'type="password"' in resp.text

    def test_login_correct_password(self):
        resp = client.post("/login", data={"password": settings.tinytrack_password}, follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/"
        assert "tt_session" in resp.cookies

    def test_login_wrong_password(self):
        resp = client.post("/login", data={"password": "wrong"}, follow_redirects=False)
        assert resp.status_code == 401

    def test_bad_session_cookie_rejected(self):
        resp = client.get("/", cookies={"tt_session": "garbage"}, follow_redirects=False)
        assert resp.status_code == 303

    def test_logout_clears_cookie(self):
        resp = client.get("/logout", follow_redirects=False)
        assert resp.status_code == 303
        assert resp.headers["location"] == "/login"


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

class TestDashboard:
    def test_shows_zero_state(self):
        resp = client.get("/", cookies=_auth_cookies())
        assert resp.status_code == 200
        assert "Unique Visitors" in resp.text
        assert "Page Views" in resp.text

    def test_counts_after_hits(self):
        _send_hit(user_agent="A")
        _send_hit(user_agent="A")
        _send_hit(user_agent="B")
        resp = client.get("/", cookies=_auth_cookies())
        # 2 unique visitors, 3 pageviews
        assert ">2</div>" in resp.text  # uniques
        assert ">3</div>" in resp.text  # pageviews

    def test_contains_chart(self):
        resp = client.get("/", cookies=_auth_cookies())
        assert "chart.js" in resp.text.lower() or "Chart(" in resp.text

    def test_contains_snippet_embed_instructions(self):
        resp = client.get("/", cookies=_auth_cookies())
        assert "snippet.js" in resp.text

    def test_period_links(self):
        resp = client.get("/", cookies=_auth_cookies())
        assert "?hours=6" in resp.text
        assert "?hours=24" in resp.text
        assert "?hours=168" in resp.text

    def test_hours_param_accepted(self):
        resp = client.get("/?hours=6", cookies=_auth_cookies())
        assert resp.status_code == 200

    def test_interval_param_accepted(self):
        resp = client.get("/?hours=24&interval=15m", cookies=_auth_cookies())
        assert resp.status_code == 200
