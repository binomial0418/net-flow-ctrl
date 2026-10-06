#!/bin/bash
# End-to-end test on a Linux box, entirely inside network namespaces: the real
# ruleset, DNS proxy and daemon, between a fake TV and a fake internet. Touches
# nothing outside the namespaces. Run as root from linux/:
#   sudo bash tests/integration_netns.sh
set -u
cd "$(dirname "$0")/.."
# Run as root: do not leave root-owned __pycache__ behind in the source tree.
export PYTHONDONTWRITEBYTECODE=1
T=/tmp/netflow-it
PASS=0; FAIL=0
pids=()

cleanup() {
	for p in "${pids[@]}"; do kill "$p" 2>/dev/null; done
	wait 2>/dev/null
	pkill -f "[n]etflow --config $T/conf.json" 2>/dev/null  # belt and braces ([n]: never matches pkill itself)
	for n in nf-wan nf-rtr nf-tv; do ip netns del $n 2>/dev/null; done
}
trap cleanup EXIT
cleanup
rm -rf $T; mkdir -p $T

check() { # name expected actual
	if [[ "$3" == $2 ]]; then echo "  ok    $1: $3"; PASS=$((PASS+1));
	else echo "  FAIL  $1: expected '$2', got '$3'"; FAIL=$((FAIL+1)); fi
}
tv()  { ip netns exec nf-tv python3 tests/fakenet.py "$@"; }
rtr() { ip netns exec nf-rtr "$@"; }
dev() { tv api GET /api/devices | python3 -c "import json,sys; d=json.load(sys.stdin)['devices']; print(d[0]['$1'] if d else 'none')"; }

# --- topology: tv <-> rtr(ens19 | ens18) <-> wan -----------------------------
for n in nf-wan nf-rtr nf-tv; do ip netns add $n; ip -n $n link set lo up; done
ip link add w0 netns nf-wan type veth peer name ens18 netns nf-rtr
ip link add eth0 netns nf-tv type veth peer name ens19 netns nf-rtr
ip -n nf-wan addr add 10.99.0.1/24 dev w0; ip -n nf-wan link set w0 up
for a in 8.8.8.8 9.9.9.9 173.194.9.9 173.194.9.10 93.184.0.10 93.184.0.20; do ip -n nf-wan addr add $a/32 dev lo; done
ip -n nf-rtr addr add 10.99.0.2/24 dev ens18; ip -n nf-rtr link set ens18 up
ip -n nf-rtr addr add 192.168.50.1/24 dev ens19; ip -n nf-rtr link set ens19 up
ip -n nf-rtr route add default via 10.99.0.1
rtr sysctl -qw net.ipv4.ip_forward=1
ip -n nf-tv addr add 192.168.50.101/24 dev eth0; ip -n nf-tv link set eth0 up
ip -n nf-tv route add default via 192.168.50.1
rtr nft -f deploy/nftables.conf || { echo "ruleset failed to load"; exit 1; }

ip netns exec nf-wan python3 tests/fakenet.py serve & pids+=($!)
cat > $T/conf.json <<EOF
{"upstream_dns": ["8.8.8.8"], "state_path": "$T/state.json", "leases_path": "$T/none"}
EOF
# Started directly, not through rtr(): backgrounding a shell function puts a
# subshell in $!, and killing that would leave the daemon running.
ip netns exec nf-rtr env PYTHONPATH=. python3 -m netflow --config $T/conf.json >$T/daemon.log 2>&1 & pids+=($!)
sleep 2

echo "boot (fail-closed until registered)"
ip netns exec nf-tv ping -c1 -W1 192.168.50.1 >/dev/null
sleep 2
check "registered" "1" "$(tv api GET /api/devices | python3 -c 'import json,sys;print(len(json.load(sys.stdin)["devices"]))')"
check "approved by default" "True" "$(dev approved)"

echo "DNS through the proxy (even to a hard-coded 8.8.8.8)"
check "plain name" "93.184.0.10" "$(tv resolve www.example.com)"
check "video name" "173.194.9.9" "$(tv resolve rr1---sn-x.googlevideo.com)"
check "video IP learned for this TV" "*192.168.50.101 . 173.194.9.9*" "$(rtr nft list set inet netflow ytvideo | tr -d '\n')"
check "name CNAMEd to video" "173.194.9.10" "$(tv resolve cdn-alias.example.net)"
check "learned through the CNAME" "*192.168.50.101 . 173.194.9.10*" "$(rtr nft list set inet netflow ytvideo | tr -d '\n')"

