import argparse
import json
from pathlib import Path

from .models import RunPolicy
from .runtime import open_control_plane


def main():
    parser = argparse.ArgumentParser(prog="falguna")
    parser.add_argument("--root", default=".")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    create = sub.add_parser("create-mission")
    create.add_argument("--title", required=True)
    create.add_argument("--requirement", required=True)
    status = sub.add_parser("status")
    status.add_argument("--run")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    control, store = open_control_plane(root)
    try:
        if args.command == "init":
            print(json.dumps({"status": "initialized", "root": str(root)}))
        elif args.command == "create-mission":
            print(json.dumps(control.create_mission(args.title, args.requirement, root, RunPolicy()), indent=2))
        elif args.command == "status":
            output = store.get("runs", args.run) if args.run else {"missions": store.list("missions"), "runs": store.list("runs")}
            print(json.dumps(output, indent=2))
    finally:
        store.close()


if __name__ == "__main__":
    main()

