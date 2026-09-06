import argparse
import json
import os
import shlex
import shutil
from pathlib import Path

from .codex_transport import CodexCliJSONTransport
from .gateway import OpenAICompatibleGateway
from .models import CommandSpec, RunPolicy
from .review import ModelSemanticReviewer
from .runtime import open_control_plane
from .usability import evidence_summary, mission_view
from .workers import StructuredEditWorker
from .web import serve


def main():
    parser = argparse.ArgumentParser(prog="falguna")
    parser.add_argument("--root", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    web = sub.add_parser("web")
    web.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost"))
    web.add_argument("--port", type=int, default=8765)
    create = sub.add_parser("create-mission")
    create.add_argument("--title", required=True)
    create.add_argument("--requirement", required=True)
    run = sub.add_parser("run")
    run.add_argument("--objective", required=True)
    run.add_argument("--editable", action="append", required=True)
    run.add_argument("--test", action="append", required=True, help="Verification command; repeat for multiple checks")
    run.add_argument("--model", default="gpt-5.4-mini")
    run.add_argument("--max-cost-usd", type=float, default=0.50)
    run.add_argument("--browser-url")
    run.add_argument("--codex", default=shutil.which("codex"))
    status = sub.add_parser("status")
    status.add_argument("--run")
    summary = sub.add_parser("summary")
    summary.add_argument("--run", required=True)
    decide = sub.add_parser("decide")
    decide.add_argument("--run", required=True)
    decide.add_argument("--action", required=True, choices=("approve", "reject", "request-changes"))
    decide.add_argument("--actor", required=True)
    decide.add_argument("--reason", required=True)
    args = parser.parse_args()
    root = Path(args.root).resolve()
    control, store = open_control_plane(root)
    try:
        if args.command == "web":
            store.close()
            serve(root, args.host, args.port)
            return
        if args.command == "init":
            print(json.dumps({"status": "initialized", "root": str(root)}))
        elif args.command == "create-mission":
            print(json.dumps(control.create_mission(args.title, args.requirement, root, RunPolicy()), indent=2))
        elif args.command == "run":
            if not args.codex:
                parser.error("authenticated Codex executable not found")
            commands = [CommandSpec(shlex.split(value), label=f"native-{index}") for index, value in enumerate(args.test, 1)]
            policy = RunPolicy(
                allowed_write_globs=args.editable,
                verification_commands=commands,
                max_cost_usd=args.max_cost_usd,
                browser_base_url=args.browser_url,
            )
            gateway = OpenAICompatibleGateway(args.model, "http://127.0.0.1:1/v1", "")
            codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
            transport = CodexCliJSONTransport(Path(args.codex), codex_home)
            worker = StructuredEditWorker(gateway, args.editable, transport=transport)
            control.reviewer = ModelSemanticReviewer(gateway, transport=transport)
            ids = control.create_mission(args.objective[:80], args.objective, root, policy)
            run_id = control.start(ids["task_id"], worker, "structured-codex", args.model, policy)
            print(json.dumps(evidence_summary(store, root / ".falguna", control.audit, run_id), indent=2))
        elif args.command == "status":
            output = mission_view(store, root / ".falguna", args.run) if args.run else {"missions": store.list("missions"), "runs": store.list("runs")}
            print(json.dumps(output, indent=2))
        elif args.command == "summary":
            print(json.dumps(evidence_summary(store, root / ".falguna", control.audit, args.run), indent=2))
        elif args.command == "decide":
            control.decide_merge(args.run, args.action, args.actor, args.reason)
            print(json.dumps({"run_id": args.run, "decision": args.action, "merge_performed": False}, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()
