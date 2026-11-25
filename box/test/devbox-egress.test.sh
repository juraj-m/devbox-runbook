#!/usr/bin/env bash
#
# Tests for devbox-egress. Offline: EGRESS_ROOT points every path into a temp
# dir, so nothing here touches nftables, dnsmasq or systemd. What the rules
# do on a real kernel is checked on the box (docs/ARCHITECTURE.md, "Outbound allowlist").
#
#   bash box/test/devbox-egress.test.sh
#
# What matters most: only listed domains are answered once enforced, a bad
# line stops the apply rather than allowing something else, and what an
# agent writes into the request queue can't add anything by itself.

set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

pass=0; fail=0
eq()    { if [ "$2" = "$3" ]; then pass=$((pass + 1)); echo "  ok    $1"; else fail=$((fail + 1)); echo "  FAIL  $1: got '$2', want '$3'"; fi; }
has()   { if printf '%s' "$2" | grep -q -- "$3"; then pass=$((pass + 1)); echo "  ok    $1"; else fail=$((fail + 1)); echo "  FAIL  $1: no '$3' in: $2"; fi; }
hasnt() { if printf '%s' "$2" | grep -q -- "$3"; then fail=$((fail + 1)); echo "  FAIL  $1: '$3' in: $2"; else pass=$((pass + 1)); echo "  ok    $1"; fi; }

export EGRESS_ROOT="$WORK/root" EGRESS_PUB=eth0
mkdir -p "$EGRESS_ROOT/etc/devbox" "$EGRESS_ROOT/var/lib/devbox/egress" \
	"$EGRESS_ROOT/var/lib/devbox/egress-requests" "$EGRESS_ROOT/var/log/devbox-egress"
E() { bash "$HERE/sbin/devbox-egress" "$@"; }
LIST="$EGRESS_ROOT/etc/devbox/egress.allow"
LOCAL="$EGRESS_ROOT/var/lib/devbox/egress/local.allow"
QUEUE="$EGRESS_ROOT/var/lib/devbox/egress-requests/queue"
LOG="$EGRESS_ROOT/var/log/devbox-egress/dns.log"

cat >"$LIST" <<'EOF'
# a comment
GitHub.com
  npmjs.org   # trailing comment

203.0.113.7
198.51.100.0/24
2001:db8::/32
EOF

echo "dns config"
out=$(E render dns audit)
has   "a listed domain goes to the listed resolvers" "$out" "server=/github.com/1.1.1.1"
has   "its addresses fill the sets" "$out" "nftset=/github.com/4#inet#devbox_egress#allow4,6#inet#devbox_egress#allow6"
has   "names are lower-cased, comments and spaces dropped" "$out" "server=/npmjs.org/1.1.1.1"
has   "audit: the rest goes to the other resolvers" "$out" "server=8.8.8.8"
hasnt "audit: nothing refused" "$out" "address=/#/"
hasnt "addresses aren't DNS entries" "$out" "203.0.113.7"
has   "tailnet names still resolve" "$out" "server=/ts.net/100.100.100.100"
out=$(E render dns enforce)
has   "enforce: every other name is NXDOMAIN" "$out" "address=/#/"
hasnt "enforce: nothing goes to the other resolvers" "$out" "server=8.8.8.8"

echo "nft rules"
out=$(E render nft enforce)
has   "listed IPv4 addresses and CIDRs" "$out" "listed4 { 198.51.100.0/24,203.0.113.7 }"
has   "listed IPv6 CIDRs" "$out" "listed6 { 2001:db8::/32 }"
has   "only the public interface is filtered" "$out" 'output oifname "eth0" jump egress'
has   "containers' traffic too" "$out" 'forward oifname "eth0" jump egress'
has   "tailscaled's own traffic passes" "$out" "meta mark and 0xff0000 == 0x80000 accept"
has   "only the DNS server's user reaches outside DNS" "$out" "meta skuid dnsmasq meta l4proto { udp, tcp } th dport 53 accept"
has   "other DNS is turned to the server" "$out" "dnat ip to 127.0.0.3"
has   "and, turned, it passes" "$out" "egress ip daddr 127.0.0.3 meta l4proto { udp, tcp } th dport 53 accept"
has   "IPv6 neighbour discovery passes" "$out" "ip6 daddr { fe80::/10, ff00::/8 } accept"
has   "containers' DNS is turned to the bridge address" "$out" 'iifname "br-\*" .* dnat ip to 172.17.0.1'
has   "enforce: the rest refused" "$out" "reject with tcp reset"
out=$(E render nft audit)
hasnt "audit: nothing refused" "$out" "reject"
has   "audit: what would be is logged" "$out" "devbox-egress would block"

echo "a bad line stops the apply"
echo "github.com/evil path" >>"$LIST"
out=$(E render dns enforce 2>&1); rc=$?
eq    "render fails" "$rc" 1
has   "naming the line" "$out" "not a domain, address or CIDR: github.com/evilpath"
sed -i.bak '$d' "$LIST"
for wide in 0.0.0.0/0 10.0.0.0/8 ::/0 2001::/16; do
	out=$(E allow "$wide" 2>&1); rc=$?
	eq    "a CIDR as wide as $wide is refused" "$rc" 1
done

echo "a damaged mode file stops the command"
echo "" >"$EGRESS_ROOT/var/lib/devbox/egress/mode"
out=$(E status 2>&1); rc=$?
eq    "status fails" "$rc" 1
has   "saying why" "$out" "set the mode with"
rm "$EGRESS_ROOT/var/lib/devbox/egress/mode"

