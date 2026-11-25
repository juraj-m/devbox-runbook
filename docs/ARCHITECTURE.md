# Architecture

How the box works, piece by piece, and why it's built this way. Each
section names the files that implement it. Numbers are from my box
(6 vCPU, 12 GB, 200 GB SSD), measured while building it.

- [Principles](#principles)
- [The box](#the-box)
- [Tailscale: the only way in](#tailscale-the-only-way-in)
- [Serving dev servers to other devices](#serving-dev-servers-to-other-devices)
- [Sessions: ssh, mosh, tmux, Claude Code](#sessions-ssh-mosh-tmux-claude-code)
- [The workspace: ws and projects.toml](#the-workspace-ws-and-projectstoml)
- [Stacks: one systemd slice each](#stacks-one-systemd-slice-each)
- [Measuring: the tree, the minutes, the peaks](#measuring-the-tree-the-minutes-the-peaks)
- [The control page](#the-control-page)
- [Pause, swap, switch, restore](#pause-swap-switch-restore)
- [Jobs](#jobs)
- [Claude sessions in a stack](#claude-sessions-in-a-stack)
- [The Claude plan's usage](#the-claude-plans-usage)
- [Outbound allowlist](#outbound-allowlist)
- [GitHub access](#github-access)
- [Secrets](#secrets)
- [Maintenance in idle time](#maintenance-in-idle-time)
- [What it doesn't protect against](#what-it-doesnt-protect-against)

## Principles

- **Disposable.** The box holds code, tools and data that can be restored.
  Deleting it loses nothing. It is never repaired by hand: a change goes
  into `bootstrap.sh` and is rolled out by rerunning it (`make update`). A
  broken box is reinstalled and bootstrapped again.
- **No prod on the box.** No prod credentials, keys, deploy tokens or prod
  data. Its secrets are dev and test values only. Then a compromised box, or
  an agent that does something stupid, can't reach production. That is also
  what makes the next point acceptable.
- **Agents get a lot of room.** The dev user is in the `docker` group,
  which is effectively root on the box. Agents run with broad permissions in
  their own stacks. The box is the blast radius, and it's built to be
  thrown away.
- **Small on purpose.** 12 GB is less than all my stacks need at once.
  Living with that limit (seeing what uses memory, pausing what isn't in
  use, switching quickly) was part of the point. How big a box really needs
  to be is answered from measured peaks, not by buying one big enough to
  run everything.
- **The laptop is a thin client.** A terminal, a browser and Tailscale.
  The same works from a tablet or phone.
- **The repo is the source of truth.** The box's setup, the tailnet's
  access policy and the outbound allowlist are files in this repo. Changes
  made on the box are copied back into the repo (`make egress-sync`).

## The box

**Provider and size.** Any VPS that installs Ubuntu 24.04 with your SSH
key. Mine is a Contabo Cloud VPS (6 vCPU, 12 GB RAM, 200 GB SSD, about $9 a
month). Prefer NVMe if offered: `node_modules` installs and builds are
disk-bound. Pick a region near you, because latency is what you feel in a
dev server. Check that the provider has a web console or rescue mode. It's
your way back in if Tailscale breaks, since public SSH will be closed.

**`box/bootstrap.sh`**, run as root, does everything in order. Each step
is safe to repeat:

| Step | What |
|---|---|
| Packages | git, tmux, mosh, zsh, jq, ripgrep, htop, build tools, ufw, age, dnsmasq, and the libraries a headless Chromium needs (Playwright's list for 24.04) |
| User | `DEV_USER` with home `DEV_HOME`, zsh, in the `docker` group, **no sudo** (admin is `ssh root@devbox` over Tailscale SSH). Lingering on, so its systemd (and the stacks) outlive logins. |
| sshd | Key-only, no passwords, root by key only. A `00-` drop-in, so it wins over a provider's cloud-init drop-in that enables passwords. Refuses to run if root has no key. |
| Tailscale | Joins with a one-off auth key as `tag:dev`, with Tailscale SSH on. Sets the dev user as operator, so it can run `tailscale serve`. |
| Firewall | ufw denies all incoming except `tailscale0` and Tailscale's UDP port (41641, for direct connections). Public 22 stays open until `make lockdown`. |
| Swap | A swap file of `SWAP_SIZE` (64 GB): where paused stacks go. Resizes it if the setting changes. |
| Docker | Published ports default to `127.0.0.1`. `devbox-docker-guard` drops new public connections to containers that bind `0.0.0.0` anyway, because Docker-published ports skip ufw. Log driver `local`. |
| Tools | gh, sops and age-plugin-yubikey, mise, Caddy. Every binary without an apt repo is pinned and checksum-verified. |
| git | Your identity; HTTPS for GitHub through the token helper; SSH remote URLs rewritten to HTTPS (the box has no GitHub SSH key). |
| Claude Code | The native installer (it updates itself). `workspace/AGENTS.md` linked into `~/.claude/rules/`, plus your rules, skills and agents if the repo has them. Status line set to `ws statusline`. |
| Units | Control page, Caddy, restore at boot, egress, maintenance timers, filled from `devbox.conf`. |
| Egress | Installs the allowlist and applies it in its current mode. |

**Same home path as the laptop.** Claude Code keys each project's memory by
absolute path (`~/.claude/projects/-Users-you---shop`). With
`DEV_HOME=/Users/you` on the box, paths match the laptop's, and so does
anything that mentions them.

**The dev user's environment** goes in `~/.zshenv`, which every zsh reads,
including non-interactive ones (`ssh devbox cmd`, Claude's shell, the
systemd units that start a zsh). Bootstrap rewrites a marked block there on
every run: PATH (mise shims, `ws`), `WS_ROOT`, `WS_TAILNET=1`,
`DEV_BIND_HOST=127.0.0.1`, `WS_SLICES=1`, the GitHub owner settings.

**Changing the box.** Edit this repo, commit, push, `make update`. It
first runs `make sync`, which brings the box's clone of this repo to the
commit the laptop is on. The box runs `ws`, the control page and the skills
from that clone. Sync refuses if the commit isn't `origin/main` or the box's
clone has commits of its own. Then the Makefile ships `box/` as a tarball
and reruns bootstrap.

## Tailscale: the only way in

Every device (laptop, phone, tablet, the box) joins one tailnet and gets a
stable `100.x` address and a MagicDNS name. Nothing on the box is reachable
from the internet once public SSH is closed.

**Sign-in.** Tailscale has no passwords of its own; it signs in through an
identity provider. I use GitHub with two hardware keys as the only second
factors, recovery codes printed and offline. I chose GitHub over Google
because a Google account usually has more ways in (other devices, phone
recovery). The trade-off: one GitHub login gates both the code and the
network.

**Tailscale SSH in `check` mode.** SSH to the box asks for a fresh
identity-provider login every 12 hours (the free plan's fixed period).
That's a hardware-key tap. Root on the box is reached the same way, so no
sudo password is needed and none exists.

**Tailnet Lock.** With it on, a new node also needs a signature from one
of your own signing devices (the laptop), which every node verifies
itself. Neither Tailscale's control plane nor someone with your GitHub
login can then add a device. Server auth keys must be signed first
(`tailscale lock sign <authkey>` on the laptop). Keep the disablement
secrets offline. Never make a server a signing node. Device approval
can't be on at the same time; Lock replaces it and is stronger.

**The policy is code** (`tailnet/policy.hujson`). Default deny. You reach
the box on every port; the box reaches nothing (only replies to
connections you open); prod, if it's on the tailnet, is reachable only by
you on 22. The `tests` block asserts that, and the API rejects a policy
whose tests fail. `make -C tailnet validate | diff | apply` pushes it
through the Tailscale API. Never edit it in the admin console: the repo
copy goes stale and the next apply silently reverts the change.

**Closing public SSH.** Bootstrap leaves public port 22 open as a fallback.
Once Tailscale SSH works, use the provider's web console once on purpose to
prove you can get in without SSH. Then `make lockdown` removes the rule.

## Serving dev servers to other devices

Goal: open `https://devbox.<tailnet>.ts.net:4200` on the phone and get the
app, exactly as `http://localhost:4200` on a laptop.

**Why HTTPS, not `http://devbox:4200`.** Browsers give a page served over
plain HTTP (other than `localhost`) no secure context: no
`navigator.locks`, `crypto.subtle`, `crypto.randomUUID`, service workers or
secure cookies. Supabase auth died at startup on exactly that. Tailscale
issues a real certificate for the box's tailnet name (MagicDNS and HTTPS
certificates must be on for the tailnet).

**The path of a request:**

```
browser ─► Tailscale (TLS, on the tailnet address, :4200)
        ─► Caddy on 127.0.0.2:14200 (compresses)
        ─► the dev server on 127.0.0.1:4200
```

- `ws up NAME` runs `tailscale serve --https=<port>` for each port in the
  project's `tailnet` list, only on a machine with `WS_TAILNET=1` (the
  box). It also writes `~/.config/devbox/caddy/sites/NAME.caddy` and
  reloads Caddy. Projects need nothing for this.
- **Caddy compresses.** Dev servers don't: an Angular dev build's page was
  13.7 MB over the wire and took 12 s on the tablet. Through Caddy (zstd)
  it was 3.0 MB. Websockets (live reload) and streams pass through. Caddy
  listens on `127.0.0.2` at port + 10000, so ports above 55535 don't fit.
  Not the server's own port, because some servers hold their port on every
  address.
- **Servers bind `127.0.0.1`** (`DEV_BIND_HOST`). Tailscale holds each
  served port on the tailnet address, so a server that binds `0.0.0.0`
  after it fails with "address in use". Projects read
  `${DEV_BIND_HOST:-0.0.0.0}`, so they keep LAN access elsewhere. For what
  can't bind `127.0.0.1` (the Supabase CLI publishes on `0.0.0.0`, with no
  setting for it), `ws start` takes the project's tailnet ports down while
  it starts, then serves them again.
- **Only the box gets a certificate.** A machine gets one only when it asks
  (`tailscale serve --https`, `tailscale cert`). Every name with a
  certificate is published in Certificate Transparency logs, so the box's
  tailnet name is publicly known as a name, though not reachable.

What a project itself must do (client URLs from `location`, CORS, allowed
hosts, CSP, auth redirect URLs) is in
[PROJECT-CHECKLIST.md](PROJECT-CHECKLIST.md).

## Sessions: ssh, mosh, tmux, Claude Code

- **ssh or mosh** to the box from any device: Terminal on the laptop,
  Termius or Blink on iOS, Termux (`pkg install openssh mosh`) on Android.
  Tailscale SSH works with a plain `ssh` client and needs no keys.
- **tmux** holds the sessions, so closing the laptop kills nothing and you
  reattach from anywhere. `ws session NAME` opens tmux in a project. On the
  box that's the stack's own tmux server (socket `ws-NAME`), started inside
  the stack's sessions slice. tmux puts each pane in a scope beside itself,
  so what you run there counts as the stack's (see
  [Stacks](#stacks-one-systemd-slice-each)).
- **Claude Code** runs on the box, logged in with your account. The laptop
  and the box share one plan, so they share its limits. Every session on
  the box loads `workspace/AGENTS.md`, which bootstrap links into
  `~/.claude/rules/`. It tells agents to use `ws run` for heavy work, to
  leave stacks they didn't start alone, and how to ask for an egress domain.
  Your own global rules, skills and agents can live in this repo
  (`claude/global.md`, `claude/skills/*`, `claude/agents/`). Bootstrap links
  them the same way, and a `git pull` updates every session.

## The workspace: ws and projects.toml

`ws` (`workspace/ws`, Python 3.11+, standard library only) turns a project
under `~/_` into a working dev environment, the same way every time: on the
box, on a rebuilt box, on the laptop.

```sh
ws list                    # the projects
ws plan shop               # the steps, in order, and which are done (changes nothing)
ws up shop                 # take them; safe to repeat
ws up shop --with assets   # include an optional part
ws start shop / ws stop shop / ws check shop
ws session shop            # tmux in the project
```

**Who owns what.** This split keeps each fact in one place:

| Layer | What | Where |
|---|---|---|
| Machine | OS, Docker, mise, sops, gh, tokens | `box/bootstrap.sh` |
| Construction | clone (with submodules), toolchain versions, dependency installs, env files, ports to publish | `workspace/projects.toml` |
| Running | how to start and stop it | the project's own repo (`make dev-up`...); the manifest names the command |
| Verification | is it working | the project's own repo (a health-check script); the manifest names the command |
| Data | big files that aren't on GitHub | an `optional` entry with `how`, done by hand |

So "start it" and "is it working" change with the project and are reviewed
there. "What does a fresh machine need to get to that point" lives here.

**Details:**

- **Toolchains.** `tools = { node = "24", python = "3.12" }` is written to
  `<dir>/mise.local.toml` (git-excluded through `.git/info/exclude`), then
  `mise install`. Every `prepare`, `start` and `check` command runs through
  `mise exec` in the project's folder. mise's shims are on PATH, so a plain
  `node` in the project is its version too, including in Claude's shell.
- **Clones** use the laptop's `git@github.com:` URLs; the box rewrites them
  to HTTPS with its tokens. An existing clone is never pulled or reset: `ws
  up` only checks out submodules that aren't there yet.
- **`prepare` runs on every `ws up`** and must be safe to repeat: installs
  from lockfiles (guarded so `npm ci` doesn't empty `node_modules` under a
  running server), env-file writers that never overwrite unsaved edits.
- **A second checkout** of the same repo, so two agents can work on two
  branches with two running stacks: `[shop-b] like = "shop"` copies
  everything except `dir` and `tailnet`. The project's own scripts see the
  `-b` and shift their ports.
- **Folders of repos**: `repos = { a = "owner/a", b = "owner/b" }` without
  `repo`. A repo in it that runs is a project of its own
  (`dir = "folder/a"`).
- **`serve`**, for a project with no Makefile to put its server in the
  background: `serve = { dev = "astro dev --host $DEV_BIND_HOST" }` runs it
  in the foreground as the stack's service.

## Stacks: one systemd slice each

With `WS_SLICES=1` (the box), everything that belongs to a project runs in
the dev user's own systemd, grouped by project:

```
ws-shop.slice
├─ ws-shop-servers.slice    ws-shop-db.service, ws-shop-api.service, ...
├─ ws-shop-sessions.slice   the stack's tmux, its panes, Claude sessions
└─ ws-shop-jobs.slice       ws-shop-job-build.service, ... (memory-capped)
```

- **`ws start`** runs each named `start` command as a oneshot unit,
  `ws-<project>-<name>.service`, in the servers slice. The unit stays active
  after the command exits and holds whatever it started in the background.
  The kernel then counts those processes' memory and CPU to the stack, and
  stopping the slice stops all of them, however they daemonised. Output:
  `journalctl --user -u <unit>`. Starting again first stops what the last
  start left.
- **`ws stop`** runs the project's own `stop` commands (which also stop
  its containers), then stops the servers slice. Sessions and jobs keep
  running.
- **Containers** are root's cgroups, not the slice's. They count as a
  stack's by their working-directory label
  (`com.docker.compose.project.working_dir`, `com.supabase.cli.workdir`)
  being in the project's folder.
- **Started by hand** (an `ng serve` in an ssh login): shown under the
  stack whose folder its processes run in, marked "outside ws". Restart it
  with `ws start` to have it counted properly.
- Two starts, stops, pauses or resumes of the same project never run at
  once (a lock in `ws`): the second fails and says so.

## Measuring: the tree, the minutes, the peaks

**The tree** (`workspace/control/boxtree.py`; `ws top`). It's built from
`/sys/fs/cgroup`, `/proc` and the Docker API, as the dev user with no root:
the base system, Docker, each stack (its servers, containers, sessions,
jobs), with memory (anonymous and page cache apart) and CPU. It also shows
the root filesystem's free space (under 20 GB is marked low) and its big
users: the swap file and Docker. Docker's size takes seconds to work out,
so it's asked at most every 10 minutes.

**The minutes** (`workspace/control/watch.py`). The control server samples
the tree every 3 s and keeps a minute at a time in
`/var/lib/devbox-control/watch/YYYY-MM-DD.jsonl`, for 35 days. For each
stack's app (servers, containers, by-hand processes in its folder), each
Claude session (its pane's cgroup, so the tests it runs count in it) and
each job (by name), it keeps the minute's highest and mean memory and its
CPU. For the box it keeps used, free, swap and memory stall time (PSI).
Those three never hold the same memory twice, so their peaks add up.

**The peaks** (`ws peaks [DAYS]`, and "Memory at peaks" on the page):

- For each stack: how often it's active (its app at 0.1 cores or more over
  a minute, and at least twice its own median, because some servers poll
  at ~0.2 cores when idle), and what it takes idle and active.
- For what runs now: its peak (the 99th-percentile minute) less what it
  takes now. The sum is what to keep free for everything to peak at once;
  the rest of the free memory is the room for more.
- A Claude session with under 30 minutes recorded borrows every session's
  peak. Paused minutes don't count. A peak shorter than 3 s can be missed.

**Room before a start** (`room()` in `watch.py`): the stack's peak less
what it holds now, against what's free once everything else running could
peak. A switch counts the others' memory as going to swap. `ws start`,
`resume` and `switch` print it first, and the page's confirmation shows it.
For now it's a note, not a refusal, while the peaks are still being learnt.
A scheduler starting agent work would decide with the same function.

## The control page

`workspace/control/server.py`, run by `devbox-control.service`, serves
`index.html` on port 7070. The `dev-server` project's `tailnet = [7070]`
publishes it at `https://devbox.<tailnet>.ts.net:7070`. It refreshes every
3 s.

- **Shows:** the tree, per stack, with memory, CPU and disk; memory at
  peaks and the room left; each stack's jobs (running, and how the last 12
  hours' ended); the Claude sessions and their state; the plan's usage.
- **Does:** Start (with a checkbox per optional part), Restart, Pause,
  Resume, Switch to, Stop, for every project with `start` commands, also
  when it's stopped. It also starts a new Claude session in a stack and
  trusts a folder. A press runs `ws start` / `stop` / `pause` / ... as a
  job of the stack (`ws run NAME start`), so `ws jobs` and `ws logs` have
  it and the page shows its log.
- **Locked down:** a system unit with `ProtectSystem=strict`, its only
  writable folder `/var/lib/devbox-control`. To act it asks the dev user's
  systemd (`systemd-run --user`) for a zsh, which reads `~/.zshenv` like a
  login, and runs `ws run` there. It answers only requests addressed to
  the box by name, and only the page itself can press buttons (its Origin,
  a JSON body).

## Pause, swap, switch, restore

The box fits one big stack. Switching between stacks (work on one, a PR
review on another) has to be quick.

- **`ws pause NAME`** freezes the servers slice
  (`systemctl --user freeze`), pauses its containers (`docker pause`), and
  pushes the servers' memory out to swap through the slice's
  `memory.reclaim`. The containers' memory goes as the room is needed,
  their cgroups being root's. Its sessions and jobs go on.
- **`ws resume NAME`** unpauses and thaws. The rest comes back from swap
  as it's used.
- **`ws switch NAME`** pauses every other running stack, then resumes NAME,
  or starts it.
- `ws stop` and `ws start` thaw a paused stack first. While paused, its
  ports take connections and never answer. AGENTS.md tells agents to check
  `ws top` when a request hangs.

Measured with a stack of Supabase, four workers and four Angular dev
servers:

| | |
|---|---|
| cold start | 11 min 47 s (the apps compile one after another) |
| pause | 7–34 s, ~8.5 GB out to swap |
| resume, until every app serves its bundle | 15–29 s |
| switch between two such stacks | 23–63 s |

**Swap** is a 64 GB file. A paused stack like that is ~9.5 GB of it; 16 GB
filled up with two (one paused, one being paused).

**After a reboot: `ws restore`** (`devbox-restore.service`, at boot). A
reboot ends every stack, paused ones too. So every start, stop, pause and
resume writes what the stack should be to
`~/.local/state/ws/intent/NAME.json`: running, paused or stopped, and what
it starts `--with`. After boot the stacks come back one at a time. Each one
that was paused is started, left until its servers go quiet (0.15 cores or
less for a minute, at most 20 min), and paused again, so the next starts
with the room to itself. The one that was running starts last. Each step is
a job (visible on the page). A failed start is tried once more. Two big
stacks paused and one running took ~40 min after the boot. `make update`
records what's up now where nothing is recorded yet (`ws remember`).

## Jobs

`ws run NAME JOB [--wait] [--parallel] [-- COMMAND]`, on the laptop and
the box alike. It's for builds, test suites, anything long or heavy.

- Runs apart from whoever started it. An agent's command timing out, or
  its session ending, doesn't end the job. `--wait` follows its output and
  exits with its code.
- In the project's folder: the command given, or `jobs.JOB` from the
  manifest.
- **One at a time per project** (a lock), unless `--parallel`. An Angular
  production build takes ~3 GB and every core, so two in parallel are no
  faster and can starve the box.
- State and output are files (`~/.local/state/ws/jobs/NAME/JOB/`):
  `ws jobs`, `ws logs NAME JOB [-f]`, `ws cancel`.
- On the box each job is a unit in `ws-<project>-jobs.slice` with
  `MemoryMax` (`WS_JOB_MEMORY_MAX`, 6G). A build that outgrows it is
  killed on its own ("failed (signal 9): over the memory cap"), instead of
  the OOM killer choosing among servers and sessions.

## Claude sessions in a stack

`ws claude NAME` (or "New Claude session" on the page) starts
`claude --remote-control` in the stack's tmux, as its own tmux session
(claude-1, claude-2, ...). Several can run at once. Each counts as the
stack's and can be attached to alone:
`ssh -t devbox tmux -L ws-NAME attach -t =claude-1`.

- **Remote Control link.** Open it in claude.ai/code or the Claude app to
  follow and answer the session from the phone. `ws` reads it from the
  session's pane, where claude prints it.
- **State** comes from the session's own hooks. They're passed with
  `claude --settings`, so they apply to that session only, and they call
  `ws claude-hook` (`control/claude_sessions.py`). States: working (a
  prompt, a tool), needs input (a permission prompt, a question), finished
  (its turn ended), ended (its process gone).
- **Trust.** The first session in a folder asks whether to trust it. The
  page's "Trust this folder" answers it (`ws claude-trust`), after checking
  that's what the pane shows.
- `ws claudes [NAME]` lists them; `ws claude-end NAME claude-N` ends one.

## The Claude plan's usage

`ws statusline` is Claude Code's status line command (`statusLine` in
`~/.claude/settings.json`; bootstrap sets it on the box). It's the only
documented source of the plan's 5-hour and weekly limits. For each
session it records the latest snapshot under
`~/.local/state/ws/claude/sessions/`, and adds a line to
`samples-YYYY-MM.jsonl` when cost, model or limits change: the history for
learning what a task takes of the window.

The page shows the two bars (current session, weekly), each session's
spend in them by stack, and under each bar, an estimate of what's left in
dollars of work. That's the rest of the bar at what 1% of it has cost over
two weeks of recorded windows. Usage outside Claude Code (claude.ai) moves
the bar unrecorded, so the estimate comes out low.

The laptop's sessions can show on the box's page too: set the laptop's
status line command to
`WS_CLAUDE_SHIP_TO=devbox ~/_/dev-server/workspace/ws statusline`. It then
copies its records to the box (rsync over ssh, in the background, at most
once a minute).

## Outbound allowlist

The box may reach only the internet hosts in `box/egress.allow`. An agent
misled by what it reads (a PR comment, a README, a package's install
script) can't send the box's tokens or dev secrets anywhere else.

**How** (`box/sbin/devbox-egress`):

- A DNS server (dnsmasq on `127.0.0.3` and the Docker bridge address
  `172.17.0.1`) answers only for listed domains. A domain covers its
  subdomains. Each address it hands out goes into an nftables set.
- Traffic leaving through the public interface may go only to those
  addresses, or to addresses and CIDRs listed directly. Containers count
  too.
- Every DNS query that would leave the box, from a process or a container,
  is redirected to that server, so no tool needs configuring.
- Exempt: tailscaled's own traffic (it marks it with fwmark `0x80000`),
  because the tailnet is how you reach the box. Also exempt: the tailnet
  itself, Docker's bridges and loopback; only the public interface is
  filtered.

**Modes:** `audit` logs what would be blocked and blocks nothing;
`enforce` refuses it; `off` removes the rules. `devbox-egress enforce`
reverts to audit after 5 minutes unless `devbox-egress confirm` follows,
in case the list breaks something you need in order to fix it.

**Building the list:** run in audit for a few days of normal work, then
`devbox-egress blocked 100` lists the unlisted names that were asked for.
Add what's legitimate, then switch to enforce.

**Adding to it:**

- In the repo: edit `egress.allow`, `make update`.
- On the box, now: `ssh root@devbox devbox-egress allow <domain>`. It goes
  into the box's own list. Later, `make egress-sync` copies those into
  `egress.allow` (with a yes for each), and you commit.
- **An agent's request:** an agent that hits a block runs
  `devbox-egress request <domain> "<why>"` and asks you in its session
  (AGENTS.md tells it to). `ssh -t root@devbox devbox-egress approve` goes
  through the queue, y/N for each. The queue is writable by the dev user,
  so nothing in it takes effect without that root step.

**Guards:** a CIDR may be no wider than /16 (IPv4) or /32 (IPv6). A mode
file that says anything else stops the script rather than allowing
everything. Removing a domain stops new lookups, but addresses it already
resolved stay allowed until the next `off` and back on (or a reboot).

**Logs:** DNS in `/var/log/devbox-egress/dns.log` (the dev user can read
it, rotated daily, kept 7 days); connections in the kernel log
(`journalctl -k | grep devbox-egress`).

## GitHub access

**One fine-grained token per owner** (your account, each org), never
`gh auth login`'s browser flow or a classic token. Those carry every scope
your account has, across every repo, including admin on your orgs. A
fine-grained token is limited to one owner and to the permissions you
list. The box can then push code and open PRs, but can't change settings,
secrets, members or deployments.

Owner tokens, repository permissions:

- read/write: Contents, Pull requests, Issues, Workflows (agents edit CI
  files), Actions (agents re-run a flaky check).
- read: Commit statuses, Metadata.
- none: everything else, in particular Administration, Secrets, Variables,
  Environments, Deployments, Webhooks, Pages.
- Expiry: 364 days. An org's lifetime policy can cap it lower.
- For an org: allow fine-grained tokens and require approval (Org >
  Settings > Personal access tokens). You approve your own requests, and
  nothing gets in unnoticed.

**Secrets repos are read-only from the box.** The laptop runs code from
them (Makefiles, env writers) while it holds prod keys, and their
`.sops.yaml` decides who can decrypt prod. A box that could push there
could add itself as a prod recipient, or put code on the laptop that runs
with prod access. On GitHub's free plan private repos have no branch
protection, so the tokens are the only lock:

- the owner tokens are given every repo **except** the secrets repos;
- a separate read-only token per secrets repo (Contents read only) lets the
  box pull it;
- `make github-check` (`devbox-github check`) fails if any token on the box
  can push to or administer one of them (`GITHUB_PROTECTED` in
  `devbox.conf`). It probes with writes that can never succeed, which
  return 403 without the permission and 422 with it.

The cost: a new repo under an owner has to be added to that token on
github.com, or pushes to it fail with 403 or "repository not found".
Nothing fails silently.

**Picking the token** (`box/bin/devbox-github`): the repo's own token if
there is one, else its owner's. git goes through `git-credential-devbox`
(with `credential.useHttpPath`, git hands it `OWNER/REPO`). gh goes through
a wrapper at `/usr/local/bin/gh` that sets `GH_TOKEN`. The repo is found
from `-R`/`--repo`, `repos/OWNER/...` and `orgs/OWNER` in `gh api`, GitHub
URLs, `gh repo ... OWNER/REPO`, else the current repo's `origin`, else the
default owner. Don't run `gh auth login` or `gh auth setup-git` on the box;
they'd replace this.

**Creating them:** GitHub has no API for creating tokens. `make
github-tokens` opens each form pre-filled through URL parameters, says what
to choose as Repository access, then runs `devbox-github set <token>` on
the box so you paste the token straight in. The token never passes
through the laptop.

**Keeping them across rebuilds:** `devbox-github export` encrypts the
stored tokens with sops (public keys only, so no hardware key needed) for
your secrets repo. `devbox-github import` restores them on a new box. The
box can't push to the secrets repo, so the laptop commits the file. Be
careful there: in my setup the laptop decrypts what the box sent, accepts
only token lines, checks with GitHub itself that no token can push to a
secrets repo, re-encrypts with its own `.sops.yaml`, and shows the diff
before committing. It takes nothing from the box on trust.

## Secrets

- **sops with age.** Each project's env values live encrypted in a
  secrets repo, split by environment (`*.dev.env`, `*.qa.env`,
  `*.prod.env`). The box has its own age key, which is a recipient of dev
  and test files only. Prod files are encrypted to your hardware keys
  (age-plugin-yubikey) and the laptop.
- **The box's key survives rebuilds.** It's stored encrypted in the
  secrets repo, and restored onto a new box from the laptop (one hardware-key
  touch). The box keeps the same recipient, so nothing gets re-encrypted.
  First build only: create it with `age-keygen` on the box, add its
  recipient to the dev/test rules in `.sops.yaml`, re-encrypt those files,
  and store the key.
- **Projects write their env files** with their own command (`make env`),
  which `prepare` runs. On the box it writes what the box can decrypt and
  skips prod.
- **No global credential logins** on the box (`wrangler login`, cloud
  CLIs' `auth login`). Credentials come from env files, scoped to dev.

## Maintenance in idle time

Unattended-upgrades applies security fixes daily. Everything else
(`devbox-maintenance`) waits for a moment when nothing is going on:

- Weekly (`MAINTENANCE_DUE`), a timer marks maintenance due
  (`/var/lib/devbox/maintenance-pending`).
- Every hour another timer checks for idle: no tmux client attached, no
  tmux output for 2 hours (a detached Claude still working counts as
  busy), no headless `claude -p`. If idle, it runs a full apt upgrade
  (Docker, Tailscale and gh included), prunes Docker images and build
  cache, and reboots if Ubuntu says one is pending. It also reboots when an
  upgraded Docker restarted under running stacks, because their containers
  are gone while their servers aren't; after the reboot `ws restore` brings
  each back whole.
- Still deferred after 3 days, it logs a warning.
  `ssh root@devbox devbox-maintenance apply --force` runs it now.
  `make status` shows whether it's due and why it's waiting.

## What it doesn't protect against

- **The dev user is effectively root** (the `docker` group), and so is
  every agent. A deliberate `docker run --privileged` or a host-path mount
  can remove the egress rules. **Not done yet, and the main gap in the
  design:** rootless Docker for the dev user, or a user per project with
  its own rootless Docker. Then containers run with the user's rights and
  live in the user's cgroups. That also changes how `boxtree.py` finds a
  stack's containers (today by their labels, in root's cgroups) and how
  `ws pause` freezes them.
- **Allowed addresses host other names.** CDNs share addresses, so a
  connection to an allowed CDN address can reach another site behind the
  same CDN.
- **Allowed services that accept data from any account**: anyone can have
  a `*.workers.dev` worker, a Supabase project or a Sentry DSN, and GitHub
  accepts a gist from the box's own token. Narrow such entries to your
  projects' own hosts where you can.
- **The agents' own GitHub reach.** The owner tokens can push code and
  re-run or dispatch Actions in every repo they cover. If a workflow can
  deploy to prod on dispatch or push, an agent on the box can trigger it.
  Keep prod deploys behind environments with required reviewers, or accept
  it knowingly.

The egress list stops accidents and naive exfiltration, not a determined
attacker who already runs code on the box. The real protection is the
principle at the top: nothing on the box is worth much.
