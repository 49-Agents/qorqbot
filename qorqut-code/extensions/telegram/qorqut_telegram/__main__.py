"""Operator commands: validate the private configuration, enroll a producer, post a notification, read owner status.

    python -m qorqut_telegram validate --config PATH
    python -m qorqut_telegram example
    python -m qorqut_telegram enroll --producer-id ID --secret-file PATH
    python -m qorqut_telegram notify --socket PATH --secret-file PATH --payload-file FILE|-
    python -m qorqut_telegram notify --socket PATH --secret-file PATH --event-key K --title T --reason R [--text-file -]
    python -m qorqut_telegram status --url http://127.0.0.1:1071
    python -m qorqut_telegram resolve --url URL --outbox-id ID --expected-revision N --action retry|mark_delivered [--provider-message-id N]
    python -m qorqut_telegram composer-check --client codex|claude --screen FILE [--cursor X,Y] [--pasted-file FILE]

Nothing here prints a token, a producer secret or a chat/agent identifier. ``validate`` reads the token file only to check its
shape and never echoes it. No command writes the live configuration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import secrets
import sys

from bazarlyk.private_files import write_private_file
from qorqut_operations.contracts import DomainError, new_id
from .client import GatewayError, notify
from .config import load_config, read_token, redacted_example


def _print(value):
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_validate(args):
    try:
        config = load_config(args.config, require_complete=True)
        problems = []
        if config.enabled:
            try:
                read_token(config)
            except DomainError as exc:
                problems.append(exc.message)
        result = {"valid": not problems, "problems": problems, "configuration": config.redacted()}
    except DomainError as exc:
        result = {"valid": False, "problems": [exc.message], "configuration": None}
    _print(result)
    return 0 if result["valid"] else 1


def cmd_example(args):
    _print(redacted_example())
    return 0


def cmd_enroll(args):
    path = Path(args.secret_file)
    if not path.is_absolute():
        print("--secret-file must be an absolute path", file=sys.stderr)
        return 2
    secret = secrets.token_urlsafe(32)
    try:
        write_private_file(path.parent, path.name, (secret + "\n").encode())
    except (DomainError, OSError, FileExistsError) as exc:
        print(f"Cannot create the secret file: {getattr(exc, 'message', exc.__class__.__name__)}", file=sys.stderr)
        return 1
    _print({"producer_id": args.producer_id, "secret_sha256": hashlib.sha256(secret.encode()).hexdigest(),
            "note": "Add this object to program_gateway.producers in the private configuration; the secret stays in its file."})
    return 0


PAYLOAD_FIELDS = {"event_key", "title", "text", "reason", "link", "state"}


def _payload(args):
    """The notification fields: one JSON object from a file or stdin, or the individual flags (text from a file or stdin)."""
    if args.payload_file is None:
        if not (args.event_key and args.title and args.reason):
            raise ValueError("Give --payload-file, or --event-key, --title and --reason")
        text = sys.stdin.read(4000) if args.text_file == "-" else Path(args.text_file).read_text(encoding="utf-8")
        return {"event_key": args.event_key, "title": args.title, "text": text.strip(), "reason": args.reason,
                "link": args.link, "state": args.state}
    if any((args.event_key, args.title, args.reason, args.link)) or args.state != "firing" or args.text_file != "-":
        raise ValueError("--payload-file carries every field; do not combine it with the individual flags")
    raw = sys.stdin.read(65536) if args.payload_file == "-" else Path(args.payload_file).read_text(encoding="utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict) or set(payload) - PAYLOAD_FIELDS:
        raise ValueError("The payload must be a JSON object with event_key, title, text, reason and optional link and state")
    return payload


def cmd_notify(args):
    try:
        fields = _payload(args)
    except (ValueError, OSError) as exc:
        print(f"Invalid notification payload: {exc.__class__.__name__ if isinstance(exc, OSError) else exc}", file=sys.stderr)
        return 2
    try:
        result = notify(args.socket, args.secret_file, event_key=fields.get("event_key"), title=fields.get("title"),
                        text=fields["text"].strip() if isinstance(fields.get("text"), str) else fields.get("text"), reason=fields.get("reason"), link=fields.get("link"),
                        state=fields.get("state", "firing"))
    except GatewayError as exc:
        _print({"ok": False, "error": {"code": exc.code, "message": str(exc), "retryable": exc.retryable}})
        return 1
    except (DomainError, OSError) as exc:
        _print({"ok": False, "error": {"code": "invalid_configuration", "message": getattr(exc, "message", "Cannot read the secret file")}})
        return 2
    _print({"ok": True, "result": result})
    return 0


def _owner(args, operation, body):
    from urllib.error import HTTPError, URLError
    from urllib.parse import urlsplit
    from urllib.request import Request, urlopen
    try:
        base = args.url.rstrip('/')
        parsed = urlsplit(base)
        if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
            raise ValueError('URL must be a service origin')
        request = Request(base + '/api/root/extensions/telegram/' + operation, data=json.dumps(body).encode(),
            headers={'Content-Type': 'application/json', 'Origin': base}, method='POST')
        with urlopen(request, timeout=30) as response:
            result = json.load(response)
    except HTTPError as error:
        try:
            result = json.load(error)
        except ValueError:
            result = {"status": "error", "error": {"message": "HTTP request failed", "status": error.code}}
    except (URLError, ValueError, TimeoutError, OSError):
        _print({"status": "error", "error": {"message": "Service request failed"}})
        return 1
    _print(result)
    return 0 if result.get("status") == "ok" else 1


def cmd_status(args):
    return _owner(args, "telegram_status", {})


def cmd_resolve(args):
    body = {"outbox_id": args.outbox_id, "action": args.action, "expected_revision": args.expected_revision,
            "idempotency_key": args.idempotency_key or new_id()}
    if args.provider_message_id is not None:
        body["provider_message_id"] = args.provider_message_id
    return _owner(args, "telegram_delivery_resolve", body)


def cmd_composer_check(args):
    """Classify a saved ``tmux capture-pane -p -e`` screen offline. Nothing is typed and no terminal is touched."""
    from bazarlyk.transport import terminal_composer
    try:
        raw = Path(args.screen).read_text(encoding="utf-8", errors="replace")
        cursor = None
        if args.cursor:
            x, y = (int(part) for part in args.cursor.split(","))
            cursor = (x, y, True)
    except (OSError, ValueError):
        print("Cannot read the screen file or the cursor position (use X,Y)", file=sys.stderr)
        return 2
    expect = None
    if args.pasted_file:
        try:
            expect = Path(args.pasted_file).read_text(encoding="utf-8")
        except (OSError, ValueError):
            print("Cannot read the pasted-text file", file=sys.stderr)
            return 2
    reading = terminal_composer.read(args.client, raw, cursor, expect=expect)
    ok = reading.state == "pasted" if expect is not None else reading.ready
    _print({"client": args.client, "state": reading.state, "reason": reading.reason, "ready": ok})
    return 0 if ok else 1


def build_parser():
    parser = argparse.ArgumentParser(prog="python -m qorqut_telegram", description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    validate = commands.add_parser("validate")
    validate.add_argument("--config", required=True)
    validate.set_defaults(handler=cmd_validate)
    commands.add_parser("example").set_defaults(handler=cmd_example)
    enroll = commands.add_parser("enroll")
    enroll.add_argument("--producer-id", required=True)
    enroll.add_argument("--secret-file", required=True)
    enroll.set_defaults(handler=cmd_enroll)
    post = commands.add_parser("notify")
    post.add_argument("--socket", required=True)
    post.add_argument("--secret-file", required=True)
    post.add_argument("--payload-file", help="JSON object with event_key, title, text, reason, optional link and state; - for stdin")
    post.add_argument("--event-key")
    post.add_argument("--title")
    post.add_argument("--text-file", default="-", help="UTF-8 file, or - for stdin")
    post.add_argument("--reason")
    post.add_argument("--link")
    post.add_argument("--state", choices=("firing", "resolved"), default="firing")
    post.set_defaults(handler=cmd_notify)
    status = commands.add_parser("status")
    status.add_argument("--url", default="http://127.0.0.1:1071")
    status.set_defaults(handler=cmd_status)
    resolve = commands.add_parser("resolve")
    resolve.add_argument("--url", default="http://127.0.0.1:1071")
    resolve.add_argument("--outbox-id", required=True, help="the exact outbox_id of the uncertain send or edit from status")
    resolve.add_argument("--action", required=True, choices=("mark_delivered", "retry"))
    resolve.add_argument("--expected-revision", type=int, required=True, help="the receipt_revision from status or delivery_get")
    resolve.add_argument("--provider-message-id", type=int)
    resolve.add_argument("--idempotency-key")
    resolve.set_defaults(handler=cmd_resolve)
    check = commands.add_parser("composer-check")
    check.add_argument("--client", required=True, choices=("codex", "claude"))
    check.add_argument("--screen", required=True)
    check.add_argument("--cursor", help="visible tmux cursor as X,Y (0-based); omit when the client hides its cursor")
    check.add_argument("--pasted-file", help="the exact text that was pasted: classify a capture taken after the paste, "
                       "and report ready only if that text (or the client's placeholder for it) occupies the composer")
    check.set_defaults(handler=cmd_composer_check)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
