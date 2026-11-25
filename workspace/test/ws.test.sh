#!/usr/bin/env bash
#
# Tests for workspace/ws. Offline: "GitHub" is bare repos in a temp dir
# (git's insteadOf maps git@github.com: there), mise and docker are stubs
# that log their calls, and the manifest is written here.
#
#   bash workspace/test/ws.test.sh

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Physical path: on macOS /var is a symlink, and the steps see /private/var.
WORK="$(cd "$(mktemp -d)" && pwd -P)"
trap 'rm -rf "$WORK"' EXIT

# The box's login opts in to these (~/.zshenv); each test sets what it means.
unset WS_TAILNET WS_SLICES DEV_BIND_HOST

pass=0; fail=0
eq()  { if [ "$2" = "$3" ]; then pass=$((pass + 1)); echo "  ok    $1"; else fail=$((fail + 1)); echo "  FAIL  $1: got '$2', want '$3'"; fi; }
has() { if printf '%s' "$2" | grep -q -- "$3"; then pass=$((pass + 1)); echo "  ok    $1"; else fail=$((fail + 1)); echo "  FAIL  $1: no '$3' in: $2"; fi; }
hasnt() { if printf '%s' "$2" | grep -q -- "$3"; then fail=$((fail + 1)); echo "  FAIL  $1: '$3' in: $2"; else pass=$((pass + 1)); echo "  ok    $1"; fi; }

export GIT_CONFIG_GLOBAL="$WORK/gitconfig" GIT_CONFIG_NOSYSTEM=1
git config --global user.name t; git config --global user.email t@t
git config --global init.defaultBranch main
git config --global protocol.file.allow always
git config --global url."$WORK/gh/".insteadOf git@github.com:

# A repo on "GitHub": OWNER/NAME with one file, optionally a submodule.
mkrepo() {
  local src="$WORK/src/$1"
  mkdir -p "$src" && git -C "$src" init -q && echo "$1" > "$src/README"
  [ -n "${2:-}" ] && git -C "$src" submodule -q add "git@github.com:$2.git" "${2#*/}"
  git -C "$src" add -A && git -C "$src" commit -q -m init
  mkdir -p "$(dirname "$WORK/gh/$1.git")" && git clone -q --bare "$src" "$WORK/gh/$1.git"
}
mkrepo acme/lib
mkrepo acme/app acme/lib
mkrepo acme/secrets
mkrepo me/one
mkrepo me/two

mkdir -p "$WORK/bin"
cat > "$WORK/bin/mise" <<'STUB'
#!/usr/bin/env bash
echo "mise $* (in $PWD)" >> "$LOG"
case $1 in
  exec) shift; [ "$1" = -- ] && shift; exec "$@" ;;
  trust|install|--version) exit 0 ;;
esac
STUB
cat > "$WORK/bin/docker" <<'STUB'
#!/usr/bin/env bash
[ -e "$NO_DOCKER" ] && exit 1; exit 0
STUB
chmod +x "$WORK/bin/"*
export PATH="$WORK/bin:$PATH" LOG="$WORK/log" NO_DOCKER="$WORK/no-docker"
export WS_ROOT="$WORK/root" WS_MANIFEST="$WORK/projects.toml" WS_STATE="$WORK/state"
cat > "$WS_MANIFEST" <<'TOML'
[acme-secrets]
repo = "acme/secrets"
prepare = ["echo secrets-setup >> $LOG"]

[app]
about = "the app"
repo = "acme/app"
needs = ["acme-secrets", "docker"]
tools = { node = "24", pnpm = "10" }
prepare = ["echo step1 in $PWD >> $LOG", "echo step2 >> $LOG; test ! -e $WS_ROOT/../fail-step2", "echo step3 >> $LOG"]
start = ["echo started >> $LOG"]
verify = "echo verified >> $LOG; test ! -e $WS_ROOT/../unhealthy"

[app.optional.assets]
about = "big files"
how = "rsync them from the Mac"

[folder]
dir = "folder"
repos = { one = "me/one", two = "me/two" }
TOML
ws() { OUT=$(python3 -I "$HERE/ws" "$@" 2>&1); RC=$?; }

echo "plan: the steps, in order, changing nothing"
ws plan app
eq "exit 0" "$RC" 0
has "dependencies come first" "$(printf '%s' "$OUT" | head -1)" "^acme-secrets"
has "clone step" "$OUT" "clone    acme/app into .*/root/app, with submodules"
has "tools step" "$OUT" "tools    node 24, pnpm 10 (mise)"
has "prepare steps, in order" "$(printf '%s' "$OUT" | sed -n '/^app/,$p' | grep prepare | head -1)" "step1"
has "machine need checked" "$OUT" "needs    docker     ok"
has "the project's own start" "$OUT" "start    echo started"
has "the project's own verify" "$OUT" "ws check app"
has "optional, by hand" "$OUT" "by hand: rsync them from the Mac"
eq "nothing created" "$(ls "$WS_ROOT" 2>/dev/null)" ""

echo "up: needs first, clone with submodules, tools, prepare in order"
: > "$LOG"
ws up app
eq "exit 0" "$RC" 0
eq "dependency cloned" "$(cat "$WS_ROOT/acme-secrets/README")" acme/secrets
eq "submodule checked out" "$(cat "$WS_ROOT/app/lib/README")" acme/lib
has "mise file written" "$(cat "$WS_ROOT/app/mise.local.toml")" 'node = "24"'
eq "mise file not untracked noise" "$(git -C "$WS_ROOT/app" status --porcelain)" ""
has "mise trusted and installed in the project" "$(cat "$LOG")" "mise install (in $WS_ROOT/app)"
eq "order: dependency's prepare, then the steps" "$(grep -E '^(secrets-setup|step[0-9])' "$LOG" | cut -d' ' -f1 | tr '\n' ' ')" "secrets-setup step1 step2 step3 "
has "prepare runs in the project, through mise" "$(cat "$LOG")" "step1 in $WS_ROOT/app"
has "...exec" "$(cat "$LOG")" "mise exec -- bash"
has "points at start" "$OUT" "ws start app"
hasnt "start not run by up" "$(cat "$LOG")" started

echo "up again: nothing recloned, prepare runs again"
echo local-work > "$WS_ROOT/app/mine"
: > "$LOG"
ws up app
eq "exit 0" "$RC" 0
has "clone done" "$OUT" "clone    acme/app .*: done"
eq "local files kept" "$(cat "$WS_ROOT/app/mine")" local-work
eq "prepare ran again" "$(grep -c '^step' "$LOG")" 3
hasnt "tools unchanged: not reinstalled" "$(cat "$LOG")" "mise install"
rm "$WS_ROOT/app/mine"

echo "a failing step stops the run"
touch "$WORK/fail-step2"; : > "$LOG"
ws up app
eq "exit 1" "$RC" 1
has "names the step" "$OUT" "failed: echo step2"
hasnt "later steps not run" "$(cat "$LOG")" step3
rm "$WORK/fail-step2"

