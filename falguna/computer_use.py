"""Falguna Browser + Computer Use V1 -- the computer-use foundation
(Section 16-18). Kept deliberately minimal, per the spec's own scope
boundary: "V1 should focus only on a minimal safe subset. Do not
over-expand scope" and "Do NOT build yet: ... unrestricted desktop
control".

Architecture mirrors falguna/workforce_workers.py's BrowserChannel exactly:
an abstract channel, an honest "no adapter available" default that reports
BLOCKED rather than guessing or fabricating success, and a real
implementation that is only reachable when its actual dependency
(`pyautogui`) is installed AND the person has explicitly turned on computer
use in Settings. Browser and computer-use paths stay entirely separate --
this module never touches Playwright, and browser_runtime.py never touches
this module.

Safety posture (Section 17): screenshot is always safe (read-only) and is
the only action this module exposes without an explicit approval gate.
Every action that moves the mouse or sends a keystroke requires BOTH
`computer_use_enabled` in settings AND passes through the exact same
sensitive-action classification `browser_runtime.classify_sensitive_action`
already applies to browser actions, reusing the one classifier rather than
inventing a second one. There is no code path here that executes a shell
command, changes a system/security setting, or reads a credential store --
those stay permanently out of scope for V1, not just ungated."""

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from .browser_runtime import classify_sensitive_action


@dataclass
class ComputerActionResult:
    status: str  # "OK" | "BLOCKED" | "FAILED"
    evidence: Dict[str, Any] = field(default_factory=dict)
    blocked_reason: Optional[str] = None


class ComputerUseChannel:
    name = "computer_use_channel"

    def screenshot(self) -> ComputerActionResult:
        raise NotImplementedError

    def click(self, x: int, y: int, description: str = "") -> ComputerActionResult:
        raise NotImplementedError

    def type_text(self, text: str, description: str = "") -> ComputerActionResult:
        raise NotImplementedError

    def key(self, combo: str, description: str = "") -> ComputerActionResult:
        raise NotImplementedError


class UnavailableComputerChannel(ComputerUseChannel):
    """The honest default (matches ManualBrowserChannel): no computer-use
    adapter is wired in, or `pyautogui` is not installed on this machine, or
    the person has not turned computer use on in Settings. Every call
    reports BLOCKED with a clear, actionable reason -- never a crash, never
    a fabricated result."""

    def __init__(self, reason: str = "computer_use_not_enabled"):
        self.reason = reason

    def _blocked(self) -> ComputerActionResult:
        return ComputerActionResult(
            status="BLOCKED",
            evidence={"reason_detail": "no computer-use adapter is available or enabled for this task"},
            blocked_reason=self.reason,
        )

    def screenshot(self) -> ComputerActionResult:
        return self._blocked()

    def click(self, x: int, y: int, description: str = "") -> ComputerActionResult:
        return self._blocked()

    def type_text(self, text: str, description: str = "") -> ComputerActionResult:
        return self._blocked()

    def key(self, combo: str, description: str = "") -> ComputerActionResult:
        return self._blocked()


def pyautogui_available() -> bool:
    try:
        import pyautogui  # noqa: F401
        return True
    except Exception:
        return False


def computer_use_available() -> Dict[str, Any]:
    """Never raises. Mirrors browser_runtime.playwright_available()'s
    two-layer "installed vs. actually usable" honesty: pyautogui being
    importable does not mean a screenshot will actually succeed. On macOS a
    process without Screen Recording permission gets either an exception or
    (on some OS versions) a black/empty image back from the OS itself, not a
    clean ImportError -- so this actually attempts a screenshot rather than
    just checking the import, and reports the exact remediation the person
    needs to take in System Settings, never a bare stack trace."""
    if not pyautogui_available():
        return {"installed": False, "usable": False,
                "detail": "pyautogui is not installed. Install it with: pip3 install --user pyautogui"}
    try:
        import pyautogui
        image = pyautogui.screenshot()
        if image is None or image.width == 0 or image.height == 0:
            raise RuntimeError("screenshot returned no image data")
        return {"installed": True, "usable": True, "detail": ""}
    except Exception as exc:
        return {
            "installed": True, "usable": False,
            "detail": (
                f"pyautogui is installed but a screenshot could not be captured ({exc}). On macOS this "
                "almost always means Screen Recording permission has not been granted to the process "
                "running Falguna -- open System Settings -> Privacy & Security -> Screen Recording, and "
                "add/enable the terminal or app that launches Falguna. Falguna cannot grant this itself."
            ),
        }


