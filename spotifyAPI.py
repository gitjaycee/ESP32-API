import os
import json
import re
import logging

import redis
import requests
import spotipy
from flask import Flask, jsonify, redirect, request
from spotipy.oauth2 import SpotifyOAuth, CacheHandler

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

# ── App ───────────────────────────────────────────────────────────────────────
app = Flask(__name__)
app.secret_key = os.environ["SECRET_KEY"]

# ── Config (all from environment variables) ───────────────────────────────────
CLIENT_ID     = os.environ["SPOTIFY_CLIENT_ID"]
CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
REDIRECT_URI  = os.environ["SPOTIFY_REDIRECT_URI"]   # e.g. https://your-app.onrender.com/callback
SCOPE         = "user-read-currently-playing user-read-playback-state"

# ── Redis — used for token persistence and lyrics cache ───────────────────────
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")
rdb = redis.from_url(REDIS_URL, decode_responses=True)

TOKEN_KEY        = "spotify:token"
LYRICS_TRACK_KEY = "spotify:lyrics:track_id"
LYRICS_DATA_KEY  = "spotify:lyrics:data"

# ── Redis-backed token cache (replaces .spotify_cache file) ───────────────────
class RedisTokenCache(CacheHandler):
    """Stores the Spotify token dict in Redis so it survives Render restarts."""

    def get_cached_token(self):
        raw = rdb.get(TOKEN_KEY)
        return json.loads(raw) if raw else None

    def save_token_to_cache(self, token_info):
        rdb.set(TOKEN_KEY, json.dumps(token_info))


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_oauth() -> SpotifyOAuth:
    return SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE,
        cache_handler=RedisTokenCache(),
        open_browser=False,
        show_dialog=False,
    )


def get_sp() -> spotipy.Spotify | None:
    """Return an authenticated Spotify client, refreshing the token if needed."""
    oauth = make_oauth()
    token_info = oauth.get_cached_token()
    if not token_info:
        return None
    if oauth.is_token_expired(token_info):
        token_info = oauth.refresh_access_token(token_info["refresh_token"])
    return spotipy.Spotify(auth=token_info["access_token"])


def get_synced_lyrics(artist: str, title: str) -> list[dict]:
    """Fetch synced (LRC) lyrics from lrclib.net, falling back to plain lyrics."""
    try:
        resp = requests.get(
            "https://lrclib.net/api/get",
            params={"artist_name": artist, "track_name": title},
            timeout=5,
        )
        if resp.status_code == 200:
            data = resp.json()
            lrc = data.get("syncedLyrics") or data.get("plainLyrics", "")
            return parse_lrc(lrc)
    except Exception as exc:
        log.warning("Lyrics fetch error: %s", exc)
    return []


def parse_lrc(lrc_text: str) -> list[dict]:
    """Parse LRC format into a list of {ms, text} dicts sorted by timestamp."""
    lines = []
    for line in lrc_text.splitlines():
        m = re.match(r"\[(\d+):(\d+[\.\d]*)\](.*)", line)
        if m:
            minutes, seconds, text = m.groups()
            ms = int(minutes) * 60_000 + int(float(seconds) * 1000)
            lines.append({"ms": ms, "text": text.strip()})
    return sorted(lines, key=lambda x: x["ms"])


def get_cached_lyrics(track_id: str, artist: str, title: str) -> list[dict]:
    """Return lyrics from Redis cache, fetching from lrclib only on a track change."""
    cached_id = rdb.get(LYRICS_TRACK_KEY)
    if cached_id == track_id:
        raw = rdb.get(LYRICS_DATA_KEY)
        return json.loads(raw) if raw else []

    lyrics = get_synced_lyrics(artist, title)
    rdb.set(LYRICS_TRACK_KEY, track_id)
    rdb.set(LYRICS_DATA_KEY, json.dumps(lyrics))
    log.info("🎵 Now playing: %s - %s (%d lyric lines)", artist, title, len(lyrics))
    return lyrics


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return (
        "<h2>ESP32 Spotify Server ✅</h2>"
        "<p><a href='/login'>Login with Spotify</a></p>"
        "<p>After login: <a href='/now'>/now</a></p>"
    )


@app.route("/login")
def login():
    url = make_oauth().get_authorize_url()
    return redirect(url)


@app.route("/callback")
def callback():
    error = request.args.get("error")
    if error:
        log.error("Spotify auth error: %s", error)
        return f"<h2>❌ Spotify error: {error}</h2>", 400

    code = request.args.get("code")
    if not code:
        return "<h2>❌ No code received. Try <a href='/login'>/login</a> again.</h2>", 400

    try:
        token_info = make_oauth().get_access_token(code, as_dict=True, check_cache=False)
        if token_info:
            log.info("✅ Token saved to Redis")
            return (
                "<h2>✅ Spotify connected!</h2>"
                "<p>You can close this tab. Play a song and poll "
                "<a href='/now'>/now</a>.</p>"
            )
        return "<h2>❌ Failed to get token — check Client ID / Secret</h2>", 500
    except Exception as exc:
        log.exception("Callback exception")
        return f"<h2>❌ Exception: {exc}</h2>", 500


@app.route("/now")
def now_playing():
    sp = get_sp()
    if not sp:
        return jsonify({
            "error": "not_authenticated",
            "fix":   f"Visit {REDIRECT_URI.replace('/callback', '/login')} to authenticate",
        }), 401

    try:
        current = sp.current_playback()
    except Exception as exc:
        log.exception("Playback fetch failed")
        return jsonify({"error": str(exc)}), 500

    if not current or not current.get("is_playing"):
        return jsonify({"playing": False})

    item     = current["item"]
    track_id = item["id"]
    title    = item["name"]
    artist   = item["artists"][0]["name"]
    progress = current["progress_ms"]

    lyrics   = get_cached_lyrics(track_id, artist, title)

    lyric_line = ""
    for line in lyrics:
        if line["ms"] <= progress:
            lyric_line = line["text"]
        else:
            break

    return jsonify({
        "playing":  True,
        "title":    title,
        "artist":   artist,
        "progress": progress,
        "lyric":    lyric_line,
    })


# ── Entry point (local dev only — Render uses gunicorn) ───────────────────────
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8888)), debug=False)