echo "a machine need is missing: nothing happens"
touch "$NO_DOCKER"; rm -rf "$WS_ROOT"; : > "$LOG"
ws up app
eq "exit 1" "$RC" 1
has "says what's missing" "$OUT" "lacks Docker"
eq "nothing cloned" "$(ls "$WS_ROOT" 2>/dev/null)" ""
ws plan app
has "plan shows it missing" "$OUT" "needs    docker     MISSING"
rm "$NO_DOCKER"

echo "an existing clone: only missing submodules are checked out"
ws up app >/dev/null
git -C "$WS_ROOT/app/lib" checkout -q -b mywork && echo wip > "$WS_ROOT/app/lib/wip" && git -C "$WS_ROOT/app/lib" add wip && git -C "$WS_ROOT/app/lib" commit -q -m wip
ws up app
eq "exit 0" "$RC" 0
eq "submodule's own branch kept" "$(git -C "$WS_ROOT/app/lib" branch --show-current)" mywork
git -C "$WS_ROOT/app" submodule -q deinit -f lib
ws plan app
hasnt "plan: clone not done while a submodule is missing" "$OUT" "acme/app into .* with submodules  \[done\]"
ws up app
eq "missing submodule checked out again" "$(cat "$WS_ROOT/app/lib/README")" acme/lib

echo "a folder of repos"
ws up folder
eq "exit 0" "$RC" 0
eq "first repo" "$(cat "$WS_ROOT/folder/one/README")" me/one
eq "second repo" "$(cat "$WS_ROOT/folder/two/README")" me/two

echo "a root repo with repos inside it (workspace files + its repos)"
mkrepo me/root
cat >> "$WS_MANIFEST" <<'TOML'

[rooted]
dir = "rooted"
repo = "me/root"
repos = { one = "me/one", two = "me/two" }
TOML
ws plan rooted
has "plan: the root first" "$(printf '%s' "$OUT" | grep clone | head -1)" "me/root into .*/root/rooted,"
ws up rooted
eq "exit 0" "$RC" 0
eq "root repo" "$(cat "$WS_ROOT/rooted/README")" me/root
eq "a repo inside it" "$(cat "$WS_ROOT/rooted/one/README")" me/one
eq "the other" "$(cat "$WS_ROOT/rooted/two/README")" me/two
ws up rooted
has "again: all done" "$(printf '%s' "$OUT" | grep -c ': done')" 3

echo "start and check: the project's own commands"
: > "$LOG"
ws start app
eq "start: exit 0" "$RC" 0
has "start ran" "$(cat "$LOG")" started
ws check app
eq "check: healthy, exit 0" "$RC" 0
touch "$WORK/unhealthy"
ws check app
eq "check: unhealthy, exit 1" "$RC" 1
rm "$WORK/unhealthy"
ws check folder
eq "no verify: exit 1" "$RC" 1

echo "optional parts"
ws up app --with assets
eq "manual optional: exit 0" "$RC" 0
has "says it's by hand" "$OUT" "assets: not automated. By hand: rsync"
ws up app --with nope
eq "unknown optional: exit 1" "$RC" 1
has "lists the real ones" "$OUT" "has: assets"

echo "startup files bash reads before the command aren't held to set -u"
# Over SSH, bash reads /etc/bash.bashrc first, which uses an unset PS1.
echo 'x=$NOT_SET_ANYWHERE' > "$WORK/bashrc"
BASH_ENV="$WORK/bashrc" ws start app
eq "exit 0" "$RC" 0
hasnt "no unbound-variable noise" "$OUT" "unbound variable"
echo 'echo $NOT_SET_ANYWHERE' > "$WORK/strict.sh"
cat >> "$WS_MANIFEST" <<TOML

[strict]
dir = "app"
start = ["bash $WORK/strict.sh"]
verify = "x=\$NOT_SET_ANYWHERE"
TOML
ws check strict
eq "but the step itself still runs with set -u" "$RC" 1

echo "tailnet ports: published only where WS_TAILNET=1 (the box), never on the Mac"
# tailscale stub: serve status as the real one prints it (TCP + Web), kept
# in $WORK_SERVE; HTTPS and TCP forwards can be added and turned off.
cat > "$WORK/bin/tailscale" <<'STUB'
#!/usr/bin/env bash
echo "tailscale $*" >> "$LOG"
H=devbox.example.ts.net
upd() { python3 -c "import json,os,sys; f=os.environ['WORK_SERVE']; d=json.load(open(f)) if os.path.exists(f) else {}; exec(sys.argv[1]); json.dump(d,open(f,'w'))" "$1"; }
case "$*" in
  "status --self --json") echo "{\"Self\":{\"DNSName\":\"$H.\"}}" ;;
  "serve status --json") cat "$WORK_SERVE" 2>/dev/null || echo '{}' ;;
  "serve --bg --https="*" http://127.0.0."*)
    p=${3#--https=}; [ -n "$(upd "print(1) if d.get('TCP',{}).get('$p',{}).get('TCPForward') else None")" ] && { echo "port $p is a TCP forward" >&2; exit 1; }
    upd "d.setdefault('TCP',{})['$p']={'HTTPS':True}; d.setdefault('Web',{})['$H:$p']={'Handlers':{'/':{'Proxy':'$4'}}}" ;;
  "serve --tcp="*" off") p=${2#--tcp=}; upd "d.get('TCP',{}).pop('$p',None)" ;;
  "serve --https="*" off") p=${2#--https=}; upd "d.get('TCP',{}).pop('$p',None); d.get('Web',{}).pop('$H:$p',None)" ;;
esac
STUB
chmod +x "$WORK/bin/tailscale"
cat > "$WORK/bin/caddy" <<'STUB'
#!/usr/bin/env bash
echo "caddy $*" >> "$LOG"
case $1 in version) echo v2.11.9 ;; esac
STUB
chmod +x "$WORK/bin/caddy"
export WORK_SERVE="$WORK/serve.json" WS_CADDY_DIR="$WORK/caddy"
mkdir -p "$WS_CADDY_DIR/sites"; : > "$WS_CADDY_DIR/Caddyfile"
cat >> "$WS_MANIFEST" <<'TOML'

