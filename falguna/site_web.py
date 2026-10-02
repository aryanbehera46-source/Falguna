"""Twenty Two Technologies -- flagship public corporate website.

Stdlib-only ThreadingHTTPServer service, following the exact pattern
already established by falguna/hq_web.py and falguna/web.py in this
codebase (see docs/website/WEBSITE_V1_SPEC.md for why). Serves on
port 8767 by default and shares the same .falguna/state.db as Falguna
Engineering (8765) and TTT HQ (8766) via runtime.open_control_plane().

This file intentionally stays free of new runtime dependencies: no
Jinja2, no Node/npm build step. Pages are server-rendered semantic HTML
built from small Python string-template helpers, one hand-authored CSS
file (SITE_CSS below), and minimal progressive-enhancement JS.
"""
from __future__ import annotations

import cgi
import hashlib
import html
import json
import mimetypes
import os
import re
import uuid
from datetime import datetime, timezone
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

from .comms import CommsStore
from .revenue_hunter import OpportunityStore
from .runtime import open_control_plane
from .site_auth import StaffAuthService, AuthError
from .phase7_portals import ExternalPortalAuth, ExternalPortalService
from .phase6_partner import CustomerVerificationService, PUBLIC_PAYMENT_WARNING
from .partner_management import PartnerError, ReferralError
from .site_content import (
    ApplicationStore, CaseStudyStore, EnquiryStore, JobStore, PostStore,
    ProductStore, ServiceStore, ContentError, hash_ip,
)

SITE_NAME = "Twenty Two Technologies"
SITE_DOMAIN = "twentytwotechnologies.com"
SITE_EMAIL = "hello@twentytwotechnologies.com"
SALES_EMAIL = "sales@twentytwotechnologies.com"

# Phase 7, Section 11 -- self-service intake / universal routing. A fixed,
# honest set of categories a submitter picks for themselves; TTT staff use
# it as a triage hint alongside the free-text message. Deliberately NOT an
# automatic classifier: no code here infers a category from message text,
# because that capability has not been built or verified (see
# EnquiryStore.submit's own docstring note on this).
INTAKE_CATEGORIES = [
    ("direct_delivery", "Direct TTT engineering delivery"),
    ("advisory_consulting", "Advisory / consulting"),
    ("specialist_partner", "Specialist or partner coordination"),
    ("vendor_sourcing", "Provider / vendor sourcing"),
    ("referral_mediation", "Referral / mediation"),
    ("product_fit", "An existing product or SaaS might already fit"),
    ("business_launch_growth", "Business Launch & Growth support"),
    ("licensed_professional", "Needs a licensed professional (legal, medical, financial, etc.)"),
    ("unsupported", "Not sure / something else"),
]
CAREERS_EMAIL = "careers@twentytwotechnologies.com"
MEDIA_EMAIL = "media@twentytwotechnologies.com"
FOUNDED_YEAR = 2020
MAX_RESUME_BYTES = 8 * 1024 * 1024  # 8MB
ALLOWED_RESUME_EXTENSIONS = {".pdf", ".doc", ".docx"}

# Real, approved brand assets (added once Aryan supplied them -- see
# docs/website/WEBSITE_V1_SPEC.md, which originally flagged "no logo
# exists" as a launch blocker; this directory is that blocker resolved).
STATIC_DIR = Path(__file__).with_name("site_static")
STATIC_ALLOWED_EXTENSIONS = {".png", ".jpg", ".jpeg", ".svg", ".webp", ".ico"}
HEADER_LOGO_PATH = "/static/brand/header-logo.png"
FAVICON_PATH = "/static/brand/favicon-64.png"
OG_IMAGE_PATH = "/static/brand/og-image.png"
FALGUNA_MARK_PATH = "/static/brand/falguna-mark-small.png"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def esc(value: Optional[str]) -> str:
    """HTML-escape for safe interpolation into templates -- the single
    chokepoint every piece of user- or DB-sourced text must pass through
    before landing in a page, which is what keeps this XSS-safe."""
    # Keep imported content presentation-ready when older seed rows use
    # manuscript-style double hyphens instead of sentence punctuation.
    clean = (value or "").replace(" -- ", ". ").replace(" & ", " and ")
    return html.escape(clean, quote=True)


# ============================================================
# Design system -- approved logo palette: black, graphite, red and amber.
# System font stack only: zero external font/script requests, which is
# also why Lighthouse/CWV budgets in Block H are realistic to hit.
# ============================================================
SITE_CSS = r"""
:root{
  --ink:#080809; --ink-2:#111113; --ink-3:#19191c; --line:#2a2a2f;
  --paper:#f7f5ef; --paper-dim:#e8e4da; --ink-soft:#aaa9a6;
  --accent:#ed1c24; --accent-hover:#ff3b30; --accent-2:#ff695f; --amber:#f29a16; --focus:#ffb13b;
  --maxw:1200px; --gutter:28px;
  --radius:18px;
  --ease:cubic-bezier(.2,.7,.2,1);
  --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
}
*{box-sizing:border-box}
html{background:var(--ink)}
body{
  margin:0;background:var(--ink);color:var(--paper);overflow-x:hidden;
  font-family:var(--font);
  font-size:17px;line-height:1.6;-webkit-font-smoothing:antialiased;
}
img,svg{max-width:100%;display:block}
a{color:inherit}
.container{width:100%;max-width:var(--maxw);margin:0 auto;padding:0 var(--gutter)}
.visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
.visually-hidden:focus{position:fixed;z-index:100;top:12px;left:12px;width:auto;height:auto;overflow:visible;clip:auto;padding:10px 14px;background:var(--paper);color:var(--ink);border-radius:8px}
:focus-visible{outline:3px solid var(--focus);outline-offset:3px}
@media (prefers-reduced-motion: reduce){*{animation-duration:.001ms !important;transition-duration:.001ms !important}}

h1,h2,h3,h4{font-weight:600;letter-spacing:-0.02em;margin:0 0 .5em}
h1{font-size:clamp(2.8rem,6vw,5.7rem);line-height:1.01;font-weight:650;max-width:16ch}
h2{font-size:clamp(1.7rem,3.1vw,2.6rem);line-height:1.12}
h3{font-size:clamp(1.2rem,1.7vw,1.5rem);line-height:1.25}
p{margin:0 0 1em;color:var(--ink-soft)}
.lede{font-size:clamp(1.05rem,1.55vw,1.28rem);color:var(--paper-dim)}
.eyebrow{font-size:.74rem;letter-spacing:.18em;text-transform:uppercase;color:#ff695f;font-weight:700}

/* header / nav */
.site-header{position:sticky;top:0;z-index:40;background:rgba(8,8,9,.9);backdrop-filter:saturate(130%) blur(16px);border-bottom:1px solid rgba(255,255,255,.08)}
.site-header .bar{display:flex;align-items:center;justify-content:space-between;min-height:76px;padding:14px var(--gutter);max-width:var(--maxw);margin:0 auto}
.wordmark{font-weight:700;font-size:1.05rem;letter-spacing:-.01em;text-decoration:none;color:var(--paper);display:flex;align-items:center;gap:.5em}
.brand-mark{display:block;width:42px;height:42px;overflow:hidden;position:relative;flex:0 0 42px;border-radius:8px;background:var(--ink)}
.brand-mark img{position:absolute;max-width:none;width:42px;height:42px;left:0;top:0;mix-blend-mode:screen}
.nav{display:flex;gap:24px;align-items:center}
.nav a{text-decoration:none;color:var(--ink-soft);font-size:.9rem;transition:color .15s var(--ease)}
.nav a:hover,.nav a[aria-current="page"]{color:var(--paper)}
.nav-toggle{display:none;background:none;border:1px solid var(--line);color:var(--paper);border-radius:10px;padding:9px 12px;font-size:1rem}
@media (max-width:1040px){
  .nav{display:none}
  .nav-toggle{display:inline-flex}
  .site-header.open .nav{display:flex;position:absolute;left:0;right:0;top:100%;align-items:stretch;flex-direction:column;background:#0d0d0f;padding:20px var(--gutter) 24px;border-bottom:1px solid var(--line);gap:6px;box-shadow:0 18px 38px rgba(0,0,0,.35)}
  .site-header.open .nav a{padding:10px 0}
}

/* buttons */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:.5em;padding:13px 22px;border-radius:999px;font-weight:650;font-size:.93rem;text-decoration:none;transition:transform .15s var(--ease),background .15s var(--ease),border-color .15s var(--ease);border:1px solid transparent;cursor:pointer}
.btn-primary{background:linear-gradient(110deg,#d9161e,var(--accent));color:#fff;box-shadow:0 10px 28px rgba(237,28,36,.16)}
.btn-primary:hover{background:var(--accent-hover);transform:translateY(-1px)}
.btn-ghost{border-color:var(--line);color:var(--paper);background:transparent}
.btn-ghost:hover{border-color:var(--paper)}
.btn-row{display:flex;gap:14px;flex-wrap:wrap;margin-top:1.8em}

/* hero */
.hero{padding:clamp(74px,9vw,116px) 0 clamp(70px,8vw,100px);border-bottom:1px solid var(--line)}
.hero .eyebrow{margin-bottom:18px;display:block}
.hero-sub{max-width:650px;margin-top:24px}
.home-hero{position:relative;isolation:isolate;min-height:760px;display:flex;align-items:center;overflow:hidden}
.home-hero:before{content:"";position:absolute;z-index:-1;width:560px;height:560px;right:max(-160px,calc((100vw - var(--maxw))/2 - 190px));top:34px;border-radius:50%;background:radial-gradient(circle,rgba(237,28,36,.12) 0,rgba(242,154,22,.045) 38%,transparent 69%);filter:blur(3px)}
.home-hero h1 em{font-style:normal;color:var(--paper);position:relative}
.home-hero h1 em:after{content:"";position:absolute;left:0;right:6%;height:3px;bottom:-8px;background:linear-gradient(90deg,var(--accent),var(--amber));border-radius:99px}
.hero-proof{display:flex;gap:26px;flex-wrap:wrap;margin-top:42px;padding-top:25px;border-top:1px solid var(--line);max-width:720px;color:var(--ink-soft);font-size:.82rem;text-transform:uppercase;letter-spacing:.08em}
.hero-proof span{display:flex;align-items:center;gap:9px}
.hero-proof span:before{content:"";width:5px;height:5px;border-radius:50%;background:var(--amber)}

/* sections / grid */
section{padding:96px 0}
section.tight{padding:56px 0}
.section-head{max-width:680px;margin-bottom:46px}
.grid{display:grid;gap:28px;min-width:0}
.grid-3{grid-template-columns:repeat(3,1fr)}
.grid-2{grid-template-columns:repeat(2,1fr)}
@media (max-width:900px){.grid-3,.grid-2{grid-template-columns:1fr}}

.card{background:linear-gradient(145deg,#151517,#101012);border:1px solid var(--line);border-radius:var(--radius);padding:30px;min-width:0;overflow-wrap:anywhere}
.card-link{text-decoration:none;display:block;transition:border-color .15s var(--ease),transform .15s var(--ease)}
.card-link:hover{border-color:#3a3a40;transform:translateY(-2px)}
.index{color:#ff695f;font-weight:700;font-size:.78rem;margin-bottom:14px;display:block}
.divider{border:0;border-top:1px solid var(--line);margin:0}
.service-rail{display:grid;grid-template-columns:repeat(3,1fr);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.service-rail a{padding:40px 30px 42px;text-decoration:none;border-right:1px solid var(--line);min-height:190px}
.service-rail a:last-child{border-right:0}
.service-rail a:hover h3{color:#ff695f}
.service-rail h3{transition:color .15s var(--ease)}
.service-rail p{font-size:.98rem;margin:0;max-width:30ch}
.work-feature{display:grid;grid-template-columns:1fr 1fr;gap:80px;align-items:start;min-width:0}
.work-feature>*{min-width:0}
.work-feature .card{min-height:0;display:block;padding:0 0 26px;border:0;border-bottom:1px solid var(--line);border-radius:0;background:transparent}
.work-feature .card + .card{padding-top:26px}
.showcase-section{border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.showcase-list{display:flex;flex-direction:column}
.showcase-item{display:block;padding:0 0 30px;margin-bottom:30px;border-bottom:1px solid var(--line);text-decoration:none}
.showcase-item:last-child{margin-bottom:0}
.showcase-item h3{margin-top:12px;transition:color .15s var(--ease)}
.showcase-item:hover h3{color:#ff695f}
.showcase-item p{max-width:48ch;margin:0}
.service-catalog{display:grid;grid-template-columns:repeat(2,1fr);column-gap:70px}
.service-catalog .service-item{display:block;padding:30px 0;border-top:1px solid var(--line);text-decoration:none}
.service-catalog .service-item:hover h3{color:#ff695f}
.service-catalog h3{transition:color .15s var(--ease)}
.service-catalog p{margin:0;max-width:42ch}
@media (max-width:760px){.service-catalog{grid-template-columns:1fr;column-gap:0}}
.cta-band{padding:96px 0;background:var(--ink);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.cta-band .container{display:flex;align-items:end;justify-content:space-between;gap:36px}
.cta-band h2{max-width:16ch;margin-bottom:0}

/* alt panel (warm white band, used sparingly) */
.panel-paper{background:var(--ink-2);color:var(--paper);border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
.panel-paper p,.panel-paper .lede{color:var(--ink-soft)}
.panel-paper .card{background:linear-gradient(145deg,#19191c,#111113);border-color:var(--line)}
.panel-paper h2,.panel-paper h3{color:var(--paper)}

/* footer */
.site-footer{border-top:1px solid var(--line);padding:64px 0 40px;color:var(--ink-soft);font-size:.92rem}
.footer-grid{display:grid;grid-template-columns:2fr 1fr 1fr 1fr;gap:32px}
@media (max-width:760px){.footer-grid{grid-template-columns:1fr 1fr}}
.footer-grid h4{color:var(--paper);font-size:.82rem;text-transform:uppercase;letter-spacing:.08em;margin-bottom:14px}
.footer-grid a{display:block;text-decoration:none;color:var(--ink-soft);margin-bottom:10px}
.footer-grid a:hover{color:var(--paper)}
.footer-bottom{margin-top:44px;padding-top:24px;border-top:1px solid var(--line);display:flex;justify-content:space-between;flex-wrap:wrap;gap:12px}

@media (max-width:760px){
  :root{--gutter:20px}
  body{font-size:16px}
  .wordmark{font-size:.95rem}
  .brand-mark{width:38px;height:38px;flex-basis:38px}
  .brand-mark img{width:38px;height:38px}
  .home-hero{min-height:auto;padding:92px 0 82px}
  .home-hero:before{width:380px;height:380px;right:-220px}
  .hero-proof{gap:14px;margin-top:32px}
  section{padding:68px 0}
  .service-rail{grid-template-columns:1fr}
  .service-rail a{padding:28px 0;border-right:0;border-bottom:1px solid var(--line);min-height:0}
  .service-rail a:last-child{border-bottom:0}
  .work-feature{grid-template-columns:1fr;gap:28px}
  .cta-band .container{align-items:flex-start;flex-direction:column}
  .btn-row .btn{width:100%}
  .footer-grid{grid-template-columns:1fr 1fr;gap:28px 22px}
  .footer-grid>div:first-child{grid-column:1/-1}
}

/* forms */
form.stack{display:flex;flex-direction:column;gap:18px;max-width:640px}
.field label{display:block;font-size:.85rem;color:var(--ink-soft);margin-bottom:6px}
.field input,.field textarea,.field select{width:100%;background:var(--ink-2);border:1px solid var(--line);color:var(--paper);border-radius:10px;padding:12px 14px;font-family:inherit;font-size:.98rem}
.field input:focus,.field textarea:focus,.field select:focus{border-color:var(--accent-2)}
.field textarea{min-height:140px;resize:vertical}
.form-note{font-size:.85rem;color:var(--ink-soft)}
.alert{padding:16px 18px;border-radius:10px;font-size:.92rem;margin-bottom:18px}
.alert-success{background:#123326;border:1px solid #1d5c3f;color:#bdf0d3}
.alert-error{background:#3a1414;border:1px solid #6b1f1f;color:#ffc9c2}
.intent-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:14px}
.intent-card{display:flex;min-height:150px;flex-direction:column;justify-content:space-between;padding:22px;border:1px solid var(--line);border-radius:14px;text-decoration:none;background:#111113;transition:.2s var(--ease)}
.intent-card:hover{border-color:var(--accent-2);transform:translateY(-2px)}
.intent-card strong{font-size:1.05rem}.intent-card span{color:var(--ink-soft);font-size:.86rem}
.app-shell{display:grid;grid-template-columns:240px minmax(0,1fr);gap:34px;align-items:start}
.app-nav{position:sticky;top:104px;padding:18px;border:1px solid var(--line);border-radius:14px;background:#111113}
.app-nav a{display:block;text-decoration:none;padding:10px;border-radius:9px;color:var(--ink-soft)}
.app-nav a[aria-current="page"],.app-nav a:hover{background:#1b1b1e;color:var(--paper)}
.metric-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin:22px 0}
.metric{padding:20px;border:1px solid var(--line);border-radius:14px;background:#111113}.metric b{font-size:1.55rem;display:block}.metric span{color:var(--ink-soft);font-size:.78rem;text-transform:uppercase;letter-spacing:.06em}
.notice{padding:18px;border-left:3px solid var(--amber);background:#161410;color:var(--paper-dim);margin:18px 0}
.thread{display:flex;flex-direction:column;gap:10px;margin-top:14px}.msg{padding:12px 14px;border-radius:12px;max-width:85%}.msg p{margin:0 0 4px}.msg-meta{font-size:.72rem;color:var(--ink-soft)}.msg-in{background:#1b1b1e;align-self:flex-start}.msg-out{background:#162018;align-self:flex-end;text-align:right}
@media(max-width:900px){.intent-grid{grid-template-columns:repeat(2,1fr)}.app-shell{grid-template-columns:1fr}.app-nav{position:static;display:flex;overflow-x:auto}.app-nav a{white-space:nowrap}.metric-grid{grid-template-columns:1fr}}
@media(max-width:520px){.intent-grid{grid-template-columns:1fr}.intent-card{min-height:112px}}

/* tags / status badges */
.badge{display:inline-block;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;padding:5px 10px;border-radius:999px;border:1px solid var(--line);color:var(--ink-soft)}
.badge-live{color:#bdf0d3;border-color:#1d5c3f}
.badge-dev{color:#ffdca8;border-color:#7a5a20}
.badge-research{color:#c9c8c4;border-color:#3a3a40}

table{width:100%;border-collapse:collapse;font-size:.92rem}
th,td{text-align:left;padding:12px 14px;border-bottom:1px solid var(--line)}
th{color:var(--ink-soft);font-weight:600;font-size:.78rem;text-transform:uppercase;letter-spacing:.06em}

/* 2026 cohesion pass: editorial depth without changing truthful content. */
.site-header{background:rgba(8,8,9,.78);backdrop-filter:saturate(150%) blur(22px)}
.site-header .bar{min-height:82px}.nav{gap:28px}.nav a{font-size:.86rem;letter-spacing:.015em}
.home-hero{min-height:min(840px,88vh)}.home-hero:before{width:720px;height:720px;background:radial-gradient(circle,rgba(237,28,36,.16) 0,rgba(242,154,22,.055) 35%,transparent 70%)}
.home-hero .container{position:relative}.home-hero h1{letter-spacing:-.055em;text-wrap:balance}.hero-sub{line-height:1.7}
.card{box-shadow:0 30px 80px -70px rgba(0,0,0,.9)}.card-link{transition:border-color .2s var(--ease),transform .2s var(--ease),background .2s var(--ease)}.card-link:hover{background:linear-gradient(145deg,#19191c,#111113)}
.btn{min-height:48px}.btn-primary{box-shadow:0 18px 48px -24px rgba(237,28,36,.65)}
@media(max-width:760px){.site-header .bar{min-height:72px}.home-hero{min-height:auto}.home-hero h1{text-wrap:pretty}.hero-sub{line-height:1.6}}
"""


