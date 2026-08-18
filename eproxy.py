"""
Streamzy EProxy - HTTPS streaming reverse proxy.

Supports eporner.com (mp4 direct) and surrit.com/missav (hls m3u8 remux to mp4).

Routes:
  GET /                          Status JSON
  GET /health                    Health check
  GET /player                    Web player UI (no auth required)
  GET /api/status                Auth status for frontend
  GET /login                     Auth login page
  POST /login                    Validate token, set session cookie
  GET /logout                    Clear session
  GET /proxy?url=<encoded>       Proxy with header injection
  GET /direct?url=<encoded>      Single-file direct stream (hls->mp4 via ffmpeg)
  GET /download?url=<encoded>    Download as attachment (hls->mp4 via ffmpeg)

All media routes require STREAMZY_TOKEN auth when the env var is set.
"""

import hmac
import ipaddress
import os
import re
import secrets
import shutil
import socket
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse, urljoin, quote, unquote

import requests
from flask import (
    Flask, request, Response, stream_with_context, jsonify, session, redirect
)

try:
    import cloudscraper
except ImportError:
    cloudscraper = None

# ── Config ────────────────────────────────────────────────────────────────────

ALLOWED_HOSTS = {"surrit.com", "eporner.com", "gvideo.eporner.com"}
BLOCKED_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("::/128"),
]

BASE_DIR = Path(__file__).resolve().parent
STREAMZY_TOKEN = os.environ.get("STREAMZY_TOKEN")
SURRIT_REFERER = os.environ.get("SURRIT_REFERER", "https://missav.ws/")
PROXY_BASE = os.environ.get("PROXY_INTERNAL_BASE", "http://127.0.0.1:8989")
FFMPEG = shutil.which("ffmpeg")
MAX_FFMPEG = int(os.environ.get("MAX_FFMPEG", "5"))

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

_URI_RE = re.compile(r'URI="([^"]+)"')
_RESP_EXCLUDE = {"content-encoding", "transfer-encoding", "connection"}

# ── App ───────────────────────────────────────────────────────────────────────

app = Flask(__name__)
app.secret_key = STREAMZY_TOKEN.encode() if STREAMZY_TOKEN else secrets.token_bytes(32)
_ffmpeg_sem = threading.Semaphore(MAX_FFMPEG)

# ── Cookie loading ────────────────────────────────────────────────────────────


def _load_cookies():
    p = BASE_DIR / "cookies.txt"
    try:
        c = p.read_text().strip()
    except OSError:
        return None
    if not c:
        return None
    parts = [x.strip() for x in c.replace("\r", ";").replace("\n", ";").split(";") if x.strip()]
    return "; ".join(parts)


_EPORNER_COOKIE = os.environ.get("EPORNER_COOKIE") or _load_cookies()

# ── Helpers ───────────────────────────────────────────────────────────────────


def _log(msg):
    print(f"[streamzy] {msg}", flush=True)


def _make_session():
    if cloudscraper is not None:
        return cloudscraper.create_scraper(
            browser={"browser": "chrome", "platform": "windows", "mobile": False}
        )
    return requests.Session()


def _encode(url):
    return quote(url, safe="")


def _decode(encoded):
    return unquote(encoded)


def _safe_ip(ip_str):
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    for net in BLOCKED_NETS:
        if ip in net:
            return False
    return not (ip.is_multicast or ip.is_reserved)


def _validate_target(url):
    """Returns (target_url, None) or (None, (error_response, status_code))."""
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return None, (jsonify({"error": "only HTTPS targets allowed"}), 400)
    host = (parsed.hostname or "").lower()
    if not host:
        return None, (jsonify({"error": "invalid hostname"}), 400)
    if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        return None, (jsonify({"error": f"host not allowed: {host}"}), 400)
    try:
        for _, _, _, _, sa in socket.getaddrinfo(host, None):
            if not _safe_ip(sa[0]):
                return None, (jsonify({"error": f"blocked IP: {sa[0]}"}), 400)
    except socket.gaierror:
        return None, (jsonify({"error": f"DNS failed: {host}"}), 502)
    return url, None