[tn]
dir = "app"
tailnet = [54321]
TOML
: > "$LOG"
ws up tn
eq "not opted in: exit 0" "$RC" 0
hasnt "not opted in: tailscale untouched" "$(cat "$LOG")" "tailscale serve"
ws plan tn
has "plan says why not" "$OUT" "tailnet  54321: not on this machine (WS_TAILNET=1 opts in)"
hasnt "not opted in: no caddy site" "$(ls "$WS_CADDY_DIR/sites")" "tn.caddy"
: > "$LOG"
WS_TAILNET=1 ws up tn
eq "opted in: exit 0" "$RC" 0
# Caddy compresses (the dev servers don't): it listens on 127.0.0.2 on the
# port + 10000 (a server may hold its own port on every address, as the
# Supabase CLI's containers do) and proxies to the server on 127.0.0.1.
SITE="$WS_CADDY_DIR/sites/tn.caddy"
has "caddy site: any host name, port + 10000" "$(cat "$SITE" 2>/dev/null)" "http://:64321 {"
has "caddy site: only on 127.0.0.2" "$(cat "$SITE" 2>/dev/null)" "bind 127.0.0.2"
has "caddy site: compresses" "$(cat "$SITE" 2>/dev/null)" "encode zstd gzip"
has "caddy site: proxies to the server's own port" "$(cat "$SITE" 2>/dev/null)" "reverse_proxy 127.0.0.1:54321"
has "caddy reloaded" "$(cat "$LOG")" "caddy reload --config $WS_CADDY_DIR/Caddyfile"
has "tailscale serves HTTPS on the project's port, to caddy" "$(cat "$LOG")" "tailscale serve --bg --https=54321 http://127.0.0.2:64321"
has "says the URL" "$OUT" "https://devbox.example.ts.net:54321"
: > "$LOG"
WS_TAILNET=1 ws up tn
has "already published: done" "$OUT" "tailnet  .*54321.*: done"
hasnt "and not published twice" "$(cat "$LOG")" "serve --bg"
hasnt "caddy unchanged: not reloaded" "$(cat "$LOG")" "caddy reload"
echo '{"TCP":{"54321":{"HTTPS":true}},"Web":{"devbox.example.ts.net:54321":{"Handlers":{"/":{"Proxy":"http://127.0.0.1:54321"}}}}}' > "$WORK_SERVE"; : > "$LOG"
WS_TAILNET=1 ws up tn
has "served straight to the server (before caddy): re-pointed at caddy" "$(cat "$LOG")" "serve --bg --https=54321 http://127.0.0.2:64321"
echo '{"TCP":{"54321":{"TCPForward":"127.0.0.1:54321"}}}' > "$WORK_SERVE"; : > "$LOG"
WS_TAILNET=1 ws up tn
eq "an old plain TCP forward: exit 0" "$RC" 0
has "is turned off" "$(cat "$LOG")" "tailscale serve --tcp=54321 off"
has "and replaced by HTTPS" "$(cat "$LOG")" "serve --bg --https=54321"

# Starting: Tailscale holds a served port on the tailnet address, and a
# server that asks for it on every address (the Supabase CLI's containers
# publish 0.0.0.0) fails with "address already in use" if Tailscale was
# there first; the other way round they coexist. So the ports are off while
# the start commands run, and served again after, also when one fails.
cat >> "$WS_MANIFEST" <<'TOML'

[tnstart]
dir = "app"
tailnet = [54321, 54323]
start = "python3 -c \"import json,os; d=json.load(open(os.environ['WORK_SERVE'])); print('served while starting:', sorted(d.get('TCP',{})))\""
TOML
WS_TAILNET=1 ws up tnstart >/dev/null; : > "$LOG"
WS_TAILNET=1 ws start tnstart
eq "start with tailnet ports: exit 0" "$RC" 0
has "its ports are off while it starts" "$OUT" "served while starting: \[\]"
has "and served again after" "$(cat "$WORK_SERVE")" '"54323": {"HTTPS": true}'
has "54321 too" "$(cat "$WORK_SERVE")" '"54321": {"HTTPS": true}'
cat >> "$WS_MANIFEST" <<'TOML'

[tnfail]
dir = "app"
tailnet = [54325]
start = "exit 3"
TOML
WS_TAILNET=1 ws up tnfail >/dev/null
WS_TAILNET=1 ws start tnfail
eq "a failed start still fails" "$RC" 1
has "but its port is served again" "$(cat "$WORK_SERVE")" '"54325": {"HTTPS": true}'
: > "$LOG"
ws start tnstart
hasnt "not opted in: start leaves tailscale alone" "$(cat "$LOG")" "tailscale serve"
# A served command returns at once, but its server binds later (astro dev:
# seconds): the port stays off until it answers, or Vite sees Tailscale's
# listener and takes the next port.
cat > "$WORK/srv.py" <<'PY'
import json, os, socket, sys, time
time.sleep(1.5)
served = sorted(json.load(open(os.environ["WORK_SERVE"])).get("TCP", {}))
open(os.environ["LOG"] + ".tnserve", "w").write(f"served when it bound: {served}\n")
s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", int(sys.argv[1]))); s.listen()
while True:
    s.accept()[0].close()
PY
cat >> "$WS_MANIFEST" <<'TOML'

[tnserve]
dir = "app"
tailnet = [54327]
serve = { dev = "exec python3 $WS_ROOT/../srv.py 54327" }
TOML
WS_TAILNET=1 ws up tnserve >/dev/null
WS_SERVE_CHECK=0.2 WS_TAILNET=1 ws start tnserve
eq "serve with a tailnet port: exit 0" "$RC" 0
has "its port is off until its server answers" "$(cat "$LOG.tnserve")" "served when it bound: \['54321', '54323', '54325'\]"
has "then served again" "$(cat "$WORK_SERVE")" '"54327": {"HTTPS": true}'
ws stop tnserve >/dev/null
cat >> "$WS_MANIFEST" <<'TOML'

[tnsilent]
dir = "app"
tailnet = [54329]
serve = { dev = "exec sleep 30" }
TOML
WS_TAILNET=1 ws up tnsilent >/dev/null
WS_SERVE_WAIT=1 WS_SERVE_CHECK=0.2 WS_TAILNET=1 ws start tnsilent
eq "one whose port never answers: exit 0" "$RC" 0
has "  says so" "$OUT" "54329 isn't answering after 1 s; served on the tailnet anyway"
has "  and serves it anyway" "$(cat "$WORK_SERVE")" '"54329": {"HTTPS": true}'
ws stop tnsilent >/dev/null
cat >> "$WS_MANIFEST" <<'TOML'

[toohigh]
dir = "app"
tailnet = [60000]
TOML
WS_TAILNET=1 ws up toohigh
eq "a port with no room for + 10000: exit 1" "$RC" 1
has "says so" "$OUT" "60000"

echo "an optional part that only starts something (ws start --with)"
cat >> "$WS_MANIFEST" <<'TOML'

[withapp]
dir = "app"
start = ["echo base-start >> $LOG"]
verify = "true"

[withapp.optional.ui]
about = "the UI, in the background"
start = "echo ui-start >> $LOG"
TOML
: > "$LOG"
ws start withapp
eq "plain start: exit 0" "$RC" 0
eq "plain start: base only" "$(grep -E '^(base|ui)-start' "$LOG" | tr '\n' ' ')" "base-start "
: > "$LOG"
ws start withapp --with ui
eq "--with ui: exit 0" "$RC" 0
eq "--with ui: base, then ui" "$(grep -E '^(base|ui)-start' "$LOG" | tr '\n' ' ')" "base-start ui-start "
ws up withapp --with ui
eq "up --with ui: exit 0" "$RC" 0
hasnt "up: nothing 'by hand' about a start-only part" "$OUT" "not automated"
ws plan withapp
has "plan names the start option" "$OUT" "ws start withapp --with ui"
ws start withapp --with nope
eq "unknown option: exit 1" "$RC" 1