# ============================================================
# Layout shell (header / nav / footer / meta) + small render helpers.
# No approved logo file exists in the repo (verified, see spec doc) --
# this uses a clean typographic wordmark and records the real asset as
# an open launch item rather than inventing a logo.
# ============================================================

NAV_ITEMS = [
    ("/services", "Services"), ("/work", "Work"),
    ("/solutions", "Solutions"), ("/business-launch-growth", "Launch & Growth"),
    ("/products", "Products"), ("/partners", "Partners"),
]

FOOTER_SERVICES = [
    ("/services/custom-software-engineering", "Custom Software Engineering"),
    ("/services/websites-and-web-applications", "Websites & Web Apps"),
    ("/services/ai-systems-and-agents", "AI Systems & Agents"),
    ("/services/digital-marketing", "Digital Marketing"),
]
FOOTER_COMPANY = [
    ("/about", "About"), ("/work", "Work"), ("/careers", "Careers"),
    ("/insights", "Insights"), ("/contact", "Contact"),
]
FOOTER_LEGAL = [
    ("/legal/privacy", "Privacy Policy"), ("/legal/terms", "Terms of Service"),
    ("/legal/accessibility", "Accessibility"), ("/portal/login", "Customer / Partner Sign In"),
]


def _logo_img(cls: str = "") -> str:
    """The real, approved Twenty Two Technologies mark. Replaces the
    temporary typographic wordmark once Aryan supplied the actual asset."""
    return (f'<span class="brand-mark {cls}" aria-hidden="true"><img src="{HEADER_LOGO_PATH}" '
             f'alt="" width="86" height="86"></span>')


def render_header(active_path: str) -> str:
    links = []
    for href, label in NAV_ITEMS:
        current = ' aria-current="page"' if active_path == href or active_path.startswith(href + "/") else ""
        links.append(f'<a href="{href}"{current}>{esc(label)}</a>')
    return f"""
<header class="site-header" id="site-header">
  <div class="bar">
    <a class="wordmark" href="/">{_logo_img()}<span>Twenty Two Technologies</span></a>
    <nav class="nav" aria-label="Primary">{''.join(links)}
      <a class="btn btn-primary" href="/contact/start-a-project" style="padding:9px 18px">Start a project</a>
    </nav>
    <button class="nav-toggle" id="nav-toggle" aria-expanded="false" aria-controls="site-header">
      <span class="visually-hidden">Toggle menu</span>&#9776;
    </button>
  </div>
</header>
<script>
document.getElementById('nav-toggle').addEventListener('click', function(){{
  var h = document.getElementById('site-header');
  var open = h.classList.toggle('open');
  this.setAttribute('aria-expanded', open ? 'true' : 'false');
}});
</script>
"""


def render_footer() -> str:
    def col(title, items):
        rows = "".join(f'<a href="{h}">{esc(l)}</a>' for h, l in items)
        return f'<div><h4>{esc(title)}</h4>{rows}</div>'
    year = datetime.now(timezone.utc).year
    return f"""
<footer class="site-footer">
  <div class="container">
    <div class="footer-grid">
      <div>
        <a class="wordmark" href="/">{_logo_img()}<span>Twenty Two Technologies</span></a>
        <p style="margin-top:16px;max-width:34ch">Established {FOUNDED_YEAR}. We design and build dependable
        software, AI systems, and digital products.</p>
        <p><a href="mailto:{SITE_EMAIL}" style="color:var(--paper);text-decoration:underline">{SITE_EMAIL}</a></p>
      </div>
      {col("Services", FOOTER_SERVICES)}
      {col("Company", FOOTER_COMPANY)}
      {col("Legal", FOOTER_LEGAL)}
    </div>
    <div class="footer-bottom">
      <span>&copy; {FOUNDED_YEAR} Twenty Two Technologies. All rights reserved.</span>
      <span>Built and operated on our own engineering stack.</span>
    </div>
  </div>
</footer>
"""


ORG_JSONLD = {
    "@context": "https://schema.org", "@type": "Organization",
    "name": "Twenty Two Technologies",
    "url": f"https://{SITE_DOMAIN}", "foundingDate": str(FOUNDED_YEAR),
    "email": SITE_EMAIL,
    "sameAs": [],
}


def page(title: str, description: str, path: str, body_html: str,
          extra_jsonld: Optional[List[Dict[str, Any]]] = None) -> bytes:
    full_title = f"{title} | {SITE_NAME}" if title != SITE_NAME else title
    canonical = f"https://{SITE_DOMAIN}{path}"
    jsonld_blocks = [ORG_JSONLD] + (extra_jsonld or [])
    jsonld_html = "".join(
        f'<script type="application/ld+json">{json.dumps(b)}</script>' for b in jsonld_blocks
    )
    html_doc = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(full_title)}</title>
<meta name="description" content="{esc(description)}">
<link rel="canonical" href="{canonical}">
<meta property="og:type" content="website">
<meta property="og:site_name" content="{esc(SITE_NAME)}">
<meta property="og:title" content="{esc(full_title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:url" content="{canonical}">
<meta property="og:image" content="https://{SITE_DOMAIN}{OG_IMAGE_PATH}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:image" content="https://{SITE_DOMAIN}{OG_IMAGE_PATH}">
<meta name="theme-color" content="#0d0d0e">
<link rel="icon" href="{FAVICON_PATH}">
<link rel="apple-touch-icon" href="{HEADER_LOGO_PATH}">
{jsonld_html}
<style>{SITE_CSS}</style>
</head>
<body>
<a class="visually-hidden" href="#main">Skip to content</a>
{render_header(path)}
<main id="main">
{body_html}
</main>
{render_footer()}
</body>
</html>"""
    return html_doc.encode("utf-8")


# ============================================================
# Homepage
# ============================================================

def render_home(services: List[Dict[str, Any]], case_studies: List[Dict[str, Any]],
                  products: List[Dict[str, Any]]) -> str:
    top_services = services[:3]
    service_cards = "".join(f"""
    <a href="/services/{esc(s['slug'])}">
      <span class="index">{i+1:02d}</span>
      <h3>{esc(s['division'])}</h3>
      <p>{esc(s['tagline'])}</p>
    </a>""" for i, s in enumerate(top_services))

    proof = case_studies[:2]
    proof_cards = "".join(f"""
    <a class="showcase-item" href="/work/{esc(c['slug'])}">
      <span class="eyebrow">{esc(c['client_label'])}</span>
      <h3>{esc(c['title'])}</h3>
      <p>{esc(c['summary'])}</p>
    </a>""" for c in proof)

    return f"""
<section class="hero home-hero">
  <div class="container">
    <span class="eyebrow">Twenty Two Technologies &middot; Est. {FOUNDED_YEAR}</span>
    <h1>We build software that is ready for the <em>real world.</em></h1>
    <p class="lede hero-sub">Custom platforms, AI systems, and digital products, designed with
    restraint, engineered for reliability, and delivered in clear milestones.</p>
    <div class="btn-row">
      <a class="btn btn-primary" href="/contact/start-a-project">Discuss your project</a>
      <a class="btn btn-ghost" href="/work">Explore selected work</a>
    </div>
  </div>
</section>

<section class="tight" aria-labelledby="intent-heading">
  <div class="container">
    <div class="section-head"><span class="eyebrow">Start with your goal</span><h2 id="intent-heading">Where do you want to go?</h2>
      <p>Choose the closest path. We will route the request to direct delivery, advisory, a qualified specialist, a product, or a licensed professional where required.</p></div>
    <div class="intent-grid">
      <a class="intent-card" href="/contact/start-a-project?intent=software"><strong>I need software</strong><span>Web, mobile, platforms and internal tools &rarr;</span></a>
      <a class="intent-card" href="/contact/start-a-project?intent=ai"><strong>I need AI or automation</strong><span>Useful systems with human control &rarr;</span></a>
      <a class="intent-card" href="/business-launch-growth"><strong>I want to start or grow</strong><span>Validation through expansion &rarr;</span></a>
      <a class="intent-card" href="/solutions"><strong>I need a specialist</strong><span>Direct, coordinated or referred help &rarr;</span></a>
      <a class="intent-card" href="/partners"><strong>I want to partner</strong><span>Register opportunities and contributions &rarr;</span></a>
      <a class="intent-card" href="/portal/login"><strong>I am a customer</strong><span>Projects, invoices and support &rarr;</span></a>
      <a class="intent-card" href="/pay"><strong>I need to pay or verify</strong><span>Official sandbox payment records &rarr;</span></a>
      <a class="intent-card" href="/products#falguna"><strong>I want FALGUNA</strong><span>AI product by Twenty Two Technologies &rarr;</span></a>
    </div>
  </div>
