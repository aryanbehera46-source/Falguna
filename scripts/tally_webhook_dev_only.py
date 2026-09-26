#!/usr/bin/env python3
"""Twenty Two Technologies -- Live Enquiry Activation V1.

DEVELOPER-ONLY LOCAL WEBHOOK RECEIVER. THIS IS NOT A LIVE INTEGRATION.

This script is groundwork for a possible future path (real, automatic
webhook delivery from Tally) -- it is NOT deployed, NOT exposed to the
public internet, and MUST NOT be pointed at from Tally's real dashboard
webhook settings unless and until Aryan explicitly approves deploying a
persistent, properly secured, publicly reachable receiver for it (this
sprint's mission explicitly rules that out without separate approval: the
public site is a free Render Static Site with no approved backend, and a
Mac that sleeps/goes offline cannot reliably receive public webhook
deliveries).

What this script actually is: a plain `http.server` process, meant to be
run and tested ONLY on localhost, that:
  1. Verifies a Tally webhook's signature -- HMAC-SHA256 of the raw request
     body, using a secret only Aryan would set in Tally's dashboard and in
     this process's TALLY_WEBHOOK_SECRET environment variable. Tally's
     documented header for this is `tally-signature`. THIS HAS NOT BEEN
     VERIFIED AGAINST A REAL TALLY-SENT REQUEST (no dashboard access was
     available while building this -- see the mission's Step 1 findings).
     Confirm the exact header name/encoding against a real webhook test
     delivery from Tally's own dashboard before ever relying on this.
  2. Rejects any request that doesn't verify, or arrives with no secret
     configured at all (fails closed, not open).
  3. On a verified request, hands the parsed JSON body to the same
     `TallyIntakeService.ingest()` this sprint's manual-import CLI uses --
     the idempotency check inside that service is what provides replay
     protection (a resent/duplicate delivery is a safe no-op), on top of
     this script's own signature check rejecting an unsigned/forged replay
     outright.

Run it locally to test the signature-verification and end-to-end ingest
path:
    TALLY_WEBHOOK_SECRET=your-test-secret python3 scripts/tally_webhook_dev_only.py --port 8765

Then, from another terminal, simulate what a Tally webhook delivery would
look like (this is NOT a real Tally payload -- it's a local test only):
    python3 -c "
import hmac, hashlib, json, urllib.request
secret = b'your-test-secret'
body = json.dumps({'eventId':'evt-1','data':{'submissionId':'test-1','formId':'VLgxN6',
    'fields':[{'label':'Name','value':'Test User'},{'label':'Email','value':'test@example.com'},
              {'label':'Message','value':'hello'}]}}).encode()
sig = hmac.new(secret, body, hashlib.sha256).hexdigest()
req = urllib.request.Request('http://127.0.0.1:8765/tally-webhook', data=body,
    headers={'Content-Type':'application/json','tally-signature':sig})
print(urllib.request.urlopen(req).read())
"

This never runs as part of the normal TTT HQ process, is not imported by
any other module in this codebase, and ships disabled by default (refuses
to start at all without TALLY_WEBHOOK_SECRET set).
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path


def _verify_signature(secret: bytes, raw_body: bytes, signature_header: str | None) -> bool:
    if not signature_header:
        return False
    expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    # Constant-time comparison -- never a plain == on a secret-derived value.
    return hmac.compare_digest(expected, signature_header.strip())


def make_handler(service, secret: bytes, actor: str):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *fmt_args):  # keep default stderr logging, just labeled
            sys.stderr.write("[tally_webhook_dev_only] " + (fmt % fmt_args) + "\n")

        def do_POST(self):
            if self.path.rstrip("/") != "/tally-webhook":
                self.send_response(404)
                self.end_headers()
                return

            length = int(self.headers.get("Content-Length", 0) or 0)
            raw_body = self.rfile.read(length) if length else b""
            signature = self.headers.get("tally-signature")

            if not _verify_signature(secret, raw_body, signature):
                # Fails closed: no secret match, no ingestion, no fabricated
                # "received" response -- an unauthorized/forged/replayed
                # request is rejected before it ever reaches TallyIntakeService.
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "invalid or missing signature"}).encode())
                return

            try:
                payload = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.send_response(400)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps({"error": "invalid JSON body"}).encode())
                return

            result = service.ingest(payload, actor=actor)
            status_code = 200 if result.get("status") in ("ingested", "duplicate") else 422
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(result).encode())

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({
                "status": "dev-only local receiver -- not a live integration",
                "note": "POST a signed Tally payload to /tally-webhook",
            }).encode())

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--bind", default="127.0.0.1", help="Bind address. Never 0.0.0.0 -- this is dev-only.")
    parser.add_argument("--app-root", type=Path, default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--actor", default="tally_webhook_dev_only")
    args = parser.parse_args()

    secret = os.environ.get("TALLY_WEBHOOK_SECRET")
    if not secret:
        print("ERROR: TALLY_WEBHOOK_SECRET is not set. This receiver refuses to start without a "
              "signature secret configured (fail closed, never fail open to an unauthenticated "
              "receiver).", file=sys.stderr)
        return 2
    if args.bind not in ("127.0.0.1", "localhost", "::1"):
        print("ERROR: refusing to bind to a non-localhost address. This script is dev-only and "
              "must never be exposed on the network.", file=sys.stderr)
        return 2

    # The `falguna` package always lives next to this script's own repo
    # (scripts/../falguna) -- separate from --app-root, which only controls
    # where .falguna/state.db is opened.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from falguna.comms import CommsStore
    from falguna.runtime import open_control_plane
    from falguna.tally_intake import TallyIntakeService
    from falguna.ttt_hq import NeedsAryanQueue

    control, store = open_control_plane(args.app_root)
    needs_aryan = NeedsAryanQueue(store, control.audit, control)
    comms = CommsStore(store, control.audit, needs_aryan)
    service = TallyIntakeService(store, control.audit, comms)

    handler = make_handler(service, secret.encode("utf-8"), args.actor)
    # Deliberately single-threaded (not ThreadingHTTPServer): StateStore's
    # sqlite3 connection is opened once, on the main thread, and sqlite3
    # connections are not safe to share across threads. A dev-only local
    # receiver has no real concurrency requirement -- one request at a time
    # is correct here, not a limitation worth working around.
    server = HTTPServer((args.bind, args.port), handler)
    print(f"[tally_webhook_dev_only] DEV-ONLY receiver listening on http://{args.bind}:{args.port}/tally-webhook")
    print("[tally_webhook_dev_only] This is NOT a live integration -- localhost only, never deployed.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
