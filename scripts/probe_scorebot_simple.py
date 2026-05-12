"""Simplest possible scorebot probe: hold one long-poll open and see what
the server pushes within the 60s ping timeout."""

from __future__ import annotations

import json
import sys
import time

from curl_cffi import requests

BASE = "https://scorebot-lb.hltv.org/socket.io/"
H = {
    "Accept": "*/*",
    "Origin": "https://www.hltv.org",
    "Referer": "https://www.hltv.org/",
}


def parse_v3(data: bytes) -> list[str]:
    packets: list[str] = []
    if not data:
        return packets
    if data[:1] in (b"\x00", b"\x01"):
        i = 0
        while i < len(data):
            i += 1  # kind byte
            digits = bytearray()
            while i < len(data) and data[i] != 0xFF:
                digits.append(data[i])
                i += 1
            if not digits:
                break
            length = 0
            for d in digits:
                length = length * 10 + d
            i += 1
            packets.append(data[i : i + length].decode("utf-8", errors="replace"))
            i += length
        return packets
    return data.decode("utf-8", errors="replace").split("\x1e")


def main(match_id: str, subscribe_event: str = '42["readyForMatch","{mid}"]', timeout: int = 60) -> None:
    sess = requests.Session(impersonate="safari17_0", headers=H)
    ts = lambda: int(time.time() * 1000)

    # Open
    r = sess.get(f"{BASE}?EIO=3&transport=polling&t={ts()}")
    packets = parse_v3(r.content)
    open_pkt = next(p for p in packets if p.startswith("0"))
    info = json.loads(open_pkt[1:])
    sid = info["sid"]
    print(f"sid={sid}  pingInterval={info['pingInterval']}  pingTimeout={info['pingTimeout']}")

    # Connect default namespace
    sess.post(
        f"{BASE}?EIO=3&transport=polling&sid={sid}&t={ts()}",
        data="40",
        headers={"Content-Type": "text/plain;charset=UTF-8"},
    )
    print("posted 40 (connect)")

    # Subscribe
    sub = subscribe_event.replace("{mid}", match_id)
    sess.post(
        f"{BASE}?EIO=3&transport=polling&sid={sid}&t={ts()}",
        data=sub,
        headers={"Content-Type": "text/plain;charset=UTF-8"},
    )
    print(f"posted {sub!r}")

    # Single long poll, no timeout enforced (let server hold up to 60s)
    start = time.time()
    print(f"starting long poll, will wait up to {timeout}s …")
    try:
        r = sess.get(
            f"{BASE}?EIO=3&transport=polling&sid={sid}&t={ts()}",
            timeout=timeout,
        )
        elapsed = time.time() - start
        print(f"  response after {elapsed:.1f}s: status={r.status_code} len={len(r.content)} body[:200]={r.content[:200]!r}")
        for p in parse_v3(r.content):
            print(f"  packet: {p[:300]}")
    except Exception as e:
        print(f"  poll failed after {time.time()-start:.1f}s: {type(e).__name__}: {str(e)[:120]}")


if __name__ == "__main__":
    mid = sys.argv[1] if len(sys.argv) > 1 else "2394323"
    evt = sys.argv[2] if len(sys.argv) > 2 else '42["readyForMatch","{mid}"]'
    main(mid, evt)
