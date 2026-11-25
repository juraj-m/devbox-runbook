# Run from the laptop. The box is rebuilt, never repaired: reinstall Ubuntu in
# the provider's panel (with your SSH key), then `make bootstrap HOST=<ip>`.
# Settings: devbox.conf (DEV_USER, TS_HOSTNAME, DEVBOX_DIR, DEVBOX_REPO).
SHELL := /bin/bash
conf = $(shell . $(CURDIR)/devbox.conf && printf %s "$$$(1)")
DEV_USER := $(call conf,DEV_USER)
HOST ?= $(call conf,TS_HOSTNAME)
DEVBOX_DIR := $(call conf,DEVBOX_DIR)
DEVBOX_REPO := $(call conf,DEVBOX_REPO)
BOX := root@$(HOST)
DEV := $(DEV_USER)@$(HOST)
# Ship this directory as a tarball, so a fresh image needs no rsync.
# COPYFILE_DISABLE and the --no-* flags keep macOS tar from shipping ._ files
# and xattrs, which GNU tar on the box warns about (harmless on Linux: drop
# them there if your tar refuses them).
push = COPYFILE_DISABLE=1 tar --no-mac-metadata --no-xattrs -C $(CURDIR) -czf - bootstrap.sh devbox.conf egress.allow bin sbin systemd | ssh $(BOX) 'rm -rf /root/devbox && mkdir -p /root/devbox && tar -C /root/devbox -xzf -'

.PHONY: bootstrap update sync lockdown unlock status ssh github-tokens github-check egress-sync

# The box's fine-grained GitHub tokens (docs/ARCHITECTURE.md, "GitHub
# access"): one per owner for code and PRs, and a read-only one per secrets
# repo. GitHub has no API to create tokens; this opens each form pre-filled,
# you set Repository access, Generate, and paste into the box. The token goes
# straight to the box, never through the laptop.
#   make github-tokens                          all of them
#   make github-tokens TOKENS=my-org/secrets    just these
# Set these to your account and orgs (here, or on the command line).
GITHUB_OWNERS ?= $(call conf,GITHUB_DEFAULT_OWNER)
# Read-only, one repo each. The owner tokens must leave these out (devbox.conf
# GITHUB_PROTECTED, which devbox-github checks).
GITHUB_READ_REPOS ?= $(call conf,GITHUB_PROTECTED)
TOKENS ?= $(GITHUB_OWNERS) $(GITHUB_READ_REPOS)
# actions=write: an agent re-runs a flaky failed check (gh run rerun).
GITHUB_PERMS := contents=write&pull_requests=write&issues=write&workflows=write&actions=write&statuses=read
GITHUB_READ_PERMS := contents=read
# 364 days: GitHub allows 366, but an org lifetime policy can cap it lower.
# $(1) name, $(2) owner, $(3) permissions.
GITHUB_TOKEN_URL =https://github.com/settings/personal-access-tokens/new?name=devbox%20$(1)&description=Coding%20box&target_name=$(2)&expires_in=364&$(3)
# The browser opener: open on macOS, xdg-open on Linux.
OPEN ?= $(shell command -v open || command -v xdg-open)

github-tokens:
	@[ -t 0 ] || { echo "needs an interactive terminal; run it in a terminal, not through an agent"; exit 1; }
	@for t in $(TOKENS); do \
		o=$${t%%/*}; echo; \
		case $$t in \
		*/*) echo "=== $$t (read-only): Repository access > Only select repositories > $${t#*/} ONLY. Generate, copy."; \
		     $(OPEN) '$(call GITHUB_TOKEN_URL,read%20'"$$o"'%2F'"$${t#*/}"','"$$o"',$(GITHUB_READ_PERMS))' ;; \
		*)   ex=$$(for r in $(GITHUB_READ_REPOS); do [ "$${r%%/*}" = "$$o" ] && printf ' %s' "$${r#*/}"; done); \
		     if [ -n "$$ex" ]; then echo "=== $$o: Repository access > Only select repositories > EVERY repo EXCEPT$$ex. Generate, copy."; \
		     else echo "=== $$o: Repository access > All repositories. Generate, copy."; fi; \
		     $(OPEN) '$(call GITHUB_TOKEN_URL,'"$$o"','"$$o"',$(GITHUB_PERMS))' ;; \
		esac; \
		ssh -t $(DEV) devbox-github set "$$t" || exit 1; \
	done
	@echo; echo "Org tokens stay pending until approved: Org > Settings > Personal access tokens > Pending requests."

