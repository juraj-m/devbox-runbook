#!/usr/bin/env bash
# devbox bootstrap: turns a fresh Ubuntu VPS into the coding box.
#
# Runs as root on the box; `make bootstrap` / `make update` copy this
# directory over and run it. Idempotent: rerunning is safe, and it is how
# every change to the box lands. Nothing here holds state that a rebuild
# can't recreate.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
STATE=/var/lib/devbox
# Every setting is in devbox.conf (DEV_USER, DEV_HOME, TS_HOSTNAME, ...).
# shellcheck source=devbox.conf
. "$HERE/devbox.conf"
for v in DEV_USER DEV_HOME PROJECTS_DIR DEVBOX_DIR TS_HOSTNAME TZ_NAME MAINTENANCE_DUE \
	GIT_NAME GIT_EMAIL GITHUB_DEFAULT_OWNER SWAP_SIZE EGRESS_MODE; do
	[ -n "${!v:-}" ] || { echo "FAIL: devbox.conf sets no $v" >&2; exit 1; }
done

export DEBIAN_FRONTEND=noninteractive
log() { printf '\n==> %s\n' "$*"; }
as_dev() { runuser -l "$DEV_USER" -c "$1"; }

mkdir -p "$STATE" /etc/devbox
install -m 644 "$HERE/devbox.conf" /etc/devbox/devbox.conf

log "base packages"
apt-get update -q
apt-get install -y -q \
	ca-certificates curl git tmux mosh zsh jq ripgrep htop unzip \
	build-essential ufw unattended-upgrades age dnsmasq-base dnsutils acl
# What a headless Chromium needs to start (Karma's unit checks, Playwright):
# Playwright's own list for Ubuntu 24.04 (`npx playwright install-deps
# --dry-run chromium`, 1.59). The browser itself is per project, in
# ~/.cache/ms-playwright.
apt-get install -y -q --no-install-recommends \
	libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 \
	libcairo2 libcups2t64 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 \
	libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 \
	libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2 xvfb \
	fonts-noto-color-emoji fonts-unifont libfontconfig1 libfreetype6 \
	xfonts-cyrillic xfonts-scalable fonts-liberation fonts-ipafont-gothic \
	fonts-wqy-zenhei fonts-tlwg-loma-otf fonts-freefont-ttf
timedatectl set-timezone "$TZ_NAME"
hostnamectl set-hostname "$TS_HOSTNAME"

log "unattended-upgrades (security fixes daily; reboots are devbox-maintenance's job)"
cat >/etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF

log "user $DEV_USER at $DEV_HOME"
if ! id "$DEV_USER" >/dev/null 2>&1; then
	mkdir -p "$(dirname "$DEV_HOME")"
	useradd --create-home --home-dir "$DEV_HOME" --shell /usr/bin/zsh "$DEV_USER"
fi
install -d -m 755 -o "$DEV_USER" -g "$DEV_USER" "$PROJECTS_DIR"
install -d -m 700 -o "$DEV_USER" -g "$DEV_USER" "$DEV_HOME/.ssh" "$DEV_HOME/.agent"
# zsh asks an interactive first-run question without a .zshrc.
[ -f "$DEV_HOME/.zshrc" ] || install -m 644 -o "$DEV_USER" -g "$DEV_USER" /dev/null "$DEV_HOME/.zshrc"
# PATH goes in .zshenv, which every zsh reads; .zshrc is interactive-only,
# so `ssh devbox claude ...` and scripts would not find claude.
ZE="$DEV_HOME/.zshenv"
[ -f "$ZE" ] || install -m 644 -o "$DEV_USER" -g "$DEV_USER" /dev/null "$ZE"
grep -q '.local/bin' "$ZE" || echo 'export PATH="$HOME/.local/bin:$PATH"' >>"$ZE"
sed -i '\|^export PATH="$HOME/.local/bin:$PATH"$|d' "$DEV_HOME/.zshrc"
# The stacks run as the dev user's own systemd units (ws start, WS_SLICES
# below). Without lingering, that user's systemd stops with the last login,
# and every stack with it.
loginctl enable-linger "$DEV_USER"
# OpenSSH fallback: the key given to the provider at order time works for
# the dev user too. Normal access is Tailscale SSH, which needs no keys.
if [ -s /root/.ssh/authorized_keys ] && [ ! -s "$DEV_HOME/.ssh/authorized_keys" ]; then
	install -m 600 -o "$DEV_USER" -g "$DEV_USER" /root/.ssh/authorized_keys "$DEV_HOME/.ssh/authorized_keys"