</section>

<section>
  <div class="container">
    <div class="section-head">
      <span class="eyebrow">Core capabilities</span>
      <h2>From the first useful release to the platform behind it.</h2>
      <p>Digital products, platforms, and experiences designed to put your business in motion.</p>
    </div>
    <div class="service-rail">{service_cards}</div>
    <div class="btn-row"><a class="btn btn-ghost" href="/services">View all services</a></div>
  </div>
</section>

<section class="showcase-section">
  <div class="container work-feature">
    <div>
      <span class="eyebrow">Proof of work</span>
      <h2>Selected work that speaks for itself.</h2>
      <p>Explore a selection of products and platforms from the Twenty Two Technologies studio.</p>
      <div class="btn-row"><a class="btn btn-primary" href="/work">See all work</a></div>
    </div>
    <div class="showcase-list">{proof_cards}</div>
  </div>
</section>

<section class="cta-band">
  <div class="container">
    <div>
      <span class="eyebrow">Let’s build what’s next</span>
      <h2>Bring your next digital product to life.</h2>
    </div>
    <a class="btn btn-primary" href="/contact/start-a-project">Start a conversation</a>
  </div>
</section>
"""


# ============================================================
# About
# ============================================================

def render_about() -> str:
    return f"""
<section class="hero" style="padding-bottom:60px">
  <div class="container">
    <span class="eyebrow">About</span>
    <h1>Established {FOUNDED_YEAR}. Still building the same way.</h1>
    <p class="lede hero-sub">Twenty Two Technologies is a software engineering studio: we design, build,
    and operate real products for clients and for ourselves. We don't outsource the parts that matter,
    and we don't ship what we haven't tested.</p>
  </div>
</section>

<section>
  <div class="container">
    <div class="section-head">
      <span class="eyebrow">The studio</span>
      <h2>Technology with ambition, clarity, and craft.</h2>
      <p>Twenty Two Technologies brings strategy, design, engineering, and growth together under one roof.
      We partner with organizations that want to move faster and build for what comes next.</p>
    </div>
    <div class="grid grid-3">
      <div><h3>Strategy</h3><p>Clear direction for complex digital opportunities.</p></div>
      <div><h3>Design</h3><p>Distinctive experiences that make technology easy to use.</p></div>
      <div><h3>Engineering</h3><p>Scalable systems built for the demands of modern business.</p></div>
    </div>
  </div>
</section>

<section>
  <div class="container">
    <div class="section-head">
      <span class="eyebrow">Where we're headed</span>
      <h2>A studio built to take on more, deliberately.</h2>
      <p>We're built to support a growing base of clients and an expanding portfolio of our own
      products, adding capacity and capability as real engagements justify it, not ahead of it.</p>
    </div>
    <div class="btn-row"><a class="btn btn-primary" href="/contact/start-a-project">Work with us</a>
      <a class="btn btn-ghost" href="/careers">See open roles</a></div>
  </div>
</section>
"""


# ============================================================
# Services (index + detail)
# ============================================================

STATUS_LABEL = {
    "current": ("badge-live", "Current capability"),
    "partner_qualified": ("badge-dev", "Via qualified partner"),
    "in_development": ("badge-dev", "In development"),
}


def render_services_index(services: List[Dict[str, Any]]) -> str:
    cards = "".join(f"""
    <a class="service-item" href="/services/{esc(s['slug'])}">
      <h3>{esc(s['division'])}</h3>
      <p>{esc(s['tagline'])}</p>
    </a>""" for s in services)
    return f"""
<section class="hero" style="padding-bottom:50px">
  <div class="container">
    <span class="eyebrow">Services</span>
    <h1>What we build, and how we deliver it.</h1>
    <p class="lede hero-sub">A full spectrum of technology, digital experience, and business services
    for ambitious companies.</p>
  </div>
</section>
<section class="tight"><div class="container"><div class="service-catalog">{cards}</div></div></section>
"""


def render_service_detail(s: Dict[str, Any], proof: List[Dict[str, Any]]) -> str:
    deliverables = "".join(f"<li>{esc(d)}</li>" for d in s["deliverables"])
    process = "".join(
        f"<tr><td>{i+1}</td><td><strong>{esc(step.get('step',''))}</strong><br>{esc(step.get('detail',''))}</td></tr>"
        for i, step in enumerate(s["process"])
    )
    proof_cards = "".join(f"""
    <a class="card card-link" href="/work/{esc(c['slug'])}">
      <span class="badge">{esc(c['client_label'])}</span>
      <h3 style="margin-top:14px">{esc(c['title'])}</h3>
    </a>""" for c in proof) or '<p>No published case study for this division yet.</p>'
    jsonld = [{
        "@context": "https://schema.org", "@type": "Service",
        "serviceType": s["division"], "provider": {"@type": "Organization", "name": SITE_NAME},
        "description": s["summary"],
    }]
    body = f"""
<section class="hero" style="padding-bottom:50px">
  <div class="container">
    <span class="eyebrow">Services</span>
    <h1>{esc(s['division'])}</h1>
    <p class="lede hero-sub">{esc(s['summary'])}</p>
  </div>
</section>
<section class="tight">
  <div class="container grid grid-2">
    <div><h2>What's included</h2><ul>{deliverables}</ul></div>
    <div><h2>Process</h2><table>{process}</table></div>
  </div>
</section>
<section class="panel-paper">
  <div class="container">
    <div class="section-head"><span class="eyebrow">Proof of work</span><h2>Relevant delivery</h2></div>
    <div class="grid grid-3">{proof_cards}</div>
  </div>
</section>
<section class="tight"><div class="container">
  <div class="btn-row"><a class="btn btn-primary" href="/contact/start-a-project">Start a project in this area</a></div>
</div></section>
"""
    return body, jsonld


# ============================================================
# Work / Case Studies (index + detail)
# ============================================================

def render_work_index(case_studies: List[Dict[str, Any]]) -> str:
    cards = "".join(f"""
    <a class="card card-link" href="/work/{esc(c['slug'])}">
      <span class="badge">{esc(c['client_label'])}</span>
      <h3 style="margin-top:14px">{esc(c['title'])}</h3>
      <p>{esc(c['summary'])}</p>
    </a>""" for c in case_studies)
    return f"""
<section class="hero" style="padding-bottom:50px">
  <div class="container">
    <span class="eyebrow">Work</span>
    <h1>Real projects. Named, and honestly labeled.</h1>
    <p class="lede hero-sub">Our own products are labeled as our own. Nothing here is a mockup or a
    fabricated client outcome.</p>
  </div>
</section>
<section class="tight"><div class="container"><div class="grid grid-3">{cards}</div></div></section>
"""


def render_case_study_detail(c: Dict[str, Any]) -> tuple:
    stack = "".join(f'<span class="badge" style="margin:0 8px 8px 0">{esc(t)}</span>' for t in c["stack"])
    own_note = ('<p class="form-note">This is a Twenty Two Technologies product/demonstration project, '
                 "shown as proof of engineering capability, not a paid third-party client engagement.</p>"
                 if c["is_own_project"] else "")
    jsonld = [{
        "@context": "https://schema.org", "@type": "CreativeWork",
        "name": c["title"], "about": c["summary"],
    }]
    body = f"""
<section class="hero" style="padding-bottom:40px">
  <div class="container">
    <span class="eyebrow">Work</span>
    <span class="badge">{esc(c['client_label'])}</span>
    <h1 style="margin-top:14px">{esc(c['title'])}</h1>
    <p class="lede hero-sub">{esc(c['summary'])}</p>
    {own_note}
  </div>
</section>
<section class="tight">
  <div class="container grid grid-2">
    <div>
      <h2>Problem</h2><p>{esc(c['problem'])}</p>
      <h2>Approach</h2><p>{esc(c['approach'])}</p>
      <h2>Outcome</h2><p>{esc(c['outcome'])}</p>
    </div>
    <div class="card"><h3>Stack</h3><div>{stack}</div></div>
  </div>
</section>
<section class="tight"><div class="container">
  <div class="btn-row"><a class="btn btn-primary" href="/contact/start-a-project">Discuss a similar project</a>
    <a class="btn btn-ghost" href="/work">All work</a></div>
</div></section>
"""
    return body, jsonld


# ============================================================
# Products & Ventures
# ============================================================

def render_products(products: List[Dict[str, Any]]) -> str:
    cards = "".join(f"""
    <div class="showcase-item">
      <h3>{f'<img src="{FALGUNA_MARK_PATH}" alt="" width="24" height="24" style="vertical-align:-5px;margin-right:8px;border-radius:6px">' if p['name'].strip().lower() == 'falguna' else ''}{esc(p['name'])}</h3>
      <p>{esc(p['tagline'])}</p>
      <p>{esc(p['summary'])}</p>
    </div>""" for p in products)
    return f"""
<section class="hero" style="padding-bottom:50px">
  <div class="container">
    <span class="eyebrow">Products & Ventures</span>
    <h1>What we're building ourselves.</h1>
    <p class="lede hero-sub">Ideas, platforms, and products created by the Twenty Two Technologies studio.</p>
  </div>
</section>
<section class="tight"><div class="container"><div class="showcase-list">{cards}</div></div></section>
"""


# ============================================================
# Careers (index + detail + application form)
# ============================================================

def render_careers_index(jobs: List[Dict[str, Any]]) -> str:
    listing = "".join(f'<a class="showcase-item" href="/careers/{esc(j["slug"])}"><h3>{esc(j["title"])}</h3><p>{esc(j["department"])}</p></a>' for j in jobs)
    listing += '<p class="form-note">Send your resume and we will be in touch when a suitable opportunity opens.</p>'
    return f"""
<section class="hero" style="padding-bottom:50px">
  <div class="container">
    <span class="eyebrow">Careers</span>
    <h1>Build real software with us.</h1>
    <p class="lede hero-sub">We are always interested in people who build, think, and move with purpose.</p>
  </div>
</section>
<section class="tight"><div class="container"><div class="showcase-list">{listing}</div>
  <div class="btn-row"><a class="btn btn-ghost" href="/careers/apply">Send a general application</a></div>
</div></section>
"""


def render_job_detail(j: Dict[str, Any]) -> tuple:
    resp = "".join(f"<li>{esc(r)}</li>" for r in j["responsibilities"])
    req = "".join(f"<li>{esc(r)}</li>" for r in j["requirements"])
    jsonld = [{
        "@context": "https://schema.org", "@type": "JobPosting",
        "title": j["title"], "description": j["summary"],
        "employmentType": j["employment_type"].upper(),
        "datePosted": j["posted_at"], "hiringOrganization": {"@type": "Organization", "name": SITE_NAME},
        "jobLocationType": "TELECOMMUTE",
    }]
    body = f"""
<section class="hero" style="padding-bottom:40px">
  <div class="container">
    <span class="eyebrow">Careers</span>
    <span class="badge">{esc(j['department'])}</span>
    <h1 style="margin-top:14px">{esc(j['title'])}</h1>
    <p class="lede hero-sub">{esc(j['summary'])}</p>
  </div>
</section>
<section class="tight">
  <div class="container grid grid-2">
    <div><h2>Responsibilities</h2><ul>{resp}</ul></div>
    <div><h2>What we're looking for</h2><ul>{req}</ul></div>
  </div>
</section>
<section class="tight"><div class="container">
  <div class="btn-row"><a class="btn btn-primary" href="/careers/apply?job={esc(j['slug'])}">Apply for this role</a></div>
</div></section>
"""
    return body, jsonld


def render_apply_form(job: Optional[Dict[str, Any]], error: Optional[str] = None,
                        success: bool = False, csrf_token: str = "") -> str:
    job_title = job["title"] if job else "General Application"
    job_field = f'<input type="hidden" name="job_slug" value="{esc(job["slug"])}">' if job else ""
    alert = ""
    if success:
        alert = '<div class="alert alert-success">Application received. We review every submission. Thank you.</div>'
    elif error:
        alert = f'<div class="alert alert-error">{esc(error)}</div>'
    form = "" if success else f"""
    <form class="stack" method="post" action="/careers/apply" enctype="multipart/form-data">
      <input type="hidden" name="csrf_token" value="{esc(csrf_token)}">
      {job_field}
      <div class="field"><label for="name">Full name</label><input id="name" name="name" required></div>
      <div class="field"><label for="email">Email</label><input id="email" name="email" type="email" required></div>
      <div class="field"><label for="resume">Resume (PDF or Word, max 8MB)</label><input id="resume" name="resume" type="file" accept=".pdf,.doc,.docx" required></div>
      <input type="text" name="website" id="website" style="position:absolute;left:-9999px" tabindex="-1" autocomplete="off">
      <button class="btn btn-primary" type="submit">Submit application</button>
    </form>"""
    return f"""
<section class="hero" style="padding-bottom:40px">
  <div class="container">
    <span class="eyebrow">Careers</span>
  <h1>Apply: {esc(job_title)}</h1>
  </div>
</section>
<section class="tight"><div class="container">{alert}{form}</div></section>
"""


# ============================================================
# Contact / Start a Project
# ============================================================

def render_contact_hub() -> str:
    return f"""
<section class="hero" style="padding-bottom:50px">
  <div class="container">
    <span class="eyebrow">Contact</span>
    <h1>Tell us what you need.</h1>
    <p class="lede hero-sub">Two paths below: a scoped project enquiry that goes straight into our
    delivery pipeline, or a general question.</p>
  </div>
