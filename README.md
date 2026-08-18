# Streamzy (EProxy)

A lightweight HTTP streaming reverse proxy built with Flask and `requests`, designed to forward video streams with range requests, custom session cookies, and referrer headers. Can be run locally or automated via GitHub Actions with Cloudflare Quick Tunnels.

---

## Features

- **Chunked Media Streaming**: Streams large video and media chunks efficiently using `Response(stream_with_context(...))`.
- **Range Request Support**: Forwards `Range` request headers to support video seeking and resuming.
- **Custom Session Handling**: Pre-configured headers, session cookies, and referrers for bypassing restrictions.
- **Automated Cloudflare Quick Tunnel**: GitHub Actions workflow to run the proxy on-demand and expose a secure HTTPS public tunnel endpoint.

---

## Installation & Local Usage

### Prerequisites
- Python 3.10+
- `pip`

### 1. Install Dependencies
```bash
pip install flask requests
```

### 2. Run the Proxy Server
```bash
python eproxy.py
```
The server will start listening on `http://0.0.0.0:8989`.

### 3. Stream through the Proxy
Pass the target media URL directly as the route path:
```bash
curl -i "http://localhost:8989/https://example.com/video.mp4"
```

---

## Running with Cloudflare Quick Tunnel (GitHub Actions)

The repository includes a GitHub Actions workflow (`.github/workflows/eproxy.yml`) to deploy the proxy on GitHub runners and expose it over the internet using Cloudflare Quick Tunnels.

1. Go to the **Actions** tab in GitHub.
2. Select **EProxy + Cloudflare Quick Tunnel**.
3. Click **Run workflow**.
4. Once started, check the **Summary** tab to copy your live `https://*.trycloudflare.com` tunnel URL.

---

## License

MIT License
