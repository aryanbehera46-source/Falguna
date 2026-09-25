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

from .revenue_hunter import OpportunityStore
from .runtime import open_control_plane
from .site_auth import StaffAuthService, AuthError
from .site_content import (
    ApplicationStore, CaseStudyStore, EnquiryStore, JobStore, PostStore,
    ProductStore, ServiceStore, ContentError, hash_ip,
)

SITE_NAME = "Twenty Two Technologies"
SITE_DOMAIN = "twentytwotechnologies.com"
SITE_EMAIL = "aryan@twentytwotechnologies.com"
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
    return html.escape(value or "", quote=True)


# ============================================================
# Design system -- deep charcoal / warm white / one restrained accent.
# System font stack only: zero external font/script requests, which is
# also why Lighthouse/CWV budgets in Block H are realistic to hit.
# ============================================================
SITE_CSS = r"""
:root{
  --ink:#0d0d0e; --ink-2:#17171a; --ink-3:#232327; --line:#2c2c31;
  --paper:#faf8f3; --paper-dim:#eeece5; --ink-soft:#c9c8c4;
  --accent:#c0392b; --accent-2:#e0644f; --focus:#7fb0ff;
  --maxw:1180px; --gutter:24px;
  --radius:14px;
  --ease:cubic-bezier(.2,.7,.2,1);
  --font:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;
}
*{box-sizing:border-box}
html{background:var(--ink)}
body{
  margin:0;background:var(--ink);color:var(--paper);
  font-family:var(--font);
  font-size:17px;line-height:1.6;-webkit-font-smoothing:antialiased;
}
img,svg{max-width:100%;display:block}
a{color:inherit}
.container{max-width:var(--maxw);margin:0 auto;padding:0 var(--gutter)}
.visually-hidden{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0);white-space:nowrap}
:focus-visible{outline:3px solid var(--focus);outline-offset:3px}
@media (prefers-reduced-motion: reduce){*{animation-duration:.001ms !important;transition-duration:.001ms !important}}

h1,h2,h3,h4{font-weight:600;letter-spacing:-0.02em;margin:0 0 .5em}
h1{font-size:clamp(2.4rem,5vw,4.6rem);line-height:1.04;font-weight:650}
h2{font-size:clamp(1.7rem,3.1vw,2.6rem);line-height:1.12}
h3{font-size:clamp(1.2rem,1.7vw,1.5rem);line-height:1.25}
p{margin:0 0 1em;color:var(--ink-soft)}
.lede{font-size:clamp(1.05rem,1.6vw,1.35rem);color:var(--paper)}
.eyebrow{font-size:.78rem;letter-spacing:.14em;text-transform:uppercase;color:var(--accent-2);font-weight:600}

/* header / nav */
.site-header{position:sticky;top:0;z-index:40;background:rgba(13,13,14,.86);backdrop-filter:saturate(140%) blur(10px);border-bottom:1px solid var(--line)}
.site-header .bar{display:flex;align-items:center;justify-content:space-between;padding:18px var(--gutter);max-width:var(--maxw);margin:0 auto}
.wordmark{font-weight:700;font-size:1.05rem;letter-spacing:-.01em;text-decoration:none;color:var(--paper);display:flex;align-items:center;gap:.5em}
.wordmark .dot{width:8px;height:8px;border-radius:50%;background:var(--accent)}
.nav{display:flex;gap:28px;align-items:center}
.nav a{text-decoration:none;color:var(--ink-soft);font-size:.94rem;transition:color .15s var(--ease)}
.nav a:hover,.nav a[aria-current="page"]{color:var(--paper)}
.nav-toggle{display:none;background:none;border:1px solid var(--line);color:var(--paper);border-radius:8px;padding:8px 10px}
@media (max-width:860px){
  .nav{display:none}
  .nav-toggle{display:inline-flex}
  .site-header.open .nav{display:flex;position:absolute;left:0;right:0;top:100%;flex-direction:column;background:var(--ink-2);padding:18px var(--gutter);border-bottom:1px solid var(--line);gap:16px}
}

/* buttons */
.btn{display:inline-flex;align-items:center;gap:.5em;padding:13px 22px;border-radius:999px;font-weight:600;font-size:.95rem;text-decoration:none;transition:transform .15s var(--ease),background .15s var(--ease);border:1px solid transparent;cursor:pointer}
.btn-primary{background:var(--accent);color:#fff}
.btn-primary:hover{background:var(--accent-2);transform:translateY(-1px)}
.btn-ghost{border-color:var(--line);color:var(--paper);background:transparent}
.btn-ghost:hover{border-color:var(--paper)}
.btn-row{display:flex;gap:14px;flex-wrap:wrap;margin-top:1.8em}

/* hero */
.hero{padding:min(14vw,140px) 0 90px;border-bottom:1px solid var(--line)}
.hero .eyebrow{margin-bottom:18px;display:block}
.hero-sub{max-width:640px;margin-top:22px}

/* sections / grid */
section{padding:88px 0}
section.tight{padding:56px 0}
.section-head{max-width:640px;margin-bottom:52px}
.grid{display:grid;gap:28px}
.grid-3{grid-template-columns:repeat(3,1fr)}
.grid-2{grid-template-columns:repeat(2,1fr)}
@media (max-width:900px){.grid-3,.grid-2{grid-template-columns:1fr}}

.card{background:var(--ink-2);border:1px solid var(--line);border-radius:var(--radius);padding:30px}
.card-link{text-decoration:none;display:block;transition:border-color .15s var(--ease),transform .15s var(--ease)}
.card-link:hover{border-color:#3a3a40;transform:translateY(-2px)}
.index{color:var(--accent-2);font-weight:700;font-size:.82rem;margin-bottom:14px;display:block}
.divider{border:0;border-top:1px solid var(--line);margin:0}

/* alt panel (warm white band, used sparingly) */
.panel-paper{background:var(--paper);color:var(--ink)}
.panel-paper p,.panel-paper .lede{color:#4a4842}
.panel-paper .card{background:#fff;border-color:#e6e3da}
.panel-paper h2,.panel-paper h3{color:var(--ink)}

/* footer */
.site-footer{border-top:1px solid var(--line);padding:64px 0 40px;color:var(--ink-soft);font-size:.92rem}
.footer-grid{display:grid;grid-template-columns:2fr 1fr 1fr 1fr;gap:32px}
@media (max-width:760px){.footer-grid{grid-template-columns:1fr 1fr}}
.footer-grid h4{color:var(--paper);font-size:.82rem;text-transform:uppercase;letter-spacing:.08em;margin-bottom:14px}
.footer-grid a{display:block;text-decoration:none;color:var(--ink-soft);margin-bottom:10px}
.footer-grid a:hover{color:var(--paper)}
.footer-bottom{margin-top:44px;padding-top:24px;border-top:1px solid var(--line);display:flex;justify-content:space-between;flex-wrap:wrap;gap:12px}

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

/* tags / status badges */
.badge{display:inline-block;font-size:.72rem;font-weight:700;text-transform:uppercase;letter-spacing:.06em;padding:5px 10px;border-radius:999px;border:1px solid var(--line);color:var(--ink-soft)}
.badge-live{color:#bdf0d3;border-color:#1d5c3f}
.badge-dev{color:#ffdca8;border-color:#7a5a20}
.badge-research{color:#c9c8c4;border-color:#3a3a40}

table{width:100%;border-collapse:collapse;font-size:.92rem}
th,td{text-align:left;padding:12px 14px;border-bottom:1px solid var(--line)}
th{color:var(--ink-soft);font-weight:600;font-size:.78rem;text-transform:uppercase;letter-spacing:.06em}
"""


