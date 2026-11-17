"""`computer chatgpt logs`: the compact access log (what reached this PC through the connector, never bodies or secrets)."""

from __future__ import annotations

import argparse

from computerlib.commandtree import Command
from computerlib.targets.chatgpt.accessview import Filter, read_records, select
from computerlib.targets.chatgpt.context import load_context


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("-n", "--lines", type=int, default=40, help="last N lines after the filters (default 40)")
    parser.add_argument("--all", action="store_true", help="include the rotated files, oldest first")
    parser.add_argument("--tool", help="only tools/call lines of this tool")
    parser.add_argument("--rpc", help="only this JSON-RPC method (initialize, tools/list, tools/call, ...)")
    parser.add_argument("--status", type=int, help="only this HTTP status")
    parser.add_argument("--client", help="only this client label (openai-mcp, unverified, ...)")
    parser.add_argument("--oauth", action="store_true", help="only the OAuth steps (/.idp/*, /.auth/*)")


def run(args: argparse.Namespace) -> int:
    ctx = load_context()
    log = ctx.layout.access_log
    records = read_records(log, include_rotated=args.all)
    if not records and not log.exists():
        print(f"no log yet at {log}")
        return 5
    wanted = Filter(tool=args.tool, rpc=args.rpc, status=args.status, client=args.client, oauth=args.oauth)
    for record in select(records, wanted, args.lines):
        print(record.raw)
    return 0


COMMAND = Command(
    name="logs",
    help="show the access log with filters (--tool, --rpc, --status, --client, --oauth)",
    run=run,
    configure=configure,
)
