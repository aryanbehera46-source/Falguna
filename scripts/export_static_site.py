#!/usr/bin/env python3
"""Static export of the public twentytwotechnologies.com showroom site.

Produces a deployment-ready static HTML export for free static hosting
(e.g. Render Static Sites) WITHOUT touching the live application, its
production database, or any private/staff data.

How it stays safe:
  - Boots the real falguna/site_web.py SiteHandler (same render functions,
    same templates, same CSS/SEO/JSON-LD) so every page is byte-for-byte
    what the live app would render -- not a re-implementation.
  - ...but against a brand-new, isolated SCRATCH database in a temp
    directory, seeded only with the same real, truthful public content
    scripts/seed_site_content.py already seeds production with (no
    careers/insights content, per that script's own no-fabrication
    policy). The production .falguna/state.db is never opened.
  - The crawler only ever starts from and follows links within the site's
    own known-public STATIC_PATHS list; /login and /staff are hard-excluded
    and never fetched or written, even if something were to link to them.
  - Database-dependent forms (contact x2, careers apply) are swapped for
    the real, verified Tally.so forms (see TALLY_FORMS below) instead of
    our own backend -- careers applicants get a genuine resume-upload
    field on Tally's side. The four unprovisioned department mailto
    addresses on the /contact hub page and the footer are also swapped
    for the matching Tally form link, so nothing on the exported site
    claims an unconfirmed mailbox works. The "Staff Sign In" footer link
    is stripped, and the Privacy Policy page is patched to disclose that
    forms are hosted by Tally.

This script only writes to OUTPUT_DIR (default: static_export/ under the
repo root) and to a temp directory it creates and destroys. It never
modifies falguna/site_web.py or any other application file, and never
commits, pushes, deploys, or touches DNS.

Usage:
    python3 scripts/export_static_site.py [output_dir]
"""
import re
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from falguna.runtime import open_control_plane  # noqa: E402
from falguna import site_web as sw  # noqa: E402
from falguna.site_content import ServiceStore, CaseStudyStore, ProductStore  # noqa: E402
import seed_site_content  # noqa: E402

# Four real, verified Tally.so forms (confirmed live and correctly titled
# at export time -- see the report). These replace both the
# database-dependent <form> elements AND the unprovisioned department
# mailto addresses (hello@ / sales@ / careers@ / media@twentytwotechnologies.com)
# that used to appear on the /contact hub page and in the footer -- nothing
# in this export claims an unconfirmed mailbox works.
TALLY_FORMS = {
    "general": "https://tally.so/r/VLgxN6",
    "project": "https://tally.so/r/vGkWW4",
    "careers": "https://tally.so/r/XxXNNz",
    "media": "https://tally.so/r/obWxxN",
}
TALLY_LABELS = {
    "general": "Open the enquiry form",
    "project": "Start the project form",
    "careers": "Open the application form",
    "media": "Open the media form",
}
TALLY_PRIVACY_URL = "https://tally.so/help/privacy-policy"
TALLY_PRIVACY_NOTE = (
    "This form is hosted by our form partner, Tally (tally.so). Your submission goes "
    "directly to Tally and then to us -- it is not stored on this website. "
    f'<a href="{TALLY_PRIVACY_URL}" target="_blank" rel="noopener noreferrer" '
    'style="color:inherit;text-decoration:underline">Tally\'s privacy policy</a>.'
)
# action="..." value -> Tally category
FORM_ACTION_TO_CATEGORY = {
    "/contact/project": "project",
    "/contact/general": "general",
    "/careers/apply": "careers",
}
FORM_NOTES = {
    "project": "Tell us about your project below.",
    "general": "Send us your question below.",
    "careers": "Attach your resume in the form below -- it accepts PDF and Word files.",
}
# Inline style (beats the .btn class by CSS specificity) so the button
# never wraps mid-word on narrow phones -- the labels above are also kept
# short for the same reason.
CTA_STYLE = ("display:inline-block;max-width:100%;box-sizing:border-box;"
              "white-space:normal;line-height:1.3;text-align:center")

OUTPUT_DIR = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "static_export" / "twentytwotechnologies-com"

EXCLUDE_PREFIXES = ("/login", "/staff", "/static/", "/logout")
SKIP_SCHEMES = ("http://", "https://", "mailto:", "tel:", "javascript:", "#")

FORM_RE_TMPL = r'<form class="stack" method="post" action="{action}"[^>]*>.*?</form>'
LOGIN_LINK_RE = re.compile(r'<a href="/login">Staff Sign In</a>')
LINK_RE = re.compile(r'''(?:href|src)=["']([^"'#][^"']*)["']''')