echo "start and prepare given as one string, not a list"
cat >> "$WS_MANIFEST" <<'TOML'

[onestring]
dir = "app"
prepare = "echo one-prepare >> $LOG"
start = "echo one-start >> $LOG"
TOML
: > "$LOG"
ws start onestring
eq "start: exit 0" "$RC" 0
eq "start ran the whole command once" "$(grep -c '^one-start$' "$LOG")" 1
ws up onestring
eq "up: exit 0" "$RC" 0
eq "prepare ran the whole command once" "$(grep -c '^one-prepare$' "$LOG")" 1

echo "start in systemd units where WS_SLICES=1 (the box), one per named command"
# Stubs: systemd-run logs and runs the command after --; systemctl and
# journalctl log; tmux logs, and has a server only when $TMUX_UP exists.
cat > "$WORK/bin/systemd-run" <<'STUB'
#!/usr/bin/env bash
echo "systemd-run $*" >> "$LOG"
while [ $# -gt 0 ] && [ "$1" != -- ]; do case $1 in --working-directory=*) cd "${1#*=}" ;; esac; shift; done; shift
exec "$@"
STUB
cat > "$WORK/bin/systemctl" <<'STUB'
#!/usr/bin/env bash
echo "systemctl $*" >> "$LOG"
STUB
cat > "$WORK/bin/journalctl" <<'STUB'
#!/usr/bin/env bash
case "$*" in *--show-cursor*) echo "-- cursor: c1" ;; *) echo "journalctl $*" >> "$LOG" ;; esac
STUB
cat > "$WORK/bin/tmux" <<'STUB'
#!/usr/bin/env bash
echo "tmux $*" >> "$LOG"
case "$*" in *has-session*) [ -e "$TMUX_UP" ] ;; esac
STUB
chmod +x "$WORK/bin/"*
export TMUX_UP="$WORK/tmux-up"
cat >> "$WS_MANIFEST" <<'TOML'

[svc]
dir = "app"
start = { db = "echo db-start >> $LOG", web = "echo web-start in $PWD >> $LOG; test ! -e $WS_ROOT/../fail-web" }
stop = "echo svc-stop >> $LOG"

[svc.optional.ui]
start = "echo ui-start >> $LOG"

[my-app]
dir = "app"
start = { db = "true" }
TOML
: > "$LOG"
DEV_BIND_HOST=127.0.0.1 SSH_CONNECTION="1.2.3.4 5 6.7.8.9 22" WS_SLICES=1 ws start svc
eq "exit 0" "$RC" 0
has "each command is a unit in the stack's servers slice" "$(cat "$LOG")" "systemd-run --user --quiet --unit=ws-svc-db.service --slice=ws-svc-servers.slice"
has "a oneshot that stays active, so what it starts in the background stays in it" "$(cat "$LOG")" "Type=oneshot -p RemainAfterExit=yes"
has "runs in the project's folder" "$(cat "$LOG")" "web-start in $WS_ROOT/app"
eq "in the manifest's order" "$(grep -E '^(db|web)-start' "$LOG" | cut -d' ' -f1 | tr '\n' ' ')" "db-start web-start "
test "$(grep -n 'stop ws-svc-db.service' "$LOG" | cut -d: -f1)" -lt "$(grep -n 'unit=ws-svc-db.service' "$LOG" | cut -d: -f1)" 2>/dev/null
eq "what's left of a previous start is stopped first" "$?" 0
has "the environment passes by name (values stay off the command line)" "$(cat "$LOG")" "--setenv=DEV_BIND_HOST "
hasnt "  no value" "$(cat "$LOG")" "DEV_BIND_HOST=127"
hasnt "the login's own variables stay behind" "$(cat "$LOG")" "setenv=SSH_CONNECTION"
: > "$LOG"
WS_SLICES=1 ws start svc --with ui
has "an optional start is a unit too, named for it" "$(cat "$LOG")" "unit=ws-svc-ui.service"
mkdir -p "$WORK/watch"
WS_WATCH_DIR="$WORK/watch" WS_CGROUP_ROOT="$WORK/no-cgroups" WS_SLICES=1 ws start svc
eq "the room note can't read the box: the start goes on" "$RC" 0
has "  and says it can't tell" "$OUT" "ws: room: can't tell"
touch "$WORK/fail-web"; : > "$LOG"
WS_SLICES=1 ws start svc
eq "a failing command: exit 1" "$RC" 1
has "says where its output is" "$OUT" "journalctl --user -u ws-svc-web.service"
rm "$WORK/fail-web"
: > "$LOG"
WS_SLICES=1 ws start my-app
has "a dash in a name is escaped (a dash nests slices)" "$(cat "$LOG")" 'unit=ws-my\\x2dapp-db.service --slice=ws-my\\x2dapp-servers.slice'
: > "$LOG"
ws start svc
eq "without WS_SLICES: exit 0" "$RC" 0
hasnt "  no units (the Mac)" "$(cat "$LOG")" "systemd-run"
has "  the commands still run" "$(cat "$LOG")" "db-start"

echo "serve: a command that is the server, run by ws in the foreground"
cat >> "$WS_MANIFEST" <<'TOML'

[web]
dir = "web"
serve = { dev = "echo $$ > $LOG.web-pid; exec sleep 60" }

# The box's stub systemd-run runs the command in the foreground: one that ends.
[webq]
dir = "web"
serve = { dev = "true" }

[dying]
dir = "web"
serve = { dev = "echo bye; exit 3" }
TOML
printf '[both]\nstart = { dev = "true" }\nserve = { dev = "sleep 1" }\n' > "$WORK/both.toml"
mkdir -p "$WS_ROOT/web"; : > "$LOG"
WS_SERVE_CHECK=0 WS_SLICES=1 ws start webq
eq "the box: exit 0" "$RC" 0
has "  a unit in the servers slice" "$(cat "$LOG")" "systemd-run --user --quiet --unit=ws-webq-dev.service --slice=ws-webq-servers.slice"
hasnt "  not a oneshot: the command is the server" "$(grep 'unit=ws-webq-dev' "$LOG")" "oneshot"
has "  checked it's still up" "$(cat "$LOG")" "systemctl --user is-active --quiet ws-webq-dev.service"
: > "$LOG"
WS_SERVE_CHECK=0.5 ws start web
eq "the Mac: exit 0" "$RC" 0
pid=$(cat "$LOG.web-pid" 2>/dev/null)
kill -0 "$pid" 2>/dev/null; eq "  it runs in the background" "$?" 0
has "  says where its output goes" "$OUT" "$WS_STATE/serve/web/dev.log"
ws stop web
eq "  ws stop: exit 0" "$RC" 0
sleep 0.3; kill -0 "$pid" 2>/dev/null; eq "  it's stopped" "$?" 1
WS_SERVE_CHECK=0.5 ws start dying
eq "one that exits at once: exit 1" "$RC" 1
has "  says so, and where its output is" "$OUT" "dev exited at once (exit 3)"
has "  its output" "$(cat "$WS_STATE/serve/dying/dev.log")" "bye"
WS_MANIFEST="$WORK/both.toml" ws plan both
eq "a name in both start and serve: exit 1" "$RC" 1
has "  says so" "$OUT" "dev in both start and serve"

