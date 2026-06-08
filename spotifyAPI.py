from flask import Flask, jsonify, redirect, request
import spotipy
from spotipy.oauth2 import SpotifyOAuth, CacheFileHandler
import requests, re, os
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

# ============================================================
# CONFIG — set these in your .env file
# ============================================================
CLIENT_ID     = os.environ["SPOTIFY_CLIENT_ID"]
CLIENT_SECRET = os.environ["SPOTIFY_CLIENT_SECRET"]
REDIRECT_URI  = os.environ.get("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
SCOPE         = "user-read-currently-playing user-read-playback-state"
# ============================================================

def make_oauth():
    handler = CacheFileHandler(cache_path=".spotify_cache")
    return SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPE,
        cache_handler=handler,
        open_browser=False
    )

def get_sp():
    oauth = make_oauth()
    token_info = oauth.get_cached_token()
    if not token_info:
        return None
    if oauth.is_token_expired(token_info):
        token_info = oauth.refresh_access_token(token_info["refresh_token"])
    return spotipy.Spotify(auth=token_info["access_token"])

def get_synced_lyrics(artist, title):
    try:
        resp = requests.get(
            "https://lrclib.net/api/get",
            params={"artist_name": artist, "track_name": title},
            timeout=5
        )
        if resp.status_code == 200:
            data = resp.json()
            lrc = data.get("syncedLyrics") or data.get("plainLyrics", "")
            return parse_lrc(lrc)
    except Exception as e:
        print(f"Lyrics error: {e}")
    return []

def parse_lrc(lrc_text):
    lines = []
    for line in lrc_text.splitlines():
        m = re.match(r"\[(\d+):(\d+[\.\d]*)\](.*)", line)
        if m:
            minutes, seconds, text = m.groups()
            ms = int(minutes) * 60000 + int(float(seconds) * 1000)
            lines.append({"ms": ms, "text": text.strip()})
    return sorted(lines, key=lambda x: x["ms"])

# In-memory cache so we don't re-fetch lyrics on every poll
lyrics_cache = {"track_id": None, "lyrics": []}

# ------------------------------------------------------------
# Routes
# ------------------------------------------------------------

@app.route("/")
def index():
    return (
        "<h2>ESP32 Spotify Server is running ✅</h2>"
        "<p><a href='/login'>Click here to login with Spotify</a></p>"
        "<p>After logging in, test: <a href='/now'>/now</a></p>"
    )

@app.route("/login")
def login():
    oauth = make_oauth()
    url = oauth.get_authorize_url()
    print(f"\n>>> Redirecting to Spotify auth...\n")
    return redirect(url)

@app.route("/callback")
def callback():
    error = request.args.get("error")
    if error:
        print(f"Spotify returned error: {error}")
        return f"<h2>❌ Spotify error: {error}</h2>", 400

    code = request.args.get("code")
    if not code:
        return "<h2>❌ No code received from Spotify. Try /login again.</h2>", 400

    try:
        oauth = make_oauth()
        token_info = oauth.get_access_token(code, as_dict=True, check_cache=False)
        if token_info:
            print("✅ Token saved! ESP32 can now poll /now")
            return (
                "<h2>✅ Spotify connected!</h2>"
                "<p>You can close this tab.</p>"
                "<p>Play a song on Spotify, then visit "
                "<a href='/now'>/now</a> to verify.</p>"
            )
        else:
            return "<h2>❌ Failed to get token — check your Client ID and Secret</h2>", 500
    except Exception as e:
        print(f"Callback exception: {e}")
        return f"<h2>❌ Exception during token exchange: {e}</h2>", 500

@app.route("/now")
def now_playing():
    sp = get_sp()
    if not sp:
        return jsonify({
            "error": "not_authenticated",
            "fix": "Visit http://127.0.0.1:8888/login in your browser first"
        }), 401

    try:
        current = sp.current_playback()
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    if not current or not current.get("is_playing"):
        return jsonify({"playing": False})

    item     = current["item"]
    track_id = item["id"]
    title    = item["name"]
    artist   = item["artists"][0]["name"]
    progress = current["progress_ms"]

    # Re-fetch lyrics only when the track changes
    if lyrics_cache["track_id"] != track_id:
        lyrics_cache["track_id"] = track_id
        lyrics_cache["lyrics"]   = get_synced_lyrics(artist, title)
        print(f"🎵 Now playing: {artist} - {title} "
              f"({len(lyrics_cache['lyrics'])} lyric lines)")

    # Find the current lyric line based on playback position
    lyric_line = ""
    for line in lyrics_cache["lyrics"]:
        if line["ms"] <= progress:
            lyric_line = line["text"]
        else:
            break

    return jsonify({
        "playing":  True,
        "title":    title,
        "artist":   artist,
        "progress": progress,
        "lyric":    lyric_line
    })

# ------------------------------------------------------------
# Entry point
# ------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 50)
    print("  ESP32 Spotify Server")
    print("=" * 50)
    print("Step 1: Visit http://127.0.0.1:8888/login")
    print("        to authenticate with Spotify")
    print("Step 2: Play a song on Spotify")
    print("Step 3: Test http://127.0.0.1:8888/now")
    print("Step 4: Point your ESP32 to YOUR PC's")
    print("        local IP (e.g. 192.168.254.104)")
    print("        NOT 127.0.0.1 !")
    print("=" * 50)
    app.run(host="0.0.0.0", port=8888, debug=True)