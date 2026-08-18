# Streamzy (EProxy)

A streaming reverse proxy for eporner.com (mp4) and surrit.com/missav (HLS m3u8) with ffmpeg remux, Playwright cookie extraction, and a GitHub Actions CI/CD pipeline via Cloudflare Quick Tunnel.

---

## Quick Start

**[Open Streamzy Player](https://mrspidy32.github.io/Streamzy/)**

1. Run the workflow from the [Actions tab](https://github.com/MrSpidy32/Streamzy/actions/workflows/eproxy.yml) to start a tunnel
2. Open the player page — it shows the live tunnel URL automatically
3. Paste a full post page URL or direct mp4/m3u8 link and click **Play**
4. Use **Direct Link** to copy a remuxed MP4 URL, or **Download** to save it

The player connects through the Cloudflare Quick Tunnel to the proxy running on GitHub Actions.

---

## Features

- **Epornor Proxy**: Stream/MP4 passthrough via cloudscraper sessions
- **Surrit/missav HLS Proxy**: Rewrites m3u8 segment URLs through `/proxy` for hls.js playback
- **HLS Direct**: ffmpeg remux to fragmented MP4 (TS -> MP4 via `aac_adtstoasc`)
- **Auth**: `STREAMZY_TOKEN` env var; Bearer header / `_token` query param / session cookie
- **SSRF Protection**: Host allowlist (`eporner.com`, `surrit.com`) + DNS IP validation
- **Cookie Scoping**: eporner cookies only for eporner.com, surrit gets missav referer
- **FFmpeg Concurrency**: Configurable max (`MAX_FFMPEG`, default 5)
- **Player UI**: Login form + hls.js player + copyable direct link + download button
- **Automated Deployment**: GitHub Actions workflow with Cloudflare Quick Tunnel

---

## Routes

All media routes use `?url=<percent-encoded-target>`.

| Route | Method | Description |
|-------|--------|-------------|
| `/` | GET | API index (JSON) |
| `/health` | GET | Health check |
| `/api/status` | GET | Auth status |
| `/player` | GET | Player UI (unauthenticated) |
| `/login` | POST | Authenticate via `Authorization: Bearer <token>` |
| `/logout` | GET | Clear session |
| `/proxy?url=...` | GET | Stream through cloudscraper (passthrough) |
| `/direct?url=...` | GET | ffmpeg remux to fragmented MP4 |
| `/download?url=...` | GET | Same as direct, triggers browser download |
| `/extract?url=...` | GET | Extract mp4 URL from a post page (JSON) |

---

## Installation & Local Usage

### Prerequisites
- Python 3.10+
- ffmpeg (for `/direct` and `/download` routes)
- `pip`

### 1. Install Dependencies
```bash
pip install -r requirements.txt
playwright install --with-deps chromium  # for cookie extraction
```

### 2. Set Environment Variables
```bash
export STREAMZY_TOKEN="your-secret-token"   # auth (optional, bypasses auth if unset)
export EP_USERNAME="SpideyDih_69"            # for cookie extraction
export EP_PASSWORD="Spidy@123"               # for cookie extraction
export SURRIT_REFERER="https://missav.ws/"   # default
export MAX_FFMPEG=5                          # default
```

### 3. Extract Cookies (optional, for eporner streams)
```bash
python extract_cookies.py
```

### 4. Run the Server
```bash
python eproxy.py
# or with gunicorn
gunicorn -w 2 --threads 16 -b 0.0.0.0:8989 --timeout 0 eproxy:app
```

### 5. Stream through the Proxy
```bash
# Proxy stream (passthrough)
curl "http://localhost:8989/proxy?url=https%3A%2F%2Fwww.eporner.com%2Fdload%2F...%2Fvideo.mp4"

# Direct remux to MP4
curl "http://localhost:8989/direct?url=https%3A%2F%2Fsurrit.com%2F...%2Fvideo.m3u8"

# With token
curl "http://localhost:8989/proxy?_token=your-secret-token&url=https%3A%2F%2F..."
```

---

## Running with Cloudflare Quick Tunnel (GitHub Actions)

The workflow (`.github/workflows/eproxy.yml`) deploys the proxy on a GitHub runner with a Cloudflare Quick Tunnel.

### Required Secrets

| Secret | Description |
|--------|-------------|
| `STREAMZY_TOKEN` | Auth token for the proxy |
| `EP_USERNAME` | eporner username for cookie extraction |
| `EP_PASSWORD` | eporner password for cookie extraction |

### Steps
1. Go to the **Actions** tab in GitHub.
2. Select **EProxy + Cloudflare Quick Tunnel**.
3. Click **Run workflow**.
4. Check the **Summary** tab for the live `https://*.trycloudflare.com` URL.
5. Open `/player` in a browser to use the web UI.

---

## License

MIT License