# ============================================================
# Layout shell (header / nav / footer / meta) + small render helpers.
# No approved logo file exists in the repo (verified, see spec doc) --
# this uses a clean typographic wordmark and records the real asset as
# an open launch item rather than inventing a logo.
# ============================================================

NAV_ITEMS = [
    ("/", "Home"), ("/about", "About"), ("/services", "Services"),
    ("/work", "Work"), ("/products", "Products"), ("/careers", "Careers"),
    ("/insights", "Insights"), ("/contact", "Contact"),
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
    ("/legal/accessibility", "Accessibility"), ("/login", "Staff Sign In"),
]


def _logo_img(cls: str = "") -> str:
    """The real, approved Twenty Two Technologies mark. Replaces the
    temporary typographic wordmark once Aryan supplied the actual asset."""
    return (f'<img class="brand-logo {cls}" src="{HEADER_LOGO_PATH}" '
             f'alt="Twenty Two Technologies" width="36" height="36">')


def render_header(active_path: str) -> str:
    links = []
    for href, label in NAV_ITEMS:
        current = ' aria-current="page"' if href == active_path else ""
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
        <p style="margin-top:16px;max-width:32ch">Established {FOUNDED_YEAR}. Custom software, AI systems, and
        digital experience engineering for clients who need real, working products -- not decks.</p>
        <p><a href="mailto:{SITE_EMAIL}" style="color:var(--paper);text-decoration:underline">{SITE_EMAIL}</a></p>
      </div>
      {col("Services", FOOTER_SERVICES)}
      {col("Company", FOOTER_COMPANY)}
      {col("Legal", FOOTER_LEGAL)}
    </div>
    <div class="footer-bottom">
      <span>&copy; {year} Twenty Two Technologies Pvt. Ltd. All rights reserved.</span>
      <span>Built and operated on our own engineering stack.</span>
    </div>
  </div>
