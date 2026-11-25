"""Claude subscription usage, from Claude Code's status line.

The status line is the only documented place the plan's limits show up
(https://code.claude.com/docs/en/statusline.md): on every reply Claude Code
runs its statusLine command with a JSON snapshot on stdin, which holds the
account's `rate_limits` (five_hour, seven_day: used_percentage, resets_at),
the session's id, folder, model, estimated cost (cost.total_cost_usd) and
context window. `ws statusline` is that command; it hands the snapshot to
record() and prints line().

Two things are kept, under STATE/claude/ (~/.local/state/ws/claude):

  sessions/<id>.json   each session's latest snapshot, with the pid of its
                       claude process (so the page can put it in the tree)
  samples-YYYY-MM.jsonl one line each time a session's cost, model or the
                       account's limits changed: the history to learn from,
                       e.g. how much of the 5-hour window a small bug fix
                       takes.

summary() turns those into what the page shows: the account's two windows
(the newest snapshot's, since they are the account's, not a session's) and
each session's cost in the current 5-hour window and the week.

Limits are per account, so they include use from other machines and
claude.ai; a session's own spend is exact (cost deltas), its share of the
quota is not. Cost is Claude Code's estimate at list price: on a
subscription it measures relative use, it is not a bill.

Other machines' sessions: where WS_CLAUDE_SHIP_TO names an ssh host (the
Mac's status line sets it to the box), `ws statusline` also copies this
machine's records there, in the background and at most once a minute
(ship()), to STATE/claude-from/<host>/. summary() reads those next to the
box's own: their limit readings, their spend, their sessions (by when they
last replied: a pid elsewhere means nothing here).
"""
import fcntl
import json
import os
import re
import socket
import subprocess
import sys
import time

STATE = os.environ.get("WS_STATE", os.path.expanduser("~/.local/state/ws"))
WINDOWS = {"five_hour": 5 * 3600, "seven_day": 7 * 86400}
# A session whose latest snapshot is older than this is listed as idle.
IDLE_AFTER = 15 * 60


def folder(state=STATE):
    return os.path.join(state, "claude")


FROM = "claude-from"


def safe_host(host):
    """A host name as a folder name."""
    return re.sub(r"[^A-Za-z0-9._-]", "_", host).lstrip(".") or "_"


def folders(state=STATE, host=None):
    """This machine's records, then each other machine's copied here, by
    host (not one named as this machine: that would count twice)."""
    me = safe_host(host or socket.gethostname())
    d = os.path.join(state, FROM)
    others = sorted(n for n in (os.listdir(d) if os.path.isdir(d) else [])
                    if n != me and os.path.isdir(os.path.join(d, n)))
    return [folder(state), *(os.path.join(d, n) for n in others)]


# ---------------------------------------------------------------- recording

def claude_pid(start=None):
    """The claude process this status line belongs to: the nearest ancestor
    named claude (the command runs under a shell Claude Code starts)."""
    pid = start or os.getppid()
    for _ in range(6):
        if not pid or pid <= 1:
            return None
        ppid, comm = parent(pid)
        if comm == "claude":
            return pid
        pid = ppid
    return None


def parent(pid):
    try:
        if os.path.exists("/proc/self/stat"):
            with open(f"/proc/{pid}/stat") as f:
                s = f.read()
            comm = s[s.index("(") + 1:s.rindex(")")]
            return int(s[s.rindex(")") + 2:].split()[1]), comm
        out = subprocess.run(["ps", "-o", "ppid=", "-o", "comm=", "-p", str(pid)],
                             capture_output=True, text=True, timeout=2).stdout.split(None, 1)
        return int(out[0]), os.path.basename(out[1].strip())
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return None, None


def limits_of(data):
    out = {}
    for w in WINDOWS:
        v = ((data.get("rate_limits") or {}).get(w)) or {}
        if v.get("used_percentage") is not None and v.get("resets_at"):
            out[w] = {"pct": float(v["used_percentage"]), "resets_at": int(v["resets_at"])}
    return out