fi

log "sshd: key-only"
# A 00- drop-in wins over the
# provider's cloud-init drop-in, which may enable passwords. Refuse if root
# has no key, or this would lock us out of the public fallback.
if [ ! -s /root/.ssh/authorized_keys ]; then
	echo "FAIL: /root/.ssh/authorized_keys is empty; add a key before hardening sshd." >&2
	exit 1
fi
HC=/etc/ssh/sshd_config.d/00-devbox-hardening.conf
cat >"$HC.new" <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF
mv "$HC.new" "$HC"
# Ubuntu 24.04 starts sshd from ssh.socket, on demand. While it isn't running
# its runtime directory /run/sshd doesn't exist, and `sshd -t` refuses without
# it ("Missing privilege separation directory"); found on a `make update`
# after a reboot. A stopped sshd picks the new config up when the
# socket next starts it, so only a running one needs the reload.
install -d -m 755 /run/sshd
sshd -t
systemctl try-reload-or-restart ssh
sshd -T | grep -Ei '^(passwordauthentication|kbdinteractiveauthentication|permitrootlogin) '

log "tailscale"
command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
if [ "$(tailscale status --json 2>/dev/null | jq -r .BackendState)" != Running ]; then
	if [ ! -s /root/ts-authkey ]; then
		echo "FAIL: not on the tailnet and no /root/ts-authkey; use 'make bootstrap'." >&2
		exit 1
	fi
	tailscale up --auth-key=file:/root/ts-authkey --ssh \
		--hostname="$TS_HOSTNAME" --advertise-tags=tag:dev
fi
rm -f /root/ts-authkey
tailscale status | head -5

log "firewall"
ufw default deny incoming
ufw default allow outgoing
ufw allow in on tailscale0 comment 'tailnet (ACLs in tailnet/policy.hujson)'
ufw allow 41641/udp comment 'tailscale direct connections (lower latency than relays)'
# Public SSH is the fallback until `make lockdown` has been run once.
if [ -f "$STATE/public-ssh-closed" ]; then
	ufw delete allow 22/tcp >/dev/null 2>&1 || true
else
	ufw allow 22/tcp comment 'public ssh fallback, closed by make lockdown'
fi
# Containers' DNS queries are turned to devbox-egress's server on the Docker
# bridge address, from any Docker network (172.16.0.0/12, 192.168.0.0/16).
for net in 172.16.0.0/12 192.168.0.0/16; do
	for proto in udp tcp; do
		ufw allow proto "$proto" from "$net" to 172.17.0.1 port 53 comment 'containers to devbox-egress DNS' >/dev/null
	done
done
ufw --force enable
ufw status verbose

log "swap: room for a paused stack's memory"
# A small box holds one big stack. A stack that is paused (frozen, not
# stopped) keeps its memory, and without swap the next stack has nowhere to
# go; with it, the kernel moves the paused stack's pages to disk as the next
# one needs the room, and reads them back when it resumes. A paused web
# stack with four Angular dev servers was ~9.5 GB on disk; 16 GB filled up
# with two of them switching (one paused, one being paused). SWAP_SIZE in
# devbox.conf: every stack you'd have paused at once, with room to spare.
SWAPFILE=/swapfile
if [ -f "$SWAPFILE" ] && [ "$(stat -c %s "$SWAPFILE")" != "$(numfmt --from=iec "$SWAP_SIZE")" ]; then
	# Resizing: everything in it has to fit back in memory first.
	if swapon --show=NAME --noheadings | grep -qx "$SWAPFILE" && ! swapoff "$SWAPFILE"; then
		echo "FAIL: can't resize $SWAPFILE: what's in it doesn't fit in memory; stop a stack (ws stop) and rerun" >&2
		exit 1
	fi
	rm -f "$SWAPFILE"