# Lockdown check, run by the box: no token on it can push to a protected repo.
github-check:
	@ssh $(DEV) devbox-github check

# First run on a fresh VPS, over public SSH. Asks for a Tailscale auth key:
# admin console > Settings > Keys, one-off + pre-approved + tag:dev (signed
# first if Tailnet Lock is on).
bootstrap:
	@[ "$(HOST)" != "$(call conf,TS_HOSTNAME)" ] || { echo "usage: make bootstrap HOST=<public ip>"; exit 1; }
	@[ -t 0 ] || { echo "needs an interactive terminal (it prompts for the auth key)"; exit 1; }
	@read -rsp 'Tailscale auth key: ' key && echo && \
		ssh $(BOX) 'umask 077 && cat >/root/ts-authkey' <<<"$$key"
	$(push)
	ssh -t $(BOX) /root/devbox/bootstrap.sh

# Every later change, over the tailnet.
update: sync
	$(push)
	ssh -t $(BOX) /root/devbox/bootstrap.sh

# The box runs ws, the control page and the skills from its own clone of
# this repo (DEVBOX_DIR), not from the tarball: clone it if it isn't there,
# then bring it to the commit this laptop is on, before bootstrap restarts
# the page and links the skills. The commit must be on GitHub (the box pulls
# from there) and be origin/main, so the box never runs something older or
# newer than what's shipped from here.
sync:
	@git -C $(CURDIR)/.. fetch -q origin
	@head=$$(git -C $(CURDIR)/.. rev-parse HEAD); main=$$(git -C $(CURDIR)/.. rev-parse origin/main); \
	if [ "$$head" != "$$main" ]; then \
		if git -C $(CURDIR)/.. merge-base --is-ancestor HEAD origin/main; then \
			echo "this repo is behind origin/main: git pull first" >&2; \
		else echo "this repo has commits not on origin/main: push them first" >&2; fi; exit 1; fi; \
	git -C $(CURDIR)/.. diff --quiet HEAD -- . ':!box' || \
		echo "note: uncommitted changes outside box/ stay here; the box runs $${head:0:7}"; \
	ssh $(DEV) "set -e; d=$(DEVBOX_DIR); \
		[ -d \$$d ] || git clone -q $(DEVBOX_REPO) \$$d; \
		git -C \$$d fetch -q origin; \
		git -C \$$d merge -q --ff-only $$head || { echo 'the box clone has its own commits or changes: sort them out there (ssh $(DEV))' >&2; exit 1; }; \
		[ \"\$$(git -C \$$d rev-parse HEAD)\" = $$head ] || { echo 'the box clone is ahead of $${head:0:7}: it has commits of its own' >&2; exit 1; }; \
		echo \"box clone: \$$(git -C \$$d log -1 --format='%h %s')\""

# Close public SSH. Only after Tailscale SSH works AND the provider's
# console (VNC, web console, rescue mode) has been used once on purpose.
lockdown:
	ssh $(BOX) 'mkdir -p /var/lib/devbox && touch /var/lib/devbox/public-ssh-closed && ufw delete allow 22/tcp; ufw status'

unlock:
	ssh $(BOX) 'rm -f /var/lib/devbox/public-ssh-closed && ufw allow 22/tcp; ufw status'

# The box's own additions to the outbound allowlist (devbox-egress allow, or
# approved requests) into egress.allow, so a rebuilt box has them too. The
# box already uses them; commit the file. Read as root, not as the dev user,
# whose shell an agent can change; the script checks each entry, and nothing
# is added without a yes.
egress-sync:
	@new=$$(ssh $(BOX) devbox-egress unsynced <egress.allow) || exit 1; \
	if [ -z "$$new" ]; then echo "egress.allow already has everything the box added"; exit 0; fi; \
	echo "The box added (devbox-egress allow, or approved requests):"; printf '  %s\n' $$new; \
	printf 'Add them to egress.allow? [y/N] '; read a; \
	case "$$a" in [yY]*) ;; *) echo "nothing added"; exit 0 ;; esac; \
	grep -qx '## Added on the box' egress.allow || printf '\n## Added on the box\n' >>egress.allow; \
	printf '%s\n' $$new >>egress.allow; \
	echo "added; commit egress.allow"

status:
	ssh $(BOX) 'devbox-maintenance status; free -h; df -h /; tailscale status | head'

ssh:
	ssh $(DEV)
