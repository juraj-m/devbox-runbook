# devbox-runbook

How I run a small, disposable cloud box for coding with Claude Code: every
project's dev stack and every agent session live on it, and I use it from a
laptop, a tablet or a phone over a private network. This repo is the
runbook, written so that you, or an agent you point at it, can build your
own. It includes the scripts that do the work.

It's a **reference copy**, taken from my own setup repo and stripped of my
projects. It works for me, but it isn't a product: read it, take what fits,
adapt the rest. Expect to change things.

## What you get

```
                          the box: an Ubuntu VPS, 12 GB
 laptop ─┐               │
 tablet ─┼─ Tailscale ──►│ ssh / mosh ─► tmux ─► claude (Remote Control)
 phone  ─┘  no public    │ https://devbox.<tailnet>.ts.net:<port>
            ports        │   ─► Tailscale TLS ─► Caddy (compresses) ─► dev server
                         │
                         │ stack "shop"   a systemd slice: its servers, containers,
                         │                Claude sessions and jobs
                         │ stack "blog"   paused: frozen, its memory in swap
                         │
                         │ control page :7070   memory and CPU per stack, peaks,
                         │                      start/stop/pause/switch, sessions
                         │ outbound: only the hosts on the allowlist
```

**The main pieces.** Each has a section in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

- **A disposable box.** A cheap VPS (mine: 6 vCPU, 12 GB, 200 GB SSD,
  about $9 a month) built by one idempotent script, `box/bootstrap.sh`. It
  holds code, tools and restorable dev data. It never holds prod credentials
  or anything that exists only there. It is rebuilt, never repaired.
- **Tailscale as the only way in.** The box has no public ports once set
  up. SSH is Tailscale SSH, which asks for a fresh identity-provider login
  every 12 hours (a hardware-key tap). Every dev server port is served over
  HTTPS on the box's tailnet name with a real certificate, so a phone's
  browser gets a secure context (service workers, `crypto.subtle`, auth
  libraries all work). The access policy is a file in the repo, applied
  through the API, and its tests assert the box can reach nothing else.
- **tmux and Claude Code on the box.** You work in tmux sessions that
  survive closing the laptop. `ws claude NAME` starts a Claude Code session
  in a project with Remote Control, so you can follow and answer it from the
  Claude app on the phone. The control page shows each session's state:
  working, needs input, finished.
- **The workspace (`ws`).** One manifest, `workspace/projects.toml`, says
  how each project becomes a working environment: clone, toolchain versions
  (mise), dependency installs, env files, ports to publish. Each project's
  own repo keeps how to start, stop and verify it; the manifest only names
  those commands. `ws up NAME` builds a project; `ws start` / `ws stop` /
  `ws check` run it.
- **Stacks as systemd slices.** Each project's servers, containers, Claude
  sessions and jobs are grouped in their own cgroups. The kernel counts
  their memory and CPU, and a stack stops as a whole.
- **Measuring.** The control page and `ws top` show memory and CPU as a
  tree: base system, Docker, each stack. A sampler records every minute,
  which gives `ws peaks`: each stack's idle and peak memory, and how much
  room is left if everything running hits its peak at once.
- **Pause and swap.** 12 GB is deliberately too small to run every stack.
  `ws pause` freezes a stack (cgroup freezer, `docker pause`) and pushes its
  memory to a 64 GB swap file. `ws switch NAME` pauses the others and
  resumes NAME. A stack that takes 12 minutes to cold-start resumes in about
  20 seconds. After a reboot the stacks come back as they were.
- **Jobs.** `ws run NAME JOB` runs builds and test suites apart from the
  agent that started them, one at a time per project, under a memory cap.
  A runaway build is killed on its own, rather than the OOM killer picking
  among your servers.
