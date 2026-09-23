"""Falguna Browser + Computer Use V1: a real, Playwright-backed browser
execution layer, first-class in Mission Control.

Architecture (Section 2 of the spec): API first -> Browser second ->
Computer-use fallback. This module is the Browser tier. It never pretends:
if the `playwright` package or its browser binaries are not installed
wherever this code is running, `playwright_available()` says so honestly
and session creation fails with a clear, actionable
`BrowserRuntimeError(PROVIDER_OFFLINE, ...)` instead of a crash or a
fabricated result -- exactly the same honesty discipline
`falguna/providers.py`'s `OllamaProvider` already applies to a missing
local model runtime.

A browser session is a first-class citizen of Falguna's own state, not a
parallel task system: `BrowserSessionStore` is a thin wrapper over the same
`StateStore` every other Falguna subsystem uses, and `falguna/web.py` merges
`browser_sessions` into the exact same Mission Control board buckets
(running/needs_you/completed/failed/archived) that Engineering Worker runs
already occupy.

Safety posture:
  - `classify_sensitive_action` inspects every click/submit/type BEFORE it
    executes and pauses the session to NEEDS_ARYAN for anything that looks
    like payment, a purchase, a contract/legal acceptance, a destructive
    delete, an account/security change, a high-risk outbound send, or raw
    credential entry -- see Section 9 of the spec. This is a heuristic, not
    a guarantee; it is deliberately conservative (over-pausing is always
    the safe failure mode here, never under-pausing).
  - `detect_challenge` looks for a CAPTCHA/human-verification marker or an
    unplanned login wall and pauses to NEEDS_ARYAN rather than ever
    attempting to solve or bypass one (Section 11 -- there is no code path
    here that could).
  - Recovery is bounded: a missing element, a navigation error, or a
    timeout gets `MAX_RECOVERY_ATTEMPTS` bounded retries with a fresh
    observation, then escalates instead of looping (Section 21).
"""

import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .attachments import AttachmentStore
from .store import StateStore, utcnow


# --------------------------------------------------------------------------
# Status / action vocabulary
# --------------------------------------------------------------------------


class BrowserSessionStatus:
    CREATED = "CREATED"
    RUNNING = "RUNNING"
    WAITING = "WAITING"
    NEEDS_ARYAN = "NEEDS_ARYAN"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ALL_SESSION_STATUSES = frozenset({
    BrowserSessionStatus.CREATED, BrowserSessionStatus.RUNNING, BrowserSessionStatus.WAITING,
    BrowserSessionStatus.NEEDS_ARYAN, BrowserSessionStatus.PAUSED, BrowserSessionStatus.COMPLETED,
    BrowserSessionStatus.FAILED, BrowserSessionStatus.CANCELLED,
})
# The board-visibility rule Mission Control needs: a session in one of these
# states is doing (or about to do) real work and can never be archived,
# mirroring the exact same guarantee `_archive_run_from_board` already gives
# an actively-running Engineering Worker mission.
ACTIVELY_RUNNING_STATUSES = frozenset({
    BrowserSessionStatus.CREATED, BrowserSessionStatus.RUNNING, BrowserSessionStatus.WAITING,
})
TERMINAL_STATUSES = frozenset({
    BrowserSessionStatus.COMPLETED, BrowserSessionStatus.FAILED, BrowserSessionStatus.CANCELLED,
})


class BrowserActionType:
    OPEN = "open"
    BACK = "back"
    FORWARD = "forward"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    SCROLL = "scroll"
    HOVER = "hover"
    SUBMIT = "submit"
    WAIT = "wait"
    INSPECT = "inspect"
    SCREENSHOT = "screenshot"
    UPLOAD = "upload"
    OPEN_TAB = "open_tab"
    CLOSE_TAB = "close_tab"
    SWITCH_TAB = "switch_tab"
    EXTRACT = "extract"


ALL_ACTION_TYPES = frozenset({
    BrowserActionType.OPEN, BrowserActionType.BACK, BrowserActionType.FORWARD, BrowserActionType.CLICK,
    BrowserActionType.TYPE, BrowserActionType.SELECT, BrowserActionType.SCROLL, BrowserActionType.HOVER,
    BrowserActionType.SUBMIT, BrowserActionType.WAIT, BrowserActionType.INSPECT, BrowserActionType.SCREENSHOT,
    BrowserActionType.UPLOAD, BrowserActionType.OPEN_TAB, BrowserActionType.CLOSE_TAB,
    BrowserActionType.SWITCH_TAB, BrowserActionType.EXTRACT,
})
# Actions that actually change page/browser state and are worth a bounded
# recovery attempt if they fail (Section 21). Pure reads (inspect/extract)
# are retried at the caller's discretion, not automatically.
STATE_CHANGING_ACTIONS = frozenset({
    BrowserActionType.OPEN, BrowserActionType.BACK, BrowserActionType.FORWARD, BrowserActionType.CLICK,
    BrowserActionType.TYPE, BrowserActionType.SELECT, BrowserActionType.SCROLL, BrowserActionType.HOVER,
    BrowserActionType.SUBMIT, BrowserActionType.UPLOAD, BrowserActionType.OPEN_TAB, BrowserActionType.CLOSE_TAB,
    BrowserActionType.SWITCH_TAB,
})
# Section 9: these actions can carry a sensitive commitment and must be
# checked before they execute. A pure navigation/read never needs the gate.
GATED_ACTIONS = frozenset({BrowserActionType.CLICK, BrowserActionType.SUBMIT, BrowserActionType.TYPE})


