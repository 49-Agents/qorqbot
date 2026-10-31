#!/usr/bin/env python3
"""biz-dashboard - one page over all business repos.

  python server.py [--root ~/projects/example-businesses] [--port 1049]

Reads every subdirectory of the root that has a business.yaml. Data comes
from the four CLIs (--json) and the files themselves. Chat = spawn
`bizrun run --stream-json -m "..."` and forward its events as SSE. Stdlib only.
"""

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.expanduser(os.environ.get("BIZ_ROOT", "~/projects/example-businesses"))
CLI_ENV = {"bizlog": "BIZLOG_CMD", "biztask": "BIZTASK_CMD", "bizledger": "BIZLEDGER_CMD",
           "bizmetrics": "BIZMETRICS_CMD", "bizrun": "BIZRUN_CMD"}
ICONS = ("circle", "square", "triangle")
COLORS = ("red", "amber", "green", "teal", "blue", "plum", "ink")
DEPARTMENTS = ("marketing", "accounting", "data-science", "code", "general")
SESSIONS = {}
LOCK = threading.Lock()


# ------------------------------------------------------------------ utils

def cli(name):
    raw = os.environ.get(CLI_ENV[name], "")
    if raw:
        return shlex.split(raw, posix=os.name != "nt")
    exe = shutil.which(name)
    if exe:
        return [exe]
    raise RuntimeError(f"{name} not on PATH (set {CLI_ENV[name]})")


def owner_actor(biz):
    """The dashboard acts as the business owner from business.yaml (human:<owner>)."""
    owner = yaml_scalars(read(os.path.join(biz, "business.yaml"))).get("owner") or "dashboard"
    return os.environ.get("BIZ_ACTOR") or f"human:{owner}"


def run_json(biz, name, *args):
    try:
        r = subprocess.run(cli(name) + ["--json"] + list(args), cwd=biz, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=60,
                           env=dict(os.environ, BIZ_ACTOR=owner_actor(biz)))
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        return {"error": str(exc)}
    if r.returncode not in (0, 1) or not r.stdout.strip():
        return {"error": (r.stderr or r.stdout).strip()[:500]}
    try:
        return json.loads(r.stdout)
    except json.JSONDecodeError:
        return {"error": r.stdout[:500]}


def read(path, default=""):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except OSError:
        return default


