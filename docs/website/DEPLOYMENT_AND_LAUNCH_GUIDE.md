# Twenty Two Technologies Website — Deployment & Launch Guide

## Local preview (works right now)

```bash
cd "/Users/aryanbehera/.codex/.chatgpt-projects/g-p-6a88a9fa4fac81919cab1fd165faa445/falguna-bootstrap"
python3 scripts/seed_site_content.py        # idempotent; safe to re-run
python3 -m falguna.site_web .               # serves http://127.0.0.1:8767
```

Create a real staff account before testing `/login` (there is no public signup route, by design):

```bash
python3 -c "
from falguna.runtime import open_control_plane
from falguna.site_auth import StaffAuthService
control, store = open_control_plane('.')
StaffAuthService(store).create_user('you@twentytwotechnologies.com', 'Your Name', 'a-real-strong-password', role='admin')
store.close()
"
```

No brand-new dependency was added -- this runs with the Python already on the machine, the same way Falguna (8765) and TTT HQ (8766) already run.


## Production deployment plan (not executed this sprint -- needs Aryan's registrar/hosting access)

The service itself (`falguna.site_web.serve_site`) intentionally binds to `127.0.0.1` only and refuses any other host (see the `ValueError` in `serve_site`) -- it is not safe or intended to be exposed directly to the internet as a bare Python `http.server`. A real launch needs:

1. **A host.** Either the same Mac (if it will stay on and reachable) behind a reverse proxy, or a small cloud VM. Given TTT HQ and Falguna already run as local macOS `.app` launchers, the lowest-risk V1 is: keep running `site_web.py` on `127.0.0.1:8767` on the Mac, and put a reverse proxy (Caddy or nginx) in front of it that terminates TLS and forwards to `127.0.0.1:8767`. Caddy is recommended specifically because it gets automatic Let's Encrypt TLS with a two-line config.
2. **DNS.** At the registrar for `twentytwotechnologies.com`: an `A`/`AAAA` (or `CNAME` if hosted behind a provider) record for the apex/`www` pointing at wherever the reverse proxy lives. `hq.twentytwotechnologies.com` and `mail.twentytwotechnologies.com` are reserved subdomains for later -- do not create them yet; TTT HQ (port 8766) must **not** be exposed publicly until it has its own hardened auth (it currently has none -- see below).
3. **Process supervision.** Add a fourth macOS launcher app (mirroring the existing `Falguna.app` / `Twenty Two Technologies.app` / `Stop *.app` pattern in `launcher/`) so the site survives a reboot the same way the other two services do. Not built this sprint -- flagged as a follow-up, not faked.
4. ~~**A real logo asset.**~~ **RESOLVED.** Aryan supplied the real, approved Twenty Two Technologies mark and the real Falguna brand mark/wordmark (4 original files). They live in `falguna/site_static/brand/` (originals + `sips`-derived web sizes: `header-logo.png` 240px, `favicon-64.png` 64px, `og-image.png` 630px, `falguna-mark-small.png` 120px -- no new dependency needed, macOS's built-in `sips` did the resizing). `_wordmark_svg()` was removed and replaced with `_logo_img()`, which is now used in the header, footer, and favicon/`og:image` tags; a new path-traversal-safe, extension-allowlisted `/static/*` route serves the files. The Falguna product card on `/products` also now shows the real Falguna mark. Verified live: all four asset URLs return `200 image/png` with correct `Content-Type` and security headers, traversal attempts (`../`, `%2e%2e/`) correctly 404, and the full `tests/test_site_web.py` suite (19 tests) still passes.
5. **Legal review.** `/legal/privacy`, `/legal/terms` carry a visible "not yet reviewed by counsel" banner (`LEGAL_REVIEW_NOTE` in `site_web.py`) -- get real review, then remove the banner in a follow-up edit.
6. **MFA for privileged staff accounts.** Deliberately not built this sprint (see `docs/website/WEBSITE_V1_SPEC.md`). Password auth + sessions + CSRF + lockout is real and tested, but is not sufficient on its own for a highly privileged account. Add TOTP (e.g. `pyotp`) or WebAuthn before treating any staff account here as high-privilege.


## Missing-credential / integration checklist

None of these were configured, faked, or assumed -- all genuinely absent and needed from Aryan before or at launch:

- [x] Approved TTT logo/wordmark file -- **DONE.** Supplied by Aryan and wired into the live site (header, footer, favicon, `og:image`, Falguna product card).
- [ ] Domain registrar access for `twentytwotechnologies.com` DNS
- [ ] Hosting decision (this Mac behind a reverse proxy, vs. a cloud VM)
- [ ] TLS certificate path (Caddy automates this against Let's Encrypt if DNS points at it -- no manual cert needed)
- [ ] Legal review of Privacy Policy / Terms
- [ ] Real staff accounts (create via the snippet above -- no seeded/default account ships in the DB other than what you create by hand)
- [ ] A decision on MFA method (TOTP vs. WebAuthn) before treating staff login as sufficient for a highly privileged account
- [ ] Analytics provider, if wanted (none is wired in -- the site works and is measurable via server logs/audit trail without one; adding e.g. Plausible/GA4 is a small follow-up, deliberately not added silently)
- [ ] Any real, current job openings and Insights posts (both ship empty on purpose -- add via `JobStore.create()` / `PostStore.create_draft()`+`publish()`)

## What is genuinely production-ready today vs. not

**Ready:** all page rendering, the real approved brand assets (Twenty Two Technologies mark + Falguna mark, wired into header/footer/favicon/`og:image`/products), the content schema (scales past 50 entries without a redeploy), both intake forms (real pipeline integration, verified live), careers application intake (real file validation, verified live), CSRF protection (verified blocks forged requests), rate limiting, staff password auth incl. lockout (verified), security headers, sitemap/robots/canonical/OG/JSON-LD, automated test coverage (19 tests, all passing), zero new runtime dependencies.

**Not ready without the items above:** public internet exposure (needs the reverse proxy + DNS + TLS), legal-reviewed policy pages, MFA on staff accounts, and a process supervisor so the site survives a machine reboot.
