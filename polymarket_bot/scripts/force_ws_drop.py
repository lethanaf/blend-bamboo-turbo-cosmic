#!/usr/bin/env python3
"""Force the recorder's market-channel TCP socket to close.

Does not stop the process. SO_LINGER on a duplicated fd does not reset the
connection while the recorder still holds it, and this kernel's sock_diag
SOCK_DESTROY returns EOPNOTSUPP. shutdown(SHUT_RDWR) on a pidfd-duplicated
socket does close the connection: the local read returns EOF and the peer
sees a FIN. That is the drop the reconnect path is tested with.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import struct
import time
from pathlib import Path
from urllib.parse import urlparse

SYS_PIDFD_OPEN = 434
SYS_PIDFD_GETFD = 438


def established_inodes(ips: set[str], port: int) -> set[str]:
    wanted = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        file = Path(path)
        if not file.exists():
            continue
        for line in file.read_text().splitlines()[1:]:
            parts = line.split()
            if len(parts) < 10 or parts[3] != "01":
                continue
            rem_ip, rem_port = parts[2].split(":")
            if int(rem_port, 16) != port:
                continue
            ip = _decode_ip(rem_ip)
            if ip in ips:
                wanted.add(parts[9])
    return wanted


def _decode_ip(hex_ip: str) -> str:
    raw = bytes.fromhex(hex_ip)
    if len(raw) == 4:
        return socket.inet_ntop(socket.AF_INET, raw[::-1])
    words = [raw[i : i + 4][::-1] for i in range(0, 16, 4)]
    return socket.inet_ntop(socket.AF_INET6, b"".join(words))


def socket_fds(pid: int, inodes: set[str]) -> list[int]:
    found = []
    for entry in Path(f"/proc/{pid}/fd").iterdir():
        try:
            target = os.readlink(entry)
        except OSError:
            continue
        for inode in inodes:
            if target == f"socket:[{inode}]":
                found.append(int(entry.name))
    return found


def dup_socket(pid: int, fd: int) -> socket.socket:
    libc = ctypes.CDLL(None, use_errno=True)
    pidfd = libc.syscall(SYS_PIDFD_OPEN, pid, 0)
    if pidfd < 0:
        raise OSError(ctypes.get_errno(), "pidfd_open")
    duped = libc.syscall(SYS_PIDFD_GETFD, pidfd, fd, 0)
    os.close(pidfd)
    if duped < 0:
        raise OSError(ctypes.get_errno(), "pidfd_getfd")
    return socket.socket(fileno=duped)


def bytes_received(sock: socket.socket) -> int:
    # struct tcp_info: tcpi_bytes_received is the 4th u64 after the 104-byte prefix.
    raw = sock.getsockopt(socket.IPPROTO_TCP, 11, 256)  # TCP_INFO
    if len(raw) < 136:
        return -1
    return struct.unpack_from("Q", raw, 128)[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pid", type=int, required=True)
    parser.add_argument("--url", default="wss://ws-subscriptions-clob.polymarket.com/ws/market")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    host = urlparse(args.url).hostname or ""
    infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    ips = {item[4][0] for item in infos}
    inodes = established_inodes(ips, 443)
    fds = socket_fds(args.pid, inodes)
    if not fds:
        raise SystemExit(f"no established socket to {host} {sorted(ips)} inodes={sorted(inodes)}")
    scored: list[tuple[int, int, socket.socket]] = []
    try:
        for fd in fds:
            sock = dup_socket(args.pid, fd)
            scored.append((bytes_received(sock), fd, sock))
        scored.sort(reverse=True)
        received, fd, chosen = scored[0]
        if received <= 0:
            raise SystemExit(f"refusing to drop: no socket has received bytes {[(n, f) for n, f, _ in scored]}")
        chosen.shutdown(socket.SHUT_RDWR)
    finally:
        for _received, _fd, sock in scored:
            sock.close()
    payload = {
        "wall": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "pid": args.pid,
        "host": host,
        "ips": sorted(ips),
        "candidates": [{"fd": fd, "bytes_received": n} for n, fd, _sock in scored],
        "fd": fd,
        "bytes_received": received,
        "method": "shutdown_rdwr",
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload))


if __name__ == "__main__":
    main()
