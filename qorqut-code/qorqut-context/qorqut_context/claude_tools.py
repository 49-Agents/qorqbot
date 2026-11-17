"""Stdio MCP relay that offers one event's Context tools to the Claude CLI.

The Claude runner starts the CLI with a private MCP configuration naming this file. The CLI starts it as a stdio MCP server
with two arguments: the runner's private Unix socket and the file of tool definitions. It answers ``initialize`` and
``tools/list`` from the definitions and forwards every ``tools/call`` to the runner over the socket. The runner applies
every limit and runs the tool. This process holds no Context data and no credentials. It uses only the standard library,
so it runs with an isolated interpreter.
"""
import json
import socket
import sys

PROTOCOL = "2025-06-18"
REPLY_BYTES = 8 * 1024 * 1024


def call(path, request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(path)
        connection.sendall(json.dumps(request).encode() + b"\n")
        data = bytearray()
        while not data.endswith(b"\n"):
            chunk = connection.recv(65536)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > REPLY_BYTES:
                raise ValueError("Relay reply exceeds its byte limit")
    return json.loads(data)


def main(argv=None):
    path, definitions_file = (argv or sys.argv)[1:3]
    with open(definitions_file, encoding="utf-8") as handle:
        definitions = json.load(handle)
    tools = [{"name": item["name"], "description": item["description"], "inputSchema": item["inputSchema"]}
             for item in definitions]
    names = {item["name"] for item in tools}

    def send(value):
        sys.stdout.write(json.dumps(value, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        identity, method = message.get("id"), message.get("method")
        if identity is None:
            continue                                   # notifications need no answer
        params = message.get("params") or {}
        if method == "initialize":
            result = {"protocolVersion": params.get("protocolVersion") or PROTOCOL, "capabilities": {"tools": {}},
                      "serverInfo": {"name": "qorqut-context", "version": "1"}}
        elif method == "tools/list":
            result = {"tools": tools}
        elif method == "ping":
            result = {}
        elif method == "tools/call" and params.get("name") in names:
            try:
                reply = call(path, {"name": params["name"], "arguments": params.get("arguments") or {}})
                result = {"content": [{"type": "text", "text": reply["text"]}], "isError": not reply["ok"]}
            except (OSError, ValueError, KeyError):
                result = {"content": [{"type": "text", "text": "The Context tool relay failed"}], "isError": True}
        else:
            send({"jsonrpc": "2.0", "id": identity, "error": {"code": -32601, "message": "Unsupported request"}})
            continue
        send({"jsonrpc": "2.0", "id": identity, "result": result})


if __name__ == "__main__":
    main()
