#!/usr/bin/env python3
"""One-time local credential reveal and public application verification."""

from __future__ import annotations

import argparse
import hashlib
import html
import http.cookiejar
import http.server
import json
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from html.parser import HTMLParser


class CredentialError(RuntimeError):
    pass


def read_lines(expected: int) -> list[str]:
    raw = sys.stdin.buffer.read()
    if b"\0" in raw or b"\r" in raw:
        raise CredentialError("credential input contains an unsupported byte")
    values = raw.decode("utf-8").splitlines()
    if len(values) != expected or any(not value for value in values):
        raise CredentialError(
            f"credential input must contain exactly {expected} non-empty lines"
        )
    return values


class TokenParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.token = ""

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag != "input":
            return
        values = dict(attrs)
        if values.get("name") == "_token" and values.get("value"):
            self.token = values["value"] or ""


def browser() -> urllib.request.OpenerDirector:
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def request(
    opener: urllib.request.OpenerDirector, url: str, data: bytes | None = None
) -> tuple[str, bytes]:
    headers = {"User-Agent": "remote-dev-credentials/1.0"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method="POST" if data is not None else "GET",
    )
    try:
        with opener.open(req, timeout=20) as response:
            return response.geturl(), response.read(2_000_000)
    except urllib.error.HTTPError as error:
        body = error.read(64_000).decode("utf-8", "replace")
        raise CredentialError(f"HTTP {error.code} from {url}: {body[:200]}") from error
    except urllib.error.URLError as error:
        raise CredentialError(f"cannot reach {url}: {error.reason}") from error


def form_token(body: bytes) -> str:
    parser = TokenParser()
    parser.feed(body.decode("utf-8", "replace"))
    if not parser.token:
        raise CredentialError("application form has no CSRF token")
    return parser.token


