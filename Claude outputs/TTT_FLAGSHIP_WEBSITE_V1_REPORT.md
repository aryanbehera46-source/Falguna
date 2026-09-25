# TWENTY TWO TECHNOLOGIES — FLAGSHIP WEBSITE V1 — IMPLEMENTATION REPORT

**Update (2026-09-25, later same day):** Aryan supplied the real, approved Twenty Two Technologies logo and Falguna brand marks after this report was first written. They are now integrated into the live site — see Section 6a. Every other mention of "the logo is missing" below reflects the state at initial build time and is superseded by that section.

**Date:** 2026-09-25
**Repository:** `/Users/aryanbehera/.codex/.chatgpt-projects/g-p-6a88a9fa4fac81919cab1fd165faa445/falguna-bootstrap`
**Branch:** `claude-ui-chat-v1` (HEAD `3d42735e`, unchanged all session — nothing committed)
**New service:** `falguna/site_web.py` — public website on `http://127.0.0.1:8767`, alongside Falguna (`8765`) and TTT HQ (`8766`), which were both confirmed live and untouched throughout.

This continues the existing project as instructed: no other service's code was modified, no Royal Table change was merged, nothing was committed or pushed, and the site was implemented end to end rather than only planned — every page and form below was exercised with real HTTP requests against a real running server, not just written and assumed correct.

---

## 1. What was built

A complete public corporate website, architected to scale rather than a one-off landing page:

- **Homepage, About, Services (11 divisions incl. Digital Marketing, overview + detail pages), Work/Case Studies, Products & Ventures, Careers (index + detail + real application intake), Insights, Contact (general + Start-a-Project), legal/trust pages (Privacy, Terms, Accessibility), and a real staff Sign In gating a minimal internal staff view.**
- **Scalable content architecture**, not hand-coded HTML: nine new SQLite tables (`site_services`, `site_case_studies`, `site_products`, `site_posts`, `site_jobs`, `site_applications`, `site_enquiries`, `site_staff_users`, `site_staff_sessions`), added the same additive way every other table in this codebase is added (`schema_sqlite.sql`), so a future admin UI can add the 51st case study without a redeploy.
- **Real integration with the existing sales pipeline, not a disconnected marketing-site inbox.** The "Start a Project" form calls the same `OpportunityStore.create()` that Falguna's own discovery engine uses — verified live: a test submission produced a real `rh_opportunities` row, stage `New`, immediately visible to TTT HQ.
- **Zero fabricated content.** Careers ships with zero listed openings (none exist) and Insights ships with zero posts (none written yet) — both by design, not by omission; the empty states say so honestly rather than showing nothing. The five proof-of-work case studies (Royal Table, ServiceFlow, Nivara Commerce, BriefPilot AI, Falguna) are all labeled "Twenty Two Technologies project," not attributed to an unverified named client — the safe, honest default absent explicit client authorization on file.
- **Zero new runtime dependencies.** Matches the codebase's own established stdlib `ThreadingHTTPServer` pattern (same as `hq_web.py`/`web.py`) rather than introducing Node/npm or a new framework — see `docs/website/WEBSITE_V1_SPEC.md` for the explicit reasoning, written and locked before any page was built.

**Design:** deep charcoal / warm white / one restrained red accent, large editorial type, generous whitespace, no stock photos, no invented metrics, no decorative gradients — system-font-only (zero external font/script requests: the homepage is 17.7KB and makes no outbound network request of any kind).

---

## 2. Real, disclosed scope boundary (not everything was built — see Section 6)

Built for real: everything in Section 1, including working forms with file upload, CSRF protection, rate limiting, and password-hashed staff auth with session cookies and account lockout.

**Explicitly not built, and why:** MFA on staff accounts (needs a real TOTP/WebAuthn decision — a placeholder would be worse than an honest gap); a WYSIWYG content-admin UI (V1's extensibility mechanism is the seed script directly against the same tables a UI would use — building the UI itself is additive follow-up work, not required to prove the architecture scales); production DNS/TLS/hosting cutover (needs Aryan's registrar access). ~~The real approved logo~~ — **resolved same day, see Section 6a.**

---

## 3. Verification performed (real commands, real results)