echo "claude: a Claude Code session in the stack, with Remote Control"
# A tmux of its own for this: sessions listed from $CT, the pane printing a link.
mkdir -p "$WORK/cbin"
cat > "$WORK/cbin/tmux" <<'STUB'
#!/usr/bin/env bash
echo "tmux $*" >> "$LOG"
case "$*" in
  *list-sessions*) [ -s "$CT" ] && cat "$CT" || exit 1 ;;
  *new-session*) for a; do :; done; echo "$a" > "$CT.cmd"; for x; do case $x in claude-*) echo "$x" >> "$CT" ;; esac; done ;;
  *display-message*) echo 4242 ;;
  *capture-pane*) [ -e "$CT.trust" ] && echo "   Yes, I trust this folder" || echo "  /remote-control is active · Continue here, on your phone, or at https://claude.ai/code/session_01ABC" ;;
esac
exit 0
STUB
chmod +x "$WORK/cbin/tmux"
: > "$LOG"
PATH="$WORK/cbin:$PATH" CT="$WORK/ct" WS_CLAUDE_URL_WAIT=1 ws claude svc
eq "exit 0" "$RC" 0
has "a tmux session of its own in the project's folder" "$(cat "$LOG")" "tmux -L ws-svc new-session -d -s claude-1 -c $WS_ROOT/app"
has "claude with Remote Control, named for the stack" "$(cat "$WORK/ct.cmd")" "claude --remote-control 'svc claude-1' --session-id "
has "  with its hooks" "$(cat "$WORK/ct.cmd")" "--settings $WS_STATE/claude/launch-settings.json"
has "the hooks run ws claude-hook" "$(cat "$WS_STATE/claude/launch-settings.json")" "claude-hook"
has "says its link" "$OUT" "in the Claude app: https://claude.ai/code/session_01ABC"
has "and how to attach from a terminal" "$OUT" "ssh -t devbox tmux -L ws-svc attach -t claude-1"
rec=$(ls "$WS_STATE/claude/launched/"*.json)
has "kept, with the link" "$(cat $rec)" '"url": "https://claude.ai/code/session_01ABC"'
PATH="$WORK/cbin:$PATH" CT="$WORK/ct" WS_CLAUDE_URL_WAIT=1 ws claude svc
has "a second one: claude-2" "$OUT" "svc claude-2: started"
touch "$WORK/ct.trust"
PATH="$WORK/cbin:$PATH" CT="$WORK/ct" WS_CLAUDE_URL_WAIT=1 ws claude svc
has "one asked to trust its folder says so" "$OUT" "it asks whether to trust $WS_ROOT/app"
echo '{"session_id": "'"$(basename "$rec" .json)"'", "hook_event_name": "UserPromptSubmit"}' | python3 -I "$HERE/ws" claude-hook
eq "ws claude-hook records what it does" "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["state"])' "$WS_STATE/claude/live/$(basename "$rec")")" "working"
: > "$LOG"
PATH="$WORK/cbin:$PATH" CT="$WORK/ct" ws claude-end svc claude-2
has "claude-end ends its tmux session" "$(cat "$LOG")" "tmux -L ws-svc kill-session -t =claude-2"
ws claude-end svc bash
eq "  only a claude-N" "$RC" 1
rm -f "$WORK/ct" "$WORK/ct.trust"; rm -rf "$WS_STATE/claude"

echo "stop: the project's stop commands, then its servers slice"
: > "$LOG"
WS_SLICES=1 ws stop svc
eq "exit 0" "$RC" 0
eq "stop command first, then the slice" "$(grep -E '^svc-stop|stop ws-svc-servers.slice' "$LOG" | tr '\n' '|')" "svc-stop|systemctl --user stop ws-svc-servers.slice|"
hasnt "sessions keep running" "$(cat "$LOG")" "sessions"
: > "$LOG"
ws stop svc
eq "without WS_SLICES: the stop commands only" "$(cat "$LOG")" "svc-stop"
ws stop app
eq "nothing to stop with: exit 1" "$RC" 1

echo "session: a tmux per stack, in its sessions slice"
: > "$LOG"
WS_SLICES=1 ws session svc
eq "exit 0" "$RC" 0
has "a new tmux server starts in the sessions slice" "$(cat "$LOG")" "systemd-run --user --quiet --scope --unit=ws-svc-tmux.scope --slice=ws-svc-sessions.slice"
has "its own socket, the session, the project's folder" "$(cat "$LOG")" "tmux -L ws-svc new -A -s svc -c $WS_ROOT/app"
touch "$TMUX_UP"; : > "$LOG"
WS_SLICES=1 ws session svc review
hasnt "a running server: no new scope (its panes are in the slice already)" "$(cat "$LOG")" "systemd-run"
has "a named session on it" "$(cat "$LOG")" "tmux -L ws-svc new -A -s review -c $WS_ROOT/app"
rm "$TMUX_UP"

echo "pause, resume, switch: a stack frozen with its memory in swap (the box)"
# Stubs with state, for this section only: systemctl keeps each slice's
# "ActiveState FreezerState ControlGroup" in $SC/<unit>, and a stop empties
# its cgroup (cgroup.events under $WS_CGROUP_ROOT); docker ps lists $PS
# (id, state, the two working-dir labels), and pause/unpause change it.
# up NAME: NAME's servers slice active, with processes in it.
up() { echo "active running /$1-servers" > "$SC/ws-$1-servers.slice"; mkdir -p "$WORK/cg/$1-servers"; echo "populated 1" > "$WORK/cg/$1-servers/cgroup.events"; }
mkdir -p "$WORK/pbin" "$WORK/sc"
cat > "$WORK/pbin/systemctl" <<'STUB'
#!/usr/bin/env bash
echo "systemctl $*" >> "$LOG"
f="$SC/$3"; [ -f "$f" ] && read -r a z c < "$f"
case $2 in
  show) [ -f "$f" ] && printf 'ActiveState=%s\nFreezerState=%s\nControlGroup=%s\n' "$a" "$z" "$c" ;;
  freeze) [ -f "$f" ] && echo "$a frozen $c" > "$f" ;;
  thaw) [ -f "$f" ] && echo "$a running $c" > "$f" ;;
  stop) [ -f "$f" ] && { echo "inactive running $c" > "$f"; echo "populated 0" > "$WS_CGROUP_ROOT$c/cgroup.events"; } ;;
  is-active) [ -f "$SC/$4" ]; exit ;;