# The /contact hub's "General: ... Projects: ... Careers: ... Media: ..."
# paragraph of raw department mailto links -- unique text on that one page.
CONTACT_HUB_RE = re.compile(r'<p>General:.*?</p>', re.DOTALL)

# Every remaining raw SITE_EMAIL (hello@...) mailto anchor -- footer,
# Privacy Policy, Accessibility page all have their own copy with slightly
# different style attributes, so this matches any of them generically.
# Pulled from the real app constant so this tracks site_web.py if it ever
# changes.
SITE_EMAIL_MAILTO_RE = re.compile(
    r'<a href="mailto:' + re.escape(sw.SITE_EMAIL) + r'"[^>]*>' +
    re.escape(sw.SITE_EMAIL) + r'</a>'
)
SITE_EMAIL_REPLACEMENT = (
    f'<a href="{TALLY_FORMS["general"]}" target="_blank" rel="noopener noreferrer" '
    'style="color:var(--paper);text-decoration:underline">Contact us &#8599;</a>'
)

# The Organization JSON-LD block (every page) also declares SITE_EMAIL as
# the org's contact email in structured data read by search engines/AI
# assistants -- that's advertising the unprovisioned address just as much
# as a visible link would, so it's dropped from the export's schema too.
JSONLD_EMAIL_RE = re.compile(r'"email":\s*"' + re.escape(sw.SITE_EMAIL) + r'",\s*')

# Only matches the Privacy Policy page (unique heading + intro text).
PRIVACY_WHAT_WE_COLLECT_RE = re.compile(r'<h2>What we collect</h2>\s*<p>.*?</p>', re.DOTALL)
PRIVACY_REPLACEMENT = (
    '<h2>What we collect</h2>'
    '<p>Contact, project-enquiry, careers, and media forms on this site are hosted by our '
    'form partner, Tally (tally.so) -- not by our own servers. When you submit one of these '
    'forms, the information you provide (name, email, message, and for careers applications, '
    'your resume) is sent directly to Tally and then relayed to us; this website does not '
    'itself receive, process, or store your submission. See '
    f'<a href="{TALLY_PRIVACY_URL}" target="_blank" rel="noopener noreferrer" '
    'style="color:inherit;text-decoration:underline">Tally\'s privacy policy</a> for how they '
    'handle your data.</p>'
)


def tally_cta_block(category: str) -> str:
    url = TALLY_FORMS[category]
    label = TALLY_LABELS[category]
    note = FORM_NOTES[category]
    return (
        '<div class="card" style="margin-top:8px">'
        f'<p>{note}</p>'
        f'<p style="margin-top:14px"><a class="btn btn-primary" style="{CTA_STYLE}" '
        f'href="{url}" target="_blank" rel="noopener noreferrer">{label} &#8599;</a></p>'
        f'<p class="form-note" style="margin-top:12px">{TALLY_PRIVACY_NOTE}</p>'
        '</div>'
    )


def contact_hub_replacement() -> str:
    order = [("general", "General"), ("project", "Projects"), ("careers", "Careers"), ("media", "Media")]
    lines = []
    for cat, label in order:
        lines.append(
            f'{label}: <a href="{TALLY_FORMS[cat]}" target="_blank" rel="noopener noreferrer" '
            f'style="color:var(--paper);text-decoration:underline">{TALLY_LABELS[cat]} &#8599;</a>'
        )
    return "<p>" + "<br>\n  ".join(lines) + "</p>"


def free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def seed_scratch_db(scratch_root: Path) -> None:
    """Seed a brand-new, isolated DB with the same real content
    scripts/seed_site_content.py seeds production with. Never opens the
    production .falguna/state.db."""
    control, store = open_control_plane(scratch_root)
    try:
        seed_site_content.seed_case_studies(CaseStudyStore(store))
        seed_site_content.seed_services(ServiceStore(store))
        seed_site_content.seed_products(ProductStore(store))
    finally:
        store.close()


def start_scratch_server(scratch_root: Path, port: int):
    """Mirrors falguna.site_web.serve_site(), but returns the server handle
    (instead of blocking on serve_forever()) so the caller can crawl it and
    then shut it down cleanly."""
    server = ThreadingHTTPServer(("127.0.0.1", port), sw.SiteHandler)
    server.app_root = scratch_root
    control, store = open_control_plane(scratch_root)
    store.close()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


