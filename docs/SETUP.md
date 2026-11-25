# Setup

Step by step, from nothing to a working box. Written so an agent on your
laptop can follow it with you. Steps marked **[you]** need a person: a
browser login, a payment, a hardware key, a secret pasted in. The agent
should say what to do and wait for you. Everything else the agent can run.

Read [ARCHITECTURE.md](ARCHITECTURE.md) for why each piece is there.

**Agents:** do the steps in order, and check each one before moving on
(every step has a **Check**). Ask the user rather than guessing their names,
domains or accounts. Never paste a token, key or auth key into the chat:
the commands below read them from a hidden prompt or from the
provider's site directly. If a check fails, stop and report it.

## 0. Before you start

You need:

- A laptop with `git`, `make`, `ssh` and a terminal (macOS or Linux).
- A GitHub account. Ideally its only second factors are hardware keys.
- A credit card for a VPS (about $10 a month).
- Optional, for secrets: `sops` and `age` on the laptop, and a secrets
  repo (see [ARCHITECTURE.md, Secrets](ARCHITECTURE.md#secrets)).

### 0.1 Make the repo yours

The box runs `ws` and the control page from **your own private copy** of
this repo, cloned at `DEVBOX_DIR` (default `~/_/dev-server`). Your
projects, allowlist and policy go in it.

```sh
mkdir -p ~/_ && cd ~/_
git clone https://github.com/juraj-m/devbox-runbook dev-server
cd dev-server && rm -rf .git && git init -b main
gh repo create YOUR-USER/dev-server --private --source . --push   # after 0.2's edits is fine too
```

### 0.2 Fill in the settings

Ask the user for each value, then edit:

- **`box/devbox.conf`**: `DEV_USER`, `DEV_HOME` (suggest the same path as
  the laptop's home: `/Users/<name>` on a Mac), `DEVBOX_REPO` (the repo
  from 0.1), `TS_HOSTNAME`, `TZ_NAME`, `GIT_NAME`, `GIT_EMAIL`,
  `GITHUB_DEFAULT_OWNER`, `GITHUB_PROTECTED` (their secrets repos, or empty),
  `SWAP_SIZE` (about the box's disk size × 0.3; keep 64G on a 200 GB disk).
- **`tailnet/policy.hujson`**: replace `you@github` with their Tailscale
  login (`<github-name>@github` when they sign in with GitHub). Fix the
  `users` in the `ssh` rule to `DEV_USER`. Drop `tag:prod` if they have no
  prod servers to put on the tailnet.
- **`workspace/projects.toml`**: keep `secrets` (if they have one) and
  `dev-server` (with their repo names). Replace the example projects with
  theirs later (step 9).
- **`box/egress.allow`**: keep the first sections. Replace the last
  ("The projects' dev and test services") with what their projects call
  (or leave it for audit mode to tell you, step 10).

Commit and push.

**Check:** `make -C box -n ssh` prints `ssh <DEV_USER>@<TS_HOSTNAME>`.

## 1. The tailnet

1. **[you]** Sign in at https://login.tailscale.com with the identity
   provider you'll keep (GitHub recommended). This creates the tailnet.
2. **[you]** Admin console > DNS: MagicDNS on, and **HTTPS Certificates
   on**. Note the tailnet's DNS name (`<something>.ts.net`).
3. **[you]** Admin console > Settings > Keys > Generate API access token
   (expires in 90 days at most). Then, in the laptop's terminal:
   `export TS_API_KEY=...` (not in the chat), or store it with sops
   (see `tailnet/Makefile`).
4. Apply the policy: `make -C tailnet validate && make -C tailnet apply`.
   **Check:** `make -C tailnet diff` says the tailnet matches.
5. **[you]** Install Tailscale on the laptop and phone/tablet and sign in.
   On the phone also install an SSH client (Termius or Blink on iOS,
   Termux from F-Droid on Android, then `pkg install openssh mosh`).
6. Optional, recommended: **Tailnet Lock** (Admin console > Settings >
   Device management: device approval off first). On the laptop:
   `tailscale lock init --gen-disablements 3 --confirm <laptop's tlpub>`.
   **[you]** Store the three disablement secrets offline, before anything
   else. From then on, auth keys must be signed: `tailscale lock sign <key>`.

**Check:** `tailscale status` on the laptop lists the laptop and the phone.

## 2. The VPS

1. **[you]** Order an Ubuntu **24.04** x86-64 VPS. At least 4 vCPU,
   8 GB (12 GB+ for a big web stack), 100 GB+ disk, NVMe if offered, in a
   region near you. Give it the laptop's SSH public key at order time.
2. **[you]** Find the provider's web console (VNC, serial or rescue mode)
   and note how to open it. Step 8 needs it.

**Check:** `ssh root@<public ip> true` works from the laptop.

## 3. Bootstrap

1. **[you]** Admin console > Settings > Keys > Generate auth key:
   **one-off**, **pre-approved**, tag **`tag:dev`**, short expiry, not
   ephemeral. With Tailnet Lock: sign it on the laptop first
   (`tailscale lock sign <key>`) and use the output.
2. **[you]**, in a real terminal (it prompts for the key, hidden):
   `make -C box bootstrap HOST=<public ip>`.
   Takes 5–10 minutes. It ends with "Next, as <DEV_USER> ...".

**Check:** `ssh <DEV_USER>@<TS_HOSTNAME> 'zsh -c "echo \$WS_SLICES; mise --version; caddy version; swapon --show"'`
prints `1`, the versions and the swap file. (The first Tailscale SSH asks
for a browser login: **[you]**.)

## 4. Claude Code on the box

**[you]** `ssh <DEV_USER>@<TS_HOSTNAME>`, run `claude`, log in. Exit.

**Check:** `ssh <DEV_USER>@<TS_HOSTNAME> claude --version`.

## 5. GitHub tokens

1. **[you]** For each org the box should work in: Org > Settings >
   Personal access tokens > Settings: allow fine-grained tokens, require
   approval.
2. **[you]**, in a real terminal: `make -C box github-tokens`. For each
   owner and each read-only repo it opens GitHub's form pre-filled and
   prints what to pick as Repository access (for an owner with a protected
   repo: every repo *except* it). Generate, copy, paste at the prompt on
   the box. Set more owners with
   `make -C box github-tokens GITHUB_OWNERS="you your-org"`.
3. **[you]** Approve org tokens: Org > Settings > Personal access tokens >
   Pending requests.

**Check:** `make -C box github-check` lists each token with its repos and
expiry, and ends "no token can push" (or there are no protected repos).

## 6. The repo on the box

`make -C box update`. It clones `DEVBOX_REPO` to `DEVBOX_DIR` on the box
(`make sync`), brings it to the laptop's commit, and reruns bootstrap, which
now links `AGENTS.md` into Claude's rules and starts the control page from
the clone.

**Check:** `ssh <DEV_USER>@<TS_HOSTNAME> 'ws list && ls -l ~/.claude/rules/'`
lists the projects and the `workspace.md` link.

## 7. The control page

`ssh <DEV_USER>@<TS_HOSTNAME> ws up dev-server`. It serves port 7070 on the
tailnet.

**Check:** open `https://<TS_HOSTNAME>.<tailnet>.ts.net:7070` on the phone.
The page shows the box's memory as a tree. (The first HTTPS request can
take a few seconds while Tailscale gets the certificate.)

## 8. Close public SSH

1. **[you]** Open the provider's web console and log in as root, to prove
   you can get in without SSH. If root has no password, set one now in an
   SSH session (`passwd`) and keep it in your password manager. Close the
   console.
2. `make -C box lockdown`.

**Check:** `ssh -o ConnectTimeout=5 root@<public ip> true` fails, and
`ssh root@<TS_HOSTNAME> ufw status` still works and shows no 22/tcp rule.

## 9. Your projects

For each project, with the user:

1. Add it to `workspace/projects.toml` (the example shows each kind):
   repo, `tools` (from `.nvmrc`, `.python-version`, CI), `prepare`
   (installs from lockfiles, env files), `tailnet` ports (every port the
   browser talks to), `start`, `stop`, `verify`.
2. Go through [PROJECT-CHECKLIST.md](PROJECT-CHECKLIST.md). Most items are
   changes in the project's own repo: bind host, client URLs, CORS,
   allowed hosts.
3. Commit, push, `make -C box sync`.
4. On the box: `ws plan NAME`, then `ws up NAME`, `ws start NAME`,
   `ws check NAME`.

**Check:** the project's app opens on the phone at
`https://<TS_HOSTNAME>.<tailnet>.ts.net:<port>`, logs in, and works. The
control page shows it as a stack with its memory.

Big data that isn't on GitHub: an `optional` entry with `how` (an rsync
from the laptop), done by hand.

## 10. The outbound allowlist

The box starts in **audit** mode: nothing is blocked, and what would be
is logged.

1. Work normally for a few days: installs, builds, tests, agents.
2. `ssh <DEV_USER>@<TS_HOSTNAME> devbox-egress blocked 100` lists the
   unlisted names that were asked for. With the user, add the legitimate
   ones to `box/egress.allow`. Commit, `make -C box update`.
3. `ssh -t root@<TS_HOSTNAME> devbox-egress enforce`, try an install and a
   build, then `devbox-egress confirm` within 5 minutes. Without the
   confirm it goes back to audit.

**Check:** `ssh <DEV_USER>@<TS_HOSTNAME> 'curl -sS -m 5 https://example.org'`
fails with "Could not resolve host", and
`curl -sS -m 5 -o /dev/null -w '%{http_code}' https://github.com` prints
200.

## 11. Secrets (if your projects need them)

1. On the box: `age-keygen -o ~/.config/sops/age/keys.txt`; note the
   public key it prints.
2. **[you]** On the laptop, in the secrets repo: add that recipient to the
   `.sops.yaml` rules for dev and test files only, `sops updatekeys` those
   files, commit, push. Store the box's private key encrypted in the
   secrets repo (for example `devbox/age-key.env`, prod-only recipients), so
   a rebuilt box gets the same key back.
3. On the box: `ws up secrets`, then `ws up` each project. Its `make env`
   writes what the box can decrypt.

**Check:** a project's env file on the box has its dev values, and no
prod file was written.

## 12. Daily use

- `ssh <DEV_USER>@<TS_HOSTNAME>` (or `mosh`), then `ws session NAME`, or
  `ws claude NAME` for a Claude session you can follow from the phone.
- The control page for what runs, and to start, pause and switch stacks.
- Every change to the box: edit, commit, push, `make -C box update`.
- `make -C box status`: maintenance due or waiting, memory, disk.

## Rebuilding

Reinstall Ubuntu in the provider's panel, then steps 3–8 with the same
repo: a fresh auth key, `make -C box bootstrap HOST=<ip>`, log in to
Claude, restore the GitHub tokens (`devbox-github import`, or step 5
again), `make -C box update`, restore the age key, `ws up` your projects.
Remove the old node in the Tailscale admin console first, so the new one
gets the same name.
