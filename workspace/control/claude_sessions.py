"""Claude Code sessions launched in a stack (ws claude NAME, the page's
"New Claude session"): what each is doing, from its own hooks.

ws claude starts `claude --remote-control` in the stack's tmux (a tmux
session of its own per Claude session, so several can run and each is
attached to alone), with a session id ws chose, and with hooks (`claude
--settings`, that session's only) that run `ws claude-hook`: each hook event
writes the session's state, keyed by that id. So the page knows, per
session:

  working      it's on a turn: a prompt came in, or a tool ran
  needs input  it asked something (AskUserQuestion), or waits on a permission
  finished     its turn ended (Stop), or it's ready for a first prompt
  ended        its tmux session is gone (claude exited)

Files, under STATE/claude/ (~/.local/state/ws/claude):
  launched/<id>.json   what ws claude started: stack, tmux socket and session,
                       name, when, its Remote Control link once seen
  live/<id>.json       the latest state the hooks wrote

The Remote Control link (https://claude.ai/code/session_...): claude prints
it in its terminal, so it's read from the tmux pane (find_url()).
"""
import json
import os
import re
import subprocess
import sys
import time

STATE = os.environ.get("WS_STATE", os.path.expanduser("~/.local/state/ws"))
# Seconds a launched session may take to say anything (its first hook).
STARTING = 45
URL = re.compile(r"https://claude\.ai/code/[A-Za-z0-9_?=&./-]+")


def folder(state, part):
    return os.path.join(state, "claude", part)


def write_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.new"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, path)


def read(path):
    try:
        with open(path) as f:
            d = json.load(f)
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- hooks

# What each hook event says the session is doing. Notification and the
# tool events depend on what they're about (state_of()).
EVENTS = ("SessionStart", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
          "PermissionRequest", "Notification", "Stop", "StopFailure", "SessionEnd")
# Notifications that mean it waits on me (code.claude.com/docs/en/hooks).
WAITS = {"permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input"}
# A tool that is a question to the user: needs input until it's answered.
ASKS = {"AskUserQuestion"}


def state_of(event):
    """The state a hook event's input means, or None to leave it as is."""
    name = event.get("hook_event_name")
    if name == "SessionStart":
        return "finished"  # ready for a prompt
    if name == "UserPromptSubmit":
        return "working"
    if name == "PreToolUse":
        return "needs input" if event.get("tool_name") in ASKS else "working"
    if name in ("PostToolUse", "PostToolUseFailure"):
        return "working"  # an answered question or permission included
    if name == "PermissionRequest":
        return "needs input"
    if name == "Notification":
        kind = event.get("notification_type") or ""
        if kind in WAITS:
            return "needs input"
        # idle_prompt: it has waited for the next prompt a while (~60 s).
        # Mostly after a Stop already; but an interrupt (Esc) fires no Stop,
        # and this is what ends its "working".
        return "finished" if kind == "idle_prompt" else None
    if name in ("Stop", "StopFailure"):
        return "finished"
    if name == "SessionEnd":
        return "ended"
    return None


def hook(event, state=STATE, now=None):
    """Record what a hook event says (ws claude-hook, with its input on
    stdin). Returns the state written, or None."""
    sid = event.get("session_id")
    if not isinstance(sid, str) or not re.fullmatch(r"[A-Za-z0-9-]{8,64}", sid):
        return None
    st = state_of(event)
    if st is None:
        return None
    rec = {"state": st, "event": event.get("hook_event_name"), "at": time.time() if now is None else now}
    if event.get("hook_event_name") == "Notification" and event.get("message") and st == "needs input":
        rec["message"] = str(event["message"])[:200]
    if event.get("hook_event_name") == "StopFailure":
        rec["message"] = "its turn ended with an error"
    if event.get("hook_event_name") == "PreToolUse" and event.get("tool_name") in ASKS:
        rec["message"] = "asks a question"
    # A question also brings its notification ("Claude needs your
    # permission"): the question says more.
    prev = read(os.path.join(folder(state, "live"), f"{sid}.json")) or {}
    if (event.get("hook_event_name") == "Notification" and prev.get("state") == "needs input"
            and prev.get("message") == "asks a question"):
        rec["message"] = "asks a question"
    write_atomic(os.path.join(folder(state, "live"), f"{sid}.json"), rec)
    remember_url(sid, state)
    return st


def remember_url(sid, state=STATE, env=os.environ):
    """A hook runs in the session's own tmux pane (TMUX, TMUX_PANE): where
    ws claude didn't see the Remote Control link at launch, take it from
    the pane now."""
    path = os.path.join(folder(state, "launched"), f"{sid}.json")
    rec = read(path)
    if not rec or rec.get("url") or not env.get("TMUX_PANE"):
        return
    r = subprocess.run(["tmux", "capture-pane", "-p", "-J", "-S", "-200", "-t", env["TMUX_PANE"]],
                       capture_output=True, text=True, timeout=5)
    m = URL.findall(r.stdout or "")
    if m:
        rec["url"] = m[-1].rstrip(".,)")
        write_atomic(path, rec)