- **An outbound allowlist.** A DNS server answers only for listed domains,
  and nftables lets traffic out only to the addresses it handed out. An
  agent misled by something it read can't casually send your tokens
  elsewhere. Agents ask for a domain; you approve it. It doesn't hold
  against an agent that sets out to get around it: see
  [Limitations](#limitations).
- **Scoped GitHub access.** One fine-grained token per owner (your account,
  each org), chosen per repo by a git credential helper and a `gh` wrapper.
  Your secrets repos are read-only from the box.
- **Maintenance in idle time.** Weekly full upgrades and reboots wait until
  no one is attached and no agent has printed anything for two hours.

## How to use this

**With an agent:** clone this repo, open Claude Code in it, and say "set
up my dev box following docs/SETUP.md". It will ask you for the things only
you can do: order the VPS, create the Tailscale auth key, approve GitHub
tokens.

**By hand:** read [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the why,
then follow [docs/SETUP.md](docs/SETUP.md) step by step.

**Getting your projects onto it:**
[docs/PROJECT-CHECKLIST.md](docs/PROJECT-CHECKLIST.md) lists what a project
needs to run on the box and be used from a phone's browser. Each item is
something that broke without it.

## What's here

| Path | What |
|---|---|
| `box/devbox.conf` | **The settings**: your user, home path, box name, time zone, git identity, GitHub owner, swap size. Edit first. |
| `box/bootstrap.sh` | Turns a fresh Ubuntu 24.04 VPS into the box. Idempotent: every change to the box lands by rerunning it. |
| `box/Makefile` | Run from the laptop: `bootstrap`, `update`, `sync`, `lockdown`, `github-tokens`, `egress-sync`, `status`. |
| `box/sbin/` | `devbox-egress` (the allowlist), `devbox-maintenance` (idle-time upgrades), `devbox-docker-guard` (no public container ports). |
| `box/bin/` | `devbox-github` and its git credential helper and `gh` wrapper: the right token per repo. |
| `box/systemd/` | The units: control page, Caddy, restore after boot, egress, maintenance timers. |
| `box/egress.allow` | The outbound allowlist. Add your projects' services. |
| `tailnet/` | An example access policy, and a Makefile that applies it through the Tailscale API. |
| `workspace/ws` | The workspace tool (Python 3.11+, no dependencies). |
| `workspace/control/` | The control page and its measuring: `boxtree.py` (the tree), `watch.py` (minutes, peaks, room), `server.py`, `claude_sessions.py`, `claude_usage.py`. |
| `workspace/projects.toml` | An example manifest: a web stack with a second checkout, a static site, a folder of repos. |
| `workspace/AGENTS.md` | What every Claude session on the box is told: `ws run` for heavy work, don't touch stacks you didn't start, how to ask for an egress domain. |

Tests: `bash workspace/test/ws.test.sh` and `bash box/test/devbox-egress.test.sh`.
Both run offline, on macOS or Linux.

## Assumptions

- An Ubuntu 24.04 x86-64 VPS with root SSH by key. The pinned binaries
  (sops, mise, Caddy) are amd64.
- A Tailscale account. The free plan is enough.
- GitHub for code. The token setup uses fine-grained personal access tokens.
- Your projects live in one folder (`~/_` here; `PROJECTS_DIR` in
  `devbox.conf`), the same on the laptop and the box.
- A laptop with `make`, `ssh` and `git`. Mine is a Mac; the laptop side
  needs little more than that.
- [sops](https://github.com/getsops/sops) with age keys for secrets, if
  your projects need any. The box decrypts dev and test values only.

## Limitations

**Rootless Docker is missing, and it's the biggest gap in the design.**
The dev user, which every agent runs as, is in the `docker` group. That
is root on the box in all but name: `docker run --privileged` or a
host-path mount can do anything root can, including removing the outbound
allowlist's rules. So the allowlist stops accidents and naive data leaks,
not an agent that deliberately sets out to get around it. What protects
you today is the box holding nothing of value: no prod credentials, no
prod data, tokens scoped to code and PRs.

The fix is still to be done: rootless Docker for the dev user (or a user
per project, each with its own rootless Docker), so containers run with
the user's rights and can't touch the host's firewall. It touches how
stacks are measured, paused and stopped, because containers would then
live in the user's own cgroups instead of root's.

Other limits are in
[ARCHITECTURE.md, "What it doesn't protect against"](docs/ARCHITECTURE.md#what-it-doesnt-protect-against).

## Status

This is a working system I use every day, captured at one point in time.
It changes weekly. This copy won't follow every change, so treat it as a
snapshot to adapt, not a dependency to track.

## License

MIT, see [LICENSE](LICENSE).