</section>
<section class="tight"><div class="container grid grid-2">
  <a class="card card-link" href="/contact/start-a-project"><h3>Start a project</h3>
    <p>Have a concrete project, however small or large? This routes directly to our project pipeline.</p></a>
  <a class="card card-link" href="/contact/general"><h3>General enquiry</h3>
    <p>Questions, partnerships, press, or anything else.</p></a>
</div></section>
<section class="tight"><div class="container">
  <p>General: <a href="mailto:{SITE_EMAIL}" style="color:var(--paper);text-decoration:underline">{SITE_EMAIL}</a><br>
  Projects: <a href="mailto:{SALES_EMAIL}" style="color:var(--paper);text-decoration:underline">{SALES_EMAIL}</a><br>
  Careers: <a href="mailto:{CAREERS_EMAIL}" style="color:var(--paper);text-decoration:underline">{CAREERS_EMAIL}</a><br>
  Media: <a href="mailto:{MEDIA_EMAIL}" style="color:var(--paper);text-decoration:underline">{MEDIA_EMAIL}</a></p>
</div></section>
"""


def render_solutions() -> str:
    return """<section class="hero"><div class="container"><span class="eyebrow">Solutions</span>
    <h1>One front door. The right delivery route.</h1><p class="lede hero-sub">We assess the need, evidence and risk before choosing direct delivery, advisory, coordinated specialists, sourced providers, referral, or a product fit.</p></div></section>
    <section><div class="container"><div class="grid grid-3">
    <div class="card"><h3>Direct delivery</h3><p>Software, web applications, automation and product engineering where our demonstrated capability fits.</p></div>
    <div class="card"><h3>Coordinated expertise</h3><p>A clearly identified specialist or provider, with scope and accountability made explicit.</p></div>
    <div class="card"><h3>Responsible routing</h3><p>Regulated legal, tax, medical and financial advice is routed to appropriately qualified professionals.</p></div>
    </div><div class="btn-row"><a class="btn btn-primary" href="/contact/start-a-project">Describe what you need</a></div></div></section>"""


def render_business_launch_growth() -> str:
    stages = [("01", "Validate", "Clarify the customer, problem, evidence and commercial model."),
              ("02", "Build the identity", "Brand, positioning and a credible market presence."),
              ("03", "Build the system", "Software, operations and measurable delivery workflows."),
              ("04", "Reach customers", "Structured acquisition foundations and campaign-ready assets."),
              ("05", "Grow deliberately", "Improve what works, expand capacity and enter new markets with evidence.")]
    cards = "".join(f'<div class="card"><span class="index">{n}</span><h3>{esc(t)}</h3><p>{esc(d)}</p></div>' for n,t,d in stages)
    return f"""<section class="hero"><div class="container"><span class="eyebrow">Business Launch &amp; Growth</span>
    <h1>From idea and capital to a working business.</h1><p class="lede hero-sub">A practical route through validation, branding, technology, operations, customer acquisition, growth and expansion.</p></div></section>
    <section><div class="container"><div class="grid grid-3">{cards}</div>
    <div class="notice">We do not present regulated legal, tax, medical or financial advice as our own. Where a requirement needs a licensed professional, that boundary and provider role will be explicit.</div>
    <div class="btn-row"><a class="btn btn-primary" href="/contact/start-a-project?intent=business-growth">Plan the next step</a></div></div></section>"""


def render_partner_program(verification=None) -> str:
    result = ""
    if verification is not None:
        label = {"ACTIVE_VALID": "Active and valid", "SUSPENDED_INVALID": "Suspended or invalid", "UNKNOWN": "Unknown partner ID"}[verification["state"]]
        result = f'<div class="alert {"alert-success" if verification["valid"] else "alert-error"}" role="status"><strong>{label}</strong><br>{esc(verification.get("name") or "No active authorization could be confirmed.")}</div>'
    return """<section class="hero"><div class="container"><span class="eyebrow">Partner with TTT</span>
    <h1>Bring opportunities. Contribute clearly. Earn under written rules.</h1><p class="lede hero-sub">The partner network is designed for attributable leads and verified contributions, with transparent eligibility, holds and clawbacks.</p>
    <div class="btn-row"><a class="btn btn-primary" href="/contact/general">Express interest</a><a class="btn btn-ghost" href="/portal/login">Partner sign in</a></div></div></section>
    <section><div class="container"><div class="grid grid-3"><div class="card"><h3>Attribution</h3><p>Registered leads and contribution history create an auditable record.</p></div><div class="card"><h3>Eligibility</h3><p>Commission status follows the official policy and verified commercial state.</p></div><div class="card"><h3>No money collection</h3><p>Partners and representatives may not collect customer money. Only official TTT payment instructions are valid.</p></div></div>""" + result + """<div class="card"><h2>Verify a partner</h2><form class="stack" method="get" action="/partners"><div class="field"><label for="partner-id">Partner ID</label><input id="partner-id" name="partner_id" required></div><button class="btn btn-primary" type="submit">Verify partner</button></form></div></div></section>"""


def render_portal_login(csrf_token: str, error: Optional[str] = None) -> str:
    alert = f'<div class="alert alert-error">{esc(error)}</div>' if error else ""
    return f"""<section class="hero"><div class="container"><span class="eyebrow">Secure application</span><h1>Customer and partner sign in.</h1>
    <p class="lede hero-sub">Use the account issued for your organization or partner profile.</p>{alert}
    <form class="stack" method="post" action="/portal/login"><input type="hidden" name="csrf_token" value="{esc(csrf_token)}">
    <div class="field"><label for="portal-email">Email</label><input id="portal-email" name="email" type="email" autocomplete="username" required></div>
    <div class="field"><label for="portal-password">Password</label><input id="portal-password" name="password" type="password" autocomplete="current-password" required></div>
    <button class="btn btn-primary" type="submit">Sign in</button></form></div></section>"""


def _portal_nav(role, active, csrf):
    links = []
    if role == "CUSTOMER":
        links = [("/app", "Overview"), ("/app/projects", "Projects"),
                 ("/app/billing", "Billing & payments"), ("/app/support", "Support"), ("/app/account", "Account & security")]
    else:
        links = [("/partners/app", "Partner overview"), ("/partners/app/leads", "Registered leads"),
                 ("/partners/app/leads/new", "Register a lead"), ("/partners/app/commissions", "Contributions & commissions"),
                 ("/partners/app/account", "Account & security")]

    def is_current(href):
        if href == active:
            return True
        if href == "/app/projects" and active.startswith("/app/projects/"):
            return True
        return False

    rows = "".join(f'<a href="{h}"' + (' aria-current="page"' if is_current(h) else '') + f'>{esc(l)}</a>' for h, l in links)
    return f'<nav class="app-nav" aria-label="Application">{rows}<form method="post" action="/portal/logout"><input type="hidden" name="csrf_token" value="{esc(csrf)}"><button class="btn btn-ghost" type="submit">Sign out</button></form></nav>'


def _format_money(currency, amount):
    if amount is None:
        return "—"
    try:
        return f'{esc(currency or "")} {float(amount):,.2f}'.strip()
    except (TypeError, ValueError):
        return "—"


def render_project_detail(detail):
    """Phase 7, Sections 1-2: one project's customer-facing detail --
    status, the real approved SOW reference (when one exists), milestones
    derived from that same SOW (never invented), and the invoices tied to
    this project's own client. `detail` is exactly what
    ExternalPortalService.customer_project_detail() returns."""
    project, sow, milestones, invoices = detail["project"], detail["sow"], detail["milestones"], detail["invoices"]

    if sow is None:
        sow_html = ('<div class="card"><h3>Approved scope</h3>'
                     '<p>No formal scope of work is on file for this project yet.</p></div>')
    else:
        scope_text = esc(sow.get("final_scope") or "Not recorded.")
        deliverables_text = esc(sow.get("deliverables") or "Not recorded.")
        acceptance_text = esc(sow.get("acceptance_criteria") or "Not recorded.")
        price_text = _format_money(sow.get("currency"), sow.get("final_price"))
        sow_html = (
            '<div class="card"><h3>Approved scope</h3>'
            f'<p>{scope_text}</p>'
            f'<p><b>Agreed price:</b> {price_text}'
            f' &middot; <b>Payment terms:</b> {esc(sow.get("payment_terms") or "Not recorded.")}'
            f' &middot; <b>Deadline:</b> {esc(sow.get("deadline") or "Not recorded.")}</p>'
            f'<p><b>Deliverables:</b> {deliverables_text}</p>'
            f'<p><b>Acceptance criteria:</b> {acceptance_text}</p>'
            '</div>'
        )

    if milestones:
        milestone_rows = "".join(
            f'<tr><td>{esc(m["name"])}</td><td>{esc(m["status"])}</td>'
            f'<td>{_format_money(None, m.get("amount"))}</td>'
            f'<td>{esc(m.get("due_date") or "—")}</td></tr>'
            for m in milestones
        )
    else:
        milestone_rows = '<tr><td colspan="4">No milestones are recorded in the approved scope yet.</td></tr>'
    milestones_html = (
        '<div class="card"><h3>Milestones</h3>'
        '<table><thead><tr><th>Milestone</th><th>Status</th><th>Amount</th><th>Due</th></tr></thead>'
        f'<tbody>{milestone_rows}</tbody></table></div>'
    )

    if invoices:
        invoice_rows = "".join(
            f'<tr><td>{esc(i.get("milestone") or i["id"])}</td><td>{esc(i["status"])}</td>'
            f'<td>{_format_money(i["currency"], i["amount"])}</td><td>{esc(i.get("due_date") or "—")}</td></tr>'
            for i in invoices
        )
    else:
        invoice_rows = '<tr><td colspan="4">No invoices are linked to this project yet.</td></tr>'
    invoices_html = (
        '<div class="card"><h3>Linked invoices</h3>'
        '<table><thead><tr><th>Reference</th><th>Status</th><th>Amount</th><th>Due</th></tr></thead>'
        f'<tbody>{invoice_rows}</tbody></table></div>'
    )

    return (
        f'<div class="card"><h3>{esc(project["id"])}</h3>'
        f'<p>Status: {esc(project["status"])} &middot; Delivery route: {esc(project["delivery_route"])}</p>'
        '<a class="btn btn-ghost" href="/app/projects">Back to projects</a></div>'
        f'{sow_html}{milestones_html}{invoices_html}'
        '<div class="card"><h3>Need help with this project?</h3>'
        '<a class="btn btn-ghost" href="/contact/general">Contact support</a></div>'
    )


def render_project_not_found():
    return ('<div class="card"><h3>Project not found</h3>'
             '<p>This project is not linked to your account.</p>'
             '<a class="btn btn-ghost" href="/app/projects">Back to projects</a></div>')


def render_support_section(bundle):
    """Phase 7, Section 4: real conversation/message history, reusing the
    existing Phase 5 communications architecture's own customer-safe
    projection (CustomerPortalService._customer_safe_conversation) rather
    than inventing a parallel support model. Only ever shows messages that
    are the customer's own (INBOUND) or that staff have actually sent/
    approved -- a DRAFT reply awaiting human review is never shown here."""
    conversations = bundle["conversations"]
    if not conversations:
        threads_html = '<div class="card"><p>No conversations on file yet.</p></div>'
    else:
        cards = []
        for conv in conversations:
            if conv["messages"]:
                msg_html = "".join(
                    '<div class="msg msg-' + ("out" if m["direction"] == "OUTBOUND" else "in") + '">'
                    f'<p>{esc(m["body"])}</p><span class="msg-meta">{esc(m.get("created_at") or "")}</span></div>'
                    for m in conv["messages"]
                )
            else:
                msg_html = '<p>No messages yet.</p>'
            subject = esc(conv.get("subject") or conv["department"].replace("_", " ").title())
            cards.append(
                f'<div class="card"><h3>{subject}</h3>'
                f'<p>Status: {esc(conv["status"])} &middot; {esc(conv["department"].replace("_", " ").title())}</p>'
                f'<div class="thread">{msg_html}</div></div>'
            )
        threads_html = "".join(cards)
    return (
        '<div class="card"><h3>Support and communication</h3>'
        '<p>Your customer-safe communication history and open support issues are shown below.</p>'
        '<a class="btn btn-primary" href="/contact/general">Contact support</a></div>'
        f'{threads_html}'
    )


def render_customer_app(identity, bundle, payments, active, csrf, project_detail="__unset__"):
    projects, invoices = bundle["projects"], bundle["invoices"]
    outstanding = sum(max(0, float(i["amount"]) - float(i["amount_received"])) for i in invoices)
    rows = "".join(
        f'<tr><td><a href="/app/billing/{esc(i["id"])}">{esc(i.get("milestone") or i["id"])}</a></td><td>{esc(i["status"])}</td>'
        f'<td>{esc(i["currency"])} {float(i["amount"]):,.2f}</td><td>{esc(i.get("due_date") or "—")}</td></tr>'
        for i in invoices
    ) or '<tr><td colspan="4">No invoices are currently available.</td></tr>'
    project_rows = "".join(
        f'<a class="card card-link" href="/app/projects/{esc(p["id"])}"><h3>{esc(p["id"])}</h3>'
        f'<p>Status: {esc(p["status"])}<br>Delivery route: {esc(p["delivery_route"])}</p></a>'
        for p in projects
    ) or '<div class="card"><h3>No active projects</h3><p>Your approved projects will appear here.</p></div>'

    if active.startswith("/app/projects/") and active != "/app/projects":
        content = render_project_not_found() if project_detail is None else render_project_detail(project_detail)
    elif active == "/app/projects":
        content = project_rows
    elif active == "/app/billing":
        content = (f'<div class="card"><h3>Invoices</h3><table><thead><tr><th>Reference</th><th>Status</th>'
                    f'<th>Amount</th><th>Due</th></tr></thead><tbody>{rows}</tbody></table></div>')
    elif active == "/app/support":
        content = render_support_section(bundle)
    else:
        content = (f'<div class="metric-grid"><div class="metric"><b>{len(projects)}</b><span>Projects</span></div>'
                    f'<div class="metric"><b>{len(invoices)}</b><span>Invoices</span></div>'
                    f'<div class="metric"><b>{outstanding:,.2f}</b><span>Outstanding</span></div></div>{project_rows}')

    return (f'<section class="hero" style="padding-bottom:38px"><div class="container">'
            f'<span class="eyebrow">Customer application</span><h1>Welcome, {esc(identity["display_name"])}.</h1>'
            f'</div></section><section class="tight"><div class="container app-shell">'
            f'{_portal_nav("CUSTOMER", active, csrf)}<div>{content}</div></div></section>')


def render_invoice_detail(detail):
    i = detail["invoice"]
    outstanding = max(0, float(i["amount"]) - float(i["amount_received"]))
    receipt_rows = "".join(f'<tr><td>{esc(r["id"])}</td><td>{_format_money(r["currency"], r["amount"])}</td><td>{esc(r["issued_at"])}</td><td>{esc(r["status"])}</td><td>{esc(r.get("provider_transaction_ref") or "—")}</td></tr>' for r in detail["receipts"]) or '<tr><td colspan="5">No verified receipt has been issued.</td></tr>'
    refund_rows = "".join(f'<tr><td>{esc(r["id"])}</td><td>{esc(r["status"])}</td><td>{_format_money(r["currency"], r["amount"])}</td><td>{esc(r.get("provider_ref") or "Available after confirmation")}</td></tr>' for r in detail["refunds"]) or '<tr><td colspan="4">No refund is recorded.</td></tr>'
    dispute_rows = "".join(f'<tr><td>{esc(d["id"])}</td><td>{esc(d["status"])}</td><td>{esc(d["reason"])}</td><td>{esc(d.get("resolution") or "Pending")}</td></tr>' for d in detail["disputes"]) or '<tr><td colspan="4">No dispute is recorded.</td></tr>'
    sub_rows = "".join(f'<tr><td>{esc(s["plan_name"])}</td><td>{esc(s["cadence"])}</td><td>{esc(s["status"])}</td><td>{esc(s["next_billing_date"])}</td><td>{esc(s["autopay_status"])}</td><td>{esc(s.get("mandate_ref") or s.get("provider_token_ref") or "Manual")}</td></tr>' for s in detail["subscriptions"]) or '<tr><td colspan="6">No subscription is linked to this invoice.</td></tr>'
    return (f'<div class="card"><h2>Invoice {esc(i["id"])}</h2><p>Milestone/project reference: {esc(i.get("milestone") or "—")}</p>'
            f'<div class="metric-grid"><div class="metric"><b>{esc(i["status"])}</b><span>Payment state</span></div><div class="metric"><b>{_format_money(i["currency"], i["amount"])}</b><span>Invoice amount</span></div><div class="metric"><b>{_format_money(i["currency"], outstanding)}</b><span>Outstanding</span></div></div>'
            f'<p>Due date: {esc(i.get("due_date") or "Not set")}. Settlement is shown only after verified reconciliation.</p></div>'
            f'<div class="card"><h3>Receipts</h3><table><thead><tr><th>Receipt</th><th>Amount</th><th>Payment date</th><th>Status</th><th>Provider reference</th></tr></thead><tbody>{receipt_rows}</tbody></table></div>'
            f'<div class="card"><h3>Refunds</h3><table><thead><tr><th>Reference</th><th>Status</th><th>Amount</th><th>Provider reference</th></tr></thead><tbody>{refund_rows}</tbody></table></div>'
            f'<div class="card"><h3>Disputes</h3><table><thead><tr><th>Reference</th><th>Status</th><th>Customer-safe summary</th><th>Resolution</th></tr></thead><tbody>{dispute_rows}</tbody></table></div>'
            f'<div class="card"><h3>Subscription / retainer</h3><table><thead><tr><th>Plan</th><th>Cadence</th><th>Status</th><th>Next billing</th><th>Payment state</th><th>Mandate/token reference</th></tr></thead><tbody>{sub_rows}</tbody></table></div>')


def render_account_security(account, csrf, message=None, error=None):
    notice = f'<div class="alert {"alert-error" if error else "alert-success"}" role="status">{esc(error or message)}</div>' if (message or error) else ""
    sessions = "".join(f'<tr><td>{"Current" if s["current"] else "Other"}</td><td>{esc(s["created_at"])}</td><td>{esc(s["expires_at"])}</td><td>{"Active" if s["active"] else "Ended"}</td><td>{esc(s["user_agent"])}</td></tr>' for s in account["sessions"])
    return (f'{notice}<div class="card"><h2>Account and security</h2><p>{esc(account["display_name"])} · {esc(account["email"])} · {esc(account["role"])}</p>'
            '<h3>Sessions</h3><table><thead><tr><th>Session</th><th>Started</th><th>Expires</th><th>Status</th><th>Browser</th></tr></thead>'f'<tbody>{sessions}</tbody></table><p>Sign out ends the current session. Changing your password ends every other active session.</p></div>'
            '<div class="card"><h3>Change password</h3><form class="stack" method="post" action="/portal/password">'f'<input type="hidden" name="csrf_token" value="{esc(csrf)}"><div class="field"><label for="current-password">Current password</label><input id="current-password" name="current_password" type="password" autocomplete="current-password" required></div><div class="field"><label for="new-password">New password (at least 12 characters)</label><input id="new-password" name="new_password" type="password" autocomplete="new-password" minlength="12" required></div><button class="btn btn-primary" type="submit">Change password</button></form></div>'
            '<div class="card"><h3>Recovery and MFA</h3><p>Password recovery is operator-assisted until a verified external email channel is activated. No reset link is sent from this local system.</p><p>MFA is a production requirement but is not represented as live: the current architecture records the policy seam and requires a provider-backed factor before activation.</p></div>')


def render_policy_acknowledgement(csrf, error=None):
    alert = f'<div class="alert alert-error">{esc(error)}</div>' if error else ""
    return (
        '<div class="card"><h3>Partner payment policy</h3>'
        f'<p>{esc(PUBLIC_PAYMENT_WARNING)}</p>'
        '<p>You must read and agree to this before registering a new lead.</p>'
        f'{alert}'
        '<form method="post" action="/partners/app/policy/acknowledge">'
        f'<input type="hidden" name="csrf_token" value="{esc(csrf)}">'
        '<button class="btn btn-primary" type="submit">I understand and agree</button></form></div>'
    )


def render_lead_form(csrf, error=None, success=False):
    if success:
        return ('<div class="card"><h3>Lead registered</h3>'
                 '<p class="alert alert-success">Thank you -- this lead has been registered and will go through '
                 'TTT qualification. Attribution and any duplicate/conflict review happen automatically; you can '
                 'track its status from Registered Leads.</p>'
                 '<a class="btn btn-ghost" href="/partners/app/leads">Back to registered leads</a></div>')
    alert = f'<div class="alert alert-error">{esc(error)}</div>' if error else ""
    return (
        '<div class="card"><h3>Register a new lead</h3>'
        f'<p>{esc(PUBLIC_PAYMENT_WARNING)}</p>'
        f'{alert}'
        '<form class="stack" method="post" action="/partners/app/leads">'
        f'<input type="hidden" name="csrf_token" value="{esc(csrf)}">'
        '<div class="field"><label for="lead_prospect_name">Prospect / contact name</label>'
        '<input id="lead_prospect_name" name="prospect_name" required></div>'
        '<div class="field"><label for="lead_org">Organization (optional)</label>'
        '<input id="lead_org" name="organization_name"></div>'
        '<div class="field"><label for="lead_email">Contact email</label>'
        '<input id="lead_email" name="contact_email" type="email"></div>'
        '<div class="field"><label for="lead_phone">Contact phone</label>'
        '<input id="lead_phone" name="contact_phone"></div>'
        '<div class="field"><label for="lead_region">Country / region</label>'
        '<input id="lead_region" name="region"></div>'
        '<div class="field"><label for="lead_industry">Industry / business type</label>'
        '<input id="lead_industry" name="industry"></div>'
        '<div class="field"><label for="lead_service">What do they need?</label>'
        '<input id="lead_service" name="requested_service" required></div>'
        '<div class="field"><label for="lead_value">Estimated opportunity size (optional)</label>'
        '<input id="lead_value" name="estimated_value" placeholder="e.g. 5000"></div>'
        '<div class="field"><label for="lead_source">Source / context</label>'
        '<input id="lead_source" name="referral_source" placeholder="e.g. existing contact, event, referral"></div>'
        '<div class="field"><label for="lead_disclosure">Relationship disclosure (optional)</label>'
        '<input id="lead_disclosure" name="relationship_disclosure" '
        'placeholder="e.g. this is a company I am also an employee of"></div>'
        '<div class="field"><label for="lead_notes">Notes</label><textarea id="lead_notes" name="notes"></textarea></div>'
        '<button class="btn btn-primary" type="submit">Register lead</button></form></div>'
    )


def render_partner_app(identity, bundle, active, csrf, lead_form_error=None, lead_form_success=False):
    p, refs, commissions = bundle["partner"], bundle["referrals"], bundle["commissions"]
    contributions = bundle["contributions"]
    policy_ok = bool(p.get("policy_acknowledged_at"))
    lead_rows = "".join(
        f'<tr><td>{esc(r["prospect_name"])}</td><td>{esc(r["requested_service"])}</td>'
        f'<td>{esc(r["attribution_status"])}</td>'
        f'<td>{"Duplicate/conflict under review" if r.get("duplicate_flag") else "—"}</td></tr>'
        for r in refs
    ) or '<tr><td colspan="4">No registered leads.</td></tr>'
    commission_rows = "".join(
        f'<tr><td>{esc(c["id"])}</td><td>{esc(c["status"])}</td><td>{float(c.get("eligible_amount") or 0):,.2f}</td></tr>'
        for c in commissions
    ) or '<tr><td colspan="3">No commission records.</td></tr>'

    if active.endswith("/leads/new"):
        content = render_policy_acknowledgement(csrf) if not policy_ok else render_lead_form(
            csrf, error=lead_form_error, success=lead_form_success,
        )
    elif active.endswith("/leads"):
        register_link = '<a class="btn btn-primary" href="/partners/app/leads/new">Register a new lead</a>'
        content = (
            f'<div class="card"><h3>Registered leads</h3>{register_link}'
            '<table><thead><tr><th>Prospect</th><th>Need</th><th>Attribution</th><th>Flag</th></tr></thead>'
            f'<tbody>{lead_rows}</tbody></table></div>'
        )
    elif active.endswith("/commissions"):
        contribution_rows = "".join(f'<tr><td>{esc(c["id"])}</td><td>{esc(c.get("contribution_type") or "—")}</td><td>{esc(c.get("referral_id") or "—")}</td><td>{esc(c.get("opportunity_id") or "—")}</td><td>{esc(c["status"])}</td></tr>' for c in contributions) or '<tr><td colspan="5">No contribution records.</td></tr>'
        content = (
            '<div class="card"><h3>Contribution history</h3><table><thead><tr><th>Reference</th><th>Type</th><th>Lead</th><th>Opportunity</th><th>Status</th></tr></thead>'f'<tbody>{contribution_rows}</tbody></table></div><div class="card"><h3>Commission status</h3><p>Amounts are based only on eligible, recorded collections; customer-private finance is not shown.</p>'
            '<table><thead><tr><th>Reference</th><th>Status</th><th>Eligible / accrued amount</th></tr></thead>'f'<tbody>{commission_rows}</tbody></table></div>'
        )
    else:
        policy_prompt = "" if policy_ok else (
            '<div class="card"><h3>Before you register a lead</h3>'
            '<p>You have not yet acknowledged the TTT partner payment policy.</p>'
            '<a class="btn btn-ghost" href="/partners/app/leads/new">Review and acknowledge</a></div>'
        )
        content = (
            f'<div class="metric-grid"><div class="metric"><b>{len(refs)}</b><span>Registered leads</span></div>'
            f'<div class="metric"><b>{len(commissions)}</b><span>Commission records</span></div>'
            f'<div class="metric"><b>{esc(p.get("verification_status") or "—")}</b><span>Verification</span></div></div>'
            f'<div class="notice">{esc(PUBLIC_PAYMENT_WARNING)}</div>{policy_prompt}'
        )

    return (
        f'<section class="hero" style="padding-bottom:38px"><div class="container">'
        f'<span class="eyebrow">Partner application</span><h1>{esc(p["full_name"])}.</h1>'
        f'<p class="lede hero-sub">Partner ID {esc(p["id"])} &middot; {esc(p.get("status") or "")}</p>'
        f'</div></section><section class="tight"><div class="container app-shell">'
        f'{_portal_nav("PARTNER", active, csrf)}<div>{content}</div></div></section>'
    )


def render_pay(identity: Optional[Dict[str, Any]], payment_bundle: Optional[Dict[str, Any]], csrf_token: str, verification: Optional[Dict[str, Any]] = None) -> str:
    result = ""
    if verification is not None:
        result = f'<div class="alert {"alert-success" if verification.get("valid") else "alert-error"}"><strong>{"Verified" if verification.get("valid") else "Not verified"}</strong><br>{esc(verification.get("warning") or PUBLIC_PAYMENT_WARNING)}</div>'
    account = '<p><a class="btn btn-primary" href="/portal/login">Sign in to view your payments</a></p>'
    if identity and payment_bundle is not None:
        rows = "".join(f'<tr><td>{esc(p["id"])}</td><td>{esc(p["status"])}</td><td>{esc(p["currency"])} {float(p["amount"]):,.2f}</td><td>{esc(", ".join(p["allowed_methods"]))}</td></tr>' for p in payment_bundle["payments"]) or '<tr><td colspan="4">No payment sessions are available.</td></tr>'
        account = f'<div class="card"><h3>Your payment records</h3><table><thead><tr><th>Reference</th><th>Status</th><th>Amount</th><th>Capabilities</th></tr></thead><tbody>{rows}</tbody></table></div>'
    return f'''<section class="hero"><div class="container"><span class="eyebrow">TTT Payments · sandbox</span><h1>Pay and verify from the official record.</h1><p class="lede hero-sub">This Phase 7 surface displays provider-neutral sandbox state only. A browser response never creates payment success.</p></div></section><section class="tight"><div class="container">{result}<div class="notice">{esc(PUBLIC_PAYMENT_WARNING)}</div>{account}<div class="card" style="margin-top:22px"><h3>Verify payment instructions</h3><form class="stack" method="post" action="/pay/verify"><input type="hidden" name="csrf_token" value="{esc(csrf_token)}"><div class="field"><label>Payment reference</label><input name="payment_id" required></div><div class="field"><label>Customer reference</label><input name="customer_ref" required></div><div class="field"><label>Beneficiary reference</label><input name="beneficiary_ref" required></div><button class="btn btn-primary" type="submit">Verify instruction</button></form></div></div></section>'''


def _intake_category_field() -> str:
    options = "".join(f'<option value="{esc(k)}">{esc(label)}</option>' for k, label in INTAKE_CATEGORIES)
    return (
        '<div class="field"><label for="intake_category">What best describes what you need?</label>'
        '<select id="intake_category" name="intake_category">'
        '<option value="">Not sure -- describe it below</option>' + options + '</select></div>'
    )


def _contact_form_fields(kind: str) -> str:
    if kind == "project":
        return """
      <div class="field"><label for="project_title">Project title</label><input id="project_title" name="project_title"></div>
      <div class="field"><label for="company">Company (optional)</label><input id="company" name="company"></div>
      <div class="field"><label for="project_type">Engagement type</label>
        <select id="project_type" name="project_type">
          <option value="">Not sure yet</option><option>Fixed-scope build</option>
          <option>Ongoing/retainer</option><option>Contract-to-hire</option>
        </select></div>
      <div class="field"><label for="budget_hint">Budget range (optional)</label><input id="budget_hint" name="budget_hint" placeholder="e.g. $2,000-$5,000 first milestone"></div>
      <div class="field"><label for="timeline">Timeline</label><input id="timeline" name="timeline" placeholder="e.g. need first milestone in 3 weeks"></div>""" + _intake_category_field()
    return '<div class="field"><label for="company">Company (optional)</label><input id="company" name="company"></div>' + _intake_category_field()


def render_contact_form(kind: str, error: Optional[str] = None, success: bool = False,
                          csrf_token: str = "") -> str:
    title = "Start a project" if kind == "project" else "General enquiry"
    alert = ""
    if success:
        alert = ('<div class="alert alert-success">Thank you. This has been logged in our pipeline '
                  "and we'll follow up by email.</div>" if kind == "project" else
                  '<div class="alert alert-success">Thank you. We will get back to you by email.</div>')
    elif error:
        alert = f'<div class="alert alert-error">{esc(error)}</div>'
    form = "" if success else f"""
    <form class="stack" method="post" action="/contact/{esc(kind)}">
      <input type="hidden" name="csrf_token" value="{esc(csrf_token)}">
      <div class="field"><label for="name">Name</label><input id="name" name="name" required></div>
      <div class="field"><label for="email">Email</label><input id="email" name="email" type="email" required></div>
      {_contact_form_fields(kind)}
      <div class="field"><label for="message">Message</label><textarea id="message" name="message" required></textarea></div>
      <input type="text" name="website" id="website" style="position:absolute;left:-9999px" tabindex="-1" autocomplete="off">
      <button class="btn btn-primary" type="submit">Send</button>
    </form>"""
    return f"""