def _upstream_headers(target_url):
    p = urlparse(target_url)
    host = (p.hostname or "").lower()
    h = {"User-Agent": UA, "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9"}
    h["Referer"] = SURRIT_REFERER if "surrit.com" in host else f"{p.scheme}://{p.netloc}/"
    if "eporner.com" in host and _EPORNER_COOKIE:
        h["Cookie"] = _EPORNER_COOKIE
    return h


def _stream_response(r, extra_headers=None):
    """Build response headers from upstream, excluding hop-by-hop headers."""
    headers = [(k, v) for k, v in r.headers.items() if k.lower() not in _RESP_EXCLUDE]
    if extra_headers:
        headers.extend(extra_headers)
    return headers


def _close(sess, r):
    try:
        r.close()
    except Exception:
        pass
    try:
        sess.close()
    except Exception:
        pass


def _cleanup_dir(path):
    try:
        shutil.rmtree(path, ignore_errors=True)
    except Exception:
        pass


# ── CORS ──────────────────────────────────────────────────────────────────────


@app.after_request
def _cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
    resp.headers["Access-Control-Allow-Methods"] = "GET, HEAD, OPTIONS, POST"
    return resp


# ── Auth ──────────────────────────────────────────────────────────────────────


def _auth():
    if not STREAMZY_TOKEN:
        return None
    a = request.headers.get("Authorization", "")
    if a.startswith("Bearer ") and hmac.compare_digest(a[7:], STREAMZY_TOKEN):
        return None
    t = request.args.get("_token")
    if t and hmac.compare_digest(t, STREAMZY_TOKEN):
        return None
    if session.get("authed"):
        return None
    return jsonify({"error": "unauthorized"}), 401


# ── Routes: basic ─────────────────────────────────────────────────────────────


