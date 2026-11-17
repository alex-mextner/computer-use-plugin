"""`computer chatgpt shim` (internal; the unit runs it): the reverse proxy in front of mcp-auth-proxy, see shim/rules.py."""

from __future__ import annotations

import argparse
from pathlib import Path

from computerlib.commandtree import Command
from computerlib.targets.chatgpt.shim import build_server, serve_until_signalled


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--external-url", required=True, help="public URL of the connector, e.g. https://dc.example.com")
    parser.add_argument("--listen-port", type=int, required=True, help="loopback port to listen on (the tunnel points here)")
    parser.add_argument("--upstream-port", type=int, required=True, help="loopback port of mcp-auth-proxy")
    parser.add_argument("--log-file", default="", help="access log path; empty = no access log")


def run(args: argparse.Namespace) -> int:
    server = build_server(args.external_url, args.listen_port, args.upstream_port, Path(args.log_file) if args.log_file else None)
    print(f"computer shim listening 127.0.0.1:{args.listen_port} -> 127.0.0.1:{args.upstream_port}", flush=True)
    serve_until_signalled(server)
    print("computer shim stopped", flush=True)
    return 0


COMMAND = Command(
    name="shim", help="(internal) the OAuth-challenge and access-log proxy that the unit runs", run=run, configure=configure
)