def sample_of(data, now, host):
    ws = data.get("workspace") or {}
    return {
        "t": round(now, 1), "host": host,
        "session": data.get("session_id"),
        "name": data.get("session_name"),
        "cwd": data.get("cwd") or ws.get("current_dir"),
        "project_dir": ws.get("project_dir"),
        "model": (data.get("model") or {}).get("id"),
        "cost": (data.get("cost") or {}).get("total_cost_usd"),
        "context_pct": (data.get("context_window") or {}).get("used_percentage"),
        "limits": limits_of(data),
        # What the spend was for, to learn from later: the prompt (one of
        # mine; a task is one or a few), the repo, and how hard it thought.
        "prompt": data.get("prompt_id"),
        "repo": "/".join(filter(None, ((ws.get("repo") or {}).get("owner"),
                                       (ws.get("repo") or {}).get("name")))) or None,
        "effort": (data.get("effort") or {}).get("level"),
    }


def changed(a, b):
    keys = ("cost", "model", "limits")
    return a is None or any(a.get(k) != b.get(k) for k in keys)


def write_atomic(path, text):
    tmp = f"{path}.{os.getpid()}.new"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


KEEP_SESSIONS = 35 * 86400


def prune(d, now):
    """Drop latest-snapshot files of sessions not heard from in 35 days (the
    samples stay: they are the history). Done when a new session appears."""
    for n in os.listdir(d):
        p = os.path.join(d, n)
        try:
            with open(p) as f:
                t = json.load(f).get("t") or 0
        except ValueError:
            t = 0
        except OSError:
            continue
        if now - t > KEEP_SESSIONS:
            try:
                os.remove(p)
            except OSError:
                pass


def record(data, state=STATE, now=None, pid=None, host=None):
    """Keep a status line snapshot: the session's latest, and a sample line
    when something that counts changed."""
    sid = data.get("session_id")
    if not sid or "/" in sid or sid.startswith("."):
        return None
    now = time.time() if now is None else now
    host = host or socket.gethostname()
    d = folder(state)
    os.makedirs(os.path.join(d, "sessions"), exist_ok=True)
    path = os.path.join(d, "sessions", f"{sid}.json")
    try:
        with open(path) as f:
            last = json.load(f).get("sample")
    except (OSError, ValueError):
        last = None
        prune(os.path.join(d, "sessions"), now)
    s = sample_of(data, now, host)
    write_atomic(path, json.dumps({"t": s["t"], "host": host, "pid": pid, "sample": s,
                                   "data": data}))
    if changed(last, s):
        month = time.strftime("%Y-%m", time.localtime(now))
        with open(os.path.join(d, f"samples-{month}.jsonl"), "a") as f:
            f.write(json.dumps(s) + "\n")
    return s