</footer>
"""


ORG_JSONLD = {
    "@context": "https://schema.org", "@type": "Organization",
    "name": "Twenty Two Technologies Pvt. Ltd.", "legalName": "Twenty Two Technologies Pvt. Ltd.",
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
    top_services = services[:6]
    service_cards = "".join(f"""
    <a class="card card-link" href="/services/{esc(s['slug'])}">
      <span class="index">{i+1:02d}</span>
      <h3>{esc(s['division'])}</h3>
      <p>{esc(s['tagline'])}</p>
    </a>""" for i, s in enumerate(top_services))

    proof = case_studies[:3]
    proof_cards = "".join(f"""
    <a class="card card-link" href="/work/{esc(c['slug'])}">
      <span class="badge">{esc(c['client_label'])}</span>
      <h3 style="margin-top:14px">{esc(c['title'])}</h3>
      <p>{esc(c['summary'])}</p>
    </a>""" for c in proof)

    venture_cards = "".join(f"""
    <div class="card">
      <span class="badge badge-{'live' if p['status']=='available' else ('dev' if p['status']=='in_development' else 'research')}">{esc(p['status'].replace('_',' ').title())}</span>
      <h3 style="margin-top:14px">{esc(p['name'])}</h3>
      <p>{esc(p['tagline'])}</p>
    </div>""" for p in products)

    return f"""
<section class="hero">
  <div class="container">
    <span class="eyebrow">Twenty Two Technologies &middot; Est. {FOUNDED_YEAR}</span>
    <h1>Software engineering for companies who need it to actually work.</h1>
    <p class="lede hero-sub">We design and build custom software, AI systems, and digital products --
    end to end, from a scoped first milestone to a full platform. Small projects and complex
    engagements, held to the same engineering bar.</p>
    <div class="btn-row">
      <a class="btn btn-primary" href="/contact/start-a-project">Start a project</a>
      <a class="btn btn-ghost" href="/work">See our work</a>
    </div>
  </div>
</section>

<section class="tight">
  <div class="container">
    <div class="section-head">
      <span class="eyebrow">What we do</span>
      <h2>Capabilities that scale with the engagement.</h2>
      <p>A cross-disciplinary studio built around real delivery, not a single narrow specialty.</p>
    </div>
    <div class="grid grid-3">{service_cards}</div>
    <div class="btn-row"><a class="btn btn-ghost" href="/services">View all services</a></div>
  </div>
</section>

<section class="panel-paper">
  <div class="container">
    <div class="section-head">
      <span class="eyebrow">Proof of work</span>
      <h2>Real projects, not mockups.</h2>
      <p>We show working software and named engagements -- including our own products, built and
      operated the same way we build for clients.</p>
    </div>
    <div class="grid grid-3">{proof_cards}</div>
    <div class="btn-row"><a class="btn btn-ghost" href="/work">View all work</a></div>
  </div>
</section>

<section>
  <div class="container">
    <div class="section-head">
      <span class="eyebrow">Products & ventures</span>
      <h2>What we're building ourselves.</h2>
      <p>Independent products developed in-house, at the stage they're honestly at.</p>
    </div>
    <div class="grid grid-3">{venture_cards}</div>
    <div class="btn-row"><a class="btn btn-ghost" href="/products">Products & ventures</a></div>
  </div>
