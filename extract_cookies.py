#!/usr/bin/env python3
"""
Playwright-based cookie extractor for Streamzy (EProxy).

Visits the target site, passes the age verification dialog, optionally logs
in with a dummy account (EP_USERNAME / EP_PASSWORD), then writes all cookies
to cookies.txt in `name=value; name2=value2` format that eproxy.py consumes.
"""

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

BASE_URL = os.environ.get("EP_BASE_URL", "https://www.eporner.com")
USERNAME = os.environ.get("EP_USERNAME", "")
PASSWORD = os.environ.get("EP_PASSWORD", "")

SCRIPT_DIR = Path(__file__).resolve().parent
COOKIES_FILE = SCRIPT_DIR / "cookies.txt"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)

AGE_BUTTON_TEXTS = [
    "I am 18",
    "I'm 18",
    "I am 18+",
    "I'm 18+",
    "Yes, I am 18",
    "I am over 18",
    "I'm over 18",
    "Enter Site",
    "Enter",
    "Continue",
    "Accept",
    "Agree",
]


def accept_age_verification(page) -> bool:
    for text in AGE_BUTTON_TEXTS:
        button = page.get_by_role("button", name=text).first
        if button.count():
            try:
                button.click(timeout=3000)
                print(f"[+] Age verification accepted via: {text}")
                return True
            except Exception:
                continue
    print("[=] No age verification button found (or already accepted)")
    return False


def login(page) -> bool:
    if not USERNAME or not PASSWORD:
        print("[!] EP_USERNAME / EP_PASSWORD not set - skipping login")
        return False

    try:
        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=20000)

        modal_link = page.locator("a[onclick*='openModal']").first
        if not modal_link.count():
            print("[!] Login modal link not found")
            return False
        modal_link.click(timeout=5000)

        login_input = page.locator("input[name='login']").first
        login_input.wait_for(state="visible", timeout=15000)
        login_input.fill(USERNAME)
        page.locator("input[name='pass'][type='password']").first.fill(PASSWORD)
        page.locator("input[name='Submit']:visible").first.click(timeout=10000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
    except Exception as exc:
        print(f"[!] Login failed: {exc}")
        return False

    try:
        page.wait_for_url("**/profile/**", timeout=15000)
        print(f"[+] Logged in as {USERNAME}")
        return True
    except Exception:
        pass

    try:
        body = page.inner_text("body")
        success = "logout" in body.lower() or "sign out" in body.lower()
    except Exception:
        success = False

    if success:
        print(f"[+] Logged in as {USERNAME}")
    else:
        print("[!] Login result not confirmed - continuing without login")
    return success


def main() -> int:
    if not USERNAME or not PASSWORD:
        print("[=] No credentials provided (EP_USERNAME/EP_PASSWORD not set)")
        print("[=] Will extract session cookies without login")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        context = browser.new_context(user_agent=USER_AGENT)
        page = context.new_page()

        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)
        accepted = accept_age_verification(page)
        login(page)

        if not accepted:
            domain = urlparse(BASE_URL).netloc
            try:
                context.add_cookies([{
                    "name": "ageverif_accepted",
                    "value": "T",
                    "domain": domain,
                    "path": "/",
                }])
                print(f"[+] Forced ageverif_accepted cookie on {domain}")
            except Exception as exc:
                print(f"[!] Could not force age cookie: {exc}")

        page.goto(BASE_URL, wait_until="domcontentloaded", timeout=30000)

        cookies = context.cookies()
        if not cookies:
            print("[!] No cookies extracted")
            return 1

        cookie_string = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        COOKIES_FILE.write_text(cookie_string + "\n")

        print(f"[+] Saved {len(cookies)} cookies to {COOKIES_FILE}")
        for c in cookies:
            v = c["value"]
            print(f"    {c['name']} = {v[:4]}{'***' if len(v) > 4 else ''}")

        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