class BrowserRuntimeError(RuntimeError):
    """Sanitized, typed failure -- mirrors falguna.providers.FalgunaModelError's
    shape so the same category vocabulary/UX pattern applies here too."""

    PROVIDER_OFFLINE = "PROVIDER_OFFLINE"       # playwright/browser binaries unavailable
    NAVIGATION_FAILED = "NAVIGATION_FAILED"
    ELEMENT_NOT_FOUND = "ELEMENT_NOT_FOUND"
    TIMEOUT = "TIMEOUT"
    SESSION_DISCONNECTED = "SESSION_DISCONNECTED"
    POLICY_BLOCKED = "POLICY_BLOCKED"
    UNEXPECTED_FAILURE = "UNEXPECTED_FAILURE"
    INTERRUPTED_BY_RESTART = "INTERRUPTED_BY_RESTART"  # Falguna process exited/crashed while this
    # session's background thread was mid-plan. No in-memory work survives a process restart
    # (Section 24 promises the *task record* survives, not that execution resumes itself), so a
    # session left in an actively-running status by a prior process is not actually running
    # anymore -- it is a silent zombie until something says so. reconcile_after_restart() below
    # is that "something": it runs once at server startup and turns every such row into an
    # honest, actionable FAILED instead of leaving Mission Control showing phantom progress.

    def __init__(self, category: str, message: str, detail: str = ""):
        self.category = category
        self.message = message
        self.detail = detail or ""
        super().__init__(f"{category}: {message}")


def playwright_available() -> Dict[str, Any]:
    """Never raises. Reports whether the `playwright` package is importable
    and whether Chromium is actually launchable -- the same two-layer
    "installed vs. actually usable" honesty `OllamaProvider.health_check()`
    already applies to local models (Section 5: detect whether the runtime
    is installed AND whether it is actually usable, never assume)."""
    try:
        import playwright  # noqa: F401
        from playwright.sync_api import sync_playwright
    except Exception as exc:
        return {"installed": False, "launchable": False, "detail": f"playwright package not importable: {exc}"}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        return {"installed": True, "launchable": True, "detail": ""}
    except Exception as exc:
        return {
            "installed": True, "launchable": False,
            "detail": f"playwright is installed but Chromium could not launch: {exc}. "
                      f"Run: pip3 install playwright && python3 -m playwright install chromium",
        }


# --------------------------------------------------------------------------
# Sensitive-action + challenge detection (Section 9, 11)
# --------------------------------------------------------------------------

_SENSITIVE_PATTERNS = [
    ("payment_or_purchase", re.compile(
        r"\b(buy now|place order|complete purchase|pay now|proceed to payment|confirm payment|"
        r"checkout|add payment method|purchase now|make payment|pay \$|submit payment)\b", re.I)),
    ("contract_or_legal", re.compile(
        r"\b(i agree(?!\s+to receive)|accept (?:the )?terms|accept (?:the )?agreement|sign (?:the )?contract|"
        r"e-?sign|electronically sign|accept & continue|i accept|i consent to)\b", re.I)),
    ("destructive_deletion", re.compile(
        r"\b(delete (?:my )?account|permanently delete|delete permanently|erase all|remove account|"
        r"close (?:my )?account|deactivate (?:my )?account)\b", re.I)),
    ("account_or_security_change", re.compile(
        r"\b(change password|reset password|update password|disable two-?factor|disable 2fa|"
        r"remove recovery|delete api key|revoke access|change security)\b", re.I)),
    ("high_risk_send", re.compile(
        r"\b(send message|send email|post publicly|publish post|send to everyone|broadcast|"
        r"submit application|apply now)\b", re.I)),
]
_CREDENTIAL_FIELD_PATTERN = re.compile(r"\b(password|passwd|pwd|credit.?card|card.?number|cvv|cvc|ssn|"
                                        r"social.?security|routing.?number|account.?number)\b", re.I)