echo "requests"
out=$(E request 'evil.com; rm -rf /' "x" 2>&1); rc=$?
eq    "a malformed domain is refused" "$rc" 1
out=$(E request example.org 2>&1); rc=$?
eq    "a request without a reason is refused" "$rc" 1
out=$(E request GitHub.com "already" 2>&1)
has   "a listed domain needs no request" "$out" "already allowed"
out=$(E request Registry.Example.org "$(printf 'build needs it\tnow\033[31m')")
has   "a request is queued" "$out" "ssh -t root@devbox devbox-egress approve"
E request registry.example.org "asked twice" >/dev/null
E request pkg.example.net "mirror" >/dev/null
printf '2026-11-24T00:00:00Z\tbad domain!\tinjected\n' >>"$QUEUE"
out=$(E pending)
eq    "pending: one line per domain, bad lines skipped" "$(wc -l <<<"$out" | tr -d ' ')" 2
has   "the reason keeps no tab or escape" "$out" "registry.example.org   (.*) build needs it now.31m"

echo "approve"
printf 'y\nn\n' >"$WORK/answers"
out=$(EGRESS_TTY="$WORK/answers" E approve)
eq    "the approved one is on the box's list" "$(cat "$LOCAL")" "pkg.example.net"
eq    "the queue is taken away" "$([ -e "$QUEUE" ] && echo there || echo gone)" gone
out=$(E request pkg.example.net "again" 2>&1)
has   "an allowed domain needs no new request" "$out" "already allowed"
E request next.example.org "after approve" >/dev/null
eq    "a new request starts a new queue" "$(cut -f2 "$QUEUE")" "next.example.org"
printf 'n\n' >"$WORK/no"
out=$(EGRESS_TTY="$WORK/no" E approve)
has   "a refused request" "$out" "nothing allowed"
out=$(EGRESS_TTY="$WORK/no" E approve)
eq    "approve with nothing pending" "$out" "no requests"

echo "approve never writes through a link in the queue's place"
printf 'keep me\n' >"$WORK/victim"
ln -sf "$WORK/victim" "$QUEUE"
printf 'example.com\n' >>"$WORK/victim"
out=$(EGRESS_TTY="$WORK/answers" E pending)
eq    "a linked queue isn't read" "$out" "no requests"
EGRESS_TTY="$WORK/answers" E approve >/dev/null
eq    "the link's target is untouched" "$(tr '\n' ' ' <"$WORK/victim")" "keep me example.com "
eq    "the link is gone" "$([ -L "$QUEUE" ] && echo there || echo gone)" gone

echo "nor is stopped by a directory in the queue's place"
mkdir -p "$QUEUE/sub"
out=$(EGRESS_TTY="$WORK/no" E approve); rc=$?
eq    "approve succeeds" "$rc" 0
has   "saying it removed it" "$out" "wasn't a plain file"
eq    "the directory is gone" "$([ -e "$QUEUE" ] && echo there || echo gone)" gone
E request dir.example.org "after a directory" >/dev/null
out=$(EGRESS_TTY="$WORK/no" E approve)
has   "a later request is still offered" "$out" "dir.example.org"

echo "requests an interrupted approve left come round again"
TAKEN="$EGRESS_ROOT/var/lib/devbox/egress/requests.taken"
printf '2026-11-24T00:00:00Z\tleft.example.org\tinterrupted\n' >"$TAKEN"
E request fresh.example.org "new" >/dev/null
out=$(EGRESS_TTY="$WORK/no" E approve)
has   "the left one" "$out" "left.example.org"
has   "and the new one" "$out" "fresh.example.org"
eq    "then both are done" "$([ -e "$TAKEN" ] && echo there || echo gone)" gone

echo "allow"
out=$(E allow 'a b' 2>&1); rc=$?
eq    "a malformed entry is refused" "$rc" 1
out=$(E allow NPMJS.org)
has   "an entry already listed is reported" "$out" "already listed: npmjs.org"
out=$(E allow 192.0.2.1 x.example.com)
eq    "new entries are added" "$(tail -n 2 "$LOCAL" | tr '\n' ' ')" "192.0.2.1 x.example.com "

echo "unsynced"
out=$(printf '# repo\nPKG.example.net\n' | E unsynced)
eq    "the box's additions the repo's list lacks" "$(tr '\n' ' ' <<<"$out")" "192.0.2.1 x.example.com "
out=$(printf 'bad entry!\n' | E unsynced 2>&1); rc=$?
eq    "a bad repo list stops it" "$rc" 1

echo "blocked"
cat >"$LOG" <<'EOF'
Nov 24 10:00:00 dnsmasq[1]: query[A] github.com from 127.0.0.1
Nov 24 10:00:00 dnsmasq[1]: forwarded github.com to 1.1.1.1
Nov 24 10:00:01 dnsmasq[1]: forwarded tracker.example.com to 8.8.8.8
Nov 24 10:00:02 dnsmasq[1]: config telemetry.example.net is NXDOMAIN
Nov 24 10:00:03 dnsmasq[1]: forwarded tracker.example.com to 8.8.4.4
Nov 24 10:00:04 dnsmasq[1]: forwarded 8x8x8x8.example to 1.1.1.1
EOF
out=$(E blocked)
eq    "unlisted names, most recent last, each once" "$(tr '\n' ' ' <<<"$out")" "telemetry.example.net tracker.example.com "

echo "status"
echo audit >"$EGRESS_ROOT/var/lib/devbox/egress/mode"
out=$(E status)
has   "the mode" "$out" "mode: audit"
has   "the box's own additions" "$out" "3 added on the box"

echo
echo "$pass passed, $fail failed"
[ "$fail" -eq 0 ]