<section class="hero" style="padding-bottom:40px">
  <div class="container">
    <span class="eyebrow">Contact</span>
    <h1>{esc(title)}</h1>
  </div>
</section>
<section class="tight"><div class="container">{alert}{form}</div></section>
"""


# ============================================================
# Insights
# ============================================================

def render_insights_index(posts: List[Dict[str, Any]]) -> str:
    if posts:
        cards = "".join(f"""
        <a class="card card-link" href="/insights/{esc(p['slug'])}">
          <span class="badge">{esc(p['category'].title())}</span>
          <h3 style="margin-top:14px">{esc(p['title'])}</h3>
          <p>{esc(p['dek'] or '')}</p>
        </a>""" for p in posts)
        listing = f'<div class="grid grid-3">{cards}</div>'
    else:
        listing = '<div class="card"><p>Nothing published yet. Real engineering notes and company news will appear here as we have something worth writing.</p></div>'
    return f"""
<section class="hero" style="padding-bottom:50px">
  <div class="container">
    <span class="eyebrow">Insights</span>
    <h1>Engineering notes and company news.</h1>
  </div>
</section>
<section class="tight"><div class="container">{listing}</div></section>
"""


def _simple_markdown(md: str) -> str:
    """Minimal, dependency-free markdown-ish rendering (paragraphs only,
    headings on lines starting with '#'). Intentionally small: posts are
    authored, not user-submitted, so this doesn't need to be a full parser."""
    out = []
    for block in md.strip().split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if block.startswith("### "):
            out.append(f"<h3>{esc(block[4:])}</h3>")
        elif block.startswith("## "):
            out.append(f"<h2>{esc(block[3:])}</h2>")
        else:
            out.append(f"<p>{esc(block)}</p>")
    return "".join(out)


