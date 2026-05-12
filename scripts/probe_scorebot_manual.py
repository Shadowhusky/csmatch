"""Manual Engine.IO v4 / Socket.IO v5 client over curl_cffi long-polling.

Goal: prove we can subscribe to HLTV's scorebot and see what event names
and payloads come through for a live match.
"""

from __future__ import annotations

import json
import sys
import time

from curl_cffi import requests

BASE = "https://scorebot-lb.hltv.org/socket.io/"
EIO = 3  # HLTV scorebot uses Engine.IO v3 (legacy framing)
H = {
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://www.hltv.org",
    "Referer": "https://www.hltv.org/",
}


def parse_v3_payload(data: bytes) -> list[str]:
    """Decode Engine.IO v3 XHR payload. Handles both text-mode
    "<len>:<packet>..." and binary-mode \\x00<digit><digit>...\\xff<packet>."""
    packets: list[str] = []
    if not data:
        return packets
    if data[:1] in (b"\x00", b"\x01"):
        i = 0
        while i < len(data):
            kind = data[i]
            i += 1
            digits = bytearray()
            while i < len(data) and data[i] != 0xFF:
                digits.append(data[i])
                i += 1
            if not digits or i >= len(data):
                break
            # Digits are raw byte values 0..9, NOT ASCII '0'..'9'.
            length = 0
            for d in digits:
                length = length * 10 + d
            i += 1  # skip 0xFF
            body = data[i : i + length]
            i += length
            if kind == 0:
                packets.append(body.decode("utf-8", errors="replace"))
            else:
                packets.append(f"<binary {len(body)} bytes>")
        return packets
    text = data.decode("utf-8", errors="replace")
    i = 0
    while i < len(text):
        j = text.find(":", i)
        if j < 0:
            break
        try:
            n = int(text[i:j])
        except ValueError:
            break
        packets.append(text[j + 1 : j + 1 + n])
        i = j + 1 + n
    return packets


def open_session() -> tuple[str, requests.Session]:
    sess = requests.Session(impersonate="safari17_0", headers=H)
    r = sess.get(f"{BASE}?EIO={EIO}&transport=polling&t={int(time.time()*1000)}")
    print(f"  open status={r.status_code}  body[:120]={r.content[:120]!r}")
    if r.status_code != 200:
        raise RuntimeError(f"open failed: {r.status_code}")
    packets = parse_v3_payload(r.content)
    print(f"  decoded {len(packets)} packets")
    for p in packets:
        print(f"    {p[:160]!r}")
    open_pkt = next((p for p in packets if p.startswith("0")), None)
    if not open_pkt:
        raise RuntimeError("no open packet found")
    info = json.loads(open_pkt[1:])
    return info["sid"], sess


def post(sess: requests.Session, sid: str, body: str) -> None:
    """Engine.IO POST. Body is a single packet string like '40' or '42[...]'."""
    url = f"{BASE}?EIO={EIO}&transport=polling&sid={sid}&t={int(time.time()*1000)}"
    r = sess.post(url, data=body, headers={"Content-Type": "text/plain;charset=UTF-8"})
    print(f"  POST {body[:80]!r} -> {r.status_code} {r.text[:60]!r}")


def poll(sess: requests.Session, sid: str) -> list[str]:
    url = f"{BASE}?EIO={EIO}&transport=polling&sid={sid}&t={int(time.time()*1000)}"
    r = sess.get(url, timeout=8)
    if r.status_code != 200:
        print(f"  poll status={r.status_code} body={r.content[:80]!r}")
        return []
    return parse_v3_payload(r.content)


def parse(packet: str) -> tuple[int, object]:
    """Parse one Engine.IO packet. Returns (type, payload)."""
    if not packet:
        return -1, None
    eio_type = int(packet[0])
    rest = packet[1:]
    if eio_type != 4:  # not a message packet
        return eio_type, rest
    # Socket.IO packet
    if not rest:
        return eio_type, None
    sio_type = int(rest[0])
    sio_rest = rest[1:]
    if sio_type == 2:  # event
        try:
            return eio_type, ("event", json.loads(sio_rest)) if sio_rest else ("event", [])
        except Exception:
            return eio_type, ("event", sio_rest)
    return eio_type, (f"sio_type_{sio_type}", sio_rest)


def main(match_id: str) -> None:
    sid, sess = open_session()
    print(f"sid={sid}")
    # Connect to default namespace
    post(sess, sid, "40")
    # Wait briefly, then poll
    time.sleep(0.4)
    packets = poll(sess, sid)
    print(f"first poll got {len(packets)} packets:")
    for p in packets:
        t, payload = parse(p)
        print(f"  eio={t}  payload={str(payload)[:200]}")

    # Now try various "subscribe" payloads to see which the server accepts
    for evt in [
        '42["readyForMatch","{mid}"]'.replace("{mid}", match_id),
        f'42["readyForMatch",{match_id}]',
        f'42["readyForScores",{match_id}]',
        f'42["readyForLog",{match_id}]',
        f'42["subscribe","match-{match_id}"]',
    ]:
        post(sess, sid, evt)
        time.sleep(0.5)
        packets = poll(sess, sid)
        print(f"\nafter {evt[:60]!r}: got {len(packets)} packets")
        for p in packets[:8]:
            t, payload = parse(p)
            print(f"  eio={t}  payload={str(payload)[:300]}")

    # Drain for ~75 seconds with regular pings, printing anything received.
    print("\n--- draining for 75s ---")
    deadline = time.time() + 75
    last_ping = 0.0
    while time.time() < deadline:
        if time.time() - last_ping > 20:
            try:
                post(sess, sid, "2")  # ping
            except Exception as e:
                print(f"  ping failed: {e}")
                break
            last_ping = time.time()
        try:
            packets = poll(sess, sid)
        except Exception as e:
            print(f"  poll error: {type(e).__name__}: {str(e)[:120]}")
            break
        for p in packets:
            t, payload = parse(p)
            print(f"  [{time.strftime('%H:%M:%S')}] eio={t}  payload={str(payload)[:280]}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "2394258")