_CHALLENGE_PATTERNS = [
    re.compile(r"\b(captcha|recaptcha|hcaptcha|are you a (?:human|robot)|i'?m not a robot|"
               r"verify you are human|human verification|prove you'?re not a robot)\b", re.I),
]


def classify_sensitive_action(action_type: str, target: str, value: Optional[str]) -> Optional[str]:
    """Returns a short reason string if this action should pause for Aryan
    before executing, else None. Deliberately over-inclusive: a false pause
    costs a click; a missed pause could cost money or an irreversible
    change. Never runs on a pure read (inspect/extract/screenshot/scroll)."""
    if action_type not in GATED_ACTIONS:
        return None
    haystack = f"{target or ''} {value or ''}"
    for reason, pattern in _SENSITIVE_PATTERNS:
        if pattern.search(haystack):
            return reason
    if action_type == BrowserActionType.TYPE and value and _CREDENTIAL_FIELD_PATTERN.search(target or ""):
        return "credential_entry"
    return None


def detect_challenge(page_text: str, page_url: str) -> Optional[str]:
    """Heuristic CAPTCHA / human-verification detector, operating on
    already-fetched page text -- never attempts to interact with or solve
    anything it finds (Section 11)."""
    haystack = f"{page_text or ''} {page_url or ''}"
    for pattern in _CHALLENGE_PATTERNS:
        if pattern.search(haystack):
            return "captcha_or_human_verification"
    return None


_LOGIN_WALL_PATTERNS = [
    re.compile(r"\b(sign in to (?:your|continue)|log ?in to (?:your|continue)|please sign in|please log ?in|"
               r"enter your password to continue|you must (?:sign|log) in)\b", re.I),
]


def detect_unplanned_login_wall(visible_text: str, expected_login: bool) -> Optional[str]:
    """A page whose visible text prominently asks the person to sign in --
    appearing when the plan never mentioned logging in -- is exactly the
    auth-wall case Section 10 asks Falguna to pause on rather than guess
    through. Deliberately checks *visible* text only (never raw HTML
    source), since a password `<input>` existing somewhere in the DOM is
    extremely common and already handled on its own by
    `classify_sensitive_action`'s `credential_entry` gate at the moment
    something is actually typed into it -- this function exists only to
    catch an unexpected navigation TO a login wall, not the mere presence
    of a login form somewhere on a normal page."""
    if expected_login:
        return None
    for pattern in _LOGIN_WALL_PATTERNS:
        if pattern.search(visible_text or ""):
            return "unplanned_authentication_wall"
    return None


# --------------------------------------------------------------------------
# Persistence
# --------------------------------------------------------------------------


