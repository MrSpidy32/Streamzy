from flask import Flask, request, Response, stream_with_context
import requests

app = Flask(__name__)

SESSION = requests.Session()

COOKIE = (
    "PHPSESSID=f8ce7430331ba55392325ba9db32506c; "
    "EPRNS=9beaf0cfc7edb2b264ccad258a7c2dfc; "
    "ageverif_accepted=T"
)

DEFAULT_HEADERS = {
    "Cookie": COOKIE,
    "Referer": "https://www.eporner.com/",
    "User-Agent": "Mozilla/5.0"
}


@app.route("/<path:url>")
def proxy(url):
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    headers = DEFAULT_HEADERS.copy()

    if "Range" in request.headers:
        headers["Range"] = request.headers["Range"]

    r = SESSION.get(
        url,
        headers=headers,
        stream=True,
        allow_redirects=True,
    )

    excluded = {
        "content-encoding",
        "transfer-encoding",
        "connection",
    }

    response_headers = [
        (k, v)
        for k, v in r.headers.items()
        if k.lower() not in excluded
    ]

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