def render_post_detail(p: Dict[str, Any]) -> tuple:
    jsonld = [{
        "@context": "https://schema.org", "@type": "BlogPosting",
        "headline": p["title"], "author": {"@type": "Person", "name": p["author"]},
        "datePublished": p["published_at"],
    }]
    body = f"""
<section class="hero" style="padding-bottom:40px">
  <div class="container">
    <span class="eyebrow">Insights &middot; {esc(p['category'].title())}</span>
    <h1>{esc(p['title'])}</h1>
    <p class="lede hero-sub">{esc(p['dek'] or '')}</p>
    <p class="form-note">By {esc(p['author'])} &middot; {esc((p['published_at'] or '')[:10])}</p>
  </div>
</section>
<section class="tight"><div class="container" style="max-width:760px">{_simple_markdown(p['body_md'])}</div></section>
"""
    return body, jsonld


# ============================================================
# Legal / trust
# ============================================================

LEGAL_REVIEW_NOTE = ('<div class="alert alert-error">Draft for internal review. This page has not yet '
                       "been reviewed by qualified legal counsel and must not be relied on as final "
                       "before that review.</div>")


def render_legal_privacy() -> str:
    return f"""
<section class="hero" style="padding-bottom:40px"><div class="container">
  <span class="eyebrow">Legal</span><h1>Privacy Policy</h1></div></section>
<section class="tight"><div class="container" style="max-width:760px">
  {LEGAL_REVIEW_NOTE}
  <h2>What we collect</h2>
  <p>Contact and project-enquiry forms collect the name, email, and message you provide. Career
  applications additionally collect a resume file and any links you provide. We log a salted,
  non-reversible hash of the submitting IP address for abuse prevention. The raw address is never stored.</p>
  <h2>How we use it</h2>
  <p>Project and general enquiries are used solely to evaluate and respond to your request.
  Applications are used solely for recruitment. We do not sell personal data.</p>
  <h2>Retention</h2>
  <p>Enquiry and application records are retained for as long as reasonably needed to evaluate them
  and, if applicable, deliver the engagement.</p>
  <h2>Contact</h2>
  <p>Requests regarding your data: <a href="mailto:{SITE_EMAIL}" style="color:inherit;text-decoration:underline">{SITE_EMAIL}</a></p>
</div></section>
"""


def render_legal_terms() -> str:
    return f"""
<section class="hero" style="padding-bottom:40px"><div class="container">
  <span class="eyebrow">Legal</span><h1>Terms of Service</h1></div></section>
<section class="tight"><div class="container" style="max-width:760px">
  {LEGAL_REVIEW_NOTE}
  <p>This website is operated under the Twenty Two Technologies brand. Use of this site does not itself
  create a client relationship or contract; engagement terms are agreed separately, in writing, per
  project.</p>
  <p>Content on this site describes our real capabilities and work as accurately as we can state it.
  If you believe something here is inaccurate, contact us and we will correct it.</p>
</div></section>
"""


def render_legal_accessibility() -> str:
    return f"""
<section class="hero" style="padding-bottom:40px"><div class="container">
  <span class="eyebrow">Legal</span><h1>Accessibility Statement</h1></div></section>
<section class="tight"><div class="container" style="max-width:760px">
  <p>We aim to meet WCAG 2.1 AA for this site: semantic landmarks, visible keyboard focus, sufficient
  color contrast, alt text on meaningful imagery, and reduced-motion support. If you hit an
  accessibility barrier anywhere on this site, tell us and we will fix it:
  <a href="mailto:{SITE_EMAIL}" style="color:inherit;text-decoration:underline">{SITE_EMAIL}</a></p>
</div></section>
"""


# ============================================================
# Staff login + gated staff area
# ============================================================

def render_login(error: Optional[str] = None, csrf_token: str = "") -> str:
    alert = f'<div class="alert alert-error">{esc(error)}</div>' if error else ""
    return f"""
<section class="hero" style="padding:120px 0"><div class="container" style="max-width:440px">
  <span class="eyebrow">Staff</span>
  <h1 style="font-size:2rem">Sign in</h1>
  {alert}
  <form class="stack" method="post" action="/login">
    <input type="hidden" name="csrf_token" value="{esc(csrf_token)}">
    <div class="field"><label for="email">Email</label><input id="email" name="email" type="email" required autofocus></div>
    <div class="field"><label for="password">Password</label><input id="password" name="password" type="password" required></div>
    <button class="btn btn-primary" type="submit">Sign in</button>
  </form>
  <p class="form-note" style="margin-top:20px">This account model is independent of, and does not
  itself expose, the internal TTT HQ operating system. Staff MFA is not yet wired into this login.
  see the deployment notes before treating this as sufficient for a highly privileged account.</p>
</div></section>
"""


def render_staff_home(user: Dict[str, Any], applications: List[Dict[str, Any]],
                        enquiries_count: int) -> str:
    new_apps = [a for a in applications if a["status"] == "new"]
    rows = "".join(f"""
    <tr><td>{esc(a['applicant_name'])}</td><td>{esc(a['job_title_snapshot'])}</td>
    <td>{esc(a['applicant_email'])}</td><td>{esc((a['created_at'] or '')[:10])}</td>
    <td><span class="badge">{esc(a['status'])}</span></td></tr>""" for a in applications[:25])
    return f"""
<section class="hero" style="padding-bottom:40px"><div class="container">
  <span class="eyebrow">Staff area</span>
  <h1 style="font-size:2.2rem">Welcome, {esc(user['display_name'])}</h1>
  <p class="lede hero-sub">A minimal internal view for what this website itself produces. Full
  pipeline management stays in TTT HQ, which this login intentionally does not expose publicly.</p>
  <div class="btn-row"><form method="post" action="/logout" style="display:inline">
    <input type="hidden" name="csrf_token" value="{esc(user.get('_csrf',''))}">
    <button class="btn btn-ghost" type="submit">Sign out</button></form></div>
</div></section>
<section class="tight"><div class="container grid grid-2">
  <div class="card"><h3>New applications</h3><p style="font-size:2.4rem;color:var(--paper);margin:0">{len(new_apps)}</p></div>
  <div class="card"><h3>Total enquiries logged</h3><p style="font-size:2.4rem;color:var(--paper);margin:0">{enquiries_count}</p></div>
</div></section>
<section class="tight"><div class="container">
  <h2>Recent applications</h2>
  <table><tr><th>Name</th><th>Role</th><th>Email</th><th>Received</th><th>Status</th></tr>{rows or '<tr><td colspan="5">None yet.</td></tr>'}</table>
</div></section>
"""


# ============================================================
# SEO: sitemap.xml / robots.txt
# ============================================================

STATIC_PATHS = [
    "/", "/about", "/services", "/solutions", "/business-launch-growth", "/partners",
    "/work", "/products", "/careers", "/insights", "/pay",
    "/contact", "/contact/start-a-project", "/contact/general",
    "/legal/privacy", "/legal/terms", "/legal/accessibility",
]


def render_sitemap(services, case_studies, products, posts, jobs) -> bytes:
    urls = list(STATIC_PATHS)
    urls += [f"/services/{s['slug']}" for s in services]
    urls += [f"/work/{c['slug']}" for c in case_studies]
    urls += [f"/insights/{p['slug']}" for p in posts]
    urls += [f"/careers/{j['slug']}" for j in jobs]
    entries = "".join(f"<url><loc>https://{SITE_DOMAIN}{u}</loc></url>" for u in urls)
    xml = f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</urlset>'
    return xml.encode("utf-8")


ROBOTS_TXT = f"""User-agent: *
Allow: /
Disallow: /staff
Disallow: /login
Disallow: /portal
Disallow: /app
Disallow: /partners/app
Sitemap: https://{SITE_DOMAIN}/sitemap.xml
""".encode("utf-8")


# ============================================================
# Security helpers: CSRF (double-submit cookie for anonymous forms,
# session-bound token for the staff area), a minimal in-process rate
# limiter, and safe multipart resume upload handling.
# ============================================================

import secrets
import time as _time
from collections import defaultdict, deque

_RATE_LIMIT_WINDOW_S = 3600
_RATE_LIMIT_MAX = 8
_rate_buckets: Dict[str, deque] = defaultdict(deque)


def rate_limited(ip_hash: Optional[str]) -> bool:
    if not ip_hash:
        return False
    now = _time.time()
    bucket = _rate_buckets[ip_hash]
    while bucket and now - bucket[0] > _RATE_LIMIT_WINDOW_S:
        bucket.popleft()
    if len(bucket) >= _RATE_LIMIT_MAX:
        return True
    bucket.append(now)
    return False


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Content-Security-Policy": "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=()",
}


def safe_upload_path(app_root: Path, filename: str) -> Path:
    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_RESUME_EXTENSIONS:
        raise ContentError("resume must be a PDF or Word document")
    safe_name = f"{uuid.uuid4().hex}{ext}"
    upload_dir = Path(app_root) / ".falguna" / "site_uploads"
    upload_dir.mkdir(parents=True, exist_ok=True)
    return upload_dir / safe_name


# ============================================================
# HTTP handler
# ============================================================

