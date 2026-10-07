#!/bin/sh
# Install or update net-flow-ctrl on the router VM. Run as root from the
# linux/ directory copied onto the VM:  sudo sh deploy/install.sh [--activate]
#
# Without --activate it only installs the program and the ruleset, and leaves
# ens19 and dnsmasq alone -- safe while enp2s0 is still on the home LAN.
# --activate brings up the TV-side network: ens19, DHCP, and the daemon.
set -eu
cd "$(dirname "$0")/.."

# aiohttp: the portal. Pillow + Noto CJK: large-type reminder images.
# avahi: answer as netflow.local on the home LAN, the name HomeBoard (and the
# ESP32 edition before) uses -- only one box may hold it at a time. It also
# reflects mDNS between the two networks, so an iPhone on the TV network finds
# the HomePods on the home LAN.
apt-get install -y -qq python3-aiohttp python3-pil fonts-noto-cjk avahi-daemon >/dev/null
sed -i -e 's/^#\?host-name=.*/host-name=netflow/' -e 's/^#\?allow-interfaces=.*/allow-interfaces=ens18,ens19/' \
	-e 's/^#\?enable-reflector=.*/enable-reflector=yes/' /etc/avahi/avahi-daemon.conf
systemctl restart avahi-daemon

install -d /opt/netflow/netflow /etc/netflow /var/lib/netflow
install -m 0644 netflow/*.py netflow/page.html /opt/netflow/netflow/
[ -f /etc/netflow/netflow.json ] || echo '{}' > /etc/netflow/netflow.json

nft -c -f deploy/nftables.conf
# Reload the ruleset only when it changed: a reload empties every set, and
# while the daemon refills them (policy at once, learned video addresses
# within a minute) there is no reason to disturb a running network.
ruleset_changed=0
cmp -s deploy/nftables.conf /etc/nftables.conf || ruleset_changed=1
install -m 0644 deploy/nftables.conf /etc/nftables.conf
install -m 0644 deploy/dnsmasq-netflow.conf /etc/dnsmasq.d/netflow.conf
dnsmasq --test
install -m 0644 deploy/netflow.service /etc/systemd/system/netflow.service
systemctl daemon-reload

if [ "${1:-}" = "--activate" ]; then
	grep -q '^auto ens19' /etc/network/interfaces.d/ens19 || sed -i '1i auto ens19' /etc/network/interfaces.d/ens19
	ifup ens19 || true
	sysctl -q -p /etc/sysctl.d/90-netflow.conf
	systemctl reload-or-restart nftables
	systemctl enable --now dnsmasq netflow
	systemctl restart dnsmasq netflow
	echo "activated: TV network live on ens19"
elif systemctl is-active -q netflow; then
	[ $ruleset_changed -eq 0 ] || systemctl reload-or-restart nftables
	systemctl restart netflow
	echo "updated and restarted"
else
	echo "installed (not activated; run again with --activate once the AP is on enp2s0)"
fi
