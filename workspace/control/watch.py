"""The box's memory and CPU over time: what each stack, Claude session and
job takes when idle and at its peaks, and so how much free memory the box
should keep for what runs now.

The control server (server.py) builds boxtree's tree every few seconds for
its page; a Recorder takes each one and keeps, per minute, for each of
these, its memory's highest and mean and its CPU's mean and highest:

  app:STACK       what the stack serves: its servers, its containers, and
                  what runs by hand in its folder (boxtree's "outside ws"),
                  not counting a Claude session there
  claude:SESSION  a Claude Code session on the box: the cgroup its claude
                  process runs in (its tmux pane or login), so the tests and
                  builds it runs itself count in it
  job:STACK/JOB   a ws run job, by its name: every run of it is one series
  stack:STACK     all of the stack, as the page shows it
  linux, docker, other   the rest of the box

app, claude and job never hold the same memory twice, so what they could
take at their peaks adds up. And the box's own: memory used, the lowest
available, swap, CPU, and how long tasks stalled waiting for memory
(PSI: /proc/pressure/memory, "some" and "full").

One JSON line per minute, in WATCH/YYYY-MM-DD.jsonl, 35 days kept. Memory
is as boxtree counts it (without the page cache, which the kernel drops
when it needs the room), in MB; CPU in cores. A minute's highest is its
highest sample: a peak shorter than the server's interval (3 s) can be
missed.

report() reads them back:
  - per stack: how often it's active (its app's CPU over its idle
    baseline: a rebuild, a request, a test run against it) or idle
    (servers waiting), its memory either way, and its peak;
  - for what runs now: each one's peak (the 99th percentile of its
    minutes' highest, so one freak minute doesn't decide) less what it
    takes now. Their sum is what to keep free so that all of them can
    peak at once; what's left of the free memory is the room for more.

  ws peaks [DAYS]    on the box: that report (the last 7 days by default)
"""
import json
import math
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import boxtree  # noqa: E402

WATCH = os.environ.get("WS_WATCH_DIR", "/var/lib/devbox-control/watch")
KEEP_DAYS = 35
# A minute is active when its mean CPU is at least ACTIVE_CPU cores and
# at least twice the thing's median: some stacks' servers poll or watch
# files even when nobody uses them (one sat at ~0.2 cores), so idle is
# measured against the stack's own baseline, not one figure for all.
ACTIVE_CPU = 0.10
ACTIVE_OVER_MEDIAN = 2
# Fewer minutes than this of a thing's own and its peak is a guess: a
# Claude session's then comes from every session's minutes.
MIN_MINUTES = 30
MB = 2 ** 20


# ---------------------------------------------------------------- what to track

def processes(node):
    for c in node.get("children", ()):
        if c["kind"] == "process":
            yield c["pid"]
            yield from processes(c)


def units_in(node):
    """The cgroups (unit and container nodes) under a node of the tree."""
    for c in node.get("children", ()):
        if c.get("unit"):
            yield c
        elif c["kind"] != "process":
            yield from units_in(c)


def vals(nodes):
    """(memory, CPU, swap) of some nodes together."""
    nodes = list(nodes)
    return (sum(n.get("mem", 0) for n in nodes), sum(n.get("cpu") or 0.0 for n in nodes),
            sum(n.get("swap", 0) for n in nodes))