class SiteHandler(BaseHTTPRequestHandler):
    server_version = "TTTSite/1.0"

    def log_message(self, format, *args):
        return

    @property
    def app_root(self):
        return self.server.app_root

    # -- low level response helpers --

    def _send(self, status: int, body: bytes, content_type: str = "text/html; charset=utf-8",
               cookies_out: Optional[List[str]] = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for c in (cookies_out or []):
            self.send_header("Set-Cookie", c)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _html(self, status: int, body: bytes, cookies_out: Optional[List[str]] = None):
        self._send(status, body, "text/html; charset=utf-8", cookies_out)

    def _redirect(self, location: str, cookies_out: Optional[List[str]] = None):
        self.send_response(303)
        self.send_header("Location", location)
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        for c in (cookies_out or []):
            self.send_header("Set-Cookie", c)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _cookies(self) -> cookies.SimpleCookie:
        jar = cookies.SimpleCookie()
        raw = self.headers.get("Cookie")
        if raw:
            try:
                jar.load(raw)
            except Exception:
                pass
        return jar

    def _client_ip(self) -> str:
        return self.client_address[0] if self.client_address else ""

    def _ensure_csrf_cookie(self, jar: cookies.SimpleCookie) -> tuple:
        existing = jar.get("csrf")
        if existing and existing.value:
            return existing.value, None
        token = new_csrf_token()
        cookie_str = f"csrf={token}; Path=/; HttpOnly; SameSite=Lax"
        return token, cookie_str

    def _session_id(self, jar: cookies.SimpleCookie) -> Optional[str]:
        c = jar.get("ttt_staff_session")
        return c.value if c else None

    def _external_session_id(self, jar: cookies.SimpleCookie) -> Optional[str]:
        c = jar.get("ttt_external_session")
        return c.value if c else None

    def _serve_static(self, path: str):
        """Serve real static assets (currently: brand/logo files) from
        STATIC_DIR. No database access needed, so this runs before the
        control plane is opened. Path-traversal safe and extension-allowlisted."""
        rel = path[len("/static/"):]
        if not rel or rel.startswith("/") or ".." in Path(rel).parts:
            return self._not_found([])
        candidate = (STATIC_DIR / rel).resolve()
        try:
            static_root = STATIC_DIR.resolve()
            candidate.relative_to(static_root)
        except ValueError:
            return self._not_found([])
        if candidate.suffix.lower() not in STATIC_ALLOWED_EXTENSIONS or not candidate.is_file():
            return self._not_found([])
        content_type = mimetypes.guess_type(str(candidate))[0] or "application/octet-stream"
        body = candidate.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "public, max-age=86400")
        for k, v in SECURITY_HEADERS.items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


    # -- routing --

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        jar = self._cookies()
        csrf_token, csrf_cookie = self._ensure_csrf_cookie(jar)
        set_cookies = [csrf_cookie] if csrf_cookie else []

        if path == "/robots.txt":
            return self._send(200, ROBOTS_TXT, "text/plain; charset=utf-8")

        if path.startswith("/static/"):
            return self._serve_static(path)

        control, store = open_control_plane(self.app_root)
        try:
            services = ServiceStore(store).list_all()
            case_studies = CaseStudyStore(store).list_published()
            products = ProductStore(store).list_all()
            posts = PostStore(store).list_published()
            jobs = JobStore(store).list_open()

            if path == "/sitemap.xml":
                return self._send(200, render_sitemap(services, case_studies, products, posts, jobs),
                                    "application/xml; charset=utf-8")

            if path == "/":
                return self._html(200, page(SITE_NAME, "Custom software engineering, AI systems, and digital product design.", "/",
                                              render_home(services, case_studies, products)), set_cookies)
            if path == "/about":
                return self._html(200, page("About", "Twenty Two Technologies: established 2020, real engineering principles.", "/about", render_about()), set_cookies)
            if path == "/solutions":
                return self._html(200, page("Solutions", "Commercial needs routed to the right accountable delivery model.", path, render_solutions()), set_cookies)
            if path == "/business-launch-growth":
                return self._html(200, page("Business Launch & Growth", "Practical support from validation through growth.", path, render_business_launch_growth()), set_cookies)
            if path == "/partners":
                partner_id = (query.get("partner_id") or [""])[0]
                verification = ExternalPortalService(store, control.audit).verify_partner(partner_id) if partner_id else None
                return self._html(200, page("Partner Network", "A governed opportunity and contribution network.", path, render_partner_program(verification)), set_cookies)

            if path == "/services":
                return self._html(200, page("Services", "Custom software, AI systems, and digital marketing services.", "/services", render_services_index(services)), set_cookies)
            if path.startswith("/services/"):
                slug = path.split("/", 2)[2]
                svc = ServiceStore(store).get_by_slug(slug)
                if not svc:
                    return self._not_found(set_cookies)
                proof = [c for c in case_studies if slug in c.get("division_slugs", [])]
                body, jsonld = render_service_detail(svc, proof)
                return self._html(200, page(svc["division"], svc["tagline"], path, body, jsonld), set_cookies)

            if path == "/work":
                return self._html(200, page("Work", "Real, named projects from client engagements and our own products.", "/work", render_work_index(case_studies)), set_cookies)
            if path.startswith("/work/"):
                slug = path.split("/", 2)[2]
                cs = CaseStudyStore(store).get_by_slug(slug)
                if not cs or not cs.get("published"):
                    return self._not_found(set_cookies)
                body, jsonld = render_case_study_detail(cs)
                return self._html(200, page(cs["title"], cs["summary"], path, body, jsonld), set_cookies)

            if path == "/products":
                return self._html(200, page("Products & Ventures", "Independent products developed in-house by Twenty Two Technologies.", "/products", render_products(products)), set_cookies)

            return self._route_get_part2(path, query, store, control.audit, jobs, posts, jar, csrf_token, set_cookies)
        finally:
            store.close()


    def _route_get_part2(self, path, query, store, audit, jobs, posts, jar, csrf_token, set_cookies):
        external_session_id = self._external_session_id(jar)
        external_auth = ExternalPortalAuth(store)
        external_identity = external_auth.identity(external_session_id) if external_session_id else None
        if path == "/portal/login":
            if external_identity:
                return self._redirect("/app" if external_identity["role"] == "CUSTOMER" else "/partners/app", set_cookies)
            return self._html(200, page("Customer & Partner Sign In", "Secure access to TTT external applications.", path, render_portal_login(csrf_token)), set_cookies)
        is_project_detail = path.startswith("/app/projects/") and path != "/app/projects"
        is_invoice_detail = path.startswith("/app/billing/") and path != "/app/billing"
        if path in {"/app", "/app/projects", "/app/billing", "/app/support", "/app/account"} or is_project_detail or is_invoice_detail:
            if not external_identity or external_identity.get("role") != "CUSTOMER":
                return self._redirect("/portal/login", set_cookies)
            session = store.get("p7_external_sessions", external_session_id)
            portals = ExternalPortalService(store, audit)
            bundle = portals.customer_bundle(external_identity)
            payments = portals.customer_payments(external_identity)
            if path == "/app/account":
                account = external_auth.account_security(external_session_id)
                return self._html(200, page("Account & Security", "Your account and active sessions.", path,
                    '<section class="tight"><div class="container app-shell">' + _portal_nav("CUSTOMER", path, session["csrf_token"]) + '<div>' + render_account_security(account, session["csrf_token"]) + '</div></div></section>'), set_cookies)
            if is_invoice_detail:
                detail = portals.customer_invoice_detail(external_identity, path[len("/app/billing/"):])
                if detail is None:
                    return self._not_found(set_cookies)
                body = '<section class="tight"><div class="container app-shell">' + _portal_nav("CUSTOMER", path, session["csrf_token"]) + '<div>' + render_invoice_detail(detail) + '</div></div></section>'
                return self._html(200, page("Invoice Detail", "Customer-safe invoice and settlement evidence.", path, body), set_cookies)
            project_detail = "__unset__"
            if is_project_detail:
                project_id = path[len("/app/projects/"):]
                project_detail = portals.customer_project_detail(external_identity, project_id)
                if project_detail is None:
                    # No existence leak: a project id outside this
                    # customer's own organization is indistinguishable from
                    # one that was never real at all.
                    return self._not_found(set_cookies)
            return self._html(200, page("Customer Application", "Your TTT projects, billing and support.", path,
                                          render_customer_app(external_identity, bundle, payments, path, session["csrf_token"],
                                                               project_detail=project_detail)), set_cookies)
        if path in {"/partners/app", "/partners/app/leads", "/partners/app/leads/new", "/partners/app/commissions", "/partners/app/account"}:
            if not external_identity or external_identity.get("role") != "PARTNER":
                return self._redirect("/portal/login", set_cookies)
            session = store.get("p7_external_sessions", external_session_id)
            bundle = ExternalPortalService(store, audit).partner_bundle(external_identity)
            if path == "/partners/app/account":
                account = external_auth.account_security(external_session_id)
                body = '<section class="tight"><div class="container app-shell">' + _portal_nav("PARTNER", path, session["csrf_token"]) + '<div>' + render_account_security(account, session["csrf_token"]) + '</div></div></section>'
                return self._html(200, page("Account & Security", "Your account and active sessions.", path, body), set_cookies)
            return self._html(200, page("Partner Application", "Your attributed opportunities and commission status.", path,
                                          render_partner_app(external_identity, bundle, path, session["csrf_token"])), set_cookies)
        if path == "/pay":
            payment_bundle = None
            if external_identity and external_identity.get("role") == "CUSTOMER":
                payment_bundle = ExternalPortalService(store, audit).customer_payments(external_identity)
            return self._html(200, page("Payments", "Official invoice and payment verification.", path,
                                          render_pay(external_identity, payment_bundle, csrf_token)), set_cookies)
        if path == "/careers":
            return self._html(200, page("Careers", "Open roles at Twenty Two Technologies.", "/careers", render_careers_index(jobs)), set_cookies)
        if path == "/careers/apply":
            job = None
            job_slug = (query.get("job") or [None])[0]
            if job_slug:
                job = JobStore(store).get_by_slug(job_slug)
            return self._html(200, page("Apply", "Apply to Twenty Two Technologies.", "/careers/apply",
                                          render_apply_form(job, csrf_token=csrf_token)), set_cookies)
        if path.startswith("/careers/"):
            slug = path.split("/", 2)[2]
            job = JobStore(store).get_by_slug(slug)
            if not job or job.get("status") != "open":
                return self._not_found(set_cookies)
            body, jsonld = render_job_detail(job)
            return self._html(200, page(job["title"], job["summary"], path, body, jsonld), set_cookies)

        if path == "/insights":
            return self._html(200, page("Insights", "Engineering notes and company news from Twenty Two Technologies.", "/insights", render_insights_index(posts)), set_cookies)
        if path.startswith("/insights/"):
            slug = path.split("/", 2)[2]
            post = PostStore(store).get_by_slug(slug)
            if not post:
                return self._not_found(set_cookies)
            body, jsonld = render_post_detail(post)
            return self._html(200, page(post["title"], post.get("dek") or post["title"], path, body, jsonld), set_cookies)

        if path == "/contact":
            return self._html(200, page("Contact", "Contact Twenty Two Technologies.", "/contact", render_contact_hub()), set_cookies)
        if path == "/contact/start-a-project":
            return self._html(200, page("Start a Project", "Tell us about your project.", "/contact/start-a-project", render_contact_form("project", csrf_token=csrf_token)), set_cookies)
        if path == "/contact/general":
            return self._html(200, page("General Enquiry", "Get in touch with Twenty Two Technologies.", "/contact/general", render_contact_form("general", csrf_token=csrf_token)), set_cookies)

        if path == "/legal/privacy":
            return self._html(200, page("Privacy Policy", "How Twenty Two Technologies handles personal data.", "/legal/privacy", render_legal_privacy()), set_cookies)
        if path == "/legal/terms":
            return self._html(200, page("Terms of Service", "Terms of Service for Twenty Two Technologies.", "/legal/terms", render_legal_terms()), set_cookies)
        if path == "/legal/accessibility":
            return self._html(200, page("Accessibility", "Accessibility statement for Twenty Two Technologies.", "/legal/accessibility", render_legal_accessibility()), set_cookies)

        if path == "/login":
            session_id = self._session_id(jar)
            auth = StaffAuthService(store)
            if session_id and auth.current_user(session_id):
                return self._redirect("/staff", set_cookies)
            return self._html(200, page("Staff Sign In", "Staff sign in for Twenty Two Technologies.", "/login", render_login(csrf_token=csrf_token)), set_cookies)

        if path == "/staff":
            session_id = self._session_id(jar)
            auth = StaffAuthService(store)
            user = auth.current_user(session_id) if session_id else None
            if not user:
                return self._redirect("/login", set_cookies)
            session = auth.session(session_id)
            user = dict(user)
            user["_csrf"] = session["csrf_token"] if session else ""
            apps = ApplicationStore(store).list_all()
            enquiries_count = len(store.list("site_enquiries"))
            return self._html(200, page("Staff", "Internal staff area.", "/staff",
                                          render_staff_home(user, apps, enquiries_count)), set_cookies)

        return self._not_found(set_cookies)

    def _not_found(self, set_cookies):
        body = page("Not found", "Page not found.", "/404",
                     '<section class="hero"><div class="container"><h1>404</h1><p class="lede">'
                     'That page does not exist. <a href="/" style="color:var(--accent-2)">Return home</a>.</p></div></section>')
        return self._html(404, body, set_cookies)


    # -- POST handlers --

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        jar = self._cookies()
        csrf_cookie_val = (jar.get("csrf").value if jar.get("csrf") else None)
        ip_hash = hashlib.sha256(self._client_ip().encode("utf-8")).hexdigest()[:32]

        control, store = open_control_plane(self.app_root)
        try:
            if path == "/careers/apply":
                return self._handle_apply(store, jar, csrf_cookie_val, ip_hash)
            if path in ("/contact/start-a-project", "/contact/general"):
                kind = "project" if path == "/contact/start-a-project" else "general"
                return self._handle_contact(store, kind, jar, csrf_cookie_val, ip_hash)
            if path == "/login":
                return self._handle_login(store, jar, csrf_cookie_val, ip_hash)
            if path == "/logout":
                return self._handle_logout(store, jar)
            if path == "/portal/login":
                return self._handle_external_login(store, jar, csrf_cookie_val, ip_hash)
            if path == "/portal/logout":
                return self._handle_external_logout(store, jar)
            if path == "/portal/password":
                return self._handle_external_password(store, jar)
            if path == "/pay/verify":
                return self._handle_payment_verification(store, jar, csrf_cookie_val)
            if path == "/partners/app/policy/acknowledge":
                return self._handle_partner_policy_acknowledge(store, jar)
            if path == "/partners/app/leads":
                return self._handle_partner_register_lead(store, jar)
            return self._not_found([])
        finally:
            store.close()

    def _read_urlencoded(self) -> Dict[str, str]:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8", errors="replace") if length else ""
        parsed = parse_qs(raw)
        return {k: v[0] for k, v in parsed.items()}


    def _handle_contact(self, store, kind, jar, csrf_cookie_val, ip_hash):
        fields = self._read_urlencoded()
        csrf_token = jar.get("csrf").value if jar.get("csrf") else ""
        if not csrf_cookie_val or fields.get("csrf_token") != csrf_cookie_val:
            body = page("Error", "Security check failed.", f"/contact/{kind}",
                         render_contact_form(kind, error="Security check failed. Please reload the page and try again.", csrf_token=csrf_token))
            return self._html(400, body)
        if fields.get("website"):  # honeypot
            return self._redirect(f"/contact/{kind}")
        if rate_limited(ip_hash):
            body = page("Error", "Too many submissions.", f"/contact/{kind}",
                         render_contact_form(kind, error="Too many submissions from this connection recently. Please try again later, or email us directly.", csrf_token=csrf_token))
            return self._html(429, body)
        try:
            control, _ = open_control_plane(self.app_root)
            opp_store = OpportunityStore(store, control.audit)
            comms_store = CommsStore(store, control.audit)
            enquiry_store = EnquiryStore(store, opp_store, comms_store)
            enquiry_store.submit(
                kind=kind, name=fields.get("name", ""), email=fields.get("email", ""),
                company=fields.get("company"), message=fields.get("message", ""),
                extra={
                    "project_title": fields.get("project_title"), "project_type": fields.get("project_type"),
                    "budget_hint": fields.get("budget_hint"), "timeline": fields.get("timeline"),
                    "intake_category": fields.get("intake_category"),
                }, source_ip=self._client_ip(),
            )
        except ContentError as exc:
            body = page("Error", "Please check the form.", f"/contact/{kind}",
                         render_contact_form(kind, error=str(exc), csrf_token=csrf_token))
            return self._html(400, body)
        body = page("Thank you", "Enquiry received.", f"/contact/{kind}", render_contact_form(kind, success=True))
        return self._html(200, body)

    def _handle_apply(self, store, jar, csrf_cookie_val, ip_hash):
        csrf_token = jar.get("csrf").value if jar.get("csrf") else ""
        content_type = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in content_type:
            return self._html(400, page("Error", "Invalid submission.", "/careers/apply", render_apply_form(None, error="Invalid submission format.", csrf_token=csrf_token)))
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers,
                                  environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": content_type})

        def val(name):
            return form.getvalue(name) or ""

        job = None
        job_slug = val("job_slug")
        if job_slug:
            job = JobStore(store).get_by_slug(job_slug)

        if not csrf_cookie_val or val("csrf_token") != csrf_cookie_val:
            return self._html(400, page("Error", "Security check failed.", "/careers/apply",
                                          render_apply_form(job, error="Security check failed. Please reload and try again.", csrf_token=csrf_token)))
        if val("website"):
            return self._redirect("/careers/apply")
        if rate_limited(ip_hash):
            return self._html(429, page("Error", "Too many submissions.", "/careers/apply",
                                          render_apply_form(job, error="Too many submissions recently. Please try again later.", csrf_token=csrf_token)))

        resume_field = form["resume"] if "resume" in form else None
        resume_meta: Dict[str, Any] = {}
        if resume_field is not None and getattr(resume_field, "filename", None):
            file_bytes = resume_field.file.read(MAX_RESUME_BYTES + 1)
            if len(file_bytes) > MAX_RESUME_BYTES:
                return self._html(400, page("Error", "File too large.", "/careers/apply",
                                              render_apply_form(job, error="Resume must be 8MB or smaller.", csrf_token=csrf_token)))
            try:
                dest = safe_upload_path(self.app_root, resume_field.filename)
            except ContentError as exc:
                return self._html(400, page("Error", "Invalid file.", "/careers/apply",
                                              render_apply_form(job, error=str(exc), csrf_token=csrf_token)))
            dest.write_bytes(file_bytes)
            resume_meta = {
                "resume_filename": os.path.basename(resume_field.filename)[:200],
                "resume_storage_rel_path": str(dest.relative_to(Path(self.app_root) / ".falguna")),
                "resume_sha256": hashlib.sha256(file_bytes).hexdigest(),
                "resume_size_bytes": len(file_bytes),
            }
        else:
            return self._html(400, page("Error", "Resume required.", "/careers/apply",
                                          render_apply_form(job, error="A resume file is required.", csrf_token=csrf_token)))

        try:
            control, _ = open_control_plane(self.app_root)
            comms_store = CommsStore(store, control.audit)
            ApplicationStore(store, comms_store).create({
                "job_id": job["id"] if job else None,
                "job_title_snapshot": job["title"] if job else "General Application",
                "applicant_name": val("name"), "applicant_email": val("email"),
                "applicant_phone": val("phone") or None,
                "links": [l.strip() for l in val("links").splitlines() if l.strip()],
                "cover_note": val("cover_note") or None,
                "source_ip_hash": hash_ip(self._client_ip()),
                **resume_meta,
            })
        except Exception as exc:
            return self._html(400, page("Error", "Could not submit.", "/careers/apply",
                                          render_apply_form(job, error=f"Could not submit: {exc}", csrf_token=csrf_token)))
        return self._html(200, page("Thank you", "Application received.", "/careers/apply",
                                      render_apply_form(job, success=True)))


    def _handle_login(self, store, jar, csrf_cookie_val, ip_hash):
        fields = self._read_urlencoded()
        csrf_token = jar.get("csrf").value if jar.get("csrf") else ""
        if not csrf_cookie_val or fields.get("csrf_token") != csrf_cookie_val:
            return self._html(400, page("Sign in", "Staff sign in.", "/login",
                                          render_login(error="Security check failed. Please reload and try again.", csrf_token=csrf_token)))
        if rate_limited(f"login:{ip_hash}"):
            return self._html(429, page("Sign in", "Staff sign in.", "/login",
                                          render_login(error="Too many attempts. Please wait before trying again.", csrf_token=csrf_token)))
        auth = StaffAuthService(store)
        try:
            result = auth.login(fields.get("email", ""), fields.get("password", ""),
                                  ip_hash=ip_hash, user_agent=self.headers.get("User-Agent"))
        except AuthError as exc:
            return self._html(401, page("Sign in", "Staff sign in.", "/login",
                                          render_login(error=str(exc), csrf_token=csrf_token)))
        session_cookie = f"ttt_staff_session={result['session_id']}; Path=/; HttpOnly; SameSite=Lax; Max-Age={12*3600}"
        return self._redirect("/staff", [session_cookie])

    def _handle_logout(self, store, jar):
        session_id = self._session_id(jar)
        if session_id:
            StaffAuthService(store).logout(session_id)
        expire_cookie = "ttt_staff_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
        return self._redirect("/login", [expire_cookie])

    def _handle_external_login(self, store, jar, csrf_cookie_val, ip_hash):
        fields = self._read_urlencoded()
        csrf_token = jar.get("csrf").value if jar.get("csrf") else ""
        if not csrf_cookie_val or fields.get("csrf_token") != csrf_cookie_val:
            return self._html(400, page("Sign in", "External application sign in.", "/portal/login",
                                          render_portal_login(csrf_token, "Security check failed. Please reload and try again.")))
        if rate_limited(f"portal-login:{ip_hash}"):
            return self._html(429, page("Sign in", "External application sign in.", "/portal/login",
                                          render_portal_login(csrf_token, "Too many attempts. Please wait before trying again.")))
        try:
            result = ExternalPortalAuth(store).login(fields.get("email", ""), fields.get("password", ""),
                                                       ip_hash=ip_hash, user_agent=self.headers.get("User-Agent"))
        except AuthError as exc:
            return self._html(401, page("Sign in", "External application sign in.", "/portal/login",
                                          render_portal_login(csrf_token, str(exc))))
        cookie = f"ttt_external_session={result['session_id']}; Path=/; HttpOnly; SameSite=Lax; Max-Age={12*3600}"
        location = "/app" if result["identity"]["role"] == "CUSTOMER" else "/partners/app"
        return self._redirect(location, [cookie])

    def _handle_external_logout(self, store, jar):
        session_id = self._external_session_id(jar)
        fields = self._read_urlencoded()
        auth = ExternalPortalAuth(store)
        if not session_id or not auth.check_csrf(session_id, fields.get("csrf_token", "")):
            return self._html(400, page("Sign out", "Security check failed.", "/portal/login",
                                          render_portal_login("", "Security check failed.")))
        auth.logout(session_id)
        return self._redirect("/portal/login", ["ttt_external_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"])

    def _handle_external_password(self, store, jar):
        session_id = self._external_session_id(jar)
        fields = self._read_urlencoded()
        auth = ExternalPortalAuth(store)
        identity = auth.identity(session_id) if session_id else None
        if not identity:
            return self._redirect("/portal/login", [])
        try:
            auth.change_password(session_id, fields.get("csrf_token", ""), fields.get("current_password", ""), fields.get("new_password", ""))
        except AuthError as exc:
            session = store.get("p7_external_sessions", session_id)
            account = auth.account_security(session_id)
            path = "/app/account" if identity["role"] == "CUSTOMER" else "/partners/app/account"
            body = '<section class="tight"><div class="container app-shell">' + _portal_nav(identity["role"], path, session["csrf_token"]) + '<div>' + render_account_security(account, session["csrf_token"], error=str(exc)) + '</div></div></section>'
            return self._html(400, page("Account & Security", "Password change failed.", path, body))
        return self._redirect("/app/account" if identity["role"] == "CUSTOMER" else "/partners/app/account", [])

    def _handle_partner_policy_acknowledge(self, store, jar):
        session_id = self._external_session_id(jar)
        fields = self._read_urlencoded()
        auth = ExternalPortalAuth(store)
        identity = auth.identity(session_id) if session_id else None
        if not identity or identity.get("role") != "PARTNER" or not auth.check_csrf(session_id, fields.get("csrf_token", "")):
            return self._redirect("/portal/login", [])
        control, _ = open_control_plane(self.app_root)
        try:
            ExternalPortalService(store, control.audit).partner_acknowledge_policy(identity)
        except (PartnerError, PermissionError):
            # A partner whose profile is not APPROVED (or otherwise
            # ineligible) cannot acknowledge the policy -- send them back
            # to their overview rather than a raw error; nothing sensitive
            # about why is exposed to the browser.
            return self._redirect("/partners/app", [])
        return self._redirect("/partners/app/leads/new", [])

    def _handle_partner_register_lead(self, store, jar):
        session_id = self._external_session_id(jar)
        fields = self._read_urlencoded()
        auth = ExternalPortalAuth(store)
        identity = auth.identity(session_id) if session_id else None
        if not identity or identity.get("role") != "PARTNER":
            return self._redirect("/portal/login", [])
        session = store.get("p7_external_sessions", session_id)
        if not auth.check_csrf(session_id, fields.get("csrf_token", "")):
            control, _ = open_control_plane(self.app_root)
            bundle = ExternalPortalService(store, control.audit).partner_bundle(identity)
            return self._html(400, page("Partner Application", "Security check failed.", "/partners/app/leads/new",
                                          render_partner_app(identity, bundle, "/partners/app/leads/new", session["csrf_token"],
                                                              lead_form_error="Security check failed. Please reload and try again.")))
        control, _ = open_control_plane(self.app_root)
        portals = ExternalPortalService(store, control.audit)
        try:
            portals.partner_register_lead(identity, fields)
        except (ReferralError, PartnerError, PermissionError) as exc:
            bundle = portals.partner_bundle(identity)
            return self._html(400, page("Partner Application", "Could not register lead.", "/partners/app/leads/new",
                                          render_partner_app(identity, bundle, "/partners/app/leads/new", session["csrf_token"],
                                                              lead_form_error=str(exc))))
        bundle = portals.partner_bundle(identity)
        return self._html(200, page("Partner Application", "Lead registered.", "/partners/app/leads/new",
                                      render_partner_app(identity, bundle, "/partners/app/leads/new", session["csrf_token"],
                                                          lead_form_success=True)))

    def _handle_payment_verification(self, store, jar, csrf_cookie_val):
        fields = self._read_urlencoded()
        csrf_token = jar.get("csrf").value if jar.get("csrf") else ""
        if not csrf_cookie_val or fields.get("csrf_token") != csrf_cookie_val:
            return self._html(400, page("Payments", "Security check failed.", "/pay", render_pay(None, None, csrf_token)))
        result = CustomerVerificationService(store).verify_payment_instruction(
            fields.get("payment_id", ""), fields.get("customer_ref", ""), fields.get("beneficiary_ref", ""),
        )
        return self._html(200, page("Payments", "Official payment verification result.", "/pay",
                                      render_pay(None, None, csrf_token, result)))


def serve_site(root, host: str = "127.0.0.1", port: int = 8767) -> None:
    """Entry point mirroring hq_web.serve_hq / web.serve. Shares the same
    .falguna/state.db as the other two services via open_control_plane."""
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("the public site binds to localhost by default; see deployment docs for a real public bind + reverse proxy setup")
    server = ThreadingHTTPServer((host, port), SiteHandler)
    server.app_root = Path(root).resolve()
    # ensure schema (incl. new site_ tables) is migrated before first request
    control, store = open_control_plane(server.app_root)
    store.close()
    print(f"Twenty Two Technologies website: http://{host}:{server.server_port}")
    server.serve_forever()


if __name__ == "__main__":
    import sys
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    serve_site(root)