@app.route("/")
def index():
    return jsonify({
        "status": "online",
        "service": "Streamzy (EProxy)",
        "auth": bool(STREAMZY_TOKEN),
        "ffmpeg": bool(FFMPEG),
        "endpoints": {
            "proxy": "/proxy?url=<encoded_url>",
            "direct": "/direct?url=<encoded_url>",
            "download": "/download?url=<encoded_url>",
            "player": "/player",
        },
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/status")
def api_status():
    return jsonify({
        "auth_required": bool(STREAMZY_TOKEN),
        "authenticated": session.get("authed", False),
    })


# ── Routes: auth ──────────────────────────────────────────────────────────────

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Streamzy Login</title>
<style>
  body{margin:0;min-height:100vh;display:flex;align-items:center;justify-content:center;
       background:#0d1117;color:#e6edf3;font-family:system-ui}
  .box{background:#161b22;padding:32px;border-radius:12px;width:min(90vw,360px);text-align:center}
  input{width:100%;padding:10px;border-radius:8px;border:1px solid #30363d;
        background:#0d1117;color:#e6edf3;font-size:14px;box-sizing:border-box;margin-bottom:12px}
  button{width:100%;padding:10px;border:none;border-radius:8px;background:#238636;
         color:#fff;font-size:14px;cursor:pointer}
  #err{color:#f85149;margin-top:8px;font-size:13px;min-height:1.2em}
</style></head><body>
<div class="box">
  <h2 style="margin-top:0">Streamzy</h2>
  <input id="tok" type="password" placeholder="Token" autofocus>
  <button onclick="login()">Login</button>
  <div id="err"></div>
</div>
<script>
async function login(){
  const t=document.getElementById('tok').value;
  try{
    const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({token:t})});
    if(r.ok){location.href='/player';}
    else{document.getElementById('err').textContent='Invalid token';}
  }catch(e){document.getElementById('err').textContent='Connection error';}
}
document.getElementById('tok').addEventListener('keydown',e=>{if(e.key==='Enter')login()});
</script></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login():
    if not STREAMZY_TOKEN:
        return redirect("/")
    if request.method == "GET":
        return Response(_LOGIN_HTML, mimetype="text/html")
    data = request.get_json(silent=True) or {}
    token = data.get("token") or request.form.get("token")
    if token and hmac.compare_digest(token, STREAMZY_TOKEN):
        session["authed"] = True
        return jsonify({"ok": True})
    return jsonify({"error": "invalid token"}), 401


@app.route("/logout")
def logout():
    session.clear()
    return redirect("/login")


# ── Routes: player ────────────────────────────────────────────────────────────

_PLAYER_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Streamzy Player</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1"></script>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; flex-direction: column;
    align-items: center; justify-content: center; gap: 16px; padding: 20px;
    background: #0d1117; color: #e6edf3;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  }
  h1 { margin: 0; font-size: 20px; }
  .hidden { display: none !important; }
  form { display: flex; gap: 8px; width: min(90vw, 760px); }
  input {
    flex: 1; padding: 10px 14px; border-radius: 8px;
    border: 1px solid #30363d; background: #161b22; color: #e6edf3;
    font-size: 14px; outline: none;
  }
  input:focus { border-color: #58a6ff; }
  button {
    padding: 10px 20px; border: none; border-radius: 8px;
    background: #238636; color: #fff; font-size: 14px; cursor: pointer;
  }
  button:hover { background: #2ea043; }
  video { width: min(90vw, 960px); max-height: 70vh; border-radius: 12px; background: #000; }
  #info { font-size: 13px; color: #8b949e; word-break: break-all; max-width: 90vw; text-align: center; }
  .link-box { display: flex; gap: 6px; width: min(90vw, 760px); align-items: center; }
  .link-box input { flex: 1; font-size: 12px; background: #161b22; color: #8b949e; }
  .link-box button { flex: none; padding: 8px 14px; font-size: 12px; background: #30363d; }
  .link-box button:hover { background: #484f58; }
  a.dl { color: #58a6ff; font-size: 13px; text-decoration: none; }
  a.dl:hover { text-decoration: underline; }
</style>
</head>
<body>
  <h1 id="login-title" class="hidden">Streamzy Login</h1>
  <div id="login-form" class="hidden">
    <input id="tok" type="password" placeholder="Token" autofocus>
    <button onclick="doLogin()">Login</button>
    <div id="err" style="color:#f85149;font-size:13px;min-height:1.2em"></div>
  </div>

  <h1 id="player-title" class="hidden">Streamzy Player</h1>
  <form id="player-form" class="hidden">
    <input id="url" type="text" placeholder="Paste m3u8 or mp4 URL here" autofocus>
    <button type="submit">Load</button>
  </form>
  <video id="v" controls playsinline class="hidden"></video>
  <div id="link-box" class="link-box hidden">
    <input id="direct-url" readonly placeholder="Direct link">
    <button onclick="copyLink()">Copy</button>
    <a id="dl-link" class="dl" href="#" target="_blank">Download</a>
  </div>
  <div id="info"></div>

<script>
const video = document.getElementById('v');
const info = document.getElementById('info');
let hls = null;

async function init() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    if (d.auth_required && !d.authenticated) {
      document.getElementById('login-title').classList.remove('hidden');
      document.getElementById('login-form').classList.remove('hidden');
      return;
    }
  } catch(e) {}
  showPlayer();
}

function showPlayer() {
  document.getElementById('login-title').classList.add('hidden');
  document.getElementById('login-form').classList.add('hidden');
  document.getElementById('player-title').classList.remove('hidden');
  document.getElementById('player-form').classList.remove('hidden');
  video.classList.remove('hidden');
}

async function doLogin() {
  const t = document.getElementById('tok').value;
  try {
    const r = await fetch('/login', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({token: t})});
    if (r.ok) showPlayer();
    else document.getElementById('err').textContent = 'Invalid token';
  } catch(e) { document.getElementById('err').textContent = 'Connection error'; }
}
document.getElementById('tok')?.addEventListener('keydown', e => { if (e.key==='Enter') doLogin(); });

function enc(url) { return encodeURIComponent(url); }

function play(url) {
  if (hls) { hls.destroy(); hls = null; }
  video.src = '';
  info.textContent = '';

  const isM3u8 = /\\.m3u8(\\?|$)/i.test(url);
  const proxyUrl = '/proxy?url=' + enc(url);
  const directUrl = '/direct?url=' + enc(url);

  document.getElementById('direct-url').value = directUrl;
  document.getElementById('dl-link').href = '/download?url=' + enc(url);

  if (isM3u8) {
    if (window.Hls && Hls.isSupported()) {
      hls = new Hls({ maxBufferLength: 30 });
      hls.loadSource(proxyUrl);
      hls.attachMedia(video);
      hls.on(Hls.Events.MANIFEST_PARSED, () => video.play().catch(() => {}));
      hls.on(Hls.Events.ERROR, (e, d) => {
        if (d.fatal) { info.textContent = 'Error: ' + d.type; hls.destroy(); hls = null; }
      });
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = proxyUrl;
      video.play().catch(() => {});
    } else {
      info.textContent = 'HLS not supported in this browser';
    }
  } else {
    video.src = proxyUrl;
    video.play().catch(() => {});
  }
}

document.getElementById('player-form').addEventListener('submit', e => {
  e.preventDefault();
  let url = document.getElementById('url').value.trim();
  if (!url) return;
  if (!/^https?:\\/\\//i.test(url)) url = 'https://' + url;
  const isDirect = /\\.mp4(\\?|$)/i.test(url) || /\\.m3u8(\\?|$)/i.test(url);
  if (isDirect) {
    play(url);
  } else {
    info.textContent = 'Extracting video from page...';
    fetch('/extract?url=' + enc(url))
      .then(r => r.json())
      .then(d => {
        if (d.error) { info.textContent = 'Error: ' + d.error; return; }
        document.getElementById('url').value = d.url;
        play(d.url);
      })
      .catch(() => { info.textContent = 'Extraction failed'; });
  }
});

function copyLink() {
  const el = document.getElementById('direct-url');
  el.select();
  navigator.clipboard.writeText(el.value).catch(() => {});
}

const initial = new URLSearchParams(location.search).get('url');
if (initial) {
  document.getElementById('url').value = initial;
  init().then(() => play(initial));
} else {
  init();
}
</script>
</body>
</html>"""


@app.route("/player")
def player():
    return Response(_PLAYER_HTML, mimetype="text/html")


# ── Routes: media ─────────────────────────────────────────────────────────────


@app.route("/extract")
def extract():
    auth_err = _auth()
    if auth_err:
        return auth_err

    raw = request.args.get("url")
    if not raw:
        return jsonify({"error": "missing url parameter"}), 400

    target = _decode(raw)
    parsed = urlparse(target)
    host = (parsed.hostname or "").lower()

    if not any(host == h or host.endswith("." + h) for h in ALLOWED_HOSTS):
        return jsonify({"error": f"host not allowed: {host}"}), 400

    if not target.lower().endswith(".mp4") and not target.lower().endswith(".m3u8"):
        # It's a page URL — fetch and extract
        headers = _upstream_headers(target)
        sess = _make_session()
        try:
            r = sess.get(target, headers=headers, allow_redirects=True, timeout=30)
            if r.status_code != 200:
                _close(sess, r)
                return jsonify({"error": f"upstream {r.status_code}"}), 502
            html = r.text
            _close(sess, r)
        except Exception as e:
            _close(sess, None)
            return jsonify({"error": str(e)}), 502

        # Try /dload/ paths first (highest quality)
        dloads = re.findall(r'/dload/([^\s"<>\']+\.mp4)', html, re.I)
        if dloads:
            best = sorted(dloads, key=lambda x: int(re.search(r'(\d+)p', x).group(1)) if re.search(r'(\d+)p', x) else 0, reverse=True)[0]
            mp4 = f"{parsed.scheme}://{parsed.netloc}/dload/{best}"
            return jsonify({"url": mp4, "source": target})

        # Fallback to <source> or regex
        mp4 = None
        m = re.search(r'<source[^>]+src="([^"]+\.mp4[^"]*)"', html, re.I)
        if m:
            mp4 = m.group(1)
        if not mp4:
            m = re.search(r'(https?://[^\s"\'<>]+\.mp4(?:\?[^\s"\'<>]*)?)', html, re.I)
            if m:
                mp4 = m.group(1)
        if not mp4:
            return jsonify({"error": "no mp4 found on page"}), 404

        if mp4.startswith("//"):
            mp4 = "https:" + mp4
        elif mp4.startswith("/"):
            mp4 = f"{parsed.scheme}://{parsed.netloc}{mp4}"

        return jsonify({"url": mp4, "source": target})

    # Already a direct URL — return as-is
    return jsonify({"url": target, "source": target})


@app.route("/proxy")
def proxy():
    auth_err = _auth()
    if auth_err:
        return auth_err

    raw = request.args.get("url")
    if not raw:
        return jsonify({"error": "missing url parameter"}), 400

    target = _decode(raw)
    target, err = _validate_target(target)
    if err:
        return err

    headers = _upstream_headers(target)
    sess = _make_session()
    try:
        r = sess.get(target, headers=headers, stream=True, allow_redirects=True, timeout=30)
    except Exception as e:
        _close(sess, r if "r" in dir() else None)
        return jsonify({"error": str(e)}), 502

    ct = r.headers.get("Content-Type", "")
    is_m3u8 = "mpegurl" in ct.lower() or target.lower().endswith(".m3u8")

    if r.status_code == 200 and is_m3u8:
        try:
            content = r.content.decode("utf-8", errors="replace")
        except Exception as e:
            _close(sess, r)
            return jsonify({"error": str(e)}), 502
        _close(sess, r)
        rewritten = _rewrite_proxy(content, target)
        return Response(rewritten, status=200, content_type="application/vnd.apple.mpegurl")

    resp_headers = _stream_response(r)

    def generate():
        try:
            yield from r.iter_content(chunk_size=1024 * 1024)
        finally:
            _close(sess, r)

    return Response(stream_with_context(generate()), status=r.status_code, headers=resp_headers)


@app.route("/direct")
def direct():
    auth_err = _auth()
    if auth_err:
        return auth_err

    raw = request.args.get("url")
    if not raw:
        return jsonify({"error": "missing url parameter"}), 400

    target = _decode(raw)
    target, err = _validate_target(target)
    if err:
        return err

    headers = _upstream_headers(target)
    return _serve_m3u8_or_stream(target, headers, "inline")


@app.route("/download")
def download():
    auth_err = _auth()
    if auth_err:
        return auth_err

    raw = request.args.get("url")
    if not raw:
        return jsonify({"error": "missing url parameter"}), 400

    target = _decode(raw)
    target, err = _validate_target(target)
    if err:
        return err

    filename = urlparse(target).path.rsplit("/", 1)[-1] or "download"
    headers = _upstream_headers(target)
    return _serve_m3u8_or_stream(target, headers, f'attachment; filename="{filename}"')


# ── HLS rewrite ───────────────────────────────────────────────────────────────


def _rewrite_proxy(content, base_url):
    """Rewrite m3u8 for browser playback: segments route through /proxy."""
    out = []
    for line in content.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            if "URI=" in s:
                def _repl(m, bu=base_url):
                    u = urljoin(bu, m.group(1))
                    return f'URI="/proxy?url={_encode(u)}"'
                s = _URI_RE.sub(_repl, s)
            out.append(s)
        else:
            u = urljoin(base_url, s)
            out.append(f"/proxy?url={_encode(u)}")
    return "\n".join(out)


def _rewrite_direct(content, base_url):
    """Rewrite m3u8 for ffmpeg: segments route through absolute proxy URL."""
    out = []
    for line in content.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.startswith("#"):
            if "URI=" in s:
                def _repl(m, bu=base_url):
                    u = urljoin(bu, m.group(1))
                    return f'URI="{PROXY_BASE}/proxy?url={_encode(u)}"'
                s = _URI_RE.sub(_repl, s)
            out.append(s)
        else:
            u = urljoin(base_url, s)
            out.append(f"{PROXY_BASE}/proxy?url={_encode(u)}")
    return "\n".join(out)


# ── FFmpeg ────────────────────────────────────────────────────────────────────


def _serve_m3u8_or_stream(target, headers, disposition):
    """Serve a target URL. m3u8 -> ffmpeg remux to mp4. Other -> stream through."""
    sess = _make_session()
    try:
        r = sess.get(target, headers=headers, stream=True, allow_redirects=True, timeout=30)
    except Exception as e:
        _close(sess, None)
        return jsonify({"error": str(e)}), 502

    ct = r.headers.get("Content-Type", "")
    is_m3u8 = "mpegurl" in ct.lower() or target.lower().endswith(".m3u8")

    if not is_m3u8:
        resp_headers = _stream_response(r)

        def generate():
            try:
                yield from r.iter_content(chunk_size=1024 * 1024)
            finally:
                _close(sess, r)

        return Response(stream_with_context(generate()), status=r.status_code, headers=resp_headers)

    # m3u8 -> ffmpeg remux
    if not FFMPEG:
        _close(sess, r)
        return jsonify({"error": "ffmpeg not available"}), 503

    try:
        content = r.content.decode("utf-8", errors="replace")
    except Exception as e:
        _close(sess, r)
        return jsonify({"error": str(e)}), 502
    finally:
        _close(sess, r)

    if not content.strip().startswith("#EXTM3U"):
        return jsonify({"error": "invalid m3u8"}), 400

    playlist = _rewrite_direct(content, target)
    tmp_dir = tempfile.mkdtemp(prefix="streamzy-")
    pl_path = os.path.join(tmp_dir, "playlist.m3u8")
    err_path = os.path.join(tmp_dir, "stderr.log")

    with open(pl_path, "w") as f:
        f.write(playlist)

    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-protocol_whitelist", "file,http,https,tcp,tls,crypto,data",
        "-i", pl_path,
        "-c", "copy",
        "-bsf:a", "aac_adtstoasc",
        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
        "-f", "mp4",
        "pipe:1",
    ]

    if not _ffmpeg_sem.acquire(timeout=10):
        _cleanup_dir(tmp_dir)
        return jsonify({"error": "server busy, try again"}), 503

    try:
        ef = open(err_path, "w")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=ef, preexec_fn=os.setsid)
    except Exception as e:
        _ffmpeg_sem.release()
        _cleanup_dir(tmp_dir)
        return jsonify({"error": str(e)}), 500

    # Read first chunk to verify ffmpeg produced output
    first_chunk = proc.stdout.read(1024 * 1024)
    if not first_chunk:
        try:
            proc.kill()
        except Exception:
            pass
        proc.wait()
        ef.close()
        _ffmpeg_sem.release()
        _cleanup_dir(tmp_dir)
        return jsonify({"error": "ffmpeg produced no output"}), 502

    resp_headers = [
        ("Content-Type", "video/mp4"),
        ("Content-Disposition", disposition),
    ]

    def generate():
        try:
            yield first_chunk
            while True:
                chunk = proc.stdout.read(1024 * 1024)
                if not chunk:
                    break
                yield chunk
        finally:
            try:
                proc.stdout.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            ef.close()
            # Log ffmpeg stderr if non-empty
            try:
                with open(err_path) as f:
                    err = f.read()
                if err.strip():
                    _log(f"ffmpeg {_redact(target)}: {err[-500:]}")
            except Exception:
                pass
            _ffmpeg_sem.release()
            _cleanup_dir(tmp_dir)

    return Response(stream_with_context(generate()), status=200, headers=resp_headers)


def _redact(url):
    p = urlparse(url)
    path = p.path[:30]
    return f"{p.netloc}{path}..."


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8989, threaded=True)