esac
exit 0
STUB
cat > "$WORK/pbin/docker" <<'STUB'
#!/usr/bin/env bash
case $1 in
  ps) cat "$PS" ;;
  pause|unpause) v=$1; echo "docker $*" >> "$LOG"; shift
    for id; do awk -F'\t' -v OFS='\t' -v id="$id" -v s="$([ $v = pause ] && echo paused || echo running)" '$1 == id { $2 = s } 1' "$PS" > "$PS.n" && mv "$PS.n" "$PS"; done ;;
esac
STUB
chmod +x "$WORK/pbin/"*
export PATH="$WORK/pbin:$PATH" SC="$WORK/sc" PS="$WORK/ps" WS_CGROUP_ROOT="$WORK/cg"
cat >> "$WS_MANIFEST" <<'TOML'

[pa]
start = { web = "echo pa-start >> $LOG" }
stop = "echo pa-stop >> $LOG"

[pa.optional.ui]
start = "echo pa-ui >> $LOG"

[pb]
start = { web = "echo pb-start >> $LOG" }

[pab]
start = { web = "echo pab-start >> $LOG" }
TOML
mkdir -p "$WORK/cg/pa-servers" "$WS_ROOT/pa" "$WS_ROOT/pb" "$WS_ROOT/pab"
echo 9000000000 > "$WORK/cg/pa-servers/memory.current"; echo 0 > "$WORK/cg/pa-servers/memory.swap.current"
: > "$WORK/cg/pa-servers/memory.reclaim"
up pa
up pb
printf 'c1\trunning\t\t%s\nc2\trunning\t%s\t\nc3\trunning\t\t%s\nc4\texited\t\t%s\n' \
  "$WS_ROOT/pa/supabase" "$WS_ROOT/pa" "$WS_ROOT/pab/supabase" "$WS_ROOT/pa/old" > "$PS"
: > "$LOG"
WS_SLICES=1 ws pause pa
eq "exit 0" "$RC" 0
has "its servers slice frozen" "$(cat "$LOG")" "systemctl --user freeze ws-pa-servers.slice"
has "its running containers paused, by either working-dir label" "$(cat "$LOG")" "docker pause c1 c2$"
hasnt "  not a container of a stack whose folder only starts the same (pab)" "$(cat "$LOG")" "c3"
hasnt "  not a stopped one" "$(cat "$LOG")" "c4"
eq "its memory pushed out (memory.reclaim)" "$(cat "$WORK/cg/pa-servers/memory.reclaim")" "1099511627776"
has "says what it holds" "$OUT" "pa: paused; its servers hold 8583 MB in memory, 0 MB in swap"
hasnt "the stop commands don't run" "$(cat "$LOG")" "pa-stop"
: > "$LOG"
WS_SLICES=1 ws pause pa
eq "again: exit 0 (nothing left to do)" "$RC" 0
hasnt "  no second freeze" "$(cat "$LOG")" "freeze"
WS_SLICES=1 ws pause pab
eq "a stack that isn't running: exit 1" "$RC" 1
has "  says so" "$OUT" "pab isn't running"
ws pause pa
eq "without WS_SLICES (the Mac): exit 1" "$RC" 1
: > "$LOG"
WS_SLICES=1 ws resume pa
eq "resume: exit 0" "$RC" 0
eq "containers unpaused first (the servers talk to them), then thawed" "$(grep -E 'unpause|thaw' "$LOG" | tr '\n' '|')" "docker unpause c1 c2|systemctl --user thaw ws-pa-servers.slice|"
WS_SLICES=1 ws resume pa
eq "resume what isn't paused: exit 1" "$RC" 1
has "  says so" "$OUT" "pa isn't paused"

echo "stop and start a paused stack: thawed first"
WS_SLICES=1 ws pause pa >/dev/null; : > "$LOG"
WS_SLICES=1 ws stop pa
eq "stop: exit 0" "$RC" 0
eq "unpaused and thawed, then the stop commands, then the slice" "$(grep -E 'unpause|thaw|pa-stop|stop ws-pa' "$LOG" | tr '\n' '|')" "docker unpause c1 c2|systemctl --user thaw ws-pa-servers.slice|pa-stop|systemctl --user stop ws-pa-servers.slice|"
up pa
WS_SLICES=1 ws pause pa >/dev/null; : > "$LOG"
WS_SLICES=1 ws start pa
eq "start: exit 0" "$RC" 0
test "$(grep -n 'thaw' "$LOG" | cut -d: -f1)" -lt "$(grep -n '^pa-start' "$LOG" | cut -d: -f1)" 2>/dev/null
eq "  thawed before the start commands" "$?" 0

echo "switch: every other running stack paused, then this one resumed or started"
up pb
: > "$LOG"
WS_SLICES=1 ws switch pb
eq "exit 0" "$RC" 0
has "the other running stack is paused" "$(cat "$LOG")" "freeze ws-pa-servers.slice"
hasnt "this one isn't" "$(cat "$LOG")" "freeze ws-pb-servers.slice"
has "this one is running already: says so" "$OUT" "pb: running already"
hasnt "  and isn't restarted" "$(cat "$LOG")" "pb-start"
: > "$LOG"
WS_SLICES=1 ws switch pa
eq "back: exit 0" "$RC" 0
has "pb paused" "$(cat "$LOG")" "freeze ws-pb-servers.slice"
has "pa resumed, not restarted" "$(cat "$LOG")" "thaw ws-pa-servers.slice"
hasnt "  (no start commands)" "$(cat "$LOG")" "pa-start"
WS_SLICES=1 ws stop pb >/dev/null; WS_SLICES=1 ws stop pa >/dev/null; : > "$LOG"
WS_SLICES=1 ws switch pa --with ui
has "a stack that isn't running is started, with what's asked" "$(grep -E '^pa-' "$LOG" | tr '\n' ' ')" "pa-start pa-ui"
hasnt "  nothing else to pause" "$(cat "$LOG")" "freeze"
WS_SLICES=1 ws switch pa --with nope
eq "an unknown optional part: exit 1" "$RC" 1
up pb
up pa
WS_SLICES=1 ws pause pa >/dev/null; : > "$LOG"
WS_SLICES=1 ws switch pa --with ui
eq "a paused stack with something to add: exit 1" "$RC" 1
has "  it resumes as it was" "$OUT" "pa is paused: it resumes as it was"
hasnt "  nothing paused for it" "$(cat "$LOG")" "freeze"
echo "a start that failed: its slice is active (it holds the failed unit), with nothing in it"
WS_SLICES=1 ws stop pa >/dev/null; WS_SLICES=1 ws stop pb >/dev/null
up pb
mkdir -p "$WORK/cg/pb-servers"; echo "populated 0" > "$WORK/cg/pb-servers/cgroup.events"
: > "$LOG"
WS_SLICES=1 ws switch pb
hasnt "switch doesn't take it for running" "$OUT" "running already"
has "  it starts it" "$(cat "$LOG")" "^pb-start"
echo "active running /pb-servers" > "$SC/ws-pb-servers.slice"; : > "$LOG"
WS_SLICES=1 ws pause pb
eq "pause: exit 1" "$RC" 1
has "  it isn't running" "$OUT" "pb isn't running"
WS_SLICES=1 ws switch pa >/dev/null
echo "active running /pb-servers" > "$SC/ws-pb-servers.slice"; : > "$LOG"
WS_SLICES=1 ws switch pa
hasnt "switch to another doesn't pause it" "$(cat "$LOG")" "freeze ws-pb-servers.slice"