def points(tree, sessions=()):
    """What to track in one tree: ({key: (memory, CPU, swap)}, {claude key:
    its session's name}, [paused stacks]). `sessions`: claude_usage's, for
    the pids of the box's running Claude sessions."""
    pids = {s["pid"]: s for s in sessions
            if s.get("pid") and s.get("running") and not s.get("remote")}
    pts, names, paused = {}, {}, []
    claude = set()  # the units Claude sessions run in, by id(): not a stack's app or a job
    for u in units_in(tree):
        s = next((pids[p] for p in processes(u) if p in pids), None)
        if s:
            claude.add(id(u))
            key = f"claude:{s['session']}"
            names[key] = s.get("name") or os.path.basename(s.get("cwd") or "") or s["session"][:8]
            pts[key] = vals([u])
    for top in tree.get("children", ()):
        kind = top["kind"]
        if kind == "stack":
            n = top["name"]
            pts[f"stack:{n}"] = vals([top])
            if top.get("paused"):
                paused.append(n)
            parts = {p["name"]: p for p in top.get("children", ())}
            app = [u for k in ("servers", "containers", "outside ws") if k in parts
                   for u in units_in(parts[k]) if id(u) not in claude]
            pts[f"app:{n}"] = vals(app)
            for j in units_in(parts["jobs"]) if "jobs" in parts else ():
                if id(j) not in claude:
                    key = f"job:{n}/{j['name']}"
                    pts[key] = tuple(a + b for a, b in zip(pts.get(key, (0, 0.0, 0)), vals([j])))
        elif kind == "base":
            pts["linux"] = vals([top])
        elif kind in ("docker", "other"):
            pts[kind] = vals([top])
    return pts, names, paused


# ---------------------------------------------------------------- recording

def read_psi(proc_root):
    """Microseconds tasks have stalled waiting for memory, since boot:
    (some, full), or None where the kernel doesn't say."""
    out = {}
    for line in boxtree.read(f"{proc_root}/pressure/memory").splitlines():
        m = re.match(r"(some|full) .*total=(\d+)", line)
        if m:
            out[m[1]] = int(m[2])
    return (out["some"], out.get("full", 0)) if "some" in out else None


