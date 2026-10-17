"""CLI entry point."""

import argparse
import json
import sys

from . import core


def parser():
    root = argparse.ArgumentParser(description="Evidence-first tools for Kilo-native crypto research")
    sub = root.add_subparsers(dest="command", required=True)
    sub.add_parser("preflight", help="Check optional API settings locally; no secrets or network requests")
    init = sub.add_parser("init", help="Create an isolated run and shared collection budget")
    init.add_argument("query")
    init.add_argument("--root", default="artifacts/runs")
    init.add_argument("--mode", choices=("short", "long", "both"), default="both")
    init.add_argument("--profile", choices=core.PROFILES, default="deep")
    init.add_argument("--max-requests", type=int, help="Shared HTTP ceiling (deep: 96; quick: 24)")
    init.add_argument("--minutes", type=int, help="Collection deadline (deep: 60; quick: 15); not model runtime")
    for name in ("fetch", "discover", "search", "protocol", "market", "fees", "rpc", "inspect", "observe", "calculate", "identity", "status", "check", "publish"):
        command = sub.add_parser(name)
        command.add_argument("run", help="Run directory returned by init")
        if name == "fetch":
            command.add_argument("url")
            command.add_argument("--kind", choices=core.KINDS, default="other")
            command.add_argument("--label", default="")
        elif name == "discover":
            command.add_argument("query")
        elif name == "search":
            command.add_argument("query", help="Public query; results are discovery leads, not evidence of claims")
            command.add_argument("--limit", type=int, default=8)
        elif name in ("protocol", "market", "fees"):
            command.add_argument("identifier")
            if name == "fees":
                command.add_argument("--metric", choices=("fees", "revenue"), default="fees")
        elif name == "rpc":
            command.add_argument("method", choices=sorted(core.RPC_METHODS))
            command.add_argument("--params", type=json.loads, default=[])
        elif name == "inspect":
            command.add_argument("evidence_id")
            command.add_argument("path", nargs="?", default="")
            command.add_argument("--limit", type=int, default=8)
            command.add_argument("--query", help="Filter a JSON array or search bounded snippets in saved text")
            command.add_argument("--links", action="store_true", help="Extract untrusted links from a saved page; no network")
        elif name in ("observe", "calculate"):
            if name == "observe":
                command.add_argument("evidence_id")
                command.add_argument("pointer")
                command.add_argument("--as-of")
                command.add_argument("--quote", help="Exact saved source quote; use empty pointer for text extraction")
                command.add_argument("--value", dest="literal", help="Exact source literal: decimal, comma-grouped digits, or English zero..twenty")
            else:
                command.add_argument("expression")
                command.add_argument("--inputs", type=json.loads, required=True)
            command.add_argument("--label", required=True)
            command.add_argument("--unit", required=True)
        elif name == "identity":
            command.add_argument("--name", required=True)
            command.add_argument("--official-url", required=True)
            command.add_argument("--chain", action="append", default=[])
            command.add_argument("--token-status", choices=("unknown", "none_verified", "confirmed"), default="unknown")
            command.add_argument("--evidence", action="append", required=True)
        elif name == "check":
            command.add_argument("--final", action="store_true")
    return root


def main(argv=None):
    args = vars(parser().parse_args(argv))
    command = args.pop("command")
    if command == "observe":
        args["path"] = args.pop("pointer")
    if command == "identity":
        args["chains"] = args.pop("chain")
        args["evidence_ids"] = args.pop("evidence")
    try:
        result = getattr(core, "inspect_source" if command == "inspect" else command)(**args)
    except (core.ResearchError, OSError, ValueError, KeyError, TypeError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if command in {"check", "publish"} and not result["ok"]:
        return 1
    if command != "inspect" and isinstance(result, dict) and result.get("status") in {"error", "unavailable"}:
        return 1
    if command == "discover" and any(record["status"] != "ok" for record in result.values()):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