class PyAutoGUIComputerChannel(ComputerUseChannel):
    """The one real implementation in V1. Requires `pyautogui` to be
    installed AND `enabled=True` to have been passed explicitly by the
    caller (which only does so when Settings' `computer_use_enabled` flag
    is on) -- constructing this class is itself the opt-in gate, mirroring
    how `GenericOpenAICompatibleProvider` only becomes reachable once the
    person has explicitly enabled and configured it."""

    name = "pyautogui_computer_channel"

    def __init__(self, enabled: bool):
        self.enabled = bool(enabled) and pyautogui_available()

    def screenshot(self) -> ComputerActionResult:
        if not pyautogui_available():
            return ComputerActionResult(status="BLOCKED", blocked_reason="pyautogui_not_installed",
                                         evidence={"reason_detail": "pyautogui is not installed on this machine; screenshot capability is unavailable"})
        try:
            import base64
            import io
            import pyautogui
            image = pyautogui.screenshot()
            buf = io.BytesIO()
            image.save(buf, format="PNG")
            # image_base64 is what lets a caller persist this as real evidence
            # (a Falguna attachment, exactly like a browser session's
            # screenshots) rather than just a width/height number -- "retain
            # evidence" needs the actual picture, not a claim about one.
            return ComputerActionResult(status="OK", evidence={
                "width": image.width, "height": image.height,
                "image_base64": base64.b64encode(buf.getvalue()).decode(),
            })
        except Exception as exc:
            return ComputerActionResult(status="FAILED", evidence={"detail": str(exc)[:500]})

    def _gated(self, action_label: str, description: str) -> Optional[ComputerActionResult]:
        if not self.enabled:
            return ComputerActionResult(status="BLOCKED", blocked_reason="computer_use_not_enabled",
                                         evidence={"reason_detail": "computer use is not enabled in Settings"})
        reason = classify_sensitive_action("click" if action_label == "click" else "type", description, description)
        if reason:
            return ComputerActionResult(status="BLOCKED", blocked_reason=f"needs_aryan:{reason}",
                                         evidence={"reason_detail": reason})
        return None

    def click(self, x: int, y: int, description: str = "", skip_gate: bool = False) -> ComputerActionResult:
        # skip_gate exists for exactly one caller: an orchestrator (like
        # web.py's computer-use session runner) that already ran the same
        # classify_sensitive_action check itself, already paused the whole
        # task for NEEDS_ARYAN approval, and is now re-invoking this exact
        # single approved step -- mirroring browser_runtime.run()'s
        # approve_gate_for_index, which is also single-use and never a
        # blanket authorization. `enabled` is never skippable: computer use
        # being off in Settings still blocks every action regardless.
        if not self.enabled:
            return ComputerActionResult(status="BLOCKED", blocked_reason="computer_use_not_enabled",
                                         evidence={"reason_detail": "computer use is not enabled in Settings"})
        if not skip_gate:
            gate = self._gated("click", description)
            if gate:
                return gate
        try:
            import pyautogui
            pyautogui.click(x, y)
            return ComputerActionResult(status="OK", evidence={"x": x, "y": y})
        except Exception as exc:
            return ComputerActionResult(status="FAILED", evidence={"detail": str(exc)[:500]})

    def type_text(self, text: str, description: str = "", skip_gate: bool = False) -> ComputerActionResult:
        if not self.enabled:
            return ComputerActionResult(status="BLOCKED", blocked_reason="computer_use_not_enabled",
                                         evidence={"reason_detail": "computer use is not enabled in Settings"})
        if not skip_gate:
            gate = self._gated("type", description or text)
            if gate:
                return gate
        try:
            import pyautogui
            pyautogui.typewrite(text)
            return ComputerActionResult(status="OK", evidence={"chars": len(text)})
        except Exception as exc:
            return ComputerActionResult(status="FAILED", evidence={"detail": str(exc)[:500]})

    def key(self, combo: str, description: str = "", skip_gate: bool = False) -> ComputerActionResult:
        if not self.enabled:
            return ComputerActionResult(status="BLOCKED", blocked_reason="computer_use_not_enabled",
                                         evidence={"reason_detail": "computer use is not enabled in Settings"})
        if not skip_gate:
            gate = self._gated("key", description or combo)
            if gate:
                return gate
        try:
            import pyautogui
            pyautogui.hotkey(*combo.split("+"))
            return ComputerActionResult(status="OK", evidence={"combo": combo})
        except Exception as exc:
            return ComputerActionResult(status="FAILED", evidence={"detail": str(exc)[:500]})


def build_computer_channel(computer_use_enabled: bool) -> ComputerUseChannel:
    if computer_use_enabled and pyautogui_available():
        return PyAutoGUIComputerChannel(enabled=True)
    return UnavailableComputerChannel(reason="pyautogui_not_installed" if computer_use_enabled else "computer_use_not_enabled")