class Recorder:
    """Takes each tree as it's built; writes a line when a minute is over."""

    def __init__(self, folder=WATCH, proc_root=boxtree.PROC_ROOT, clock=time.time):
        self.folder, self.proc_root, self.clock = folder, proc_root, clock
        self.minute, self.psi = None, read_psi(proc_root)
        self.reset()

    def reset(self):
        self.n, self.acc, self.names, self.paused = 0, {}, {}, set()
        self.box = {"used_max": 0, "used_sum": 0, "avail_min": None, "swap_max": 0,
                    "cpu_sum": 0.0, "cpu_max": 0.0, "disk_min": None}

    def add(self, tree, sessions=()):
        now = self.clock()
        minute = int(now // 60) * 60
        if self.minute is not None and minute != self.minute:
            self.flush()
        self.minute = minute
        pts, names, paused = points(tree, sessions)
        self.n += 1
        for k, (mem, cpu, swap) in pts.items():
            a = self.acc.setdefault(k, [0, 0, 0, 0.0, 0.0, 0])  # n, max, sum, cpu sum, cpu max, swap
            a[0] += 1
            a[1] = max(a[1], mem)
            a[2] += mem
            a[3] += cpu
            a[4] = max(a[4], cpu)
            a[5] = max(a[5], swap)
        self.names.update(names)
        self.paused.update(paused)
        b, cpu = self.box, tree.get("cpu") or 0.0
        b["used_max"] = max(b["used_max"], tree["mem"])
        b["used_sum"] += tree["mem"]
        b["avail_min"] = tree["available"] if b["avail_min"] is None else min(b["avail_min"], tree["available"])
        b["swap_max"] = max(b["swap_max"], tree.get("swap_used", 0))
        b["cpu_sum"] += cpu
        b["cpu_max"] = max(b["cpu_max"], cpu)
        d = tree.get("disk")
        if d:
            b["disk_min"] = d["free"] if b["disk_min"] is None else min(b["disk_min"], d["free"])

    def line(self):
        mb = lambda n: round(n / MB)  # noqa: E731
        psi, stall = read_psi(self.proc_root), None
        if psi and self.psi:
            stall = [max(psi[0] - self.psi[0], 0) // 1000, max(psi[1] - self.psi[1], 0) // 1000]
        self.psi = psi
        b, n = self.box, self.n
        return {
            "t": self.minute, "n": n,
            "box": {"used": [mb(b["used_max"]), mb(b["used_sum"] / n)], "avail_min": mb(b["avail_min"] or 0),
                    "swap": mb(b["swap_max"]), "cpu": [round(b["cpu_sum"] / n, 2), round(b["cpu_max"], 2)],
                    "stall_ms": stall,
                    **({} if b["disk_min"] is None else {"disk_free_min": mb(b["disk_min"])})},
            "k": {k: [mb(a[1]), mb(a[2] / a[0]), round(a[3] / a[0], 2), round(a[4], 2), mb(a[5])]
                  for k, a in sorted(self.acc.items())},
            "paused": sorted(self.paused), "names": self.names,
        }

    def flush(self):
        """Write the minute so far (if any sample came) and start the next."""
        if self.minute is None or not self.n:
            return
        line = self.line()
        os.makedirs(self.folder, exist_ok=True)
        day = time.strftime("%Y-%m-%d", time.localtime(self.minute))
        path = os.path.join(self.folder, f"{day}.jsonl")
        new_day = not os.path.exists(path)
        with open(path, "a") as f:
            f.write(json.dumps(line, separators=(",", ":")) + "\n")
        if new_day:
            prune(self.folder, self.minute)
        self.reset()


DAY_FILE = re.compile(r"(\d{4}-\d{2}-\d{2})\.jsonl")


def prune(folder, now):
    cut = time.strftime("%Y-%m-%d", time.localtime(now - KEEP_DAYS * 86400))
    for n in os.listdir(folder):
        m = DAY_FILE.fullmatch(n)
        if m and m[1] < cut:
            try:
                os.remove(os.path.join(folder, n))
            except OSError:
                pass


# ---------------------------------------------------------------- reading

def load(folder=WATCH, since=0):
    """The minutes recorded since `since`, in time order."""
    first = time.strftime("%Y-%m-%d", time.localtime(since - 86400))
    out = []
    names = sorted(n for n in (os.listdir(folder) if os.path.isdir(folder) else [])
                   if DAY_FILE.fullmatch(n) and n[:10] >= first)
    for n in names:
        try:
            with open(os.path.join(folder, n)) as f:
                for ln in f:
                    try:
                        r = json.loads(ln)
                    except ValueError:
                        continue  # a line cut short by a crash
                    if isinstance(r, dict) and r.get("t", 0) >= since:
                        out.append(r)
        except OSError:
            pass
    return sorted(out, key=lambda r: r["t"])


def percentile(xs, q):
    """Nearest rank: a value that was seen."""
    xs = sorted(xs)
    return xs[max(0, min(len(xs) - 1, math.ceil(len(xs) * q) - 1))] if xs else None


def median(xs):
    return percentile(xs, 0.5)


def stack_of(key):
    m = re.match(r"(?:app|stack|job):([^/]+)", key)
    return m[1] if m else None


def minutes(rows, keys):
    """[(t, [max, mean, cpu mean, cpu max, swap])] of the keys, minutes
    their stack was paused left out (a frozen stack's memory says nothing
    about what it takes when it runs)."""
    keys = set(keys)
    return [(r["t"], v) for r in rows for k, v in r.get("k", {}).items()
            if k in keys and stack_of(k) not in r.get("paused", ())]


def describe(series):
    if not series:
        return None
    busy = max(ACTIVE_CPU, ACTIVE_OVER_MEDIAN * median([v[2] for _, v in series]))
    active = [v for _, v in series if v[2] >= busy]
    idle = [v for _, v in series if v[2] < busy]
    t_peak, v_peak = max(series, key=lambda x: x[1][0])
    return {
        "minutes": len(series), "active_minutes": len(active),
        "idle_mem": median([v[1] for v in idle]), "active_mem": median([v[1] for v in active]),
        "p99": percentile([v[0] for _, v in series], 0.99), "peak": v_peak[0], "peak_at": t_peak,
        "busy_cpu": round(busy, 2),
        "cpu_idle": round(sum(v[2] for v in idle) / len(idle), 2) if idle else None,
        "cpu_active": round(sum(v[2] for v in active) / len(active), 2) if active else None,
        "cpu_peak": max(v[3] for _, v in series),
    }


def report(rows, tree, sessions=(), now=None, days=7):
    """What runs now and what it could take at its peaks; each stack, Claude
    and job as recorded. Memory in MB, as recorded."""
    now = time.time() if now is None else now
    rows = [r for r in rows if r["t"] >= now - days * 86400]
    pts, names, paused = points(tree, sessions)
    seen = sorted({k for r in rows for k in r.get("k", {})})
    claude_keys = [k for k in seen if k.startswith("claude:")]
    for r in rows:
        names = {**r.get("names", {}), **names}

    def peak_of(key):
        d = describe(minutes(rows, [key]))
        if key.startswith("claude:") and (not d or d["minutes"] < MIN_MINUTES):
            pooled = describe(minutes(rows, claude_keys))
            return pooled, "every session's"
        return d, None

    running = []
    for key, (mem, cpu, _) in sorted(pts.items()):
        kind = key.split(":")[0]
        if kind not in ("app", "claude", "job") or stack_of(key) in paused:
            continue
        if kind == "app" and mem == 0:
            continue  # a stopped stack's: nothing runs
        d, source = peak_of(key)
        cur = round(mem / MB)
        peak = d["p99"] if d and (d["minutes"] >= MIN_MINUTES or source) else None
        running.append({"key": key, "name": names.get(key, key.split(":", 1)[1]), "kind": kind,
                        "mem": cur, "cpu": round(cpu, 2), "peak": peak, "from": source,
                        "minutes": d["minutes"] if d else 0,
                        "more": max(peak - cur, 0) if peak is not None else None})
    resume = []
    for n in paused:
        d = describe(minutes(rows, [f"app:{n}"]))
        cur, _, swap = pts.get(f"app:{n}", (0, 0, 0))
        cur, swap = round(cur / MB), round(swap / MB)
        if d and d["idle_mem"] is not None:
            resume.append({"stack": n, "mem": cur, "swap": swap, "idle_mem": d["idle_mem"],
                           "more": max(d["idle_mem"] - cur, 0), "peak": d["p99"]})
        else:
            # Never seen running: what it has in swap comes back as it's used.
            resume.append({"stack": n, "mem": cur, "swap": swap, "idle_mem": None,
                           "more": swap, "peak": None})
    need = sum(r["more"] or 0 for r in running)
    available = round(tree["available"] / MB)
    stacks = {k[4:]: describe(minutes(rows, [k])) for k in seen if k.startswith("app:")}
    jobs = {k[4:]: describe(minutes(rows, [k])) for k in seen if k.startswith("job:")}
    box = None
    if rows:
        hi = max(rows, key=lambda r: r["box"]["used"][0])
        lo = min(rows, key=lambda r: r["box"]["avail_min"])
        stalls = [r["box"].get("stall_ms") or [0, 0] for r in rows]
        box = {"used_peak": hi["box"]["used"][0], "used_peak_at": hi["t"],
               "avail_min": lo["box"]["avail_min"], "avail_min_at": lo["t"],
               "swap_peak": max(r["box"]["swap"] for r in rows),
               "stall_some_s": sum(s[0] for s in stalls) / 1000, "stall_full_s": sum(s[1] for s in stalls) / 1000,
               "disk_free_min": None, "disk_free_min_at": None}
        # Minutes from before disk was recorded have none.
        dl = [r for r in rows if r["box"].get("disk_free_min") is not None]
        if dl:
            d = min(dl, key=lambda r: r["box"]["disk_free_min"])
            box.update(disk_free_min=d["box"]["disk_free_min"], disk_free_min_at=d["t"])
    td = tree.get("disk")
    disk = td and {"path": td["path"], "free": round(td["free"] / MB), "total": round(td["total"] / MB),
                   "low": td["low"], "low_under": round(boxtree.LOW_DISK / MB)}
    out = {
        "days": days, "minutes": len(rows), "since": rows[0]["t"] if rows else None,
        "active_cpu": ACTIVE_CPU, "available": available, "total": round(tree["total"] / MB),
        "running": sorted(running, key=lambda r: -(r["more"] or 0)), "need": need,
        "unknown": [r["name"] for r in running if r["peak"] is None],
        "spare": available - need, "resume": resume,
        "stacks": {k: v for k, v in stacks.items() if v},
        "claude": describe(minutes(rows, claude_keys)), "claude_sessions": len(claude_keys),
        "jobs": {k: v for k, v in jobs.items() if v}, "box": box, "disk": disk,
    }
    # Every stack the box knows of: whether it has the room to start, resume
    # or be switched to, for the page's confirmations.
    known = {k.split(":", 1)[1] for k in pts if k.startswith("app:")} | set(out["stacks"])
    out["room"] = {n: {a: room(out, n, a) for a in ("start", "resume", "switch")} for n in sorted(known)}
    return out


def room(r, stack, action):
    """Whether STACK has the room to start, resume or be switched to, from
    report() r: what it needs (its peak, less what it holds now; a paused
    one never seen running: what it has in swap), against what's free once
    everything else running could peak. A switch pauses the other stacks
    first: their memory goes to swap and their peaks don't come. MB.
    {"need", "free", "fits" (None: no peak known), "text"}."""
    d = r["stacks"].get(stack)
    peak = d["p99"] if d and d["minutes"] >= MIN_MINUTES else None
    me = next((x for x in r["running"] if x["key"] == f"app:{stack}"), None)
    paused = next((x for x in r["resume"] if x["stack"] == stack), None)
    if action == "resume" and not paused:
        return {"need": None, "free": None, "fits": None, "text": f"{stack} isn't paused."}
    held = (paused or me or {}).get("mem", 0)
    if paused and action != "start":
        need = max(peak - held, 0) if peak is not None else paused["more"]
    elif action == "switch" and me:
        need = me["more"]  # running already: it stays as it is
    else:
        need = peak  # a start: from nothing (a restart's servers stop first)
    others = [x for x in r["running"] if x["key"] != f"app:{stack}"]
    free = r["available"] - sum(x["more"] or 0 for x in others)
    off = []
    if action == "switch":
        off = [x for x in others if x["kind"] == "app"]
        free += sum(x["mem"] + (x["more"] or 0) for x in off)
    if action == "start" and me:
        free += me["mem"]  # its own servers stop first
    fits = None if need is None else need <= free
    where = f"{gb(free)} free once {'the others are paused and ' if off else ''}what runs could peak"
    if need is None:
        text = f"{stack} hasn't been seen running long enough for a peak; {where}."
    elif fits:
        text = f"{stack} needs up to {gb(need)}; {where}: room to spare, {gb(free - need)}."
    else:
        big = sorted((x for x in others if x["kind"] == "app"), key=lambda x: -x["mem"])
        hint = f" Pausing {big[0]['name']} gives back about {gb(big[0]['mem'])}." if big and action != "switch" else ""
        text = f"{stack} needs up to {gb(need)}; {where}: {gb(need - free)} short, so it would push into swap.{hint}"
    return {"need": need, "free": free, "fits": fits, "text": text}


# ---------------------------------------------------------------- text

def gb(mb):
    return "–" if mb is None else f"{mb / 1024:.1f} GB" if mb >= 1024 else f"{mb} MB"


def when(t):
    return time.strftime("%a %H:%M", time.localtime(t))


def share(a, n):
    return f"{round(100 * a / n)}%" if n else "–"


def render(r):
    out = []
    disk = r.get("disk")
    if disk and disk["low"]:
        out += [f"Low disk: {gb(disk['free'])} free on {disk['path']}, under {gb(disk['low_under'])}. "
                "Builds and installs may fail; see what holds it with ws top.", ""]
    if not r["minutes"]:
        out.append("Nothing recorded yet: the control server (devbox-control) records a minute at a time.")
    else:
        out.append(f"The last {r['days']} days: {r['minutes']} minutes recorded, since {when(r['since'])}.")
    out.append("")
    known = [x for x in r["running"] if x["peak"] is not None]
    out.append(f"Now {gb(r['available'])} free of {gb(r['total'])}. " + (
        "Nothing runs." if not r["running"] else
        "What runs has too little recorded for a peak yet:" if not known else
        f"At their peaks (99th percentile minute), what runs could take {gb(r['need'])} more:"))
    for x in r["running"]:
        label = {"app": "", "claude": "Claude ", "job": "job "}[x["kind"]] + x["name"]
        if x["peak"] is None:
            tail = f"no peak yet ({x['minutes']} minutes recorded)"
        else:
            tail = f"peak {gb(x['peak'])}" + (f" ({x['from']})" if x["from"] else "") + f"   +{gb(x['more'])}"
        out.append(f"  {label[:44]:44} {gb(x['mem']):>8} now   {tail}")
    if known:
        out.append(f"  That leaves {gb(r['spare'])} for anything else" + (
            f", not counting {', '.join(r['unknown'])}." if r["unknown"] else "."))
    for x in r["resume"]:
        if x["idle_mem"] is None:
            out.append(f"  Resuming {x['stack']}: up to {gb(x['more'])} more (what it has in swap; "
                       f"not seen running yet).")
        else:
            out.append(f"  Resuming {x['stack']}: about {gb(x['more'])} more (idle {gb(x['idle_mem'])}, "
                       f"peak {gb(x['peak'])}).")
    if r["jobs"]:
        big = max(r["jobs"].items(), key=lambda kv: kv[1]["peak"])
        out.append(f"  A ws run job takes up to {gb(big[1]['peak'])} ({big[0]}).")
    out.append("")
    if r["stacks"]:
        out.append(f"{'Stacks (servers, containers)':34} {'idle':>8} {'active':>8} {'peak':>8}   "
                   f"active minutes   CPU idle/active")
        cores = lambda c: "–" if c is None else f"{c:.2f}"  # noqa: E731
        for n, d in sorted(r["stacks"].items()):
            out.append(f"  {n:32} {gb(d['idle_mem']):>8} {gb(d['active_mem']):>8} {gb(d['peak']):>8}   "
                       f"{share(d['active_minutes'], d['minutes']):>4} of {d['minutes']:<6}   "
                       f"{cores(d['cpu_idle'])} / {cores(d['cpu_active'])}")
        out.append("  (active: a minute at twice its median CPU and at least "
                   f"{r['active_cpu']} cores; peak at "
                   + ", ".join(f"{n} {when(d['peak_at'])}" for n, d in sorted(r["stacks"].items())) + ")")
    c = r["claude"]
    if c:
        out.append(f"Claude sessions ({r['claude_sessions']}): idle {gb(c['idle_mem'])}, active "
                   f"{gb(c['active_mem'])}, 99th percentile {gb(c['p99'])}, peak {gb(c['peak'])} "
                   f"({when(c['peak_at'])})")
    if r["jobs"]:
        out.append("Jobs: " + ", ".join(f"{n} peak {gb(d['peak'])}" for n, d in
                                        sorted(r["jobs"].items(), key=lambda kv: -kv[1]["peak"])))
    b = r["box"]
    if b:
        out.append(f"The box: highest used {gb(b['used_peak'])} ({when(b['used_peak_at'])}), lowest free "
                   f"{gb(b['avail_min'])} ({when(b['avail_min_at'])}), swap up to {gb(b['swap_peak'])}; "
                   f"tasks stalled on memory {b['stall_some_s']:.0f} s (all of them at once: {b['stall_full_s']:.0f} s).")
    if disk:
        low = (f"; lowest {gb(b['disk_free_min'])} free ({when(b['disk_free_min_at'])})"
               if b and b.get("disk_free_min") is not None else "")
        out.append(f"Disk {disk['path']}: {gb(disk['free'])} free of {gb(disk['total'])}{low}.")
    return "\n".join(out)
