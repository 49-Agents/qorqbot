"""Keyless local helper for the installation's existing trusted owner HTTP API."""
import argparse
import json
from pathlib import Path
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


def invoke(url, business, operation, arguments):
    base = url.rstrip('/')
    parsed = urlsplit(base)
    if parsed.scheme not in {'http', 'https'} or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
        raise ValueError('URL must be a service origin, such as http://127.0.0.1:1049')
    path = '/api/biz/' + quote(business, safe='') if business else '/api/root'
    request = Request(base + path + '/operations/' + operation, data=json.dumps(arguments).encode(),
        headers={'Content-Type': 'application/json', 'Origin': base}, method='POST')
    with urlopen(request, timeout=30) as result:
        return json.load(result)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:1049')
    scope = parser.add_mutually_exclusive_group(required=True)
    scope.add_argument('--business', help='Business slug')
    scope.add_argument('--global', dest='global_scope', action='store_true')
    commands = parser.add_subparsers(dest='command', required=True)
    recipients = commands.add_parser('recipients')
    recipients.add_argument('--all-businesses', action='store_true')
    recipients.add_argument('--role-id')
    send = commands.add_parser('send')
    send.add_argument('--to', dest='to_agent_id')
    send.add_argument('--reply-to')
    send.add_argument('--message-file', required=True, help='UTF-8 file, or - for stdin')
    send.add_argument('--idempotency-key', required=True, help='Reuse this key for retries of the same message')
    history = commands.add_parser('history')
    history.add_argument('--thread-id')
    history.add_argument('--counterpart-id', help='Current conversation counterpart agent ID')
    history.add_argument('--after', type=int, default=0)
    history.add_argument('--limit', type=int, default=20)
    args = parser.parse_args(argv)
    body = {}
    if args.command == 'send':
        if not args.to_agent_id and not args.reply_to:
            parser.error('send requires --to or --reply-to')
        body = {'message': sys.stdin.read(16001) if args.message_file == '-' else Path(args.message_file).read_text(encoding='utf-8'),
                'idempotency_key': args.idempotency_key}
        if args.to_agent_id: body['to_agent_id'] = args.to_agent_id
        if args.reply_to: body['reply_to'] = args.reply_to
        operation = 'agents_message_send'
    elif args.command == 'history':
        body = {'after': args.after, 'limit': args.limit}
        if args.thread_id: body['thread_id'] = args.thread_id
        if args.counterpart_id: body['counterpart_id'] = args.counterpart_id
        operation = 'agents_messages_list'
    else:
        body = {'all_businesses': args.all_businesses}
        if args.role_id: body['role_id'] = args.role_id
        operation = 'agents_message_recipients'
    try:
        result = invoke(args.url, args.business, operation, body)
    except HTTPError as error:
        try: result = json.load(error)
        except ValueError: result = {'status': 'error', 'error': {'message': 'HTTP request failed', 'status': error.code}}
    except (URLError, ValueError, TimeoutError, OSError):
        print(json.dumps({'status': 'error', 'error': {'message': 'Service request failed; use the same idempotency key when retrying'}}))
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get('status') == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
