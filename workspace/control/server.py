#!/usr/bin/env python3
"""The box's control plane: the tree boxtree.py builds, as a page
(index.html) and as JSON (/api/tree), sampled every few seconds, each
sample kept a minute at a time for the peaks (watch.py, /api/peaks); and each
stack's start, stop, pause, resume and switch (POST /api/stacks/NAME/
ACTION), which run `ws ACTION NAME` as a ws job of the stack (ws run), so
the page, `ws jobs` and `ws logs NAME start` all show how it went.

The server is a system unit with a read-only file system, so it doesn't run
them itself: it asks the dev user's systemd (systemd-run --user) for a zsh,
which reads ~/.zshenv (PATH, WS_SLICES, WS_TAILNET, DEV_BIND_HOST) as a
login on the box would, and runs `ws run` there.

  python3 workspace/control/server.py        (box: devbox-control.service)

From the tablet: https://<box>.<tailnet>.ts.net:7070 (dev-server's `tailnet`
port in projects.toml: ws up dev-server serves it). Listens on
DEV_BIND_HOST (127.0.0.1 on the box), port WS_CONTROL_PORT (7070).

Answers only requests addressed to the box by name (the Host header): the
tree holds command lines, and a page elsewhere could otherwise read it by
pointing a name of its own at 127.0.0.1 (DNS rebinding). A start or stop
must also come from this page itself (its Origin, and a JSON body, which
another site's page can't send without asking first): anyone on the tailnet
may use the page, no site they visit may press its buttons.
"""
import json
import os
import re
import shlex
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import boxtree  # noqa: E402
import claude_sessions  # noqa: E402
import claude_usage  # noqa: E402
import manifest  # noqa: E402
import watch  # noqa: E402

PORT = int(os.environ.get("WS_CONTROL_PORT", "7070"))
BIND = os.environ.get("DEV_BIND_HOST") or "127.0.0.1"
INTERVAL = float(os.environ.get("WS_CONTROL_INTERVAL", "3"))
# The page as it was when the server started, not as it is on disk now: a
# git pull that changes both must not show buttons for routes this process
# doesn't have yet. A restart (make update) brings in both.
with open(os.path.join(HERE, "index.html"), "rb") as _f:
    PAGE = _f.read()
MANIFEST = os.environ.get("WS_MANIFEST", os.path.join(HERE, "..", "projects.toml"))
ROOT = os.environ.get("WS_ROOT", os.path.expanduser("~/_"))


def projects():
    """The manifest, read each time: it changes with a git pull."""
    return manifest.load(MANIFEST)


def stacks():
    """Every project in the manifest, by its folder."""
    return {n: os.path.join(ROOT, p.get("dir", n)) for n, p in projects().items()}


STATE = os.environ.get("WS_STATE", os.path.expanduser("~/.local/state/ws"))


def job_metas():
    """ws run's jobs, from their meta.json (ws's state folder)."""
    out = []
    root = os.path.join(STATE, "jobs")
    for stack in os.listdir(root) if os.path.isdir(root) else []:
        sd = os.path.join(root, stack)
        for job in os.listdir(sd) if os.path.isdir(sd) else []:
            try:
                with open(os.path.join(sd, job, "meta.json")) as f:
                    out.append(json.load(f))
            except (OSError, ValueError):
                pass
    return out


HOST = socket.gethostname()