fi
if [ ! -f "$SWAPFILE" ]; then
	fallocate -l "$SWAP_SIZE" "$SWAPFILE"
	chmod 600 "$SWAPFILE"
	mkswap "$SWAPFILE"
fi
swapon --show=NAME --noheadings | grep -qx "$SWAPFILE" || swapon "$SWAPFILE"
grep -q "^$SWAPFILE " /etc/fstab || echo "$SWAPFILE none swap sw 0 0" >>/etc/fstab
swapon --show

log "docker"
command -v docker >/dev/null || curl -fsSL https://get.docker.com | sh
# `-p 8080:80` binds to 127.0.0.1 unless a container asks for a specific
# address. Docker-published ports bypass ufw (Docker writes its own iptables rules);
# devbox-docker-guard below covers containers that bind 0.0.0.0 anyway.
DJ=/etc/docker/daemon.json
cat >"$DJ.new" <<'EOF'
{
  "ip": "127.0.0.1",
  "log-driver": "local"
}
EOF
if ! cmp -s "$DJ.new" "$DJ"; then
	mv "$DJ.new" "$DJ"
	systemctl restart docker
else
	rm "$DJ.new"
fi
usermod -aG docker "$DEV_USER"

log "gh"
if ! command -v gh >/dev/null; then
	install -d -m 755 /etc/apt/keyrings
	curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
		-o /etc/apt/keyrings/githubcli-archive-keyring.gpg
	chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
	echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
		>/etc/apt/sources.list.d/github-cli.list
	apt-get update -q
	apt-get install -y -q gh
fi

log "sops (pinned release binary; Ubuntu doesn't package it)"
SOPS_VERSION=3.13.3
SOPS_SHA256=e5bec3346a873ae91d871550f3e698c1aad962aff462a080e40f25fde17fef6b
if [ "$(sops --version 2>/dev/null | awk 'NR==1{print $2}')" != "$SOPS_VERSION" ]; then
	curl -fsSL -o /tmp/sops "https://github.com/getsops/sops/releases/download/v$SOPS_VERSION/sops-v$SOPS_VERSION.linux.amd64"
	echo "$SOPS_SHA256  /tmp/sops" | sha256sum -c -
	install -m 755 /tmp/sops /usr/local/bin/sops
	rm /tmp/sops
fi
# sops needs the plugin just to encrypt to the YubiKey recipients (no key
# required for that). 0.5.1 ships no Linux build; 0.5.0's recipients are
# the same format.
AGEYK_VERSION=0.5.0
AGEYK_SHA256=bf7a02418de04b3d3df9791e185d493eb344829bca4009247a41bc4d7630b47f
if ! dpkg -s age-plugin-yubikey 2>/dev/null | grep -q "^Version: $AGEYK_VERSION"; then
	deb=/tmp/age-plugin-yubikey_${AGEYK_VERSION}-1_amd64.deb
	curl -fsSL -o "$deb" "https://github.com/str4d/age-plugin-yubikey/releases/download/v$AGEYK_VERSION/age-plugin-yubikey_${AGEYK_VERSION}-1_amd64.deb"
	echo "$AGEYK_SHA256  $deb" | sha256sum -c -
	apt-get install -y -q "$deb"
	rm "$deb"
fi
log "mise (pinned release binary): the toolchains each project asks for"
# Versions per project come from dev-server workspace/projects.toml, which
# ws writes to <project>/mise.local.toml. The shims (on PATH in .zshenv)
# pick them by directory, so non-interactive shells (ssh devbox cmd,
# Claude's) get the project's node too.
MISE_VERSION=2026.11.9
MISE_SHA256=f13f3e5ec88807e0161669b3aa07a91d84478cdf1e95b5b92b63f85a25d42395
if [ "$(mise --version 2>/dev/null | awk '{print $1}')" != "$MISE_VERSION" ]; then
	curl -fsSL -o /tmp/mise "https://github.com/jdx/mise/releases/download/v$MISE_VERSION/mise-v$MISE_VERSION-linux-x64"
	echo "$MISE_SHA256  /tmp/mise" | sha256sum -c -
	install -m 755 /tmp/mise /usr/local/bin/mise
	rm /tmp/mise
