#!/usr/bin/env python3
"""Automated smoke tests for the static export produced by
scripts/export_static_site.py. Serves the export directory locally
(stdlib http.server only) and checks it the way a static host would be
used, plus a leak scan the way a security review would.

Usage:
    python3 scripts/smoke_test_static_export.py [export_dir]

Exits non-zero if any check fails. Prints a PASS/FAIL line per check.
"""
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from functools import partial
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parent.parent
EXPORT_DIR = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "static_export" / "twentytwotechnologies-com"

LINK_RE = re.compile(r'''(?:href|src)=["']([^"'#][^"']*)["']''')
TALLY_LINK_RE = re.compile(r'https://tally\.so/r/[A-Za-z0-9]+')
EXPECTED_TALLY_IDS = {"VLgxN6", "vGkWW4", "XxXNNz", "obWxxN"}
LEAK_PATTERNS = [
    r'href="/login"', r'href="/staff"', r'action="/staff"', r'action="/login"',
    r'\b8765\b', r'\b8766\b', r'rh_opportunities', r'needs_aryan', r'\.falguna/state\.db',
    r'audit\.jsonl', r'<form\b', r'mailto:', r'aryan@',
]


class Result:
    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.failures = []

    def check(self, ok: bool, label: str):
        if ok:
            self.passed += 1
            print(f"  PASS {label}")
        else:
            self.failed += 1
            self.failures.append(label)
            print(f"  FAIL {label}")


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_static_server(root: Path, port: int):
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def fetch(base_url: str, path: str):
    url = urljoin(base_url, path)
    req = urllib.request.Request(url, headers={"User-Agent": "TTT-smoke-test/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def check_page(base_url: str, html_path: Path, r: Result, checked_assets: set):
    rel = html_path.relative_to(EXPORT_DIR)
    text = html_path.read_text(encoding="utf-8")

    r.check(bool(re.search(r"<title>[^<]{3,}</title>", text)), f"{rel}: has <title>")
    r.check('name="description"' in text, f"{rel}: has meta description")
    r.check('name="viewport"' in text and "width=device-width" in text,
             f"{rel}: has mobile viewport meta tag")
    r.check('rel="canonical"' in text, f"{rel}: has canonical link")

    leaks = [pat for pat in LEAK_PATTERNS if re.search(pat, text, re.IGNORECASE)]
    r.check(not leaks, f"{rel}: no private-data / staff-login / raw-form leakage" +
             (f" (found: {leaks})" if leaks else ""))

    mixed = re.findall(r'''(?:href|src)=["']http://[^"']*["']''', text)
    r.check(not mixed, f"{rel}: no http:// mixed-content refs" + (f" ({mixed})" if mixed else ""))

    for href in LINK_RE.findall(text):
        if not href.startswith("/") or href in checked_assets:
            continue
        checked_assets.add(href)
        status, _ = fetch(base_url, href)
        r.check(status == 200, f"link {href} (from {rel}) resolves 200 (got {status})")


def main():
    if not EXPORT_DIR.is_dir():
        print(f"No export found at {EXPORT_DIR}. Run scripts/export_static_site.py first.")
        return 1

    port = free_port()
    server, thread = start_static_server(EXPORT_DIR, port)
    base_url = f"http://127.0.0.1:{port}"
    time.sleep(0.3)
    r = Result()
    checked_assets = set()

    try:
        print(f"Serving {EXPORT_DIR} at {base_url}\n")

        print("-- required files --")
        r.check((EXPORT_DIR / "404.html").is_file(), "404.html exists")
        r.check((EXPORT_DIR / "robots.txt").is_file(), "robots.txt exists")
        r.check((EXPORT_DIR / "sitemap.xml").is_file(), "sitemap.xml exists")
        r.check((EXPORT_DIR / "index.html").is_file(), "homepage index.html exists")
        r.check(not (EXPORT_DIR / "login").exists(), "/login was NOT exported")
        r.check(not (EXPORT_DIR / "staff").exists(), "/staff was NOT exported")

        print("\n-- missing-page (404) behavior --")
        status, body = fetch(base_url, "/this-page-does-not-exist-xyz")
        r.check(status == 404, f"unmatched path returns HTTP 404 (got {status})")
        text_404 = (EXPORT_DIR / "404.html").read_text(encoding="utf-8")
        r.check("<html" in text_404.lower() and "404" in text_404, "404.html is a real styled page")

        print("\n-- per-page checks (title, meta, canonical, links, leaks) --")
        html_files = sorted(EXPORT_DIR.rglob("*.html"))
        for html_path in html_files:
            check_page(base_url, html_path, r, checked_assets)

        print(f"\n-- checked {len(checked_assets)} unique internal links/assets across {len(html_files)} pages --")

        print("\n-- Tally form integration (real network check, not local) --")
        found_urls = set()
        for html_path in html_files:
            found_urls |= set(TALLY_LINK_RE.findall(html_path.read_text(encoding="utf-8")))
        found_ids = {u.rsplit("/", 1)[-1] for u in found_urls}
        r.check(found_ids == EXPECTED_TALLY_IDS,
                 f"all 4 expected Tally forms present in export (found {sorted(found_ids)})")
        for url in sorted(found_urls):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "TTT-smoke-test/1.0"})
                with urllib.request.urlopen(req, timeout=15) as resp:
                    status = resp.status
            except urllib.error.HTTPError as exc:
                status = exc.code
            except Exception as exc:  # network error, DNS, timeout, etc.
                status = f"ERROR: {exc}"
            r.check(status == 200, f"{url} is live and reachable (got {status})")
    finally:
        server.shutdown()
        server.server_close()

    print(f"\n{r.passed} passed, {r.failed} failed")
    if r.failed:
        print("Failures:")
        for f in r.failures:
            print(f"  - {f}")
    return 1 if r.failed else 0


if __name__ == "__main__":
    sys.exit(main())
