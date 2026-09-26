#!/usr/bin/env python3
"""Twenty Two Technologies -- Live Enquiry Activation V1.

CHOSEN OPERATIONAL PATH (Step 2): the public site is a free Render Static
Site with no persistent backend, and Falguna/TTT HQ run locally/
intermittently on Aryan's own Mac -- there is nowhere to responsibly host
an always-on, authenticated public webhook receiver this sprint without
paid/persistent hosting (out of scope without explicit approval). So the
real path today is:

    Tally submission -> Tally's own dashboard/notification (verified
    manually by Aryan -- see the mission's Step 1/Step 8 findings) ->
    Aryan exports/downloads that one submission as JSON from Tally's
    dashboard -> this script -> real CommsStore/EnquiryStore/
    ApplicationStore state -> Falguna AI workforce triage in TTT HQ.

This is a manual step today (one export, one command, per real
submission) -- never an automatic public ingestion pipeline. It is exactly
the mission's own stated "preferred first operational path", chosen
because it needs no new hosting, no new public attack surface, and no
credentials this environment doesn't already have.

Usage:
    python3 scripts/import_tally_submission.py path/to/export.json
    python3 scripts/import_tally_submission.py path/to/export.json --app-root /path/to/falguna-bootstrap

The JSON file is expected to be Tally's own webhook/export payload shape
for ONE submission:
    {"eventId": "...", "eventType": "FORM_RESPONSE", "createdAt": "...",
     "data": {"responseId": "...", "submissionId": "...", "formId": "...",
              "fields": [{"key": "...", "label": "...", "value": "..."}, ...]}}

It can also be pointed at a JSON file containing a LIST of such payloads
(e.g. several exports batched together) -- each is ingested independently
and idempotently, so re-running this script on the same file(s) is always
safe (already-ingested submissions come back as "duplicate", never a
second lead/application/opportunity).

Never sends anything externally and never fabricates a submission -- if
the JSON file doesn't parse, or a submission is missing a field this
system can't confidently match by label, it is reported as rejected here
and recorded in `tally_intake_events` for visibility in TTT HQ's
Communications view. Nothing about this script assumes network access;
it only ever touches the local `.falguna/state.db` this environment
already uses.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("export_path", type=Path, help="Path to a Tally export JSON file (one payload, or a JSON list of payloads).")
    parser.add_argument("--app-root", type=Path, default=Path(__file__).resolve().parent.parent,
                         help="Falguna app root containing .falguna/state.db (default: repo root this script lives in).")
    parser.add_argument("--actor", default="aryan_manual_import", help="Actor name recorded in the audit trail.")
    args = parser.parse_args()

    # The `falguna` package always lives next to this script's own repo
    # (scripts/../falguna) -- separate from --app-root, which only controls
    # where .falguna/state.db is opened (so a smoke test can point state at
    # a scratch directory without needing a second copy of the package).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from falguna.comms import CommsStore
    from falguna.runtime import open_control_plane
    from falguna.tally_intake import TallyIntakeService
    from falguna.ttt_hq import NeedsAryanQueue

    if not args.export_path.exists():
        print(f"ERROR: {args.export_path} does not exist", file=sys.stderr)
        return 2

    try:
        raw = json.loads(args.export_path.read_text())
    except json.JSONDecodeError as exc:
        print(f"ERROR: {args.export_path} is not valid JSON: {exc}", file=sys.stderr)
        return 2

    payloads = raw if isinstance(raw, list) else [raw]

    control, store = open_control_plane(args.app_root)
    needs_aryan = NeedsAryanQueue(store, control.audit, control)
    comms = CommsStore(store, control.audit, needs_aryan)
    service = TallyIntakeService(store, control.audit, comms)

    exit_code = 0
    for i, payload in enumerate(payloads):
        result = service.ingest(payload, actor=args.actor)
        status = result.get("status")
        label = f"[{i + 1}/{len(payloads)}]"
        if status == "ingested":
            print(f"{label} INGESTED  form_type={result.get('form_type')} "
                  f"conversation_id={result.get('conversation_id')} "
                  f"enquiry_id={result.get('enquiry_id')} application_id={result.get('application_id')} "
                  f"opportunity_id={result.get('opportunity_id')}")
            if result.get("workforce_actions"):
                for action in result["workforce_actions"]:
                    print(f"       -> AI workforce: {action}")
        elif status == "duplicate":
            print(f"{label} DUPLICATE (already ingested) form_type={result.get('form_type')} "
                  f"conversation_id={result.get('conversation_id')}")
        else:
            exit_code = 1
            print(f"{label} REJECTED  reason={result.get('reason')!r}", file=sys.stderr)

    store.close()
    print()
    print("Open TTT HQ -> Communications to see these conversations, review AI drafts, "
          "and approve/send anything that needs a human decision.")
    print("If any submission above was REJECTED, check falguna/tally_intake.py's "
          "FIELD_LABEL_CANDIDATES against this export's real field labels -- see the "
          "module's own docstring for how to tighten the mapping.")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