fi
# The workspace's environment, in a block of .zshenv that every run rewrites
# from devbox.conf:
# - mise's shims, and ws, which lives in the dev-server clone (make sync
#   clones it).
# - WS_TAILNET: the box's stacks are used from the tablet and phone: ws up
#   publishes a project's `tailnet` ports with tailscale serve. Only here; on
#   the laptop ws leaves them private. Operator lets the dev user run
#   tailscale serve; it is in the docker group already, so this adds no real
#   power.
# - DEV_BIND_HOST: Tailscale serves those ports over HTTPS and holds them on
#   the tailnet address, so a project's servers must bind 127.0.0.1 here (a
#   0.0.0.0 bind fails as "in use"). Projects read DEV_BIND_HOST
#   (docs/PROJECT-CHECKLIST.md).
# - WS_SLICES: ws start runs each stack in its own systemd slice, so the
#   control plane (devbox-control, below) can measure it and it stops as a
#   whole.
# - The GitHub token picker's settings (devbox-github).
sed -i '/^# >>> devbox/,/^# <<< devbox/d' "$ZE"
cat >>"$ZE" <<EOF
# >>> devbox: written by bootstrap.sh from devbox.conf, every run. Edit there.
export PATH="\$HOME/.local/share/mise/shims:$DEVBOX_DIR/workspace:\$PATH"
export WS_ROOT="$PROJECTS_DIR"
export WS_TAILNET=1
export DEV_BIND_HOST=127.0.0.1
export WS_SLICES=1
export DEVBOX_GITHUB_OWNER="$GITHUB_DEFAULT_OWNER"
export DEVBOX_GITHUB_PROTECTED="${GITHUB_PROTECTED:-}"
# <<< devbox
EOF
tailscale set --operator="$DEV_USER"

log "caddy (pinned release binary): compression between Tailscale and the dev servers"
# Tailscale serves each project port over HTTPS to Caddy on
# 127.0.0.2:<port + 10000>, which compresses and passes it on to the server
# on 127.0.0.1:<port> (not the same port: some servers, like the Supabase
# CLI's containers, hold theirs on every address). Dev servers don't
# compress (an Angular dev build's main.js: 9 MB, 1.9 MB gzipped).
# The base Caddyfile imports sites/*.caddy, which ws writes per project.
CADDY_VERSION=2.11.9
CADDY_SHA512=0e85544340290a16ce4bfb76fe7d65e9de1bf3a6e67f5ccfff30b30d27ea4c73e078077b7bf3e58ae60b6b12e44327705287059c775bca5782e8ee85ea7edd75
if [ "$(caddy version 2>/dev/null | awk '{print $1}')" != "v$CADDY_VERSION" ]; then
	curl -fsSL -o /tmp/caddy.tar.gz "https://github.com/caddyserver/caddy/releases/download/v$CADDY_VERSION/caddy_${CADDY_VERSION}_linux_amd64.tar.gz"
	echo "$CADDY_SHA512  /tmp/caddy.tar.gz" | sha512sum -c -
	tar -xzf /tmp/caddy.tar.gz -C /tmp caddy
	install -m 755 /tmp/caddy /usr/local/bin/caddy
	rm -f /tmp/caddy.tar.gz /tmp/caddy