def until(ts, now):
    """When a window resets: "in 43 min", "in 2 h 05", or past a day "Sat 03:20"."""
    m = max(int(ts - now) // 60, 0)
    if m < 60:
        return f"in {m} min"
    if m < 24 * 60:
        return f"in {m // 60} h {m % 60:02d}"
    return time.strftime("%a %H:%M", time.localtime(ts))


def line(data, now=None):
    """The status line itself: model, context, the two windows."""
    now = time.time() if now is None else now
    parts = [(data.get("model") or {}).get("display_name") or "?"]
    ctx = (data.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        parts.append(f"ctx {ctx:.0f}%")
    lim = limits_of(data)
    for w, label in (("five_hour", "5h"), ("seven_day", "week")):
        if w in lim and lim[w]["resets_at"] > now:
            parts.append(f"{label} {lim[w]['pct']:.0f}% (resets {until(lim[w]['resets_at'], now)})")
    return " · ".join(parts)


# ---------------------------------------------------------------- reading

def samples(state=STATE, now=None, months=2, host=None):
    """This month's and last month's sample lines (enough for a week), from
    every machine's records, in time order."""
    out = []
    for d in folders(state, host):
        try:
            names = sorted(n for n in os.listdir(d) if n.startswith("samples-") and n.endswith(".jsonl"))
        except OSError:
            continue
        for n in names[-months:]:
            try:
                with open(os.path.join(d, n)) as f:
                    for ln in f:
                        try:
                            out.append(json.loads(ln))
                        except ValueError:
                            pass  # a line cut short by a crash
            except OSError:
                pass  # replaced by a copy from its machine meanwhile
    return sorted((s for s in out if isinstance(s, dict) and "t" in s), key=lambda s: s["t"])


def latest(state=STATE, host=None):
    """Each session's latest snapshot, from every machine's records; with
    `local`: whether it was recorded here."""
    out = []
    for i, base in enumerate(folders(state, host)):
        d = os.path.join(base, "sessions")
        for n in os.listdir(d) if os.path.isdir(d) else []:
            try:
                with open(os.path.join(d, n)) as f:
                    x = json.load(f)
            except (OSError, ValueError):
                continue
            if isinstance(x, dict) and "t" in x:
                out.append({**x, "local": i == 0})
    return out


def spend(rows):
    """[(t, session, model, dollars)]: what each sample added. The first
    sample of a session is its baseline (spent before it was recorded, or
    $0 at its start); a drop means /clear started the count again."""
    last, out = {}, []
    for s in rows:
        c = s.get("cost")
        if c is None or not s.get("session"):
            continue
        k = s["session"]
        if k in last:
            d = c - last[k] if c >= last[k] else c
            if d > 0:
                out.append((s["t"], k, s.get("model"), d))
        last[k] = c
    return out


# A window's bar must have moved this many points for its ratio to count:
# below it the whole-percent readings make the ratio mostly rounding.
CALIBRATE_MIN_PCT = 3
CALIBRATE_DAYS = 14


def calibration(rows, now, days=CALIBRATE_DAYS):
    """Dollars of Claude Code's estimate per point of each window's bar,
    from the recorded windows of the last `days`: in each, the spend between
    its first and last reading over how far the bar moved between them,
    pooled. Use the bar moves for that isn't recorded (claude.ai, the
    desktop app) makes it low, so what's left in dollars comes out low too.
    {window: {"usd_per_pct", "windows"}} for those with any to go by."""
    sp = spend(rows)
    out = {}
    for w in WINDOWS:
        by = {}
        for s in rows:
            v = (s.get("limits") or {}).get(w)
            if v and s["t"] >= now - days * 86400:
                by.setdefault(v["resets_at"], []).append((s["t"], v["pct"]))
        usd = pct = n = 0
        for readings in by.values():
            (t0, p0), (t1, p1) = min(readings), max(readings)
            if p1 - p0 < CALIBRATE_MIN_PCT:
                continue
            usd += sum(d for t, _, _, d in sp if t0 < t <= t1)
            pct += p1 - p0
            n += 1
        if n and usd > 0:
            out[w] = {"usd_per_pct": round(usd / pct, 3), "windows": n}
    return out


def owner(path, stacks):
    best = None
    for name, d in stacks.items():
        d = d.rstrip("/")
        if path and (path == d or path.startswith(d + "/")) and (best is None or len(d) > len(stacks[best])):
            best = name
    return best


def summary(stacks, state=STATE, now=None, alive=None, host=None):
    """The account's windows and each session's spend in them."""
    now = time.time() if now is None else now
    rows = samples(state, now, host=host)
    snaps = latest(state, host)
    # The account's limits: the newest reading of each window not yet reset.
    limits = {}
    for s in rows + [x["sample"] for x in snaps if x.get("sample")]:
        for w, v in (s.get("limits") or {}).items():
            if w in WINDOWS and v["resets_at"] > now and (w not in limits or s["t"] >= limits[w]["as_of"]):
                limits[w] = {**v, "as_of": s["t"]}
    starts = {w: (limits[w]["resets_at"] - span if w in limits else now - span)
              for w, span in WINDOWS.items()}
    per = {}
    for t, sid, model, d in spend(rows):
        p = per.setdefault(sid, {"five_hour": 0.0, "seven_day": 0.0, "models": {}})
        for w in WINDOWS:
            if t >= starts[w]:
                p[w] += d
        if t >= starts["seven_day"]:
            p["models"][model] = p["models"].get(model, 0.0) + d
    sessions = []
    for x in snaps:
        s = x.get("sample") or {}
        sid = s.get("session")
        p = per.get(sid, {"five_hour": 0.0, "seven_day": 0.0, "models": {}})
        # A pid means something only on the machine that recorded it. A
        # session copied from another machine: running while it replies.
        local = x.get("local", True)
        if local:
            running = (x.get("pid") is not None and alive is not None
                       and (host is None or x.get("host") == host) and alive(x["pid"]))
        else:
            running = now - x["t"] <= IDLE_AFTER
        if not running and p["seven_day"] == 0:
            continue  # ended, and nothing spent this week
        where = s.get("project_dir") or s.get("cwd")
        sessions.append({
            "session": sid, "name": s.get("name"), "host": x.get("host"),
            "pid": x.get("pid") if local else None, "remote": not local,
            "running": running, "idle": now - x["t"] > IDLE_AFTER, "updated": x["t"],
            "cwd": s.get("cwd"), "stack": owner(where, stacks), "model": s.get("model"),
            "context_pct": s.get("context_pct"), "cost": s.get("cost"),
            "five_hour": round(p["five_hour"], 4), "seven_day": round(p["seven_day"], 4),
            "models": {m: round(v, 4) for m, v in p["models"].items()},
        })
    total = {w: sum(s[w] for s in sessions) for w in WINDOWS}
    for s in sessions:
        s["share"] = {w: (s[w] / total[w] if total[w] else None) for w in WINDOWS}
    sessions.sort(key=lambda s: (-s["five_hour"], -s["seven_day"]))
    cal = calibration(rows, now)
    # What's left of each window, in dollars of work at the recorded rate.
    for w, v in limits.items():
        if w in cal:
            v["left_usd"] = round(max(100 - v["pct"], 0) * cal[w]["usd_per_pct"], 2)
    return {"limits": limits, "starts": starts, "sessions": sessions, "total": total, "calibration": cal}


# ---------------------------------------------------------------- shipping

SHIP_EVERY = 60
SHIP_TIMEOUT = 120


def ship(target, state=STATE, host=None, now=time.time, sleep=time.sleep, run=subprocess.run):
    """Copy this machine's records to `target` (an ssh host), into
    ~/.local/state/ws/claude-from/<host>/ there, a mirror (rsync --delete).
    One at a time (a lock; a second returns "busy") and at most every
    SHIP_EVERY seconds: it waits out the rest of the minute first, then
    copies, so the copy has what the status lines meanwhile recorded.
    Returns how it went; also kept in .shipped (its mtime: when)."""
    d = folder(state)
    os.makedirs(d, exist_ok=True)
    lock = open(os.path.join(d, ".ship.lock"), "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return "busy"
    stamp = os.path.join(d, ".shipped")
    try:
        wait = os.path.getmtime(stamp) + SHIP_EVERY - now()
    except OSError:
        wait = 0
    if wait > 0:
        sleep(wait)
    dest = f".local/state/ws/{FROM}/{safe_host(host or socket.gethostname())}"
    argv = ["rsync", "-a", "--delete", "--exclude", ".ship*", "--exclude", "*.new",
            "-e", "ssh -o BatchMode=yes -o ConnectTimeout=10",
            # The folder may not be there yet (the Mac's rsync has no --mkpath).
            f"--rsync-path=mkdir -p {dest} && rsync",
            d + "/", f"{target}:{dest}/"]
    try:
        r = run(argv, capture_output=True, text=True, timeout=SHIP_TIMEOUT)
        result = "ok" if r.returncode == 0 else f"rsync exit {r.returncode}: {(r.stderr or '').strip()[-300:]}"
    except (OSError, subprocess.SubprocessError) as e:
        result = f"{type(e).__name__}: {e}"
    # Also after a failure: the next try is a minute away, not every reply.
    write_atomic(stamp, json.dumps({"t": now(), "to": target, "result": result}) + "\n")
    lock.close()
    return result


def ship_in_background(target):
    """ship() in a process of its own, so the status line doesn't wait."""
    try:
        subprocess.Popen([sys.executable, "-I", os.path.realpath(__file__), "ship", target],
                         start_new_session=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass


def main():
    """ws statusline: record the snapshot on stdin, print the line. Never
    fails loudly: a broken status line is worse than a missing sample."""
    try:
        data = json.load(sys.stdin)
    except ValueError:
        return 0
    try:
        record(data, pid=claude_pid())
    except OSError as e:
        print(f"(ws statusline: {e.strerror})", end=" ")
    target = os.environ.get("WS_CLAUDE_SHIP_TO")
    if target:
        ship_in_background(target)
    print(line(data))
    return 0


if __name__ == "__main__" and sys.argv[1:2] == ["ship"] and len(sys.argv) == 3:
    ship(sys.argv[2])
