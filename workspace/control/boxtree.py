"""The box's memory and CPU as a tree: what runs, grouped by what it is for.

  box
    Linux              the kernel, systemd, system services (Caddy, sshd...)
    Docker             dockerd, containerd, and containers no stack claims
    <stack>            per project in projects.toml:
      servers            ws start's units (ws-NAME-servers.slice)
      sessions           ws session's tmux and its panes (Claude, shells)
      containers         its Docker containers, by their working-dir label
      outside ws         processes started by hand, found by their folder
    Logins and other   everything else: ssh logins, shells

All numbers come from the kernel, per cgroup (/sys/fs/cgroup), never added
up from process lists: a cgroup's figure includes every process in it. The
cgroups are the systemd units: `ws start` (WS_SLICES=1) puts each stack in
its own slice, so a stack's figure is exact. What wasn't started by ws sits
in some login's cgroup; it's shown under the stack whose folder its
processes run in (most of its memory), marked "outside ws".

Memory is what stopping it would give back: memory.current less the page
cache, which the kernel drops by itself when it needs the room (shared
memory stays in: it can't be dropped without swap). Under Linux, "kernel and
uncharged" is the used memory no cgroup accounts for, so the stacks and the
rest add up to the box's used memory (MemTotal - MemAvailable). CPU is in
cores (1.0 = one core busy), over the interval between two samples.

Processes are listed with their resident memory (VmRSS): a detail inside a
unit, not a share of it (shared pages are counted in every process).

Disk is the root filesystem's (statvfs, what a non-root user may still
write), with its big users: swap files (/proc/swaps) and Docker's images,
containers, volumes and build cache (its API's /system/df, slow, so asked
at most every DOCKER_DF_EVERY seconds). Under LOW_DISK free, it is "low".

Reads only: /sys/fs/cgroup, /proc, the filesystem's statvfs and the Docker
API. No root needed.
"""
import http.client
import json
import os
import re
import socket
import time

CG_ROOT = os.environ.get("WS_CGROUP_ROOT", "/sys/fs/cgroup")
PROC_ROOT = os.environ.get("WS_PROC_ROOT", "/proc")
DOCKER_SOCK = os.environ.get("WS_DOCKER_SOCK", "/var/run/docker.sock")
DISK_PATH = os.environ.get("WS_DISK_PATH", "/")
LOW_DISK = 20 * 2**30
DOCKER_DF_EVERY = 600
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
HOME = os.path.expanduser("~")
WORKDIR_LABELS = ("com.docker.compose.project.working_dir", "com.supabase.cli.workdir")
SHELLS = {"zsh", "bash", "sh", "dash", "fish"}
PLUMBING = SHELLS | {"login", "sshd", "tmux", "tailscaled", "mosh-server", "sudo"}
DOCKER_UNITS = {"docker.service": "docker", "containerd.service": "containerd", "docker.socket": "docker.socket"}


def read(path, default=""):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


# ---------------------------------------------------------------- sampling