</section>

<section class="tight">
  <div class="container grid grid-2" style="align-items:center">
    <div>
      <span class="eyebrow">How we engage</span>
      <h2>Start small. Scale if it's working.</h2>
      <p>Most engagements begin with a scoped first milestone -- one feature, one integration, one
      discovery phase -- priced and delivered on its own, so you can evaluate us before committing to
      more. No inflated minimums, no artificial ceilings on scope.</p>
      <div class="btn-row"><a class="btn btn-primary" href="/contact/start-a-project">Tell us about your project</a></div>
    </div>
    <div class="card">
      <h3>A typical first engagement</h3>
      <table>
        <tr><th>Step</th><th>What happens</th></tr>
        <tr><td>Scope exchange</td><td>Must-haves, deadline, existing assets, decision process</td></tr>
        <tr><td>Proposal</td><td>Named proof of work, phased milestones, explicit assumptions</td></tr>
        <tr><td>First milestone</td><td>Small, fixed, delivered before any larger commitment</td></tr>
        <tr><td>Delivery & QA</td><td>Isolated build, independent review, agreed verification</td></tr>
      </table>
    </div>
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

<section class="tight">
  <div class="container grid grid-2">
    <div>
      <h2>How we work</h2>
      <p>Every engagement starts with a real scope exchange, not a template proposal. We name the
      proof of work that's actually comparable, price a first milestone on its own, and keep delivery
      isolated and independently verified before anything is called done.</p>
    </div>
    <div>
      <h2>What we won't do</h2>
      <p>We don't claim capabilities we haven't proven, publish invented client logos or testimonials,
      or promise turnaround times we can't back with evidence. Where a capability depends on a
      qualified specialist or partner rather than an in-house team, we say so.</p>
    </div>
  </div>
</section>

<section class="panel-paper">
  <div class="container">
    <div class="section-head">
      <span class="eyebrow">Engineering principles</span>
      <h2>What "done" means here.</h2>
    </div>
    <div class="grid grid-3">
      <div class="card"><h3>Isolated delivery</h3><p>Work happens on isolated branches/worktrees with
      sandboxed execution, so a build in progress can never destabilize a live system.</p></div>
      <div class="card"><h3>Independent verification</h3><p>A second, independent review checks
      requirement fit, scope, and regression risk before anything is presented as finished.</p></div>
      <div class="card"><h3>Honest status</h3><p>Research, in development, beta, and available are
      real distinctions we hold ourselves to across our own products, not just client work.</p></div>
    </div>
  </div>
</section>

<section>
  <div class="container">
    <div class="section-head">
      <span class="eyebrow">Where we're headed</span>
      <h2>A studio built to take on more, deliberately.</h2>
      <p>We're built to support a growing base of clients and an expanding portfolio of our own
      products -- adding capacity and capability as real engagements justify it, not ahead of it.</p>
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
    <a class="card card-link" href="/services/{esc(s['slug'])}">
      <span class="badge {STATUS_LABEL.get(s['status'], STATUS_LABEL['current'])[0]}">{esc(STATUS_LABEL.get(s['status'], STATUS_LABEL['current'])[1])}</span>
      <h3 style="margin-top:14px">{esc(s['division'])}</h3>
      <p>{esc(s['tagline'])}</p>
    </a>""" for s in services)
    return f"""
<section class="hero" style="padding-bottom:50px">
  <div class="container">
    <span class="eyebrow">Services</span>
    <h1>What we build, and how we deliver it.</h1>
    <p class="lede hero-sub">Current, in-house capability; capability we deliver through a named,
    qualified partner; and services in active development are marked distinctly below -- we don't
    blur the line.</p>
  </div>