echo "forwarding, timing"
check "site" "OK 2097152" "$(tv get http://93.184.0.10/)"
for i in 1 2 3; do tv get http://173.194.9.9/ >/dev/null; done
sleep 3
check "usage time" "[1-9]*" "$(dev usedSec)"
check "YouTube time" "[1-9]*" "$(dev ytUsedSec)"
check "bytes counted" "[1-9]??????*" "$(dev down)"
# Traffic on a pair renews it to the set's full timeout: shorten one by hand,
# then use it.
printf '%s\n' "destroy element inet netflow ytvideo { 192.168.50.101 . 173.194.9.9 }" \
	"add element inet netflow ytvideo { 192.168.50.101 . 173.194.9.9 timeout 30s }" | rtr nft -f -
tv get http://173.194.9.9/ >/dev/null
check "traffic renews the pair" "*173.194.9.9 timeout 30s expires 5[0-9]m*" "$(rtr nft list set inet netflow ytvideo | tr -d '\n')"

echo "encrypted DNS refused"
check "DoT 853" "REFUSED" "$(tv connect 9.9.9.9 853)"
check "DoH 8.8.8.8:443" "REFUSED" "$(tv connect 8.8.8.8 443)"
check "DoH resolver name" "NXDOMAIN" "$(tv resolve dns.google)"
check "Firefox DoH canary" "NXDOMAIN" "$(tv resolve use-application-dns.net)"
tv api POST /api/global '{"resetMin":300,"defaultAllow":true,"activeKBmin":200,"blockEncDns":false}' >/dev/null
sleep 2
check "DoT when allowed" "OPEN" "$(tv connect 9.9.9.9 853)"
check "DoH name when allowed" "93.184.0.10" "$(tv resolve dns.google)"
tv api POST /api/global '{"resetMin":300,"defaultAllow":true,"activeKBmin":200,"blockEncDns":true}' >/dev/null

echo "block YouTube"
MAC=$(dev mac)
tv api POST /api/device "{\"mac\":\"$MAC\",\"approved\":true,\"quotaMin\":480,\"blockYoutube\":true}" >/dev/null
sleep 1
check "youtube NXDOMAIN" "NXDOMAIN" "$(tv resolve www.youtube.com)"
check "CNAME into youtube NXDOMAIN" "NXDOMAIN" "$(tv resolve cdn-alias.example.net)"
check "video refused" "FAIL*" "$(tv get http://173.194.9.9/)"
check "other site fine" "OK 2097152" "$(tv get http://93.184.0.10/)"

echo "manual block"
tv api POST /api/device "{\"mac\":\"$MAC\",\"approved\":true,\"quotaMin\":480,\"manualBlock\":true}" >/dev/null
sleep 1
check "site refused" "FAIL*" "$(tv get http://93.184.0.10/)"
check "reason" "1" "$(dev reason)"
check "portal still reachable" "*timeValid*" "$(tv api GET /api/status)"

echo "shutdown saves state"
kill -TERM "${pids[1]}"; sleep 2
check "state saved" "*manual_block\": true*" "$(tr -d '\n' < $T/state.json)"

echo "learned video addresses survive a ruleset reload and a restart"
rtr nft -f deploy/nftables.conf  # what a reload of nftables.service does: every set emptied
check "set emptied by the reload" "*timeout 1h	}}" "$(rtr nft list set inet netflow ytvideo | tr -d '\n')"
ip netns exec nf-rtr env PYTHONPATH=. python3 -m netflow --config $T/conf.json >>$T/daemon.log 2>&1 & pids+=($!)
sleep 3
check "restored from state" "*192.168.50.101 . 173.194.9.9*192.168.50.101 . 173.194.9.10*" "$(rtr nft list set inet netflow ytvideo | tr -d '\n')"
kill -TERM "${pids[-1]}"; sleep 2

echo
echo "passed $PASS, failed $FAIL"
[ $FAIL -eq 0 ] || { echo "--- daemon log"; tail -30 $T/daemon.log; exit 1; }