echo "restore: after a boot, the stacks as they were, one at a time"
# Their start commands do what a real start leaves: the slice active with
# processes in it. rb's fails while $WORK/fail-rb exists.
for x in ra rb rc; do
  cat >> "$WS_MANIFEST" <<TOML

[$x]
start = { web = 'echo $x-start >> \$LOG; test ! -e \$WS_ROOT/../fail-$x; echo "active running /$x-servers" > \$SC/ws-$x-servers.slice; mkdir -p \$WS_CGROUP_ROOT/$x-servers; echo "populated 1" > \$WS_CGROUP_ROOT/$x-servers/cgroup.events' }

[$x.optional.ui]
start = 'echo $x-ui >> \$LOG'
TOML
  mkdir -p "$WS_ROOT/$x"
done
rm -rf "$WS_STATE/intent"   # what the sections above left
intent() { python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print(d["state"], *d["with"])' "$WS_STATE/intent/$1.json" 2>/dev/null; }
down() { for x; do rm -f "$SC/ws-$x-servers.slice"; mkdir -p "$WORK/cg/$x-servers"; echo "populated 0" > "$WORK/cg/$x-servers/cgroup.events"; done; }
WS_SLICES=1 ws start ra --with ui
eq "a start records it: running, with what it started with" "$(intent ra)" "running ui"
WS_SLICES=1 ws pause ra
eq "a pause: paused, keeping what it starts with" "$(intent ra)" "paused ui"
WS_SLICES=1 ws resume ra
eq "a resume: running" "$(intent ra)" "running ui"
WS_SLICES=1 ws pause ra >/dev/null
WS_SLICES=1 ws start rc >/dev/null; WS_SLICES=1 ws stop rc
eq "a stop: stopped" "$(intent rc)" "stopped"
WS_SLICES=1 ws start rb >/dev/null
eq "  rb running" "$(intent rb)" "running"
ws start ra
eq "without WS_SLICES (the Mac) nothing is recorded" "$(intent ra)" "paused ui"
WS_SLICES=1 ws pause ra >/dev/null
down ra rb rc; : > "$LOG"   # the boot
WS_SETTLE_HOLD=0 WS_SETTLE_EVERY=0 WS_SLICES=1 ws restore
eq "restore: exit 0" "$RC" 0
eq "the paused one started (with its part), settled and paused; then the running one" \
  "$(grep -E '^r[abc]-(start|ui)|freeze ws-r' "$LOG" | tr '\n' '|')" "ra-start|ra-ui|systemctl --user freeze ws-ra-servers.slice|rb-start|"
hasnt "a stopped one stays stopped" "$(cat "$LOG")" "rc-start"
eq "each step is a job of the stack (the page shows it)" "$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["state"])' "$WS_STATE/jobs/ra/pause/meta.json")" "ok"
eq "  what each should be is unchanged" "$(intent ra) / $(intent rb)" "paused ui / running"
has "says what it restored" "$OUT" "restored: ra (paused), rb (running)"
: > "$LOG"
WS_SETTLE_HOLD=0 WS_SETTLE_EVERY=0 WS_SLICES=1 ws restore
has "again: what's up is left alone" "$OUT" "ra: up already"
hasnt "  nothing started" "$(cat "$LOG")" "-start"
down rb; touch "$WORK/fail-rb"; : > "$LOG"
WS_SETTLE_HOLD=0 WS_SETTLE_EVERY=0 WS_SLICES=1 ws restore
eq "a start that keeps failing: exit 1" "$RC" 1
eq "  tried twice" "$(grep -c '^rb-start' "$LOG")" 2
has "  says which" "$OUT" "not restored: rb"
rm "$WORK/fail-rb"
echo "remember: what's up now, where nothing is recorded"
rm -rf "$WS_STATE/intent"
up rb; echo "active running" > "$SC/ws-ra-ui.service"; : > "$LOG"
WS_SLICES=1 ws remember
eq "remember: exit 0" "$RC" 0
eq "  a paused one: paused; a running one: running" "$(intent ra) / $(intent rb)" "paused ui / running"
eq "  a stopped one: nothing" "$(intent rc)" ""
has "  its optional parts from their units" "$(cat "$LOG")" "systemctl --user is-active --quiet ws-ra-ui.service"
rm "$SC/ws-ra-ui.service"
WS_SLICES=1 ws start rb --with ui >/dev/null; WS_SLICES=1 ws remember
eq "  what's recorded already is kept" "$(intent rb)" "running ui"
WS_SLICES=1 ws stop ra >/dev/null; WS_SLICES=1 ws stop rb >/dev/null
WS_SLICES=1 ws restore
has "nothing wanted up: says so" "$OUT" "nothing to restore"
ws restore
eq "without WS_SLICES: exit 1" "$RC" 1
rm -f "$SC"/*
export PATH="${PATH#"$WORK/pbin:"}"
unset WS_CGROUP_ROOT

echo "run: a job in the background, outside whoever asked (the Mac: a process group)"
export WS_STATE="$WORK/state"
cat >> "$WS_MANIFEST" <<'TOML'

[jobsapp]
dir = "app"

[jobsapp.jobs]
build = "echo built in $PWD"
TOML
ws run jobsapp hello --wait -- 'echo hi; echo there'
eq "--wait: exit 0" "$RC" 0
has "--wait: shows its output" "$OUT" "there"
ws run jobsapp fails --wait -- 'echo before; exit 3'
eq "--wait: the job's exit code" "$RC" 3
ws jobs jobsapp
has "jobs: the finished one" "$OUT" "hello *ok"
has "jobs: the failed one, with its code" "$OUT" "fails *failed (exit 3)"
ws run jobsapp build --wait
eq "a job named in the manifest: exit 0" "$RC" 0
has "  runs in the project's folder" "$OUT" "built in $WS_ROOT/app"
ws run jobsapp nosuch
eq "no command and not in the manifest: exit 1" "$RC" 1
ws run jobsapp 'bad name' -- true
eq "a name that isn't a plain word: exit 1" "$RC" 1

ws run jobsapp slow -- 'sleep 3'
eq "no --wait: returns at once, exit 0" "$RC" 0
has "  says how to follow it" "$OUT" "ws logs jobsapp slow"
ws jobs jobsapp
has "jobs: running" "$OUT" "slow *running"
ws run jobsapp slow -- 'sleep 3'
eq "the same job again while it runs: exit 1" "$RC" 1
ws cancel jobsapp slow
eq "cancel: exit 0" "$RC" 0
sleep 0.5; ws jobs jobsapp
has "jobs: cancelled" "$OUT" "slow *cancelled"
ws logs jobsapp hello
has "logs: the job's output" "$OUT" "hi"

# One job at a time per stack, unless --parallel: each build takes all the
# cores and gigabytes it can.
M="$WORK/order"; : > "$M"
ws run jobsapp a -- "echo a-start >> $M; sleep 1; echo a-end >> $M"
ws run jobsapp b -- "echo b-start >> $M; echo b-end >> $M"
ws jobs jobsapp
has "the second waits its turn" "$OUT" "b *queued"
for _ in $(seq 1 30); do [ "$(wc -l < "$M")" -ge 4 ] && break; sleep 0.2; done
eq "one after the other" "$(tr '\n' ' ' < "$M")" "a-start a-end b-start b-end "
: > "$M"
ws run jobsapp c -- "echo c-start >> $M; sleep 1; echo c-end >> $M"
ws run jobsapp d --parallel -- "echo d-start >> $M; echo d-end >> $M"
for _ in $(seq 1 30); do [ "$(wc -l < "$M")" -ge 4 ] && break; sleep 0.2; done
eq "--parallel: doesn't wait" "$(head -2 "$M" | tr '\n' ' ')" "c-start d-start "

# A job whose process is gone without a word (the machine rebooted).
mkdir -p "$WS_STATE/jobs/jobsapp/ghost"
echo '{"stack": "jobsapp", "job": "ghost", "cmd": "x", "state": "running", "pid": 999999, "queued_at": 1, "started_at": 1}' > "$WS_STATE/jobs/jobsapp/ghost/meta.json"
ws jobs jobsapp
has "a running job with no process: lost" "$OUT" "ghost *lost"

echo "run on the box: the job is a unit in the stack's jobs slice, with a memory cap"
: > "$LOG"
WS_SLICES=1 ws run svc hello --wait -- 'echo hi from the box'
eq "exit 0" "$RC" 0
has "a unit in the jobs slice" "$(cat "$LOG")" "systemd-run --user --quiet --unit=ws-svc-job-hello.service --slice=ws-svc-jobs.slice"
has "capped, so a build can't push out the stack" "$(cat "$LOG")" "MemoryMax="
has "its output" "$OUT" "hi from the box"
has "systemd lets the runner record a memory-cap kill (not stop the unit: that read as cancelled)" "$(cat "$LOG")" "OOMPolicy=continue"
# The kernel's count of memory-cap kills in the job's cgroup (memory.events,
# a stand-in here): the job raises it and dies of SIGKILL, as on the box.
EV="$WORK/memory.events"; echo "oom_kill 0" > "$EV"
WS_JOB_MEMORY_MAX=100M WS_MEMORY_EVENTS="$EV" WS_SLICES=1 ws run svc hungry --wait -- "echo 'oom_kill 1' > $EV; kill -9 \$\$"
eq "a job killed at the memory cap: fails (not 130, cancelled)" "$RC" 1
has "  and says why" "$OUT" "over the memory cap"
WS_SLICES=1 ws jobs svc
has "jobs says so too, with the cap the job ran under" "$OUT" "hungry *failed: over the memory cap (100M"

echo "like: a second checkout of a project (shop-b)"
cat >> "$WS_MANIFEST" <<'TOML'

[base]
about = "the base"
repo = "acme/app"
dir = "base-tree"
tailnet = [4210]
start = ["echo start in $PWD >> $LOG"]
stop = ["echo stop in $PWD >> $LOG"]

[base.optional.ui]
about = "the ui"
start = "echo ui >> $LOG"

[base.optional.data]
about = "the data"
how = "copy it"

[base-b]
like = "base"
about = "the base, again"

[base-b.optional.ui]
about = "the ui, on b's port"
start = "echo ui-b >> $LOG"
TOML
ws plan base-b
has "a copy has the base's start" "$OUT" "start .*echo start in"
has "  and its own about" "$OUT" "the base, again"
has "  its own version of an optional part" "$OUT" "the ui, on b's port"
has "  and the base's others" "$OUT" "optional data: the data"
has "  in a folder of its own, by its name (dir isn't copied)" "$OUT" "base-b"
WS_TAILNET=1 ws plan base
has "(the base serves its port)" "$OUT" "4210"
WS_TAILNET=1 ws plan base-b
hasnt "  serving none of the base's ports (tailnet isn't copied)" "$OUT" "4210"
ws up base-b >/dev/null; : > "$LOG"
ws start base-b --with ui
eq "it starts: exit 0" "$RC" 0
has "  in its own folder" "$(cat "$LOG")" "start in $WS_ROOT/base-b"
has "  with its own optional part" "$(cat "$LOG")" "ui-b"
cat >> "$WS_MANIFEST" <<'TOML'

[base-c]
like = "base-b"
TOML
ws list
eq "a copy of a copy: exit 1" "$RC" 1
has "  says why" "$OUT" "copy itself"
python3 - "$WS_MANIFEST" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
open(p, "w").write(s.replace('[base-c]\nlike = "base-b"\n', '[base-c]\nlike = "nosuch"\n'))
PY
ws list
eq "a copy of nothing: exit 1" "$RC" 1
has "  says so" "$OUT" "like = 'nosuch'"
python3 - "$WS_MANIFEST" <<'PY'
import sys
p = sys.argv[1]
s = open(p).read()
open(p, "w").write(s.replace('[base-c]\nlike = "nosuch"\n', ''))
PY

echo "a start and a stop of the same project don't run at once"
cat >> "$WS_MANIFEST" <<'TOML'

[slow]
dir = "app"
start = ["touch $WS_ROOT/../slow-started; sleep 3"]
stop = ["echo slow-stopped >> $LOG"]
TOML
ws up slow >/dev/null; : > "$LOG"; rm -f "$WORK/slow-started"
python3 -I "$HERE/ws" start slow >/dev/null 2>&1 &
SLOW=$!
for _ in $(seq 50); do [ -e "$WORK/slow-started" ] && break; sleep 0.1; done
ws stop slow
eq "a stop during a start: exit 1" "$RC" 1
has "  says why" "$OUT" "a start, stop, pause or resume of it is running already"
hasnt "  and stopped nothing" "$(cat "$LOG")" "slow-stopped"
ws start slow
eq "a second start: exit 1 too" "$RC" 1
wait "$SLOW"
ws stop slow
eq "after the start: the stop runs" "$RC" 0
has "  and stops it" "$(cat "$LOG")" "slow-stopped"

echo "top: Linux only"
WS_CGROUP_ROOT="$WORK/no-cgroups" ws top
eq "no cgroup v2 here (the Mac): exit 1" "$RC" 1
has "says where it works" "$OUT" "run it on the box"

echo "mistakes"
ws up nosuch
eq "unknown project: exit 1" "$RC" 1
has "says so" "$OUT" "no project 'nosuch'"
ws list
has "list shows projects" "$OUT" "app  *the app"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