def snapshot(cg_root=CG_ROOT, proc_root=PROC_ROOT, cpus=None, disk_path=DISK_PATH):
    """Everything build() needs, read once."""
    groups = {}
    for dirpath, _, files in os.walk(cg_root):
        rel = os.path.relpath(dirpath, cg_root)
        if rel == "." or "cgroup.procs" not in files:
            continue
        stat = dict(l.split(None, 1) for l in read(f"{dirpath}/memory.stat").splitlines() if " " in l)
        cpu = dict(l.split(None, 1) for l in read(f"{dirpath}/cpu.stat").splitlines() if " " in l)
        groups[rel] = {
            "current": int(read(f"{dirpath}/memory.current", "0") or 0),
            "swap": int(read(f"{dirpath}/memory.swap.current", "0") or 0),
            "frozen": read(f"{dirpath}/cgroup.freeze").strip() == "1",
            "file": int(stat.get("file", 0)),
            "shmem": int(stat.get("shmem", 0)),
            "usec": int(cpu.get("usage_usec", 0)),
            "pids": [int(p) for p in read(f"{dirpath}/cgroup.procs").split()],
        }
    procs = {}
    for g in groups.values():
        for pid in g["pids"]:
            p = process(proc_root, pid)
            if p:
                procs[pid] = p
    mem = {}
    for line in read(f"{proc_root}/meminfo").splitlines():
        k, _, v = line.partition(":")
        if v.strip():
            mem[k] = int(v.split()[0]) * 1024
    ticks = [int(x) for x in (read(f"{proc_root}/stat").splitlines() or ["cpu 0"])[0].split()[1:]]
    idle = sum(ticks[3:5])
    st = os.statvfs(disk_path)
    # /proc/swaps sizes are in KiB; only a file takes room on the filesystem.
    swapfiles = [[f[0], int(f[2]) * 1024] for f in
                 (l.split() for l in read(f"{proc_root}/swaps").splitlines()[1:])
                 if len(f) >= 3 and f[1] == "file"]
    return {"t": time.monotonic(), "groups": groups, "procs": procs,
            "mem_total": mem.get("MemTotal", 0), "mem_available": mem.get("MemAvailable", 0),
            "swap_used": mem.get("SwapTotal", 0) - mem.get("SwapFree", 0),
            "cpu_busy": sum(ticks) - idle, "cpu_all": sum(ticks),
            "cpus": cpus or os.cpu_count() or 1,
            "disk": {"path": disk_path, "total": st.f_blocks * st.f_frsize, "free": st.f_bavail * st.f_frsize},
            "swapfiles": swapfiles}


def process(proc_root, pid):
    d = f"{proc_root}/{pid}"
    stat = read(f"{d}/stat")
    if not stat:
        return None  # gone since cgroup.procs was read
    # The name in (parentheses) may hold spaces and parentheses itself.
    rest = stat[stat.rfind(")") + 2:].split()
    rss = next((int(l.split()[1]) * 1024 for l in read(f"{d}/status").splitlines()
                if l.startswith("VmRSS:")), 0)
    argv = [a for a in read(f"{d}/cmdline").split("\0") if a]
    try:
        cwd = os.readlink(f"{d}/cwd")
    except OSError:
        cwd = ""
    return {"ppid": int(rest[1]), "ticks": int(rest[11]) + int(rest[12]), "rss": rss,
            "argv": argv or [stat[stat.find("(") + 1:stat.rfind(")")]], "cwd": cwd}