def fetch(base_url: str, path: str):
    url = urljoin(base_url, path)
    req = urllib.request.Request(url, headers={"User-Agent": "TTT-static-export/1.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return resp.status, resp.read()


def discover_links(html_text: str):
    found = set()
    for m in LINK_RE.finditer(html_text):
        href = m.group(1)
        if href.startswith(SKIP_SCHEMES):
            continue
        found.add(href.split("?")[0].split("#")[0])
    return found


def postprocess_html(html_text: str) -> str:
    """Strip the staff-login link, swap the three database-dependent
    <form> elements for the matching verified Tally form, swap the
    unprovisioned department mailto links (contact hub + footer) for
    Tally links too, and patch the Privacy Policy page to disclose Tally
    as the form host. Generic/regex-by-content on purpose: each rule
    matches by the real rendered markup, not by page, so it can't miss
    one and can't accidentally fire on the wrong page."""
    html_text = LOGIN_LINK_RE.sub("", html_text)
    for action, category in FORM_ACTION_TO_CATEGORY.items():
        pattern = re.compile(FORM_RE_TMPL.format(action=re.escape(action)), re.DOTALL)
        html_text = pattern.sub(tally_cta_block(category), html_text)
    html_text = CONTACT_HUB_RE.sub(lambda m: contact_hub_replacement(), html_text, count=1)
    html_text = SITE_EMAIL_MAILTO_RE.sub(lambda m: SITE_EMAIL_REPLACEMENT, html_text)
    html_text = JSONLD_EMAIL_RE.sub("", html_text)
    html_text = PRIVACY_WHAT_WE_COLLECT_RE.sub(lambda m: PRIVACY_REPLACEMENT, html_text, count=1)
    return html_text


def path_to_file(path: str) -> Path:
    if path == "/":
        return OUTPUT_DIR / "index.html"
    if path in ("/robots.txt", "/sitemap.xml"):
        return OUTPUT_DIR / path.lstrip("/")
    return OUTPUT_DIR / path.strip("/") / "index.html"


def crawl_and_export(base_url: str):
    seen = set()
    queue = list(sw.STATIC_PATHS) + ["/robots.txt", "/sitemap.xml"]
    written = []
    while queue:
        path = queue.pop(0)
        if path in seen or path in ("/login", "/staff") or path.startswith(EXCLUDE_PREFIXES):
            continue
        seen.add(path)
        try:
            status, body = fetch(base_url, path)
        except urllib.error.HTTPError as exc:
            print(f"  SKIP {path}: HTTP {exc.code}")
            continue
        if status != 200:
            print(f"  SKIP {path}: HTTP {status}")
            continue
        is_html = not path.endswith((".xml", ".txt"))
        text = body.decode("utf-8")
        if is_html:
            for href in discover_links(text):
                if href not in seen and href not in queue and not href.startswith(EXCLUDE_PREFIXES):
                    queue.append(href)
            text = postprocess_html(text)
            body = text.encode("utf-8")
        dest = path_to_file(path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(body)
        written.append(path)
        print(f"  OK   {path} -> {dest.relative_to(OUTPUT_DIR)}")
    return written


def export_404(base_url: str):
    try:
        status, body = fetch(base_url, "/__ttt_static_export_404_probe__")
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read()
    if status != 404:
        print(f"  WARN 404 probe returned {status}, not 404 -- check site_web.py routing")
    text = postprocess_html(body.decode("utf-8"))
    (OUTPUT_DIR / "404.html").write_text(text, encoding="utf-8")
    print(f"  OK   404.html (real app 404 page, status was {status})")


def copy_static_assets():
    src = ROOT / "falguna" / "site_static"
    dst = OUTPUT_DIR / "static"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    print(f"  OK   copied {src} -> {dst}")


def main():
    if OUTPUT_DIR.exists():
        shutil.rmtree(OUTPUT_DIR)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    scratch_root = Path(tempfile.mkdtemp(prefix="ttt-static-export-"))
    print(f"Scratch DB: {scratch_root} (isolated, never touches production .falguna/state.db)")
    seed_scratch_db(scratch_root)

    port = free_port()
    server, thread = start_scratch_server(scratch_root, port)
    base_url = f"http://127.0.0.1:{port}"
    print(f"Scratch server: {base_url} (127.0.0.1 only, not publicly reachable)")
    time.sleep(0.3)

    try:
        print("Crawling and exporting public routes...")
        written = crawl_and_export(base_url)
        print("Exporting styled 404 page...")
        export_404(base_url)
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(scratch_root, ignore_errors=True)

    print("Copying static brand assets...")
    copy_static_assets()

    print(f"\nExported {len(written)} pages + 404.html + static assets to:\n  {OUTPUT_DIR}")
    return written


if __name__ == "__main__":
    main()