fi
CADDY_DIR="$DEV_HOME/.config/devbox/caddy"
install -d -m 755 -o "$DEV_USER" -g "$DEV_USER" "$DEV_HOME/.config" "$DEV_HOME/.config/devbox" "$CADDY_DIR" "$CADDY_DIR/sites"
cat >"$CADDY_DIR/Caddyfile" <<'EOF'
# Written by dev-server box/bootstrap.sh. Sites: sites/*.caddy, written by ws.
{
	# Plain http on loopback: Tailscale does TLS in front.
	auto_https off
	# 127.0.0.1, not localhost: the reload client resolves localhost to ::1
	admin 127.0.0.1:2019
	persist_config off
}

import sites/*.caddy
EOF
chown "$DEV_USER:$DEV_USER" "$CADDY_DIR/Caddyfile"

# The box's own age key (~/.config/sops/age/keys.txt) is not created here:
# restore it from wherever you keep secrets, so a rebuilt box keeps the same
# recipient and can read the dev secrets without re-encrypting anything
# (docs/ARCHITECTURE.md, "Secrets").

log "github: per-owner tokens for git and gh (devbox-github)"
# /usr/local/bin comes before /usr/bin, so the gh wrapper shadows the real gh.
install -m 755 "$HERE"/bin/* /usr/local/bin/
as_dev "
	git config --global user.name '$GIT_NAME'
	git config --global user.email '$GIT_EMAIL'
"
as_dev '
	git config --global init.defaultBranch main
	git config --global credential.https://github.com.helper devbox
	git config --global credential.https://github.com.useHttpPath true
	# Clone URLs copied from the laptop are SSH; the box has no GitHub SSH key.
	git config --global --unset-all url.https://github.com/.insteadOf || true
	git config --global --add url.https://github.com/.insteadOf git@github.com:
	git config --global --add url.https://github.com/.insteadOf ssh://git@github.com/
'

log "claude code (native installer; updates itself)"
[ -x "$DEV_HOME/.local/bin/claude" ] || as_dev 'curl -fsSL https://claude.ai/install.sh | bash'
# Every Claude session on the box reads the workspace guide (ws, ws run, the
# memory budget, the egress list) and, if you keep them in the dev-server
# repo, your own rules (claude/global.md), from the dev-server clone:
# symlinks in ~/.claude/rules/, which Claude Code loads like
# ~/.claude/CLAUDE.md, so a git pull updates them. (Not @-imports in
# ~/.claude/CLAUDE.md: Claude Code didn't load those from the user-level file
# when this was written.) Shared skills (claude/skills/*, one link per skill:
# ~/.claude/skills also holds others) and agents (claude/agents, the whole
# directory) come the same way. A real directory where a link goes would
# swallow it (ln puts the link inside), and its own copy would stay in force:
# that is reported, not overwritten.
as_dev "D='$DEVBOX_DIR'"'
	link() {
		if [ -d "$2" ] && [ ! -L "$2" ]; then
			echo "! $2 is a directory, not a link: replace it with a link to $1 by hand"
		else
			ln -sfn "$1" "$2"
		fi
	}
	mkdir -p ~/.claude/rules ~/.claude/skills
	link "$D/workspace/AGENTS.md" ~/.claude/rules/workspace.md
	[ ! -f "$D/claude/global.md" ] || link "$D/claude/global.md" ~/.claude/rules/global.md
	for s in "$D"/claude/skills/*/; do
		[ -d "$s" ] || continue
		s=${s%/}
		link "$s" ~/.claude/skills/"${s##*/}"
	done
	[ ! -d "$D/claude/agents" ] || link "$D/claude/agents" ~/.claude/agents
'
# The status line is the only documented place Claude Code reports the
# plan's 5-hour and weekly limits; ws statusline records each session's
# snapshot for the control plane (workspace/control/claude_usage.py). Set
# only if no status line is configured, so one of your own is kept.
as_dev "WS='$DEVBOX_DIR/workspace/ws'"' python3 - <<"EOF"
import json, os
p = os.path.expanduser("~/.claude/settings.json")
os.makedirs(os.path.dirname(p), exist_ok=True)
try:
    with open(p) as f:
        s = json.load(f)
except FileNotFoundError:
    s = {}