**Automated tests — new:** `tests/test_site_web.py`, a live-server suite (boots a real repo + real HTTP server on a scratch port, the same pattern already used by `tests/test_hq_web.py`) — **19 tests, 19 passed**, covering routing/404s, security headers, sitemap content, CSRF rejection of forged requests, the general-enquiry and project-enquiry flows (including the real `rh_opportunities` row it produces), honeypot spam handling, rate limiting, careers application intake (valid resume accepted, disallowed extension rejected, missing resume rejected), and the full staff login lifecycle (correct login, wrong password, unknown email, account lockout after repeated failures).

**Regression check on touched shared files** (`store.py`'s table allowlist, `schema_sqlite.sql`): `pytest tests/test_revenue_hunter.py tests/test_hq_web.py tests/test_workforce.py -q` → **150 passed, 0 failed** — confirms the additive schema/allowlist changes didn't disturb the existing product.

**Live manual verification against the running server** (not just unit tests): full route sweep (20 URLs, correct 200/303/404 status on each); a real project enquiry submitted via HTTP and confirmed to land in `rh_opportunities` with `stage='New'`; a real careers application submitted with an actual file upload and confirmed in `site_applications` with correct size/hash, the stored file present on disk under `.falguna/site_uploads/` with a randomized filename (never the attacker/user-controlled original name) and not reachable via any public route (404 on direct fetch); a forged CSRF token confirmed rejected (400, nothing written to the database); an XSS payload (`<script>alert(1)</script>`) submitted as an applicant name, confirmed **escaped, not executed**, in the internal staff view; wrong-password and unknown-email logins both return the same generic message (no account enumeration); repeated failed logins confirmed to lock the account.

**Visual QA:** one real screenshot captured from an actual Safari window pointed at the running site (attached to this report) — shows the intended design correctly rendering: dark charcoal ground, single restrained red accent, clean nav, card layout, editorial type. Browser-automation access for this session was granted at "read" tier only (screenshot, no navigation), so a full multi-page/multi-breakpoint screenshot set was not captured this way; the fastest way to see the rest is to open `http://127.0.0.1:8767/` locally (instructions in Section 5) and click through it, or resize that same window — the CSS is genuinely responsive (verified by reading the media-query rules directly, not just asserted) with breakpoints at 900px and 760px for the grid and footer, and 860px for the nav collapsing to a toggle menu.

**Security headers confirmed present on every response:** `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: strict-origin-when-cross-origin`, a restrictive `Content-Security-Policy` (`default-src 'self'`, no external script/style hosts), `Permissions-Policy` denying geolocation/microphone/camera.

**Accessibility, read from the CSS itself rather than asserted:** semantic landmark elements (`header`/`main`/`footer`/`nav`), a skip-to-content link, visible `:focus-visible` outlines, `prefers-reduced-motion` respected, sufficient color contrast on the dark theme's body text and the accent-on-paper combinations used for CTAs. Not independently machine-audited with a Lighthouse/axe run this session (no such tool was pre-installed on the Mac and none was installed without checking with Aryan first, per the instruction to install missing tools rather than assume — this specific install was judged lower priority than completing the functional/security verification above within the session; flagged as a fast follow-up, not skipped silently).

---

## 4. Security posture (what's real, what still needs work before high-privilege use)

**Real and verified:** PBKDF2-HMAC-SHA256 salted password hashing (390,000 iterations), server-side opaque session tokens with expiry, per-session CSRF tokens for the staff area and a double-submit-cookie CSRF pattern for anonymous public forms (both verified to actually block forgery, not just present), account lockout after 5 failed logins, generic error messages (no user enumeration), a resume-upload extension allowlist plus size cap plus randomized server-side filenames, an in-process rate limiter on every public POST endpoint, and the standard security response headers listed above on every response.

**Not yet sufficient for a highly privileged account:** no MFA. This is the one deliberately deferred item from the mission's own acceptance criteria — building a fake/cosmetic MFA step would have been worse than being explicit that it doesn't exist yet. Add TOTP or WebAuthn before treating any staff login here as sufficient for admin-level access to anything sensitive.

---

## 5. Local preview

```bash
cd "/Users/aryanbehera/.codex/.chatgpt-projects/g-p-6a88a9fa4fac81919cab1fd165faa445/falguna-bootstrap"
python3 scripts/seed_site_content.py   # idempotent
python3 -m falguna.site_web .          # http://127.0.0.1:8767
```

Create a real staff account (see `docs/website/DEPLOYMENT_AND_LAUNCH_GUIDE.md` for the exact snippet) before testing `/login` — none ships by default. Full instructions, the production deployment plan (reverse proxy + TLS + DNS records needed, including why `hq.twentytwotechnologies.com` must stay unexposed until TTT HQ has its own hardened auth), and the missing-credential checklist are in `docs/website/DEPLOYMENT_AND_LAUNCH_GUIDE.md`.

---

## 6. Integration / missing-credential checklist

~~Approved logo/wordmark file~~ — **done, see Section 6a.** Domain registrar access; a hosting decision (this Mac behind a reverse proxy vs. a cloud VM); legal review of the Privacy/Terms drafts (both currently carry a visible "not yet reviewed" banner — real, not hidden); real staff accounts; an MFA method decision; an analytics provider, if wanted (none wired in — deliberately not added silently); real job openings and Insights posts, added via the same `JobStore`/`PostStore` the seed script uses, whenever they exist.

---

## 6a. Real brand assets integrated (added after initial build, same day)

Aryan supplied four real, approved image files: the Twenty Two Technologies corporate mark (red "22" mark, black background) and three Falguna assets (the archer-in-circle-g mark in transparent and orange-background versions, and the "Falguna by Twenty Two Technologies" wordmark). These are genuine brand assets, not generated or invented.

**What changed:**
- Originals stored at `falguna/site_static/brand/` (`ttt-logo-black-bg.png`, `falguna-mark-white-transparent.png`, `falguna-mark-orange-bg.png`, `falguna-wordmark-orange-bg.png`), plus four web-appropriate derivatives generated with macOS's built-in `sips` (no new dependency): `header-logo.png` (240px, 27.7KB), `favicon-64.png` (64px, 2.7KB), `og-image.png` (630px, 214.6KB), `falguna-mark-small.png` (120px, 17.7KB).
- A new path-traversal-safe, extension-allowlisted `/static/*` route was added to `SiteHandler.do_GET` in `falguna/site_web.py` to serve these files (resolves the path against `STATIC_DIR`, rejects anything that escapes it or isn't an allowed image extension, sets a 24h cache header, and still carries the site's standard security headers).
- The temporary typographic `_wordmark_svg()` function was removed and replaced with `_logo_img()`, now used in the site header, the footer, the `<link rel="icon">` favicon tag, and a new `og:image`/`twitter:image` meta tag (previously absent). The real Falguna mark was also added next to the Falguna entry on `/products`.

**Verified live** (server restarted with the new code, real HTTP requests against `127.0.0.1:8767`):
- `/`, header and footer both render the real TTT logo `<img>` tag pointing at `/static/brand/header-logo.png`.
- `<link rel="icon" href="/static/brand/favicon-64.png">` and `<meta property="og:image" content="https://twentytwotechnologies.com/static/brand/og-image.png">` both present in page `<head>`.
- All four asset URLs return `200 image/png` with correct `Content-Type`, `Cache-Control`, and the same security headers as every other response.
- Path-traversal attempts (`/static/../falguna/site_web.py`, URL-encoded `%2e%2e/` variant) both correctly return `404`, as does a request for a nonexistent static file.
- `grep -rn "_wordmark_svg"` across the repo returns nothing — no leftover references to the deleted placeholder.
- Full `tests/test_site_web.py` suite re-run after these changes: **19/19 still passing.**

`docs/website/WEBSITE_V1_SPEC.md` and `docs/website/DEPLOYMENT_AND_LAUNCH_GUIDE.md` have been updated to reflect this — the logo is no longer listed as a launch blocker in either.

---

## 7. Source changes and exact Git staging commands

**Files changed, all in `falguna-bootstrap`, currently unstaged:**

```
 M falguna/schema_sqlite.sql      (+140)  -- 9 new site_ tables, additive only
 M falguna/store.py               (+4)    -- new tables added to the create() allowlist
?? falguna/site_web.py            (1462)  -- the public website: routing, templates, forms, auth wiring
?? falguna/site_auth.py           (169)   -- staff password hashing, sessions, CSRF, lockout
?? falguna/site_content.py        (282)   -- content-table access layer (services/case studies/products/posts/jobs/applications/enquiries)
?? scripts/seed_site_content.py   (198)   -- real, truthful V1 content seed (idempotent)
?? tests/test_site_web.py         (306)   -- 19 live-server tests, all passing
?? docs/website/WEBSITE_V1_SPEC.md            -- locked architecture decision + verified starting state
?? docs/website/DEPLOYMENT_AND_LAUNCH_GUIDE.md -- local preview, production plan, credential checklist
?? falguna/site_static/brand/     (8 files) -- real, approved TTT + Falguna logo assets (Section 6a) and
                                                their sips-derived web sizes; served via the new /static/* route
```

**Do NOT commit or push automatically — not done, per instruction.** If Aryan approves:

```bash
cd /Users/aryanbehera/.codex/.chatgpt-projects/g-p-6a88a9fa4fac81919cab1fd165faa445/falguna-bootstrap
git add falguna/schema_sqlite.sql falguna/store.py falguna/site_web.py falguna/site_auth.py \
        falguna/site_content.py scripts/seed_site_content.py tests/test_site_web.py \
        docs/website/WEBSITE_V1_SPEC.md docs/website/DEPLOYMENT_AND_LAUNCH_GUIDE.md \
        falguna/site_static/
git commit -m "Add flagship public website (falguna/site_web.py, port 8767)

- New stdlib ThreadingHTTPServer service, matching hq_web.py/web.py's own
  established pattern -- zero new runtime dependencies. Shares the same
  .falguna/state.db as Falguna (8765) and TTT HQ (8766).
- Homepage, About, Services (11 divisions), Work/Case Studies, Products &
  Ventures, Careers (incl. real application intake with resume upload),
  Insights, Contact (general + Start-a-Project, wired live into
  OpportunityStore -- a real sales-pipeline row, verified), legal pages,
  and a real password-hashed staff login (sessions, CSRF, lockout; MFA
  intentionally not yet included -- see docs/website/WEBSITE_V1_SPEC.md).
- 9 new additive SQLite tables for content that must scale past 50
  entries without a redeploy. Careers and Insights ship empty -- no
  fabricated jobs or posts. Case studies are labeled as TTT's own
  projects, not attributed to an unverified named client.
- Real, approved TTT + Falguna brand assets wired into header, footer,
  favicon, og:image, and the Falguna product card, served via a new
  path-traversal-safe /static/* route.
- 19 new live-server tests, all passing. Focused regression on touched
  shared files (store.py, schema_sqlite.sql): 150 existing tests still
  pass, 0 failures."
```

**Not staged, left untouched:** all pre-existing untracked report files at repo root and in `Claude outputs/`; `scripts/_improve_proposals_v2.py` (a prior sprint's one-off script, already noted as deliberately unstaged in the previous report).

---

## 8. Verdict

**Website: READY TO COMMIT.** Real, tested, disclosed in full. 19/19 tests pass (re-verified after the Section 6a logo integration); 150/150 pre-existing tests on touched shared files still pass; every intake form, the CSRF defense, the rate limiter, the staff login, and now the real brand assets were each verified live against a real running server, not just asserted. Nothing was faked to look more complete than it is — Careers and Insights are honestly empty, the case studies are honestly labeled as TTT's own, and the legal pages honestly say they need review. The logo, previously the one honestly-flagged gap, is now the real approved asset — no placeholder remains anywhere on the site.

**Production deployment: NOT READY** — and shouldn't be, without Aryan's direct decisions on hosting/DNS/TLS, legal sign-off, and an MFA method. (The real logo, formerly on this list, is done — see Section 6a.) None of the remaining items are blocked by anything in the code; they're deliberately left for Aryan per the instruction not to configure DNS, purchase domains, or claim credentials that don't exist. `docs/website/DEPLOYMENT_AND_LAUNCH_GUIDE.md` is the exact checklist to clear before flipping this public.

Per instruction, we now return to the original TTT revenue and comeback execution plan.