class BrowserSessionStore:
    """Thin wrapper over StateStore -- the same shape as ConversationStore
    and ResearchStore. No new database, no new process."""

    def __init__(self, store: StateStore):
        self.store = store

    def create(self, objective: str, task_type: str, project_id: Optional[str], actor: str,
               headless: bool, privacy_mode: str, plan: Optional[List[dict]] = None,
               conversation_id: Optional[str] = None, research_id: Optional[str] = None) -> str:
        objective = (objective or "").strip()
        if not objective:
            raise ValueError("browser session objective cannot be empty")
        now = utcnow()
        return self.store.create("browser_sessions", {
            "objective": objective[:2000], "task_type": task_type or "general", "project_id": project_id,
            "status": BrowserSessionStatus.CREATED, "headless": 1 if headless else 0,
            "privacy_mode": privacy_mode, "plan_json": json.dumps(plan) if plan is not None else None,
            "current_url": None, "active_tab_id": None, "needs_aryan_reason": None,
            "error": None, "error_category": None, "error_detail": None,
            "conversation_id": conversation_id, "research_id": research_id, "mc_archived": 0,
            "actor": actor or "Aryan", "created_at": now, "updated_at": now,
            "started_at": None, "completed_at": None, "next_step_index": 0,
        })

    def get(self, session_id: str) -> Optional[dict]:
        return self.store.get("browser_sessions", session_id)

    def list(self, limit: int = 200) -> List[dict]:
        rows = self.store.list("browser_sessions")
        rows.sort(key=lambda r: r["created_at"], reverse=True)
        return rows[:limit]

    def set_status(self, session_id: str, status: str, **extra: Any) -> None:
        if status not in ALL_SESSION_STATUSES:
            raise ValueError(f"unknown browser session status: {status}")
        fields = dict(extra)
        fields["status"] = status
        row = self.get(session_id)
        if status == BrowserSessionStatus.RUNNING and row and not row.get("started_at"):
            fields["started_at"] = utcnow()
        if status in TERMINAL_STATUSES:
            fields["completed_at"] = utcnow()
        self.store.update("browser_sessions", session_id, **fields)

    def archive(self, session_id: str, archived: bool) -> None:
        self.store.update("browser_sessions", session_id, mc_archived=1 if archived else 0)

    def reconcile_after_restart(self) -> List[str]:
        """Call once, at process startup, before serving any request.

        A browser session's execution lives entirely in an in-memory background
        thread of one Falguna process. If that process exits (crash, kill, or an
        ordinary restart to pick up new code) while a session's status is CREATED,
        RUNNING, or WAITING, nothing will ever move that row again -- the thread
        that owned it is gone. Left alone, Mission Control would show that session
        as perpetually "running" with no way to tell the user it isn't. This walks
        every browser session still in an actively-running status and marks it
        FAILED with error_category=INTERRUPTED_BY_RESTART, a plain-language error,
        and next_step_index left exactly where it was -- so the record itself is
        never lost (Section 24's guarantee) even though the run itself was. The
        user can see how far it got and start a fresh session to pick up from
        there; this method does not attempt to auto-resume, since re-driving a
        real browser without the user present is exactly the kind of silent
        action Section 22's approval model exists to prevent.

        Returns the ids of every session it closed out, for startup logging.
        """
        affected: List[str] = []
        for row in self.list(limit=10_000):
            if row["status"] not in ACTIVELY_RUNNING_STATUSES:
                continue
            resumed_from = row.get("next_step_index") or 0
            self.set_status(
                row["id"], BrowserSessionStatus.FAILED,
                error=(
                    "Falguna restarted while this browser session was in progress "
                    f"(it had completed step {resumed_from} of its plan). No browser "
                    "state survives a restart, so this session was stopped rather than "
                    "left showing as running. Start a new browser task to continue."
                ),
                error_category=BrowserRuntimeError.INTERRUPTED_BY_RESTART,
                error_detail="",
            )
            affected.append(row["id"])
        return affected

    # -- tabs --

    def add_tab(self, session_id: str, tab_index: int, url: Optional[str] = None, title: Optional[str] = None,
                opened_by_action_id: Optional[str] = None) -> str:
        now = utcnow()
        return self.store.create("browser_tabs", {
            "session_id": session_id, "tab_index": tab_index, "url": url, "title": title,
            "opened_by_action_id": opened_by_action_id, "status": "OPEN", "created_at": now,
            "updated_at": now, "closed_at": None,
        })

    def update_tab(self, tab_id: str, **fields: Any) -> None:
        self.store.update("browser_tabs", tab_id, **fields)

    def close_tab(self, tab_id: str) -> None:
        self.store.update("browser_tabs", tab_id, status="CLOSED", closed_at=utcnow())

    def list_tabs(self, session_id: str) -> List[dict]:
        rows = self.store.list("browser_tabs", "session_id=?", (session_id,))
        rows.sort(key=lambda r: r["tab_index"])
        return rows

    # -- actions / evidence --

    def record_action(self, session_id: str, seq: int, action_type: str, target: Optional[str], value: Optional[str],
                       result: str, detail: str = "", tab_id: Optional[str] = None,
                       screenshot_attachment_id: Optional[str] = None) -> str:
        return self.store.create("browser_actions", {
            "session_id": session_id, "tab_id": tab_id, "seq": seq, "action_type": action_type,
            "target": (target or "")[:500], "value": (value or "")[:500] if value else None,
            "result": result, "detail": (detail or "")[:2000],
            "screenshot_attachment_id": screenshot_attachment_id, "created_at": utcnow(),
        })

    def list_actions(self, session_id: str) -> List[dict]:
        rows = self.store.list("browser_actions", "session_id=?", (session_id,))
        rows.sort(key=lambda r: r["seq"])
        return rows

    def latest_action_with_screenshot(self, session_id: str) -> Optional[dict]:
        actions = [a for a in self.list_actions(session_id) if a.get("screenshot_attachment_id")]
        return actions[-1] if actions else None

    # -- downloads --

    def record_download(self, session_id: str, filename: str, source_url: Optional[str],
                         attachment_id: Optional[str]) -> str:
        return self.store.create("browser_downloads", {
            "session_id": session_id, "filename": filename, "source_url": source_url,
            "attachment_id": attachment_id, "created_at": utcnow(),
        })

    def list_downloads(self, session_id: str) -> List[dict]:
        rows = self.store.list("browser_downloads", "session_id=?", (session_id,))
        rows.sort(key=lambda r: r["created_at"])
        return rows


# --------------------------------------------------------------------------
# Runtime
# --------------------------------------------------------------------------