</section>
<section class="tight"><div class="container"><div class="grid grid-3">{cards}</div></div></section>
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
    badge_cls, badge_label = STATUS_LABEL.get(s["status"], STATUS_LABEL["current"])
    jsonld = [{
        "@context": "https://schema.org", "@type": "Service",
        "serviceType": s["division"], "provider": {"@type": "Organization", "name": SITE_NAME},
        "description": s["summary"],
    }]
    body = f"""
<section class="hero" style="padding-bottom:50px">
  <div class="container">
    <span class="eyebrow">Services</span>
    <span class="badge {badge_cls}" style="margin-bottom:14px;display:inline-block">{esc(badge_label)}</span>
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
                 "shown as proof of engineering capability -- not a paid third-party client engagement.</p>"
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
    status_meta = {
        "research": ("badge-research", "Research"), "in_development": ("badge-dev", "In development"),
        "beta": ("badge-dev", "Beta"), "available": ("badge-live", "Available"),
    }
    cards = "".join(f"""
    <div class="card">
      <span class="badge {status_meta.get(p['status'], status_meta['research'])[0]}">{esc(status_meta.get(p['status'], status_meta['research'])[1])}</span>
      <h3 style="margin-top:14px">{f'<img src="{FALGUNA_MARK_PATH}" alt="" width="24" height="24" style="vertical-align:-5px;margin-right:8px;border-radius:6px">' if p['name'].strip().lower() == 'falguna' else ''}{esc(p['name'])}</h3>
      <p>{esc(p['tagline'])}</p>
      <p>{esc(p['summary'])}</p>
    </div>""" for p in products)
    return f"""
<section class="hero" style="padding-bottom:50px">
  <div class="container">
    <span class="eyebrow">Products & Ventures</span>
    <h1>What we're building ourselves.</h1>
    <p class="lede hero-sub">Status badges reflect where each product genuinely is today -- research,
    in development, beta, or available -- not aspiration.</p>
  </div>
</section>
<section class="tight"><div class="container"><div class="grid grid-3">{cards}</div></div></section>
"""


# ============================================================
# Careers (index + detail + application form)
# ============================================================

def render_careers_index(jobs: List[Dict[str, Any]]) -> str:
    if jobs:
        cards = "".join(f"""
        <a class="card card-link" href="/careers/{esc(j['slug'])}">
          <span class="badge">{esc(j['department'])} &middot; {esc(j['employment_type'].replace('_',' ').title())}</span>
          <h3 style="margin-top:14px">{esc(j['title'])}</h3>
          <p>{esc(j['location_policy'])}</p>
        </a>""" for j in jobs)
        listing = f'<div class="grid grid-3">{cards}</div>'
    else:
        listing = ('<div class="card"><p>No open roles right now. We post here the moment a real '
                    "position opens -- we don't list placeholder vacancies. Strong candidates are "
                    "welcome to send a general application below.</p></div>")
    return f"""
<section class="hero" style="padding-bottom:50px">
  <div class="container">
    <span class="eyebrow">Careers</span>
    <h1>Build real software with us.</h1>
    <p class="lede hero-sub">Every listing here is a genuinely open role. We add openings as we need
    them, not on a schedule.</p>
  </div>