if "statusLine" not in s:
    s["statusLine"] = {"type": "command", "command": os.environ["WS"] + " statusline"}
    with open(p + ".new", "w") as f:
        json.dump(s, f, indent=2)
    os.replace(p + ".new", p)
EOF'

log "systemd units"
install -m 755 "$HERE/sbin/devbox-maintenance" /usr/local/sbin/devbox-maintenance
install -m 755 "$HERE/sbin/devbox-docker-guard" /usr/local/sbin/devbox-docker-guard
install -m 755 "$HERE/sbin/devbox-egress" /usr/local/sbin/devbox-egress
# The dev user's PATH has no sbin; its agents run request, pending, blocked.
ln -sfn /usr/local/sbin/devbox-egress /usr/local/bin/devbox-egress
# The units name the dev user, its home and uid, the dev-server clone and the
# time zone as @PLACEHOLDERS@, filled from devbox.conf here.
DEV_UID=$(id -u "$DEV_USER")
for f in "$HERE"/systemd/*; do
	sed -e "s|@DEV_USER@|$DEV_USER|g" -e "s|@DEV_HOME@|$DEV_HOME|g" -e "s|@DEV_UID@|$DEV_UID|g" \
		-e "s|@PROJECTS_DIR@|$PROJECTS_DIR|g" -e "s|@DEVBOX_DIR@|$DEVBOX_DIR|g" \
		-e "s|@TZ_NAME@|$TZ_NAME|g" -e "s|@MAINTENANCE_DUE@|$MAINTENANCE_DUE|g" \
		"$f" >"/etc/systemd/system/${f##*/}.new"
	if grep -v '^#' "/etc/systemd/system/${f##*/}.new" | grep -q '@[A-Z_]*@'; then
		echo "FAIL: ${f##*/} has a placeholder devbox.conf doesn't fill" >&2; exit 1
	fi
	chmod 644 "/etc/systemd/system/${f##*/}.new"
	mv "/etc/systemd/system/${f##*/}.new" "/etc/systemd/system/${f##*/}"
done
systemctl daemon-reload
systemctl enable --now devbox-docker-guard.service
systemctl enable devbox-caddy.service
systemctl restart devbox-caddy.service   # picks up a new binary or base Caddyfile
# Runs from the dev-server clone (make sync clones it); until then it
# retries every 30 s.
systemctl enable devbox-control.service
systemctl restart devbox-control.service
# After a boot, the stacks as they were (ws restore). Enabled, not started:
# it runs at boot. What each stack is now is recorded once, so the next
# reboot has it before any start, pause or stop records it (ws remember).
systemctl enable devbox-restore.service
runuser -u "$DEV_USER" -- env XDG_RUNTIME_DIR="/run/user/$DEV_UID" \
	DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$DEV_UID/bus" zsh -c 'ws remember' \
	|| echo "ws remember failed: the next reboot may not bring the stacks back"
systemctl enable --now devbox-maintenance-mark.timer devbox-maintenance-apply.timer

log "outbound allowlist (devbox-egress)"
# The list comes from the repo. The box's own additions, the mode, and the
# requests the dev user's agents make live in $STATE.
install -d -m 755 "$STATE/egress" /var/log/devbox-egress
install -m 644 "$HERE/egress.allow" /etc/devbox/egress.allow
install -d -m 755 -o "$DEV_USER" -g "$DEV_USER" "$STATE/egress-requests"
[ -f "$STATE/egress/mode" ] || echo "$EGRESS_MODE" >"$STATE/egress/mode"
cat >/etc/logrotate.d/devbox-egress <<'EOF'
/var/log/devbox-egress/dns.log {
	daily
	rotate 7
	compress
	delaycompress
	missingok
	notifempty
	copytruncate
}
EOF
systemctl enable devbox-egress.service
devbox-egress apply
devbox-egress status
systemctl list-timers 'devbox-*' --no-pager

log "done"
cat <<EOF
Next, as $DEV_USER (ssh $DEV_USER@$TS_HOSTNAME):
  claude            # log in with your Claude account
Then, from the laptop: make github-tokens, make sync (docs/SETUP.md).
EOF