def settings(ws_path):
    """The --settings JSON a launched session gets: its hooks, each running
    `ws claude-hook` (fast, and never fails the session)."""
    cmd = f"{json.dumps(sys.executable)} -I {json.dumps(ws_path)} claude-hook"
    entry = [{"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}]
    tool_entry = [{"matcher": "*", "hooks": [{"type": "command", "command": cmd, "timeout": 10}]}]
    hooks = {e: (tool_entry if e in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionRequest")
                 else entry) for e in EVENTS}
    return {"hooks": hooks}


def hook_main():
    """ws claude-hook: never loud, never fails: a hook that errs would show
    in the session."""
    try:
        event = json.load(sys.stdin)
        if isinstance(event, dict):
            hook(event)
    except Exception:  # noqa: BLE001
        pass
    return 0


# ---------------------------------------------------------------- the tmux side

def tmux(socket, *args):
    return subprocess.run(["tmux", "-L", socket, *args], capture_output=True, text=True)


def proc_start(pid, proc="/proc"):
    """A process's start time (clock ticks since boot), or None if gone:
    with the pid, it names the process (a pid is reused, the pair isn't)."""
    try:
        with open(f"{proc}/{pid}/stat") as f:
            s = f.read()
        return int(s[s.rindex(")") + 2:].split()[19])
    except (OSError, ValueError, IndexError):
        return None


def alive(rec, proc="/proc"):
    """Whether the session's pane still runs: by its process, which the
    control server can see (it can't reach tmux: its /tmp is its own)."""
    return bool(rec.get("pid")) and proc_start(rec["pid"], proc) == rec.get("pid_start")


def find_url(rec):
    """The Remote Control link claude printed in its pane, if it has."""
    r = tmux(rec["socket"], "capture-pane", "-p", "-J", "-S", "-200", "-t", f"={rec['tmux']}:")
    m = URL.findall(r.stdout or "")
    return m[-1].rstrip(".,)") if m else None


def trust_prompt(rec):
    """Whether its pane shows claude's "trust this folder?" question."""
    r = tmux(rec["socket"], "capture-pane", "-p", "-J", "-t", f"={rec['tmux']}:")
    return "Yes, I trust this folder" in (r.stdout or "")


# ---------------------------------------------------------------- reading

def sessions(state=STATE, stack=None, is_alive=alive, url_of=find_url, keep_ended=12 * 3600, now=None,
             write=True):
    """The launched sessions, newest first: their record, their state (from
    the hooks; "ended" once its process is gone), and the link once seen
    (looked for while missing, and kept). Ended ones are listed for
    keep_ended, then their files removed. write=False (the control server,
    whose file system is read-only): nothing kept or removed, and no link
    looked for (url_of=None)."""
    now = time.time() if now is None else now
    d = folder(state, "launched")
    out = []
    for n in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not n.endswith(".json"):
            continue
        rec = read(os.path.join(d, n))
        if not rec or (stack and rec.get("stack") != stack):
            continue
        live = read(os.path.join(folder(state, "live"), n)) or {}
        up = is_alive(rec)
        if not up:
            ended = rec.get("ended_at") or live.get("at") or now
            if not write:
                rec["ended_at"] = ended
            elif not rec.get("ended_at"):
                rec["ended_at"] = ended
                write_atomic(os.path.join(d, n), rec)
            if now - ended > keep_ended:
                for f in (os.path.join(d, n), os.path.join(folder(state, "live"), n)):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                continue
        elif not rec.get("url") and url_of:
            url = url_of(rec)
            if url:
                rec["url"] = url
                if write:
                    write_atomic(os.path.join(d, n), rec)
        # No hook yet: starting, or held up in its terminal (a folder it
        # hasn't been told to trust asks first, and holds the hooks back).
        state_now = "ended" if not up else live.get("state") or (
            "starting" if now - rec.get("started_at", now) < STARTING else "needs input")
        if up and not live and rec.get("waiting") == "trust":
            state_now, live = "needs input", {"message": "asks whether to trust its folder"}
        elif up and not live and state_now == "needs input":
            live = {"message": "no word from it yet: look in its terminal"}
        out.append({**rec, "state": state_now, "state_at": live.get("at") if up else rec.get("ended_at"),
                    "message": live.get("message") if up and state_now == "needs input" else None,
                    # No "=" (tmux's exact match) here: run in zsh, the box's
                    # login shell, "=claude-1" means the path of a command.
                    "attach": f"tmux -L {rec['socket']} attach -t {rec['tmux']}",
                    # Still asking: no hook yet (a trusted folder's session has spoken).
                    "waiting": rec.get("waiting") if up and not live.get("state") else None})
    return sorted(out, key=lambda r: -r.get("started_at", 0))