</section>
<section class="tight"><div class="container">{listing}
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
        alert = '<div class="alert alert-success">Application received. We review every submission -- thank you.</div>'
    elif error:
        alert = f'<div class="alert alert-error">{esc(error)}</div>'
    form = "" if success else f"""
    <form class="stack" method="post" action="/careers/apply" enctype="multipart/form-data">
      <input type="hidden" name="csrf_token" value="{esc(csrf_token)}">
      {job_field}
      <div class="field"><label for="name">Full name</label><input id="name" name="name" required></div>
      <div class="field"><label for="email">Email</label><input id="email" name="email" type="email" required></div>
      <div class="field"><label for="phone">Phone (optional)</label><input id="phone" name="phone"></div>
      <div class="field"><label for="links">Portfolio / LinkedIn / GitHub (one per line, optional)</label><textarea id="links" name="links" style="min-height:70px"></textarea></div>
      <div class="field"><label for="cover_note">Note to us</label><textarea id="cover_note" name="cover_note"></textarea></div>
      <div class="field"><label for="resume">Resume (PDF or Word, max 8MB)</label><input id="resume" name="resume" type="file" accept=".pdf,.doc,.docx" required></div>
      <p class="form-note">Honeypot field below must stay empty (bots only).</p>
      <input type="text" name="website" id="website" style="position:absolute;left:-9999px" tabindex="-1" autocomplete="off">
      <button class="btn btn-primary" type="submit">Submit application</button>
    </form>"""
    return f"""
<section class="hero" style="padding-bottom:40px">
  <div class="container">
    <span class="eyebrow">Careers</span>
    <h1>Apply &mdash; {esc(job_title)}</h1>
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
  <p>Direct: <a href="mailto:{SITE_EMAIL}" style="color:var(--paper);text-decoration:underline">{SITE_EMAIL}</a></p>
</div></section>
"""


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
      <div class="field"><label for="timeline">Timeline</label><input id="timeline" name="timeline" placeholder="e.g. need first milestone in 3 weeks"></div>"""
    return '<div class="field"><label for="company">Company (optional)</label><input id="company" name="company"></div>'


def render_contact_form(kind: str, error: Optional[str] = None, success: bool = False,
                          csrf_token: str = "") -> str:
    title = "Start a project" if kind == "project" else "General enquiry"
    alert = ""
    if success:
        alert = ('<div class="alert alert-success">Thank you -- this has been logged in our pipeline '
                  "and we'll follow up by email.</div>" if kind == "project" else
                  '<div class="alert alert-success">Thank you -- we\'ll get back to you by email.</div>')
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

LEGAL_REVIEW_NOTE = ('<div class="alert alert-error">Draft for internal review -- this page has not yet '
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
  non-reversible hash of the submitting IP address for abuse prevention -- never the raw address.</p>
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
  <p>This website is operated by Twenty Two Technologies Pvt. Ltd. Use of this site does not itself
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
  itself expose, the internal TTT HQ operating system. Staff MFA is not yet wired into this login --
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
    "/", "/about", "/services", "/work", "/products", "/careers", "/insights",
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
                return self._html(200, page("Work", "Real, named projects -- client engagements and our own products.", "/work", render_work_index(case_studies)), set_cookies)
            if path.startswith("/work/"):
                slug = path.split("/", 2)[2]
                cs = CaseStudyStore(store).get_by_slug(slug)
                if not cs or not cs.get("published"):
                    return self._not_found(set_cookies)
                body, jsonld = render_case_study_detail(cs)
                return self._html(200, page(cs["title"], cs["summary"], path, body, jsonld), set_cookies)

            if path == "/products":
                return self._html(200, page("Products & Ventures", "Independent products developed in-house by Twenty Two Technologies.", "/products", render_products(products)), set_cookies)

            return self._route_get_part2(path, query, store, jobs, posts, jar, csrf_token, set_cookies)
        finally:
            store.close()


    def _route_get_part2(self, path, query, store, jobs, posts, jar, csrf_token, set_cookies):
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
                         render_contact_form(kind, error="Security check failed -- please reload the page and try again.", csrf_token=csrf_token))
            return self._html(400, body)
        if fields.get("website"):  # honeypot
            return self._redirect(f"/contact/{kind}")
        if rate_limited(ip_hash):
            body = page("Error", "Too many submissions.", f"/contact/{kind}",
                         render_contact_form(kind, error="Too many submissions from this connection recently -- please try again later, or email us directly.", csrf_token=csrf_token))
            return self._html(429, body)
        try:
            control, _ = open_control_plane(self.app_root)
            opp_store = OpportunityStore(store, control.audit)
            enquiry_store = EnquiryStore(store, opp_store)
            enquiry_store.submit(
                kind=kind, name=fields.get("name", ""), email=fields.get("email", ""),
                company=fields.get("company"), message=fields.get("message", ""),
                extra={
                    "project_title": fields.get("project_title"), "project_type": fields.get("project_type"),
                    "budget_hint": fields.get("budget_hint"), "timeline": fields.get("timeline"),
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
                                          render_apply_form(job, error="Security check failed -- please reload and try again.", csrf_token=csrf_token)))
        if val("website"):
            return self._redirect("/careers/apply")
        if rate_limited(ip_hash):
            return self._html(429, page("Error", "Too many submissions.", "/careers/apply",
                                          render_apply_form(job, error="Too many submissions recently -- please try again later.", csrf_token=csrf_token)))

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
            ApplicationStore(store).create({
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
                                          render_login(error="Security check failed -- please reload and try again.", csrf_token=csrf_token)))
        if rate_limited(f"login:{ip_hash}"):
            return self._html(429, page("Sign in", "Staff sign in.", "/login",
                                          render_login(error="Too many attempts -- please wait before trying again.", csrf_token=csrf_token)))
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
    print(f"Twenty Two Technologies -- public website: http://{host}:{server.server_port}")
    server.serve_forever()


if __name__ == "__main__":
    import sys
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    serve_site(root)
