# Twenty Two Technologies — Flagship Website — V1 Spec (locked)

**Verified before writing this spec (real commands, this session):**
- Branch `claude-ui-chat-v1` @ `3d42735e` — clean except pre-existing untracked report files. Nothing to preserve-by-caution beyond those.
- Falguna (8765) and TTT HQ (8766) both live (`200 OK`).
- Stack: Python 3.9.6, stdlib-only web layer (`http.server.ThreadingHTTPServer` + hand-rolled HTML strings in `hq_web.py`/`web.py`, ~4-5k lines each). Node 24/npm 11 exist on the machine but are unused by the product.
- No existing frontend framework, no existing `static/`, `public/`, `frontend/` or `website/` directory, **no brand asset files anywhere in the repo** (no logo SVG/PNG) at V1 build time. This was a real, disclosed launch blocker (Section 9 of final report), not invented. **Update:** Aryan subsequently supplied the real, approved Twenty Two Technologies logo and Falguna brand marks; they now live in `falguna/site_static/brand/`, served via a new path-traversal-safe `/static/*` route, and are wired into the header, footer, favicon, `og:image`, and the Falguna product card. This blocker is resolved.
- No existing user-auth/session/login system for humans (only Falguna's own *browser automation* session store, which is unrelated). Careers/applications/staff-login tables do not exist yet.
- `OpportunityStore.create(fields, actor, source)` (`falguna/revenue_hunter.py:184`) is the real, already-built entry point into the sales pipeline — writes `rh_opportunities` (stage `New`), stage history, and an audit event. This is what the site's intake forms call directly (no duplicate lead table).

## Architecture decision

Follow the codebase's own established pattern rather than introducing a new stack: a third stdlib `ThreadingHTTPServer` service, `falguna/site_web.py`, serving the public site on **port 8767**, reading/writing the *same* shared `.falguna/state.db` via `runtime.open_control_plane(root)` — identical wiring to how `hq_web.py` and `web.py` already share state. Zero new runtime dependencies (no Jinja2, no Node build step, no npm packages) — server-rendered semantic HTML + one hand-authored CSS file + minimal progressive-enhancement JS. This is the fastest path to strong Core Web Vitals/Lighthouse scores and keeps ops surface identical to the two services Aryan already runs today (same launcher pattern, same DB, same audit log).

Content that must scale past 50 entries (services, case studies, products, insights posts, jobs) lives in new SQLite tables (additive `CREATE TABLE IF NOT EXISTS` in `schema_sqlite.sql`, the codebase's existing migration convention) — not hand-coded HTML — so a future admin UI can add entries without a redeploy. V1 seeds these tables with only real, truthful content (see Block C).

New tables (all additive, `site_` prefixed to avoid any collision):
`site_services`, `site_case_studies`, `site_products`, `site_posts`, `site_jobs`, `site_applications`, `site_enquiries`, `site_staff_users`, `site_staff_sessions`.

## Explicit, disclosed scope for this pass (not every acceptance item is achievable in one sitting — see final report Section 9)

Built for real, end to end: visual system + homepage; About; Services overview + detail pages incl. Digital Marketing; Work/Case Studies (Royal Table correctly labeled as TTT's own demonstration project, Falguna as TTT's own product); Products & Ventures; Insights (real publishing system, ships with zero fabricated posts); legal/trust pages (flagged for legal review); Careers (jobs index/detail + real application intake with file upload + internal review queue), ships with zero fabricated openings; Contact + Start a Project, both wired live to `OpportunityStore`; a real, server-enforced staff login (password hashing + sessions + CSRF) gating a placeholder staff area — **not** exposing port 8766 publicly, per instruction; sitemap/robots/meta/JSON-LD; automated tests; manual QA across breakpoints.

Explicitly deferred and disclosed, not faked: MFA (needs a real TOTP/WebAuthn integration decision + secrets management — documented as a follow-up, not implemented as a placeholder claiming to be real MFA); production DNS/TLS cutover (needs Aryan's registrar/hosting access); a WYSIWYG admin UI for the new content tables (V1 content is seeded directly into the DB with a reusable seed script, which *is* the extensibility mechanism — a UI on top of the same tables is additive later work).
