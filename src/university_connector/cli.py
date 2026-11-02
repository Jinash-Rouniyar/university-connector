from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import webbrowser

from university_connector.auth import AuthError
from university_connector.config import Settings


def _runtime(args: argparse.Namespace):
    from university_connector.runtime import Runtime

    settings = Settings()
    if getattr(args, "host", None):
        settings.host = args.host
    if getattr(args, "port", None):
        settings.port = args.port
    if getattr(args, "public_url", None):
        settings.public_url = args.public_url
    return Runtime(settings)


def cmd_serve(args: argparse.Namespace) -> None:
    import uvicorn

    from university_connector.auth.web_portal import create_app

    runtime = _runtime(args)
    app = create_app(runtime)
    key = runtime.access_key.value
    print(
        "\nUniversity Connector\n"
        f"  Portal:        {runtime.portal_url}\n"
        f"  MCP endpoint:  {runtime.settings.mcp_url}\n"
        f"  Access key:    {key}\n"
        "  Agents send the key as 'Authorization: Bearer <key>', or append ?key=<key> to the MCP URL.\n",
        flush=True,
    )
    if not args.no_browser:
        local = f"http://{runtime.settings.host}:{runtime.settings.port}/?key={key}"
        webbrowser.open(local)
    uvicorn.run(app, host=runtime.settings.host, port=runtime.settings.port, log_level="info")


def cmd_login(args: argparse.Namespace) -> None:
    runtime = _runtime(args)
    university = runtime.registry.get(args.university_id)
    print(f"Opening {university.name} sign-in. Finish signing in in the browser window...")
    try:
        asyncio.run(runtime.auth.login(university.id))
    except AuthError as exc:
        sys.exit(f"Sign-in did not complete: {exc}")
    print(f"Connected to {university.name}.")


def cmd_refresh(args: argparse.Namespace) -> None:
    runtime = _runtime(args)
    try:
        asyncio.run(runtime.auth.refresh(args.university_id))
    except AuthError as exc:
        sys.exit(f"Silent refresh failed: {exc}\nRun: university-connector login {args.university_id}")
    print("Silent refresh succeeded.")


def cmd_status(args: argparse.Namespace) -> None:
    runtime = _runtime(args)
    statuses = [runtime.auth.status(u.id) for u in runtime.registry.all()]
    for status in statuses:
        status.pop("reconnect_url", None)
    print(json.dumps(statuses, indent=2))


def cmd_universities(args: argparse.Namespace) -> None:
    runtime = _runtime(args)
    for university in runtime.registry.all():
        print(f"{university.id:24} {university.lms_type:12} {university.name}")


def cmd_key(args: argparse.Namespace) -> None:
    runtime = _runtime(args)
    print(runtime.access_key.rotate() if args.rotate else runtime.access_key.value)


def cmd_stdio(args: argparse.Namespace) -> None:
    from university_connector.mcp.server import create_mcp

    runtime = _runtime(args)
    create_mcp(runtime).run(transport="stdio", show_banner=False)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="university-connector",
        description="Connect your university LMS to AI agents over MCP.",
    )
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the auth portal + MCP server (HTTP)")
    serve.add_argument("--host")
    serve.add_argument("--port", type=int)
    serve.add_argument("--public-url", help="Public tunnel URL, used in links sent to agents")
    serve.add_argument("--no-browser", action="store_true", help="Don't open the portal")
    serve.set_defaults(func=cmd_serve)

    login = sub.add_parser("login", help="Sign in to a university in a browser window")
    login.add_argument("university_id")
    login.set_defaults(func=cmd_login)

    refresh = sub.add_parser("refresh", help="Try a silent (headless) session refresh")
    refresh.add_argument("university_id")
    refresh.set_defaults(func=cmd_refresh)

    sub.add_parser("status", help="Show connection status").set_defaults(func=cmd_status)
    sub.add_parser("universities", help="List supported universities").set_defaults(
        func=cmd_universities
    )

    key = sub.add_parser("key", help="Print the access key")
    key.add_argument("--rotate", action="store_true", help="Generate a new key")
    key.set_defaults(func=cmd_key)

    sub.add_parser("stdio", help="Run the MCP server over stdio (Cursor, Claude Desktop)").set_defaults(
        func=cmd_stdio
    )

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    args.func(args)


if __name__ == "__main__":
    main()