def docker_containers(sock=DOCKER_SOCK):
    """The running containers (Docker API), or None if Docker can't be asked."""
    class Conn(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(3)
            self.sock.connect(sock)
    try:
        c = Conn("docker")
        c.request("GET", "/containers/json")
        r = c.getresponse()
        return json.loads(r.read()) if r.status == 200 else None
    except (OSError, ValueError, http.client.HTTPException):
        return None


def docker_df(sock=DOCKER_SOCK):
    """Docker's disk use (its API's /system/df), or None if it can't be asked.
    It sizes every layer and volume: seconds, not milliseconds."""
    class Conn(http.client.HTTPConnection):
        def connect(self):
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.settimeout(30)
            self.sock.connect(sock)
    try:
        c = Conn("docker")
        c.request("GET", "/system/df")
        r = c.getresponse()
        return json.loads(r.read()) if r.status == 200 else None
    except (OSError, ValueError, http.client.HTTPException):
        return None


def docker_disk_of(df):
    """Bytes Docker holds on disk: image layers, containers' own layers,
    volumes and build cache. None for no answer."""
    if df is None:
        return None
    n = df.get("LayersSize") or 0
    n += sum(c.get("SizeRw") or 0 for c in df.get("Containers") or ())
    n += sum(max((v.get("UsageData") or {}).get("Size") or 0, 0) for v in df.get("Volumes") or ())
    n += sum(b.get("Size") or 0 for b in df.get("BuildCache") or ())
    return n


_docker_disk = {"at": None, "size": None}


def docker_disk(sock=DOCKER_SOCK, every=DOCKER_DF_EVERY):
    """docker_disk_of(docker_df()), asked at most every EVERY seconds."""
    now = time.monotonic()
    if _docker_disk["at"] is None or now - _docker_disk["at"] >= every:
        _docker_disk.update(at=now, size=docker_disk_of(docker_df(sock)))
    return _docker_disk["size"]


def sample(stacks, interval=1.0, prev=None, jobs=()):
    """A tree with CPU: two snapshots, interval apart (or one, after prev)."""
    if prev is None:
        prev = snapshot()
        time.sleep(interval)
    return build(stacks, snapshot(), prev, docker_containers(), jobs, docker_disk=docker_disk())


# ---------------------------------------------------------------- the tree

def unescape(name):
    return re.sub(r"\\x([0-9a-fA-F]{2})", lambda m: chr(int(m.group(1), 16)), name)


def units(groups):
    """The cgroups everything is counted in, none inside another: systemd's
    services and scopes. Slices are only folders of them (and so is a user's
    manager, user@UID.service, which holds that user's slices)."""
    kids = {}
    for path in groups:
        while path:
            parent = os.path.dirname(path)
            if path in kids.setdefault(parent, []):
                break
            kids[parent].append(path)
            path = parent
    out = []

    def walk(path):
        name = os.path.basename(path)
        children = kids.get(path, [])
        if children and (name.endswith(".slice") or re.fullmatch(r"user@\d+\.service", name) or path == ""):
            for c in sorted(children):
                walk(c)
        elif name.endswith(".slice"):
            pass  # an emptied folder (a jobs slice after its last job): systemd keeps no processes in slices
        elif path:
            out.append(path)
    walk("")
    return out


def short_cmd(argv):
    """A command line to read: programs by name, the home folder as ~,
    anything inside node_modules by its file name."""
    out = []
    for i, a in enumerate(argv):
        if a.startswith("/") and (i == 0 or "/node_modules/" in a):
            a = os.path.basename(a)
        elif a.startswith(HOME + "/"):
            a = "~" + a[len(HOME):]
        out.append(a)
    # One line: a script's continuation lines and newlines become spaces.
    s = " ".join(" ".join(out).replace("\\\n", " ").split())
    return s if len(s) <= 120 else s[:119] + "…"


def owner(path, stacks):
    """The stack whose folder path is in (the deepest one)."""
    best = None
    for name, d in stacks.items():
        if path == d or path.startswith(d.rstrip("/") + "/"):
            if best is None or len(d) > len(stacks[best]):
                best = name
    return best


class Builder:
    def __init__(self, stacks, cur, prev):
        self.stacks, self.cur, self.prev = stacks, cur, prev
        self.dt = (cur["t"] - prev["t"]) if prev else None

    def mem(self, path):
        g = self.cur["groups"][path]
        cache = max(g["file"] - g["shmem"], 0)
        return g["current"] - cache, cache

    def cpu(self, path):
        if not self.dt or path not in self.prev["groups"]:
            return None
        return max(self.cur["groups"][path]["usec"] - self.prev["groups"][path]["usec"], 0) / 1e6 / self.dt

    def proc_cpu(self, pid):
        if not self.dt or pid not in self.prev["procs"]:
            return None
        return max(self.cur["procs"][pid]["ticks"] - self.prev["procs"][pid]["ticks"], 0) / CLK_TCK / self.dt

    def processes(self, path):
        procs = self.cur["procs"]
        pids = [p for p in self.cur["groups"][path]["pids"] if p in procs]
        nodes = {p: {"name": short_cmd(procs[p]["argv"]), "kind": "process", "pid": p,
                     "mem": procs[p]["rss"], "cpu": self.proc_cpu(p), "children": []} for p in pids}
        roots = []
        for p in pids:
            parent = procs[p]["ppid"]
            (nodes[parent]["children"] if parent in nodes else roots).append(nodes[p])
        # tree_mem: a process and everything under it, so a small parent
        # (npm, sh -c) of a big child is not mistaken for something small.
        def total(n):
            n["tree_mem"] = n["mem"] + sum(total(c) for c in n["children"])
            n["children"].sort(key=lambda c: -c["tree_mem"])
            return n["tree_mem"]
        for r in roots:
            total(r)
        return sorted(roots, key=lambda c: -c["tree_mem"])

    def unit(self, path, name, kind="unit", **extra):
        mem, cache = self.mem(path)
        return {"name": name, "kind": kind, "unit": os.path.basename(path), "path": path,
                "mem": mem, "cache": cache, "swap": self.cur["groups"][path].get("swap", 0),
                "cpu": self.cpu(path),
                "children": self.processes(path), **extra}

    def root_cmd(self, path):
        """What a login or pane is running: its first process, or, when that
        is a shell running something, the something."""
        roots = self.processes(path)
        if not roots:
            return "(empty)"
        r = roots[0]
        if r["children"] and r["name"].split()[0].lstrip("-") in SHELLS:
            return r["children"][0]["name"]
        return r["name"]

    def folder_owner(self, path):
        """The stack most of a cgroup's processes' memory runs in, if any.
        Shells and login plumbing don't count: a shell sitting in a stack's
        folder is a login, not the stack's work."""
        procs = self.cur["procs"]
        by, total = {}, 0
        for pid in self.cur["groups"][path]["pids"]:
            if pid in procs and os.path.basename(procs[pid]["argv"][0]).lstrip("-") not in PLUMBING:
                total += procs[pid]["rss"]
                s = owner(procs[pid]["cwd"], self.stacks)
                if s:
                    by[s] = by.get(s, 0) + procs[pid]["rss"]
        if not by:
            return None
        best = max(by, key=by.get)
        return best if by[best] * 2 >= total else None


def group(name, kind, children, **extra):
    children = [c for c in children if c is not None]
    cpus = [c["cpu"] for c in children]
    return {"name": name, "kind": kind, "mem": sum(c["mem"] for c in children),
            "cache": sum(c.get("cache", 0) for c in children),
            "swap": sum(c.get("swap", 0) for c in children),
            "cpu": None if any(c is None for c in cpus) or not children else sum(cpus),
            "children": sorted(children, key=lambda c: -c["mem"]), **extra}


PARTS = ("servers", "jobs", "sessions", "containers", "outside ws")


def add_jobs(per, metas, now, recent):
    """ws run's jobs (their meta.json, from ws's state folder): a running one
    gets its state on its unit's node; one that ended in the last `recent`
    seconds is a row of its own, saying how it ended."""
    for m in metas:
        stack, job = m.get("stack"), m.get("job")
        if not stack or not job:
            continue
        parts = per.setdefault(stack, {k: [] for k in PARTS})
        start = m.get("started_at")
        info = {"state": m.get("state"), "exit": m.get("exit"), "cmd": m.get("cmd"), "oom": m.get("oom"),
                "elapsed": int((m.get("ended_at") or now) - start) if start else None}
        node = next((n for n in parts["jobs"] if n["name"] == job and n.get("unit") == m.get("unit")), None)
        if node is not None:
            node.update(info)
            continue
        ended = m.get("ended_at")
        if m.get("state") in ("queued", "running"):
            info["state"] = "lost"  # no unit holds it any more
        elif not ended or now - ended > recent:
            continue
        parts["jobs"].append({"name": job, "kind": "job", "mem": 0, "cache": 0, "cpu": 0,
                              "children": [], **info})


def disk(cur, docker_size=None):
    """The root filesystem: total, free, used, low, and its big users."""
    d = cur.get("disk")
    if not d:
        return None
    users = [{"name": f"swap file {name}", "size": size} for name, size in cur.get("swapfiles", ())]
    if docker_size is not None:
        users.append({"name": "Docker", "size": docker_size})
    return {"path": d.get("path", "/"), "total": d["total"], "free": d["free"],
            "used": d["total"] - d["free"], "low": d["free"] < LOW_DISK,
            "users": sorted(users, key=lambda u: -u["size"])}


def build(stacks, cur, prev=None, containers=(), jobs=(), now=None, recent=12 * 3600, docker_disk=None):
    b = Builder(stacks, cur, prev)
    by_id = {c["Id"]: c for c in containers or ()}
    base, docker, rest = [], [], []
    per = {s: {k: [] for k in PARTS} for s in stacks}

    for path in units(cur["groups"]):
        parts = path.split("/")
        name = parts[-1]
        g = cur["groups"][path]
        # Mounts, sockets, finished units: nothing in them. ws's own units
        # stay, even empty: a start whose servers all died says so.
        if not g["pids"] and g["current"] < 2**20 and "ws.slice" not in parts:
            continue
        i = parts.index("ws.slice") + 1 if "ws.slice" in parts else 0
        ws = parts[i] if 0 < i < len(parts) - 1 and parts[i].endswith(".slice") else None
        if ws:
            esc_stack = ws[3:-len(".slice")]
            stack = unescape(esc_stack)
            part = next((k for k in ("servers", "jobs", "sessions")
                         if f"ws-{esc_stack}-{k}.slice" in parts), "sessions")
            label = unescape(re.sub(r"\.(service|scope)$", "", name))
            if label.startswith(f"ws-{stack}-"):
                label = label[len(f"ws-{stack}-"):]
                if part == "jobs" and label.startswith("job-"):
                    label = label[len("job-"):]
            elif name.startswith("tmux-spawn-"):
                label = f"tmux pane: {b.root_cmd(path)}"
            per.setdefault(stack, {k: [] for k in PARTS})
            per[stack][part].append(b.unit(path, label))
        elif parts[0] == "system.slice" and name.startswith("docker-") and name.endswith(".scope"):
            cid = name[len("docker-"):-len(".scope")]
            c = by_id.get(cid)
            full = c["Names"][0].lstrip("/") if c and c.get("Names") else f"container {cid[:12]}"
            labels = (c or {}).get("Labels") or {}
            stack = next((owner(labels[k], stacks) for k in WORKDIR_LABELS if labels.get(k)), None)
            # supabase_realtime_shop-supabase: the project is the
            # stack's already; the rest says which container.
            project = labels.get("com.docker.compose.project", "")
            label = full[:-len(project) - 1] if project and full.endswith("_" + project) else full
            node = b.unit(path, label, kind="container", container=full,
                          paused=(c or {}).get("State") == "paused")
            (per[stack]["containers"] if stack else docker).append(node)
        elif parts[0] == "system.slice" and name in DOCKER_UNITS:
            docker.append(b.unit(path, DOCKER_UNITS[name]))
        elif parts[0] == "user.slice" and not (name == "init.scope" or "session.slice" in parts):
            # Logins, tmux panes, user apps: whoever started them.
            label = name
            m = re.fullmatch(r"session-(\w+)\.scope", name)
            if m:
                label = f"login session {m.group(1)}: {b.root_cmd(path)}"
            elif name.startswith("tmux-spawn-"):
                label = f"tmux pane: {b.root_cmd(path)}"
            node = b.unit(path, label)
            stack = b.folder_owner(path)
            (per[stack]["outside ws"] if stack else rest).append(node)
        else:
            label = re.sub(r"\.(service|scope)$", "", name)
            if path == "init.scope":
                label = "systemd"
            elif name == "init.scope":
                label = "systemd --user"
            base.append(b.unit(path, unescape(label)))

    used = cur["mem_total"] - cur["mem_available"]
    box_cpu = None
    if prev and cur["cpu_all"] > prev["cpu_all"]:
        box_cpu = (cur["cpu_busy"] - prev["cpu_busy"]) / (cur["cpu_all"] - prev["cpu_all"]) * cur["cpus"]

    add_jobs(per, jobs or (), time.time() if now is None else now, recent)
    # ws pause freezes a stack's servers slice (a folder, not a unit).
    paused = {unescape(m.group(1)) for path, g in cur["groups"].items() if g.get("frozen")
              for m in [re.fullmatch(r"ws-(.+)-servers\.slice", os.path.basename(path))] if m}
    stack_nodes = []
    for s, parts in per.items():
        kids = [group(k, "part", v) for k, v in parts.items() if v]
        if kids:
            stack_nodes.append(group(s, "stack", kids, dir=stacks.get(s), paused=s in paused))
    stack_nodes.sort(key=lambda n: -n["mem"])
    docker_node = group("Docker", "docker", docker)
    if containers is None:
        docker_node["note"] = "Docker API unreachable: containers unnamed and unassigned"
    top = [docker_node, *stack_nodes, group("Logins and other", "other", rest)]

    charged = sum(n["mem"] for n in top) + sum(n["mem"] for n in base)
    charged_cpu = sum(n["cpu"] or 0 for n in top) + sum(n["cpu"] or 0 for n in base)
    kernel = {"name": "kernel and uncharged", "kind": "kernel", "mem": max(used - charged, 0), "cache": 0,
              "cpu": None if box_cpu is None else max(box_cpu - charged_cpu, 0), "children": [],
              "note": "used memory no cgroup accounts for: the kernel's own, page tables, buffers"}
    linux = group("Linux", "base", [kernel, *base])
    linux["children"].sort(key=lambda c: (c["kind"] != "kernel", -c["mem"]))
    return {"name": os.uname().nodename, "kind": "box", "mem": used, "total": cur["mem_total"],
            "available": cur["mem_available"], "swap_used": cur.get("swap_used", 0),
            "cpu": box_cpu, "cpus": cur["cpus"], "disk": disk(cur, docker_disk),
            "children": [linux, *(n for n in top if n["children"] or n.get("note"))]}


# ---------------------------------------------------------------- text

def fmt_mem(n):
    if 0 < n < 2**20:
        return "<1 MB"
    return f"{n / 2**30:.1f} GB" if n >= 2**30 else f"{n / 2**20:.0f} MB"


def job_label(node):
    """running 3m12s, failed (exit 1) 40s, ok 2m05s."""
    s = node["state"]
    if s == "failed" and node.get("oom"):
        s += ": over the memory cap"
    elif s == "failed" and node.get("exit") is not None:
        s += f" (signal {-node['exit']})" if node["exit"] < 0 else f" (exit {node['exit']})"
    e = node.get("elapsed")
    if e is not None:
        s += f" {e // 60}m{e % 60:02d}s" if e >= 60 else f" {e}s"
    return s


def render(tree, min_process=20 * 2**20):
    cpu = f" · CPU {tree['cpu']:.1f} of {tree['cpus']} cores" if tree["cpu"] is not None else ""
    lines = [f"{tree['name']}  {fmt_mem(tree['mem'])} used of {fmt_mem(tree['total'])} "
             f"({fmt_mem(tree['available'])} available){cpu}"]
    d = tree.get("disk")
    if d:
        low = f": low, under {LOW_DISK // 2**30} GB" if d["low"] else ""
        lines.append(f"disk {d['path']}  {fmt_mem(d['used'])} used of {fmt_mem(d['total'])} "
                     f"({fmt_mem(d['free'])} free{low})"
                     + "".join(f" · {u['name']} {fmt_mem(u['size'])}" for u in d["users"]))

    def walk(node, depth):
        small = [c for c in node["children"] if c["kind"] == "process" and c["tree_mem"] < min_process]
        for c in node["children"]:
            if c in small:
                continue
            label = "  " * depth + c["name"]
            if c["kind"] == "process":
                label += f"  [{c['pid']}]"
            if c.get("state"):
                label += f"  {job_label(c)}"
            if c.get("paused") and c["kind"] == "stack":
                label += f"  paused, {fmt_mem(c['swap'])} in swap"
            cpu = "" if c["cpu"] is None else f"{c['cpu']:5.2f}"
            lines.append(f"{label[:78]:78} {fmt_mem(c['mem']):>8} {cpu:>5}")
            if c.get("note"):
                lines.append("  " * (depth + 1) + f"({c['note']})")
            walk(c, depth + 1)
        if small:
            lines.append("  " * depth + f"+ {len(small)} smaller processes")
    walk(tree, 1)
    return "\n".join(lines)
