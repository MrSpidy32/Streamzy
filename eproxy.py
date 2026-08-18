import os
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
from flask import Flask, request, Response, stream_with_context, jsonify
import requests

app = Flask(__name__)

SESSION = requests.Session()

DEFAULT_COOKIE = os.environ.get(
    "EPORNER_COOKIE",
    (
        "PHPSESSID=f8ce7430331ba55392325ba9db32506c; "
        "EPRNS=9beaf0cfc7edb2b264ccad258a7c2dfc; "
        "ageverif_accepted=T"
    )
)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}


@app.route("/")
def index():
    return jsonify({
        "status": "online",
        "service": "Streamzy (EProxy)",
        "endpoints": {
            "proxy": "/<target_url>",
            "examples": [
                "/https://www.eporner.com/dload/lYSc4ULMix3/1080/17691181-1080p-av1.mp4",
                "/https://www.eporner.com/dload/lYSc4ULMix3/1080/17691181-1080p-av1.mp4?_cookie=PHPSESSID=YOUR_FRESH_ID"
            ]
        },
        "cookie_options": {
            "query_parameter": "?_cookie=<your_cookie_string>",
            "header": "X-Proxy-Cookie: <your_cookie_string> or Cookie: <your_cookie_string>"
        }
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/<path:url>")
def proxy(url):
    # Ensure scheme
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    # Preserve target query string while extracting special proxy query params
    target_query_pairs = []
    custom_cookie = None

    if request.query_string:
        parsed_qs = parse_qs(request.query_string.decode("utf-8", errors="ignore"), keep_blank_values=True)
        for key, values in parsed_qs.items():
            if key in ("_cookie", "_c"):
                if values:
                    custom_cookie = values[0]
            else:
                for v in values:
                    target_query_pairs.append((key, v))

        if target_query_pairs:
            encoded_qs = urlencode(target_query_pairs)
            parsed_url = urlparse(url)
            # Merge with any existing query in url
            if parsed_url.query:
                combined_qs = f"{parsed_url.query}&{encoded_qs}"
            else:
                combined_qs = encoded_qs
            url = urlunparse(parsed_url._replace(query=combined_qs))

    # Resolve cookie priority: Query Param > X-Proxy-Cookie / Cookie Header > Default
    cookie_header = (
        custom_cookie
        or request.headers.get("X-Proxy-Cookie")
        or request.headers.get("Cookie")
        or DEFAULT_COOKIE
    )

    # Determine referer based on target domain
    parsed_target = urlparse(url)
    default_referer = f"{parsed_target.scheme}://{parsed_target.netloc}/"
    referer_header = request.headers.get("X-Proxy-Referer") or default_referer

    headers = DEFAULT_HEADERS.copy()
    headers["Cookie"] = cookie_header
    headers["Referer"] = referer_header

    # Forward Range requests for seeking in video players
    if "Range" in request.headers:
        headers["Range"] = request.headers["Range"]

    try:
        r = SESSION.get(
            url,
            headers=headers,
            stream=True,
            allow_redirects=True,
            timeout=30,
        )
    except Exception as exc:
        return jsonify({"error": f"Upstream connection failed: {str(exc)}"}), 502

    excluded_headers = {
        "content-encoding",
        "transfer-encoding",
        "connection",
    }

    response_headers = [
        (k, v)
        for k, v in r.headers.items()
        if k.lower() not in excluded_headers
    ]

    # Add CORS headers for web players
    response_headers.append(("Access-Control-Allow-Origin", "*"))
    response_headers.append(("Access-Control-Allow-Headers", "*"))
    response_headers.append(("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS"))

    return Response(
        stream_with_context(
            r.iter_content(chunk_size=1024 * 1024)
        ),
        status=r.status_code,
        headers=response_headers,
    )


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=8989,
        threaded=True,
    )