def running_here(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def claude():
    """The plan's windows and each Claude session's spend (claude_usage.py,
    from what ws statusline recorded)."""
    return claude_usage.summary(stacks(), STATE, alive=running_here, host=HOST)


# ------------------------------------------------------------ start, stop

ACTIONS = ("start", "stop", "pause", "resume", "switch")
# What takes --with: the ones that may start the stack.
STARTS = ("start", "switch")
ACTIVE = ("queued", "running")
# How the server reaches the dev user's own systemd (see the top); a test
# puts a stand-in here. The script to run is its last argument.
LAUNCH = shlex.split(os.environ.get(
    "WS_CONTROL_LAUNCH", "systemd-run --user --quiet --collect --pipe --wait -- /usr/bin/zsh -c"))
ACT_LOCK = threading.Lock()


def job_state(m):
    """As ws has it: queued or running, but its runner is gone, is "lost"."""
    s = m.get("state")
    if s in ACTIVE:
        pid = m.get("pid")
        if pid is None:
            return s if time.time() - m.get("queued_at", 0) < 30 else "lost"
        if not running_here(pid):
            return "lost"
    return s


def action_meta(name, action):
    try:
        with open(os.path.join(STATE, "jobs", name, action, "meta.json")) as f:
            m = json.load(f)
    except (OSError, ValueError):
        return None
    return dict(m, state=job_state(m))


def startable(ps):
    return {n: p for n, p in ps.items() if p.get("start") or p.get("serve")}


def controls(tree):
    """Each project that has start commands: whether its servers run, what
    it can start with (its optional parts that start something), and how
    its last start or stop went."""
    nodes = {n["name"]: n for n in (tree or {}).get("children", []) if n.get("kind") == "stack"}
    out = []
    for name, p in startable(projects()).items():
        node = nodes.get(name) or {}
        servers = next((c for c in node.get("children", []) if c["name"] == "servers"), None)
        acts = [m for m in (action_meta(name, a) for a in ACTIONS) if m]
        last = max(acts, key=lambda m: m.get("queued_at", 0), default=None)
        out.append({
            "name": name, "about": p.get("about", ""),
            "built": os.path.isdir(os.path.join(ROOT, p.get("dir", name))),
            "running": bool(servers and servers["children"]),
            "paused": bool(node.get("paused")),
            "mem": node.get("mem", 0),
            "swap": node.get("swap", 0),
            "with": [{"name": x, "about": o.get("about", "")}
                     for x, o in p.get("optional", {}).items() if "start" in o],
            "action": last and {k: last.get(k) for k in
                                ("job", "cmd", "state", "exit", "queued_at", "started_at", "ended_at")},
        })
    return out


def act(name, action, extras):
    """Start, stop, pause, resume or switch to a stack, as a ws job of it.
    Returns (HTTP code, message)."""
    p = startable(projects()).get(name)
    if action not in ACTIONS or p is None:
        return 404, f"no stack {name!r} to {action}"
    opts = {x for x, o in p.get("optional", {}).items() if "start" in o}
    if not isinstance(extras, list) or not all(isinstance(x, str) for x in extras):
        return 400, "'with' is a list of names"
    if action not in STARTS and extras:
        return 400, f"a {action} takes no 'with'"
    if set(extras) - opts:
        return 400, f"{name} can't start with {', '.join(sorted(set(extras) - opts))}" + \
            (f" (it can with: {', '.join(sorted(opts))})" if opts else "")
    for a in ACTIONS:
        m = action_meta(name, a)
        if m and m["state"] in ACTIVE:
            return 409, f"{name}: its {a} is {m['state']} already"
    cmd = ["ws", action, name, *(w for x in extras for w in ("--with", x))]
    # --parallel: not queued behind a build the stack is running.
    script = shlex.join(["ws", "run", name, action, "--parallel", "--", shlex.join(cmd)])
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={env['XDG_RUNTIME_DIR']}/bus")
    try:
        r = subprocess.run([*LAUNCH, script], capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return 500, f"couldn't run ws ({type(e).__name__}: {e})"
    out = (r.stdout + r.stderr).strip()
    return (202, out) if r.returncode == 0 else (500, out or f"ws run failed (exit {r.returncode})")


def zsh(script):
    """Run a ws command as the dev user's own (see the top): (code, output)."""
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path={env['XDG_RUNTIME_DIR']}/bus")
    try:
        r = subprocess.run([*LAUNCH, script], capture_output=True, text=True, timeout=90, env=env)
    except (OSError, subprocess.SubprocessError) as e:
        return 500, f"couldn't run ws ({type(e).__name__}: {e})"
    out = (r.stdout + r.stderr).strip()
    return (200, out) if r.returncode == 0 else (500, out or f"ws failed (exit {r.returncode})")


def claude_new(name):
    """A Claude session in the stack (ws claude NAME)."""
    if name not in projects():
        return 404, f"no stack {name!r}"
    return zsh(shlex.join(["ws", "claude", name]))


def claude_session_cmd(verb, name, tname):
    """ws claude-end / claude-trust for one of the stack's sessions."""
    if name not in projects() or not isinstance(tname, str) or not re.fullmatch(r"claude-\d+", tname):
        return 404, "no such Claude session"
    return zsh(shlex.join(["ws", verb, name, tname]))


def claude_launched():
    """The sessions ws claude started, by stack (claude_sessions.py)."""
    out = {}
    for r in claude_sessions.sessions(STATE, url_of=None, write=False):
        out.setdefault(r["stack"], []).append(r)
    return out


def log_tail(name, action, lines=80):
    """The end of a stack's last start or stop's output, or None."""
    if action not in ACTIONS or name not in startable(projects()):
        return None
    try:
        with open(os.path.join(STATE, "jobs", name, action, "log"), "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - 64 * 1024))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return None
    return "\n".join(text.splitlines()[-lines:]) + "\n"


def tailnet_name():
    try:
        r = subprocess.run(["tailscale", "status", "--self", "--json"],
                           capture_output=True, text=True, timeout=5)
        return json.loads(r.stdout)["Self"]["DNSName"].rstrip(".")
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
        return None


def allowed_hosts():
    names = ["127.0.0.1", "localhost"]
    if os.environ.get("WS_CONTROL_HOSTS"):
        names += os.environ["WS_CONTROL_HOSTS"].split(",")
    else:
        n = tailnet_name()
        if n:
            names.append(n)
    return {f"{n}:{PORT}" for n in names}


class Sampler(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.tree, self.error, self.at = None, None, None
        self.claude, self.claude_error = None, None
        self.watch_error = None
        # Each tree, a minute at a time, for the peaks (watch.py). Where it
        # can't write (a test, the Mac), the page still works.
        self.recorder = watch.Recorder()
        self.peaks, self.peaks_at = None, 0
        self.lock = threading.Lock()

    def run(self):
        prev = boxtree.snapshot()
        while True:
            time.sleep(INTERVAL)
            try:
                cur = boxtree.snapshot()
                tree = boxtree.build(stacks(), cur, prev, boxtree.docker_containers(), job_metas(),
                                     docker_disk=boxtree.docker_disk())
                prev, err = cur, None
            except Exception as e:  # noqa: BLE001 - keep serving the last good tree
                tree, err = None, f"{type(e).__name__}: {e}"
            try:
                usage, uerr = claude(), None
            except Exception as e:  # noqa: BLE001 - the tree doesn't depend on it
                usage, uerr = None, f"{type(e).__name__}: {e}"
            werr = None
            if tree:
                try:
                    self.recorder.add(tree, ((usage or self.claude) or {}).get("sessions", ()))
                except Exception as e:  # noqa: BLE001 - the page doesn't depend on it
                    werr = f"{type(e).__name__}: {e}"
            with self.lock:
                if tree:
                    self.tree, self.at = tree, time.time()
                self.error = err
                if usage:
                    self.claude = usage
                self.claude_error = uerr
                self.watch_error = werr

    def json(self):
        with self.lock:
            tree, data = self.tree, {"tree": self.tree, "at": self.at, "interval": INTERVAL,
                                     "error": self.error, "claude": self.claude,
                                     "claude_error": self.claude_error}
        # Read now, not with the sample: a start just asked for shows at once.
        try:
            data["stacks"], data["stacks_error"] = controls(tree), None
        except Exception as e:  # noqa: BLE001 - the tree doesn't depend on it
            data["stacks"], data["stacks_error"] = None, f"{type(e).__name__}: {e}"
        try:
            data["claude_launched"], data["claude_launched_error"] = claude_launched(), None
        except Exception as e:  # noqa: BLE001 - the tree doesn't depend on it
            data["claude_launched"], data["claude_launched_error"] = None, f"{type(e).__name__}: {e}"
        return json.dumps(data).encode()

    def peaks_json(self, days=7):
        """watch.report() of the recorded minutes and the latest tree:
        worked out at most once a minute (the minutes change no faster)."""
        with self.lock:
            tree, sessions = self.tree, (self.claude or {}).get("sessions", ())
            if self.peaks and time.time() - self.peaks_at < 60:
                return self.peaks
        if not tree:
            return json.dumps({"report": None, "error": "no sample yet"}).encode()
        try:
            now = time.time()
            r = watch.report(watch.load(self.recorder.folder, now - days * 86400), tree, sessions, now, days)
            body = json.dumps({"report": r, "error": self.watch_error}).encode()
        except Exception as e:  # noqa: BLE001 - the tree doesn't depend on it
            return json.dumps({"report": None, "error": f"{type(e).__name__}: {e}"}).encode()
        with self.lock:
            self.peaks, self.peaks_at = body, now
        return body


class Handler(BaseHTTPRequestHandler):
    server_version = "devbox-control"

    def do_GET(self):
        if self.headers.get("Host") not in HOSTS:
            return self.send(421, b"misdirected request\n", "text/plain")
        path = self.path.split("?")[0]
        if path == "/":
            return self.send(200, PAGE, "text/html; charset=utf-8")
        if path == "/api/tree":
            return self.send(200, SAMPLER.json(), "application/json")
        if path == "/api/peaks":
            return self.send(200, SAMPLER.peaks_json(), "application/json")
        m = re.fullmatch(rf"/api/stacks/([^/]+)/({'|'.join(ACTIONS)})/log", path)
        if m:
            text = log_tail(m[1], m[2])
            if text is None:
                return self.send(404, b"no such log\n", "text/plain")
            return self.send(200, text.encode(), "text/plain; charset=utf-8")
        self.send(404, b"not found\n", "text/plain")

    def do_POST(self):
        host = self.headers.get("Host")
        if host not in HOSTS:
            return self.send(421, b"misdirected request\n", "text/plain")
        if self.headers.get("Origin") not in (f"https://{host}", f"http://{host}"):
            return self.reply(403, "only from the control page itself")
        if (self.headers.get("Content-Type") or "").split(";")[0].strip() != "application/json":
            return self.reply(415, "a JSON body, please")
        path = self.path.split("?")[0]
        m = re.fullmatch(rf"/api/stacks/([^/]+)/({'|'.join(ACTIONS)}|claude|claude-end|claude-trust)", path)
        if not m:
            return self.reply(404, "not found")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}") if 0 <= n <= 4096 else None
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return self.reply(400, "a JSON object of at most 4 KB")
        if m[2] == "claude":
            return self.reply(*claude_new(m[1]))
        if m[2] in ("claude-end", "claude-trust"):
            return self.reply(*claude_session_cmd(m[2], m[1], body.get("tmux")))
        with ACT_LOCK:
            code, msg = act(m[1], m[2], body.get("with", []))
        self.reply(code, msg)

    def reply(self, code, message):
        body = json.dumps({"ok": code < 300, "message": message}).encode()
        self.send(code, body, "application/json")

    def send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; "
                         "script-src 'self' 'unsafe-inline'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    HOSTS = allowed_hosts()
    SAMPLER = Sampler()
    SAMPLER.start()
    print(f"devbox-control on http://{BIND}:{PORT}, for {', '.join(sorted(HOSTS))}", flush=True)
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
