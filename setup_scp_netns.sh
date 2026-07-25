#!/usr/bin/env bash
set -euo pipefail

NS_C="scp-c"
NS_R="scp-r"
NS_S="scp-s"

C_IF="veth-c"
R_C_IF="veth-r0"
R_S_IF="veth-r1"
S_IF="veth-s"

C_ADDR="10.0.1.1/24"
R_C_ADDR="10.0.1.254/24"
R_S_ADDR="10.0.2.254/24"
S_ADDR="10.0.2.1/24"

C_GW="10.0.1.254"
S_GW="10.0.2.254"
SERVER_IP="10.0.2.1"

need_root() {
    if [[ ${EUID} -ne 0 ]]; then
        echo "Run as root: sudo $0 $*" >&2
        exit 1
    fi
}

ns_exists() {
    ip netns list | awk '{print $1}' | grep -qx "$1"
}

cleanup() {
    ip netns del "${NS_C}" 2>/dev/null || true
    ip netns del "${NS_R}" 2>/dev/null || true
    ip netns del "${NS_S}" 2>/dev/null || true
}

disable_offloads() {
    local ns="$1"
    local dev="$2"

    if command -v ethtool >/dev/null 2>&1; then
        ip netns exec "${ns}" \
            ethtool -K "${dev}" gro off gso off tso off lro off \
            >/dev/null 2>&1 || true
    fi
}

apply_netem() {
    local dev="$1"
    local rate="$2"
    local delay="$3"
    local loss="$4"
    local limit="$5"

    if [[ "${loss}" == "0" || "${loss}" == "0%" || "${loss}" == "0.0%" ]]; then
        ip netns exec "${NS_R}" tc qdisc replace dev "${dev}" root netem \
            limit "${limit}" \
            delay "${delay}" \
            rate "${rate}"
    else
        ip netns exec "${NS_R}" tc qdisc replace dev "${dev}" root netem \
            limit "${limit}" \
            delay "${delay}" \
            loss "${loss}" \
            rate "${rate}"
    fi
}

setup() {
    local rate="${1:-5mbit}"
    local delay="${2:-50ms}"
    local loss="${3:-0%}"
    local limit="${4:-1000}"

    cleanup

    ip netns add "${NS_C}"
    ip netns add "${NS_R}"
    ip netns add "${NS_S}"

    ip link add "${C_IF}" type veth peer name "${R_C_IF}"
    ip link add "${R_S_IF}" type veth peer name "${S_IF}"

    ip link set "${C_IF}" netns "${NS_C}"
    ip link set "${R_C_IF}" netns "${NS_R}"
    ip link set "${R_S_IF}" netns "${NS_R}"
    ip link set "${S_IF}" netns "${NS_S}"

    ip -n "${NS_C}" addr add "${C_ADDR}" dev "${C_IF}"
    ip -n "${NS_R}" addr add "${R_C_ADDR}" dev "${R_C_IF}"
    ip -n "${NS_R}" addr add "${R_S_ADDR}" dev "${R_S_IF}"
    ip -n "${NS_S}" addr add "${S_ADDR}" dev "${S_IF}"

    for ns in "${NS_C}" "${NS_R}" "${NS_S}"; do
        ip -n "${ns}" link set lo up
    done

    ip -n "${NS_C}" link set "${C_IF}" up
    ip -n "${NS_R}" link set "${R_C_IF}" up
    ip -n "${NS_R}" link set "${R_S_IF}" up
    ip -n "${NS_S}" link set "${S_IF}" up

    ip -n "${NS_C}" route add default via "${C_GW}" dev "${C_IF}"
    ip -n "${NS_S}" route add default via "${S_GW}" dev "${S_IF}"

    ip netns exec "${NS_R}" sysctl -q -w net.ipv4.ip_forward=1
    ip netns exec "${NS_R}" sysctl -q -w net.ipv4.conf.all.rp_filter=0
    ip netns exec "${NS_R}" sysctl -q -w net.ipv4.conf.default.rp_filter=0

    disable_offloads "${NS_C}" "${C_IF}"
    disable_offloads "${NS_R}" "${R_C_IF}"
    disable_offloads "${NS_R}" "${R_S_IF}"
    disable_offloads "${NS_S}" "${S_IF}"

    # Router egress toward the server: client -> server.
    apply_netem "${R_S_IF}" "${rate}" "${delay}" "${loss}" "${limit}"

    # Router egress toward the client: server -> client.
    apply_netem "${R_C_IF}" "${rate}" "${delay}" "${loss}" "${limit}"

    cat <<EOF

SCP namespace topology is ready:

  ${NS_C} (${C_ADDR}) <---> ${NS_R} <---> ${NS_S} (${S_ADDR})

Each direction independently uses:
  rate  = ${rate}
  delay = ${delay}
  loss  = ${loss}
  limit = ${limit} packets

Expected unloaded RTT: approximately 2 x ${delay}

Server address: ${SERVER_IP}

Verify:
  sudo $0 ping
  sudo $0 show

Run programs:
  sudo ip netns exec ${NS_S} <server-command>
  sudo ip netns exec ${NS_C} <client-command> ${SERVER_IP}

Remove topology:
  sudo $0 clean
EOF
}

show() {
    echo "=== namespaces ==="
    ip netns list
    echo

    for ns in "${NS_C}" "${NS_R}" "${NS_S}"; do
        if ns_exists "${ns}"; then
            echo "=== ${ns}: addresses ==="
            ip -n "${ns}" -br addr
            echo "=== ${ns}: routes ==="
            ip -n "${ns}" route
            echo
        fi
    done

    if ns_exists "${NS_R}"; then
        echo "=== client -> server qdisc (${R_S_IF}) ==="
        ip netns exec "${NS_R}" tc -s qdisc show dev "${R_S_IF}"
        echo
        echo "=== server -> client qdisc (${R_C_IF}) ==="
        ip netns exec "${NS_R}" tc -s qdisc show dev "${R_C_IF}"
    fi
}

ping_test() {
    if ! ns_exists "${NS_C}" || ! ns_exists "${NS_S}"; then
        echo "Topology not found. Run: sudo $0 setup" >&2
        exit 1
    fi

    ip netns exec "${NS_C}" ping -c 5 "${SERVER_IP}"
}

case "${1:-setup}" in
    setup)
        need_root "$@"
        shift || true
        setup "$@"
        ;;
    clean)
        need_root "$@"
        cleanup
        ;;
    show)
        need_root "$@"
        show
        ;;
    ping)
        need_root "$@"
        ping_test
        ;;
    *)
        cat >&2 <<EOF
Usage:
  sudo $0 setup [rate] [one-way-delay] [loss] [queue-limit]
  sudo $0 show
  sudo $0 ping
  sudo $0 clean

Examples:
  sudo $0 setup 5mbit 50ms 0% 1000
  sudo $0 setup 5mbit 50ms 5% 1000
EOF
        exit 2
        ;;
esac