class PlaywrightBrowserRuntime:
    """Drives one real browser session through an observe -> decide -> act ->
    verify loop (Section 20). Executes a bounded, explicit list of steps
    produced either directly by the caller or by `falguna/browser_planner.py`
    from a natural-language objective. Never executes past MAX_STEPS. Pauses
    to NEEDS_ARYAN for a sensitive action or a detected challenge before
    performing it, records evidence for every state-changing action, and
    attempts bounded recovery before escalating."""

    MAX_STEPS = 40
    MAX_RECOVERY_ATTEMPTS = 2
    DEFAULT_TIMEOUT_MS = 15000

    def __init__(self, app_root: Path, store: StateStore, sessions: BrowserSessionStore,
                 attachments: AttachmentStore, audit=None):
        self.app_root = Path(app_root)
        self.store = store
        self.sessions = sessions
        self.attachments = attachments
        self.audit = audit

    def _log(self, event: str, data: dict) -> None:
        if self.audit is not None:
            try:
                self.audit.append(event, data)
            except Exception:
                pass  # audit is best-effort evidence, never allowed to crash a session

    def run(self, session_id: str, steps: List[dict], resume_from_index: int = 0,
            approve_gate_for_index: Optional[int] = None) -> None:
        """Runs `steps` (the session's full, persisted plan) starting at
        `resume_from_index` (0-based into `steps`). A first run always
        passes 0; resuming after a NEEDS_ARYAN pause (Section 22's "Approve
        once") passes the session's own `next_step_index`, so the exact
        step that paused -- a click Aryan just approved, a page Aryan just
        solved a CAPTCHA on, a login Aryan just completed by hand -- is
        re-attempted rather than skipped or restarted from scratch.

        `approve_gate_for_index`: when resuming specifically because Aryan
        clicked "Approve once" on a sensitive-action pause, this is that
        step's index -- classify_sensitive_action would otherwise flag the
        exact same click/type/submit forever (nothing about the page state
        changes just because a human looked at it), so the gate is bypassed
        for ONLY this one step, this one time. It never suppresses the gate
        for any other step in the plan, and a resume triggered any other
        way (a challenge pause, a plain retry) leaves it None, so the gate
        stays fully live -- this is deliberately a single-use approval, not
        a blanket authorization (Section 22: "Do not add blanket permanent
        authorization yet").

        Session persistence (Section 24): the browser runs against a
        per-session PERSISTENT profile directory
        (<app_root>/.falguna/browser_profiles/<session_id>/), not a
        throwaway in-memory context, so cookies and login state genuinely
        survive across separate `run()` invocations for the same session --
        a real login Aryan completes by hand during a pause is still there
        on resume, not lost the moment this process-level call returns."""
        session = self.sessions.get(session_id)
        if session is None:
            return
        if session["status"] == BrowserSessionStatus.CANCELLED:
            # A cancel request can land before this background thread even
            # gets scheduled to start (or before a resume call reaches
            # here) -- honor it immediately rather than clobbering it with
            # RUNNING and executing a plan the person already stopped.
            self._log("browser_session_cancelled", {"session_id": session_id, "at_step": resume_from_index})
            return
        check = playwright_available()
        if not check["installed"] or not check["launchable"]:
            self.sessions.set_status(
                session_id, BrowserSessionStatus.FAILED,
                error="The local browser runtime (Playwright) is not available on this machine.",
                error_category=BrowserRuntimeError.PROVIDER_OFFLINE, error_detail=check["detail"],
            )
            self._log("browser_session_provider_offline", {"session_id": session_id, "detail": check["detail"]})
            return
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError, Error as PWError

        headless = bool(session.get("headless", 1))
        resume_url = session.get("current_url") if resume_from_index > 0 else None
        self.sessions.set_status(session_id, BrowserSessionStatus.RUNNING)
        seq = 0
        try:
            with sync_playwright() as p:
                profile_dir = self.app_root / ".falguna" / "browser_profiles" / session_id
                profile_dir.mkdir(parents=True, exist_ok=True)
                # A persistent context (not browser.launch()+new_context())
                # is what makes resume real rather than cosmetic -- see the
                # docstring above.
                context = p.chromium.launch_persistent_context(str(profile_dir), headless=headless, accept_downloads=True)

                def on_download(download):
                    # Section 12: fires for ANY download during the session,
                    # regardless of which planned step triggered it -- saved
                    # through the exact same AttachmentStore every Chat/Work
                    # upload already goes through, so it shows up in Files
                    # automatically (Section 26), tagged with this session.
                    try:
                        path = download.path()
                        data = Path(path).read_bytes() if path else b""
                        import base64 as _b64
                        saved = self.attachments.save_base64(
                            download.suggested_filename or "download", "application/octet-stream",
                            _b64.b64encode(data).decode(), browser_session_id=session_id,
                        )
                        self.sessions.record_download(session_id, download.suggested_filename or "download",
                                                       download.url, saved["id"])
                        self._log("browser_session_download", {"session_id": session_id, "filename": download.suggested_filename})
                    except Exception as exc:
                        self._log("browser_session_download_failed", {"session_id": session_id, "detail": str(exc)[:300]})

                # launch_persistent_context starts with one already-open page
                # (about:blank on a fresh profile) rather than none -- reuse
                # it instead of opening a redundant second tab.
                page = context.pages[0] if context.pages else context.new_page()
                page.on("download", on_download)
                tab_id = self.sessions.add_tab(session_id, 0)
                tabs_by_page = {page: tab_id}
                page_order = [page]
                active_page = page
                self.sessions.set_status(session_id, BrowserSessionStatus.RUNNING, active_tab_id=tab_id)
                if resume_url:
                    # Resuming a paused session: the persistent profile keeps
                    # cookies/login state, but not the open tab itself, so
                    # get back to where the session paused before continuing
                    # the plan from resume_from_index (Section 24).
                    try:
                        page.goto(resume_url, timeout=self.DEFAULT_TIMEOUT_MS, wait_until="domcontentloaded")
                    except (PWTimeoutError, PWError) as exc:
                        self.sessions.set_status(
                            session_id, BrowserSessionStatus.FAILED,
                            error="Could not return to the page this session paused on to resume it.",
                            error_category=BrowserRuntimeError.SESSION_DISCONNECTED, error_detail=str(exc)[:2000],
                        )
                        self._log("browser_session_resume_failed", {"session_id": session_id, "detail": str(exc)[:300]})
                        context.close()
                        return

                def current_tab_id():
                    return tabs_by_page.get(active_page)

                def snapshot(reason: str) -> Optional[str]:
                    """Screenshot capture, deliberately not on every single
                    action (Section 8/38: avoid excessive screenshot spam) --
                    only on state-changing actions and pauses."""
                    try:
                        data = active_page.screenshot(type="png")
                    except Exception:
                        return None
                    saved = self.attachments.save_base64(
                        f"browser-{session_id}-{seq:03d}-{reason}.png", "image/png",
                        __import__("base64").b64encode(data).decode(), browser_session_id=session_id,
                    )
                    return saved["id"]

                for idx, step in enumerate(steps[: self.MAX_STEPS]):
                    if idx < resume_from_index:
                        continue  # already executed before an earlier pause
                    seq = idx + 1

                    # Cooperative cancellation (Section 39: no unlimited
                    # retry/run loops without a real stop path). A cancel
                    # request from the UI sets status=CANCELLED directly;
                    # this notices it at the next step boundary rather than
                    # requiring the whole plan to finish first.
                    live = self.sessions.get(session_id)
                    if live and live["status"] == BrowserSessionStatus.CANCELLED:
                        self._log("browser_session_cancelled", {"session_id": session_id, "at_step": idx})
                        context.close()
                        return

                    action_type = step.get("action")
                    target = step.get("target") or ""
                    value = step.get("value")
                    if action_type not in ALL_ACTION_TYPES:
                        self.sessions.record_action(session_id, seq, str(action_type), target, value, "FAILED",
                                                     detail=f"unknown action type: {action_type}", tab_id=current_tab_id())
                        continue

                    # -- Sensitive-action gate: check BEFORE executing. Classify
                    # against the element's real visible text (a "Buy Now"
                    # button's accessible name), not just its CSS selector --
                    # falling back to the selector itself (hyphens/# turned
                    # into spaces) whenever the element's text cannot be
                    # resolved safely, so a fetch failure never silently
                    # disables the gate. --
                    descriptor = re.sub(r"[#._\-\[\]='\"]+", " ", target or "")
                    if action_type in GATED_ACTIONS and target:
                        try:
                            element_text = active_page.locator(target).first.inner_text(timeout=800)
                            if element_text:
                                descriptor = f"{descriptor} {element_text}"
                        except Exception:
                            pass  # fall back to the selector-derived descriptor above
                    reason = classify_sensitive_action(action_type, descriptor, value)
                    if reason and idx == approve_gate_for_index:
                        # This exact occurrence was just explicitly approved
                        # by Aryan (see the `approve_gate_for_index`
                        # docstring above) -- record that plainly in the
                        # evidence trail rather than silently proceeding.
                        self._log("browser_session_gate_approved", {
                            "session_id": session_id, "reason": reason, "action": action_type, "step_index": idx,
                        })
                        reason = None
                    if reason:
                        shot = snapshot("needs-aryan")
                        self.sessions.record_action(session_id, seq, action_type, target, value, "NEEDS_ARYAN",
                                                     detail=reason, tab_id=current_tab_id(), screenshot_attachment_id=shot)
                        self.sessions.set_status(
                            session_id, BrowserSessionStatus.NEEDS_ARYAN,
                            needs_aryan_reason=f"sensitive_action:{reason}", current_url=active_page.url,
                            next_step_index=idx,
                        )
                        self._log("browser_session_needs_aryan", {"session_id": session_id, "reason": reason, "action": action_type})
                        context.close()
                        return

                    # -- Challenge detection: check page state before acting.
                    # Visible text only (page.inner_text), never raw HTML
                    # source -- see detect_unplanned_login_wall's docstring
                    # for why that distinction matters. --
                    try:
                        visible_text = active_page.inner_text("body")
                    except Exception:
                        visible_text = ""
                    challenge = detect_challenge(visible_text, active_page.url) or detect_unplanned_login_wall(
                        visible_text, expected_login=bool(step.get("expected_login")))
                    if challenge:
                        shot = snapshot("challenge")
                        self.sessions.record_action(session_id, seq, action_type, target, value, "NEEDS_ARYAN",
                                                     detail=challenge, tab_id=current_tab_id(), screenshot_attachment_id=shot)
                        self.sessions.set_status(
                            session_id, BrowserSessionStatus.NEEDS_ARYAN,
                            needs_aryan_reason=challenge, current_url=active_page.url,
                            next_step_index=idx,
                        )
                        self._log("browser_session_challenge_pause", {"session_id": session_id, "reason": challenge})
                        context.close()
                        return

                    attempt = 0
                    while True:
                        try:
                            active_page, page_order, tabs_by_page = self._execute_action(
                                action_type, target, value, active_page, page_order, tabs_by_page, context,
                                session_id, seq, on_download=on_download,
                            )
                            break
                        except (PWTimeoutError, PWError) as exc:
                            attempt += 1
                            if attempt > self.MAX_RECOVERY_ATTEMPTS:
                                shot = snapshot("failed")
                                category = BrowserRuntimeError.TIMEOUT if isinstance(exc, PWTimeoutError) else BrowserRuntimeError.ELEMENT_NOT_FOUND
                                self.sessions.record_action(session_id, seq, action_type, target, value, "FAILED",
                                                             detail=str(exc)[:500], tab_id=current_tab_id(), screenshot_attachment_id=shot)
                                self.sessions.set_status(
                                    session_id, BrowserSessionStatus.FAILED,
                                    error=f"Could not complete the '{action_type}' step after {attempt} attempts.",
                                    error_category=category, error_detail=str(exc)[:2000], current_url=active_page.url,
                                    next_step_index=idx,
                                )
                                self._log("browser_session_failed", {"session_id": session_id, "action": action_type, "attempts": attempt})
                                context.close()
                                return
                            time.sleep(0.3 * attempt)  # bounded backoff, then one more real attempt -- never a busy loop

                    if action_type in STATE_CHANGING_ACTIONS:
                        shot = snapshot(action_type)
                        self.sessions.record_action(session_id, seq, action_type, target, value, "OK",
                                                     tab_id=current_tab_id(), screenshot_attachment_id=shot)
                    else:
                        self.sessions.record_action(session_id, seq, action_type, target, value, "OK", tab_id=current_tab_id())
                    # A cancel request can also land WHILE this step's own
                    # action was executing -- re-check here rather than
                    # blindly forcing RUNNING back over a CANCELLED that
                    # just landed, which would otherwise win this race and
                    # silently keep the plan going.
                    live_after = self.sessions.get(session_id)
                    if live_after and live_after["status"] == BrowserSessionStatus.CANCELLED:
                        self._log("browser_session_cancelled", {"session_id": session_id, "at_step": idx})
                        context.close()
                        return
                    self.sessions.set_status(session_id, BrowserSessionStatus.RUNNING, current_url=active_page.url,
                                              next_step_index=idx + 1)

                # Final-state challenge/login-wall check. The per-step check
                # above only looks at the page state BEFORE that step's own
                # action runs (observe-before-act, Section 20) -- so a
                # captcha or login wall first revealed by the LAST step's
                # own action (e.g. a plan that ends on "open" landing
                # directly on a challenge page) would otherwise never be
                # checked at all before the session gets marked COMPLETED.
                # This closes that gap by checking the page exactly as it
                # stands once every planned step has actually run.
                try:
                    visible_text = active_page.inner_text("body")
                except Exception:
                    visible_text = ""
                last_step_expected_login = bool(steps[-1].get("expected_login")) if steps else False
                final_challenge = detect_challenge(visible_text, active_page.url) or detect_unplanned_login_wall(
                    visible_text, expected_login=last_step_expected_login)
                if final_challenge:
                    shot = snapshot("challenge")
                    self.sessions.record_action(session_id, seq + 1, "challenge_check", "", None, "NEEDS_ARYAN",
                                                 detail=final_challenge, tab_id=current_tab_id(), screenshot_attachment_id=shot)
                    self.sessions.set_status(
                        session_id, BrowserSessionStatus.NEEDS_ARYAN,
                        needs_aryan_reason=final_challenge, current_url=active_page.url,
                        # Every real step already executed successfully by
                        # this point (this check runs only after the loop
                        # finishes) -- resume should re-check the final
                        # state, not re-run the last step a second time.
                        next_step_index=len(steps),
                    )
                    self._log("browser_session_challenge_pause", {"session_id": session_id, "reason": final_challenge})
                    context.close()
                    return

                # All steps completed without a pause.
                shot = snapshot("completed")
                self.sessions.record_action(session_id, seq + 1, "complete", "", None, "OK", screenshot_attachment_id=shot)
                self.sessions.set_status(session_id, BrowserSessionStatus.COMPLETED, current_url=active_page.url,
                                          next_step_index=len(steps))
                self._log("browser_session_completed", {"session_id": session_id, "steps": seq})
                context.close()
        except Exception as exc:  # genuinely unexpected -- still never a crash the person sees raw
            self.sessions.set_status(
                session_id, BrowserSessionStatus.FAILED,
                error="Falguna hit an unexpected internal error running this browser session.",
                error_category=BrowserRuntimeError.UNEXPECTED_FAILURE, error_detail=str(exc)[:2000],
            )
            self._log("browser_session_unexpected_failure", {"session_id": session_id, "detail": str(exc)[:500]})

    def _execute_action(self, action_type, target, value, active_page, page_order, tabs_by_page, context, session_id, seq,
                         on_download=None):
        timeout = self.DEFAULT_TIMEOUT_MS
        if action_type == BrowserActionType.OPEN:
            active_page.goto(target, timeout=timeout, wait_until="domcontentloaded")
        elif action_type == BrowserActionType.BACK:
            active_page.go_back(timeout=timeout)
        elif action_type == BrowserActionType.FORWARD:
            active_page.go_forward(timeout=timeout)
        elif action_type == BrowserActionType.CLICK:
            active_page.click(target, timeout=timeout)
        elif action_type == BrowserActionType.TYPE:
            active_page.fill(target, value or "", timeout=timeout)
        elif action_type == BrowserActionType.SELECT:
            active_page.select_option(target, value, timeout=timeout)
        elif action_type == BrowserActionType.SCROLL:
            active_page.mouse.wheel(0, int(value) if value else 800)
        elif action_type == BrowserActionType.HOVER:
            active_page.hover(target, timeout=timeout)
        elif action_type == BrowserActionType.SUBMIT:
            active_page.click(target, timeout=timeout)
        elif action_type == BrowserActionType.WAIT:
            if target:
                active_page.wait_for_selector(target, timeout=timeout)
            else:
                active_page.wait_for_timeout(min(int(value or 1000), 10000))
        elif action_type == BrowserActionType.INSPECT or action_type == BrowserActionType.EXTRACT:
            pass  # evidence capture happens via the caller's record_action; nothing to do to the page itself
        elif action_type == BrowserActionType.SCREENSHOT:
            pass  # handled by the caller's snapshot() after every state-changing action already
        elif action_type == BrowserActionType.UPLOAD:
            path = Path(value) if value else None
            if not path or not path.is_file():
                raise RuntimeError(f"upload source file not found: {value}")
            active_page.set_input_files(target, str(path), timeout=timeout)
        elif action_type == BrowserActionType.OPEN_TAB:
            new_page = context.new_page()
            if on_download is not None:
                # Section 12: downloads must be detected on every tab, not
                # just the session's original tab -- a link opened via
                # open_tab that itself triggers a download would otherwise
                # go unrecorded.
                new_page.on("download", on_download)
            if target:
                new_page.goto(target, timeout=timeout, wait_until="domcontentloaded")
            tab_id = self.sessions.add_tab(session_id, len(page_order), url=target or None)
            tabs_by_page[new_page] = tab_id
            page_order = page_order + [new_page]
            active_page = new_page
        elif action_type == BrowserActionType.CLOSE_TAB:
            idx = int(target) if target else (len(page_order) - 1)
            if 0 <= idx < len(page_order) and len(page_order) > 1:
                closing = page_order[idx]
                tab_id = tabs_by_page.get(closing)
                if tab_id:
                    self.sessions.close_tab(tab_id)
                closing.close()
                page_order = [p for p in page_order if p != closing]
                if active_page == closing:
                    active_page = page_order[-1]
        elif action_type == BrowserActionType.SWITCH_TAB:
            idx = int(target) if target else 0
            if 0 <= idx < len(page_order):
                active_page = page_order[idx]
        return active_page, page_order, tabs_by_page
