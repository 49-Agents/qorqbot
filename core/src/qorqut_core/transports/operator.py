"""Explicit serve/call commands. Online calls use HTTP, never a local writer."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import SecretStr, ValidationError

from ..contracts import OperationResult, canonical
from .configuration import MAX_INPUT_BYTES, InputError, json_object, read_configuration, read_file, read_token

MAX_RESPONSE_BYTES = 32 * 1024 * 1024


class TransportError(Exception):
    pass


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's default errors echo arbitrary argv, including mistaken tokens.
        self.exit(2, "Invalid command arguments; use --help for supported options\n")


def endpoint(base: str, name: str) -> str:
    try:
        parsed = urlsplit(base)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in {"", "/"}
                or any(c.isspace() or ord(c) < 32 for c in base) or "\\" in base
                or parsed.port == 0 or not re.fullmatch(r"[a-z][a-z0-9_]*", name) or len(name) > 200):
            raise ValueError("Invalid endpoint")
        return base.rstrip("/") + "/api/v1/operations/" + name
    except (ValueError, UnicodeError):
        raise InputError("An explicit HTTP/HTTPS origin and operation name are required") from None


def call(url: str, name: str, token: SecretStr, arguments: dict) -> OperationResult:
    target = endpoint(url, name)
    try:
        body = canonical(arguments).encode("utf-8")
        if len(body) > MAX_INPUT_BYTES:
            raise InputError("Arguments exceed 1 MiB")
    except (ValueError, UnicodeError, RecursionError):
        raise InputError("Invalid JSON arguments") from None
    try:
        # No retries, redirects, environment proxies or environment CA overrides.
        # HTTPX's default verification uses the normal trusted certificate bundle.
        with httpx.Client(trust_env=False, follow_redirects=False, verify=True,
                timeout=httpx.Timeout(30.0, connect=5.0, pool=5.0),
                transport=httpx.HTTPTransport(retries=0, verify=True, trust_env=False)) as client:
            with client.stream("POST", target, content=body, headers={
                    "Authorization": "Bearer " + token.get_secret_value(), "Content-Type": "application/json",
                    "Accept": "application/json", "Accept-Encoding": "identity"}) as response:
                if response.is_redirect or response.headers.get("content-encoding", "identity") != "identity":
                    raise TransportError("Unexpected transport response")
                length = response.headers.get("content-length")
                if length is not None and (not length.isdigit() or len(length) > 10 or int(length) > MAX_RESPONSE_BYTES):
                    raise TransportError("Response exceeds the read limit")
                raw = bytearray()
                for chunk in response.iter_raw(chunk_size=65536):
                    if len(raw) + len(chunk) > MAX_RESPONSE_BYTES:
                        raise TransportError("Response exceeds the read limit")
                    raw.extend(chunk)
                result = OperationResult.model_validate(json_object(bytes(raw)))
                if result.ok and response.status_code != 200:
                    raise TransportError("Unexpected transport response")
                return result
    except (httpx.HTTPError, ValidationError, InputError, ValueError, OSError):
        raise TransportError("No valid operation result received; execution may have occurred. Preserve the original idempotency key.") from None


def main(argv=None) -> int:
    parser = Parser(prog="qorqut-core", description="Core HTTP/MCP server and HTTP operator client", allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True, parser_class=Parser)
    server = commands.add_parser("serve", help="Serve an explicitly initialized installation", allow_abbrev=False)
    server.add_argument("--installation", required=True, type=Path)
    server.add_argument("--transport-config", required=True, type=Path)
    client = commands.add_parser("call", help="Call one shared operation over HTTP", allow_abbrev=False)
    client.add_argument("name")
    client.add_argument("--url", required=True)
    client.add_argument("--token-file", required=True, type=Path)
    client.add_argument("--arguments-file", type=Path)
    client.add_argument("--json", action="store_true", dest="as_json")
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            settings, credentials = read_configuration(args.installation, args.transport_config)
            from .server import serve
            try:
                serve(settings, credentials)
            except Exception:
                print("Core server failed; verify explicit configuration and installation availability", file=sys.stderr)
                return 1
            return 0
        endpoint(args.url, args.name)
        token = read_token(args.token_file)
        arguments = json_object(read_file(args.arguments_file)) if args.arguments_file else {}
        result = call(args.url, args.name, token, arguments)
        if args.as_json:
            print(canonical(result))
        elif result.ok:
            suffix = f" ({result.operation_id})" if result.operation_id is not None else ""
            print(f"{result.outcome.capitalize()}{suffix}")
        else:
            print(f"Failed: {result.error.code}")
        return 0 if result.ok else 1
    except InputError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except TransportError:
        print("No valid operation result received; execution may have occurred. Preserve the original idempotency key.", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; an in-flight operation may have run. Preserve the original idempotency key.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