def yaml_scalars(text):
    """Top-level `key: value` pairs only; enough for business.yaml and request headers."""
    out = {}
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*?)\s*(?:#.*)?$", line)
        if m and not line.startswith(" "):
            v = m.group(2).strip().strip('"\'')
            out[m.group(1)] = None if v in ("", "null", "~") else v
    return out


def agent_meta(biz, name):
    """icon/color/department for one agent, defaults applied and invalid values replaced."""
    y = yaml_scalars(read(os.path.join(biz, "agents", name, "agent.yaml")))
    icon, color, dept = y.get("icon"), y.get("color"), y.get("department")
    return {"icon": icon if icon in ICONS else "circle",
            "color": color if color in COLORS else "ink",
            "department": dept if dept in DEPARTMENTS else "general"}


def businesses():
    out = []
    for p in sorted(glob.glob(os.path.join(ROOT, "*", "business.yaml"))):
        biz = os.path.dirname(p)
        y = yaml_scalars(read(p))
        slug = os.path.basename(biz)
        agents = sorted(d for d in os.listdir(os.path.join(biz, "agents"))
                        if os.path.isdir(os.path.join(biz, "agents", d)) and not d.startswith("_"))
        out.append({"slug": slug, "path": biz, "name": y.get("name") or slug, "one_liner": y.get("one_liner"),
                    "stage": y.get("stage"), "website": y.get("website"), "id": y.get("id"), "agents": agents,
                    "agent_meta": {a: agent_meta(biz, a) for a in agents}})
    return out


def biz_path(slug):
    if not re.match(r"^[A-Za-z0-9._-]+$", slug):
        return None
    p = os.path.join(ROOT, slug)
    return p if os.path.exists(os.path.join(p, "business.yaml")) else None


def git_recent(biz, n=15):
    try:
        r = subprocess.run(["git", "log", f"-{n}", "--format=%h%x09%ad%x09%an%x09%s", "--date=short"], cwd=biz,
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return []
    rows = []
    for line in r.stdout.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            rows.append({"sha": parts[0], "date": parts[1], "author": parts[2], "subject": parts[3]})
    return rows


def requests_list(biz):
    executed = set()
    log = run_json(biz, "bizlog", "log", "-n", "0")
    if isinstance(log, list):
        for e in log:
            r = (e.get("refs") or {}).get("request")
            if r and str(e.get("actor", "")).startswith("system:"):
                executed.add(r)
    out = []
    for p in sorted(glob.glob(os.path.join(biz, "requests", "*", "*.yaml")), reverse=True):
        y = yaml_scalars(read(p))
        rid = y.get("id") or os.path.splitext(os.path.basename(p))[0]
        out.append({"id": rid, "tool": y.get("tool"), "agent": y.get("agent"), "not_before": y.get("not_before"),
                    "executed": rid in executed, "path": os.path.relpath(p, biz).replace("\\", "/")})
    return out


def agent_info(biz, name):
    adir = os.path.join(biz, "agents", name)
    sessions = sorted(glob.glob(os.path.join(adir, "sessions", "*.jsonl")), reverse=True)
    notes = sorted(glob.glob(os.path.join(adir, "notes", "*.md")), reverse=True)
    meta = agent_meta(biz, name)
    return {"name": name, "agent_yaml": read(os.path.join(adir, "agent.yaml")), "plan": read(os.path.join(adir, "PLAN.md")),
            "sessions": [os.path.basename(s) for s in sessions[:30]],
            "notes": [os.path.basename(n) for n in notes[:30]],
            "learnings": read(notes[0]) if notes and "session-learnings" in notes[0] else "",
            "icon": meta["icon"], "color": meta["color"], "department": meta["department"]}


def session_events(biz, agent, fname):
    if not re.match(r"^[\w.-]+\.jsonl$", fname) or not re.match(r"^[\w-]+$", agent):
        return []
    p = os.path.join(biz, "agents", agent, "sessions", fname)
    out = []
    for line in read(p).splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


# --------------------------------------------------------------- sessions

class Live:
    def __init__(self, biz, agent, message):
        self.id = uuid.uuid4().hex[:12]
        self.biz, self.agent, self.message = biz, agent, message
        self.lines, self.done, self.error = [], False, None
        self.started = time.time()
        cmd = cli("bizrun") + ["run", "--agent", agent, "--stream-json", "-m", message]
        try:
            self.proc = subprocess.Popen(cmd, cwd=biz, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                         encoding="utf-8", errors="replace", bufsize=1,
                                         env=dict(os.environ, PYTHONIOENCODING="utf-8"))
        except (OSError, RuntimeError) as exc:
            self.proc, self.done, self.error = None, True, str(exc)
            self.lines.append({"kind": "error", "error": str(exc)})
            return
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self.lines.append(json.loads(line))
            except json.JSONDecodeError:
                self.lines.append({"kind": "raw", "text": line})
        err = self.proc.stderr.read()
        self.proc.wait()
        if self.proc.returncode != 0:
            self.error = (err or "").strip()[-800:] or f"exit {self.proc.returncode}"
            self.lines.append({"kind": "error", "error": self.error})
        self.done = True


def live_list(biz=None):
    with LOCK:
        return [{"id": s.id, "biz": os.path.basename(s.biz), "agent": s.agent, "message": s.message,
                 "done": s.done, "error": s.error, "started": s.started, "events": len(s.lines)}
                for s in SESSIONS.values() if biz is None or s.biz == biz]


# ---------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if os.environ.get("BIZ_DASH_LOG"):
            super().log_message(fmt, *args)

    def send_json(self, data, code=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, ctype):
        try:
            with open(path, "rb") as fh:
                body = fh.read()
        except OSError:
            return self.send_json({"error": "not found"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        parts = [p for p in u.path.split("/") if p]
        if not parts:
            return self.send_file(os.path.join(HERE, "index.html"), "text/html; charset=utf-8")
        if parts[0] != "api":
            return self.send_json({"error": "not found"}, 404)
        try:
            return self.api_get(parts[1:], q)
        except Exception as exc:  # noqa: BLE001 - surface to the page
            return self.send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)

    def api_get(self, parts, q):
        if parts == ["biz"]:
            return self.send_json(businesses())
        if parts[:1] == ["sessions"] and len(parts) == 1:
            return self.send_json(live_list())
        if parts[:1] == ["session"] and len(parts) == 3 and parts[2] == "events":
            return self.stream_session(parts[1])
        if parts[:1] == ["biz"] and len(parts) >= 3:
            biz = biz_path(parts[1])
            if not biz:
                return self.send_json({"error": "no such business"}, 404)
            what = parts[2]
            if what == "log":
                return self.send_json(run_json(biz, "bizlog", "log", "-n", q.get("n", "40")))
            if what == "tasks":
                data = run_json(biz, "biztask", "ls")
                if q.get("closed"):
                    data = run_json(biz, "biztask", "ls", "--closed")
                return self.send_json(data)
            if what == "agents":
                names = [d for d in sorted(os.listdir(os.path.join(biz, "agents")))
                         if os.path.isdir(os.path.join(biz, "agents", d)) and not d.startswith("_")]
                return self.send_json([agent_info(biz, n) for n in names])
            if what == "session" and len(parts) == 5:
                return self.send_json(session_events(biz, parts[3], parts[4]))
            if what == "analytics":
                weeks = q.get("weeks", "8")
                return self.send_json({
                    "report": run_json(biz, "bizmetrics", "report", "--weeks", weeks),
                    "channels": run_json(biz, "bizmetrics", "channels", "--weeks", "4"),
                    "posts": run_json(biz, "bizmetrics", "posts", "--weeks", "4"),
                    "research": run_json(biz, "bizmetrics", "research", "ls"),
                    "learnings": read(os.path.join(biz, "analytics", "LEARNINGS.md")),
                    "research_files": {os.path.basename(p): read(p) for p in
                                       sorted(glob.glob(os.path.join(biz, "analytics", "research", "*.md")), reverse=True)[:10]},
                })
            if what == "finance":
                return self.send_json({"report": run_json(biz, "bizledger", "report"),
                                       "recent": run_json(biz, "bizledger", "ls", "-n", "25"),
                                       "recurring": run_json(biz, "bizledger", "recurring", "ls")})
            if what == "requests":
                return self.send_json(requests_list(biz))
            if what == "git":
                return self.send_json(git_recent(biz))
            if what == "context":
                files = {os.path.basename(p): read(p) for p in sorted(glob.glob(os.path.join(biz, "context", "*.md")))}
                files["business.yaml"] = read(os.path.join(biz, "business.yaml"))
                return self.send_json(files)
            if what == "live":
                return self.send_json(live_list(biz))
        return self.send_json({"error": "not found"}, 404)

    def do_POST(self):
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        n = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self.send_json({"error": "bad json"}, 400)
        if parts[:1] == ["api"] and len(parts) >= 4 and parts[1] == "biz":
            biz = biz_path(parts[2])
            if not biz:
                return self.send_json({"error": "no such business"}, 404)
            if parts[3] == "chat":
                agent = str(body.get("agent", ""))
                msg = str(body.get("message", "")).strip()
                if not re.match(r"^[\w-]+$", agent) or not os.path.isdir(os.path.join(biz, "agents", agent)):
                    return self.send_json({"error": "no such agent"}, 400)
                if not msg:
                    return self.send_json({"error": "empty message"}, 400)
                if any(s.biz == biz and s.agent == agent and not s.done for s in SESSIONS.values()):
                    return self.send_json({"error": f"{agent} is already running a session"}, 409)
                s = Live(biz, agent, msg)
                with LOCK:
                    SESSIONS[s.id] = s
                return self.send_json({"session": s.id})
            if parts[3] == "gateway":
                try:
                    r = subprocess.run(cli("bizrun") + ["mock-gateway"], cwd=biz, capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=120)
                    return self.send_json({"out": (r.stdout + r.stderr).strip(), "rc": r.returncode})
                except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                    return self.send_json({"error": str(exc)}, 500)
            if parts[3] == "task":
                title = str(body.get("title", "")).strip()
                owner = str(body.get("owner", "")).strip()
                if not title:
                    return self.send_json({"error": "empty title"}, 400)
                args = ["add", title] + (["--owner", owner] if owner else [])
                for t in body.get("tags") or []:
                    args += ["-t", str(t)]
                return self.send_json(run_json(biz, "biztask", *args))
            if parts[3] == "agent" and len(parts) == 6 and parts[5] == "meta":
                name = parts[4]
                if (not re.match(r"^[\w-]+$", name) or name.startswith("_")
                        or not os.path.isdir(os.path.join(biz, "agents", name))):
                    return self.send_json({"error": "no such agent"}, 400)
                fields = (("icon", ICONS), ("color", COLORS), ("department", DEPARTMENTS))
                updates = {}
                for key, allowed in fields:
                    if key in body:
                        val = str(body[key])
                        if val not in allowed:
                            return self.send_json({"error": f"bad {key}: {val}"}, 400)
                        updates[key] = val
                if not updates:
                    return self.send_json({"error": "no fields given"}, 400)
                ypath = os.path.join(biz, "agents", name, "agent.yaml")
                lines = read(ypath).splitlines()
                for key, val in updates.items():
                    pat = re.compile(rf"^{key}:.*$")
                    for i, line in enumerate(lines):
                        if pat.match(line):
                            lines[i] = f"{key}: {val}"
                            break
                    else:
                        lines.append(f"{key}: {val}")
                with open(ypath, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
                warn = None
                try:
                    subprocess.run(["git", "add", os.path.join("agents", name, "agent.yaml")], cwd=biz,
                                   capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15)
                    msg = "agent(%s): set %s" % (name, ", ".join(f"{k}={v}" for k, v in updates.items()))
                    r = subprocess.run(["git", "commit", "-q", "-m", msg], cwd=biz, capture_output=True, text=True,
                                       encoding="utf-8", errors="replace", timeout=15)
                    if r.returncode != 0:
                        warn = (r.stderr or r.stdout).strip()[-500:]
                except (OSError, subprocess.TimeoutExpired) as exc:
                    warn = str(exc)
                meta = agent_meta(biz, name)
                resp = {"ok": True, "icon": meta["icon"], "color": meta["color"], "department": meta["department"]}
                if warn:
                    resp["warn"] = warn
                return self.send_json(resp)
        return self.send_json({"error": "not found"}, 404)

    def stream_session(self, sid):
        with LOCK:
            s = SESSIONS.get(sid)
        if not s:
            return self.send_json({"error": "no such session"}, 404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        i = 0
        try:
            while True:
                while i < len(s.lines):
                    self.wfile.write(f"data: {json.dumps(s.lines[i], ensure_ascii=False)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    i += 1
                if s.done:
                    self.wfile.write(b"event: done\ndata: {}\n\n")
                    self.wfile.flush()
                    return
                time.sleep(0.4)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return


def main(argv=None):
    global ROOT
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--root", default=ROOT, help="folder of business repos")
    p.add_argument("--port", type=int, default=int(os.environ.get("BIZ_DASH_PORT", "1049")))
    p.add_argument("--host", default="127.0.0.1")
    args = p.parse_args(argv)
    ROOT = os.path.abspath(os.path.expanduser(args.root))
    if not os.path.isdir(ROOT):
        print(f"no such root: {ROOT}", file=sys.stderr)
        return 2
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    print(f"biz-dashboard on http://{args.host}:{args.port}  root {ROOT}  ({len(businesses())} businesses)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