def safe_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    # HTTPS for a published service, or the Mac end of an SSH forward.
    forward = parsed.scheme == "http" and parsed.hostname == "127.0.0.1" and parsed.port is not None
    if (
        not (parsed.scheme == "https" or forward)
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise CredentialError("application URL must be an https origin or http://127.0.0.1:<port>")
    return value.rstrip("/")


def coder_login(args: argparse.Namespace) -> None:
    account, password = read_lines(2)
    origin = safe_origin(args.url)
    payload = json.dumps({"email": account, "password": password}).encode()
    req = urllib.request.Request(
        f"{origin}/api/v2/users/login",
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "remote-dev-credentials/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as response:
            result = json.load(response)
    except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as error:
        raise CredentialError("Coder login failed") from error
    if not isinstance(result, dict) or not result.get("session_token"):
        raise CredentialError("Coder login returned no session token")


def coolify_login(args: argparse.Namespace) -> None:
    account, password = read_lines(2)
    origin = safe_origin(args.url)
    opener = browser()
    page_url, page = request(opener, f"{origin}/login")
    if urllib.parse.urlsplit(page_url).path == "/register":
        raise CredentialError("Coolify has no owner user")
    payload = urllib.parse.urlencode(
        {"_token": form_token(page), "email": account, "password": password}
    ).encode()
    final_url, _ = request(opener, f"{origin}/login", payload)
    if urllib.parse.urlsplit(final_url).path in ("/login", "/register"):
        raise CredentialError("Coolify login was not authenticated")


def coolify_status(args: argparse.Namespace) -> None:
    origin = safe_origin(args.url)
    final_url, _ = request(browser(), f"{origin}/login")
    print(
        "absent"
        if urllib.parse.urlsplit(final_url).path == "/register"
        else "present"
    )


def coolify_register(args: argparse.Namespace) -> None:
    name, account, password = read_lines(3)
    origin = safe_origin(args.url)
    opener = browser()
    page_url, page = request(opener, f"{origin}/register")
    if urllib.parse.urlsplit(page_url).path != "/register":
        raise CredentialError("Coolify registration is not available")
    payload = urllib.parse.urlencode(
        {
            "_token": form_token(page),
            "name": name,
            "email": account,
            "password": password,
            "password_confirmation": password,
        }
    ).encode()
    final_url, _ = request(opener, f"{origin}/register", payload)
    if urllib.parse.urlsplit(final_url).path in ("/login", "/register"):
        raise CredentialError(
            "Coolify registration did not create an authenticated owner"
        )


def credential_receipt(
    profile: str, service: str, account: str, password: str, url: str
) -> str:
    payload = "\0".join(
        ("remote-dev-credential-v1", profile, service, url, account, password)
    ).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def reveal_page(
    service: str,
    account: str,
    password: str,
    receipt: str,
    url: str,
    clear_seconds: int,
) -> bytes:
    service_text = html.escape(service)
    account_text = html.escape(account, quote=True)
    password_text = html.escape(password, quote=True)
    receipt_text = html.escape(receipt, quote=True)
    url_text = html.escape(url, quote=True)
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>Remote Dev credential</title>
<style>
body {{ font: 16px/1.5 system-ui, sans-serif; margin: 0; background: #10131a; color: #f4f6fb; }}
main {{ max-width: 680px; margin: 8vh auto; padding: 32px; background: #191e29; border-radius: 16px; }}
label {{ display: block; margin: 18px 0 6px; color: #abb6ca; }}
input {{ box-sizing: border-box; width: 100%; padding: 12px; font: 15px ui-monospace, monospace; }}
.notice {{ color: #f4cd7c; }}
.muted {{ color: #abb6ca; }}
</style>
</head>
<body><main>
<h1>{service_text}</h1>
<p><a href="{url_text}" rel="noreferrer noopener">{url_text}</a></p>
<p class="notice">Save this credential in your password manager. The page is served once from 127.0.0.1 and will clear itself in <span id="count">{clear_seconds}</span> seconds.</p>
<label for="account">Account</label>
<input id="account" readonly value="{account_text}">
<label for="password">Password</label>
<input id="password" readonly value="{password_text}">
<label for="receipt">Finalization receipt</label>
<input id="receipt" readonly value="{receipt_text}">
<p class="muted">Copying is manual: clipboard managers and sync services may retain copied values. The server bootstrap copy remains until you run the explicit finalize command.</p>
</main>
<script>
history.replaceState(null, "", "/");
let remaining = {clear_seconds};
const timer = setInterval(() => {{
  remaining -= 1;
  document.getElementById("count").textContent = String(Math.max(remaining, 0));
  if (remaining <= 0) {{
    clearInterval(timer);
    document.getElementById("account").value = "";
    document.getElementById("password").value = "";
    document.getElementById("receipt").value = "";
    document.querySelector("main").innerHTML = "<h1>Credential cleared</h1><p>Close this tab.</p>";
  }}
}}, 1000);
</script></body></html>""".encode()


def reveal(args: argparse.Namespace) -> None:
    account, password = read_lines(2)
    origin = safe_origin(args.url)
    receipt = credential_receipt(
        args.profile, args.service, account, password, origin
    )
    token = secrets.token_urlsafe(32)
    page = reveal_page(
        args.service, account, password, receipt, origin, args.clear_seconds
    )
    credential_json = json.dumps(
        {
            "version": 1,
            "service": args.service,
            "url": origin,
            "account": account,
            "password": password,
            "receipt": receipt,
            "clear_after_seconds": args.clear_seconds,
        }
    ).encode()
    served = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            host_header = self.headers.get("Host", "")
            if (
                self.path != f"/{token}"
                or served.is_set()
                or not host_header.startswith("127.0.0.1:")
            ):
                self.send_response(410)
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                return
            served.set()
            wants_json = "application/json" in self.headers.get("Accept", "")
            response = credential_json if wants_json else page
            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/json" if wants_json else "text/html; charset=utf-8",
            )
            self.send_header("Content-Length", str(len(response)))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")
            if not wants_json:
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; form-action 'none'; frame-ancestors 'none'",
                )
            self.end_headers()
            self.wfile.write(response)
            threading.Thread(target=self.server.shutdown, daemon=True).start()

        def log_message(self, _format: str, *_arguments: object) -> None:
            return

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    reveal_url = f"http://127.0.0.1:{port}/{token}"
    timer = threading.Timer(args.open_timeout, server.shutdown)
    timer.daemon = True
    timer.start()
    if args.consumer == "app":
        print(
            json.dumps(
                {
                    "version": 1,
                    "handoff_url": reveal_url,
                    "accept": "application/json",
                    "expires_in_seconds": args.open_timeout,
                    "single_use": True,
                    "receipt": receipt,
                }
            ),
            flush=True,
        )
    else:
        print("OPEN     one-time credential page in the local browser", file=sys.stderr)
        if not webbrowser.open(reveal_url, new=2):
            print(f"OPEN     {reveal_url}", file=sys.stderr)
    server.serve_forever()
    timer.cancel()
    server.server_close()
    if not served.is_set():
        raise CredentialError("one-time credential page was not opened before timeout")


def print_receipt(args: argparse.Namespace) -> None:
    account, password = read_lines(2)
    print(
        credential_receipt(
            args.profile,
            args.service,
            account,
            password,
            safe_origin(args.url),
        )
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command in ("coder-login", "coolify-login", "coolify-status", "coolify-register"):
        sub = subparsers.add_parser(command)
        sub.add_argument("--url", required=True)
    reveal_parser = subparsers.add_parser("reveal")
    reveal_parser.add_argument("--profile", required=True)
    reveal_parser.add_argument("--service", required=True)
    reveal_parser.add_argument("--url", required=True)
    reveal_parser.add_argument("--consumer", choices=("browser", "app"), default="browser")
    reveal_parser.add_argument("--open-timeout", type=int, default=120)
    reveal_parser.add_argument("--clear-seconds", type=int, default=300)
    receipt_parser = subparsers.add_parser("receipt")
    receipt_parser.add_argument("--profile", required=True)
    receipt_parser.add_argument("--service", required=True)
    receipt_parser.add_argument("--url", required=True)
    return parser


def main() -> None:
    args = build_parser().parse_args()
    actions = {
        "coder-login": coder_login,
        "coolify-login": coolify_login,
        "coolify-status": coolify_status,
        "coolify-register": coolify_register,
        "reveal": reveal,
        "receipt": print_receipt,
    }
    try:
        actions[args.command](args)
    except CredentialError as error:
        print(f"ERROR    {error}", file=sys.stderr)
        raise SystemExit(1) from error


if __name__ == "__main__":
    main()
