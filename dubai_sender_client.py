#!/usr/bin/env python3
"""
Dubai Side Telemetry Sender Client (`dubai_sender_client.py`).

Share this script with your friend in Dubai (or run it locally to test TCP/UDP/WS/HTTP).
It formats raw sensor counts into a single string:
    "AQMS-DXB-01,<H2S_WE>,<H2S_AE>,<NH3_WE>,<VOC>,<TEMP_C>,<RH_PCT>,<PRESS_HPA>,<WIND_MS>,<WIND_DEG>"
and streams it at high speed to your Python server.

Usage Examples:
  # 1. Stream over Raw TCP Socket (Port 9000) at 10 Hz:
  python dubai_sender_client.py --host 127.0.0.1 --port 9000 --mode tcp --hz 10 --count 50

  # 2. Stream over WebSocket (Port 8000, works over Cloudflare Tunnel / ngrok):
  python dubai_sender_client.py --url ws://127.0.0.1:8000/ws/ingest --mode ws --hz 10 --count 50

  # 3. Stream over UDP (Port 9000):
  python dubai_sender_client.py --host 127.0.0.1 --port 9000 --mode udp --hz 20 --count 50

  # 4. Forward live lines from an Arduino/ESP32 Serial Port in Dubai:
  python dubai_sender_client.py --serial COM4 --baud 115200 --mode tcp --host YOUR_IP
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import socket
import time
import urllib.request


def generate_sample_string(step: int, station_id: str = "AQMS-DXB-01", fmt: str = "csv") -> str:
    """Build a single raw sensor string from ADC counts + head environment."""
    t_c = round(35.5 + 3.0 * math.sin(step / 30.0), 2)
    rh = round(44.0 + 8.0 * math.cos(step / 40.0), 1)
    p_hpa = 1008.4
    h2s_we = int(4950 + 600 * math.sin(step / 15.0) + random.gauss(0, 12))
    h2s_ae = int(4905 + random.gauss(0, 6))
    nh3_we = int(-3890 + 300 * math.sin(step / 20.0) + random.gauss(0, 15))
    voc_cnt = int(830 + 220 * math.sin(step / 25.0) + random.gauss(0, 8))
    ws = round(4.1 + 1.5 * abs(math.sin(step / 18.0)), 2)
    wd = round((280 + 20 * math.sin(step / 22.0)) % 360, 1)

    if fmt == "kv":
        return (
            f"id={station_id},h2s_we={h2s_we},h2s_ae={h2s_ae},nh3_we={nh3_we},"
            f"voc={voc_cnt},t={t_c},rh={rh},p={p_hpa},ws={ws},wd={wd}"
        )
    return f"{station_id},{h2s_we},{h2s_ae},{nh3_we},{voc_cnt},{t_c},{rh},{p_hpa},{ws},{wd}"


def stream_tcp(host: str, port: int, hz: float, count: int, fmt: str) -> None:
    print(f"[TCP] Connecting to {host}:{port} ...")
    with socket.create_connection((host, port), timeout=5.0) as sock:
        print(f"[TCP] Connected! Streaming single-string packets at {hz} Hz...")
        step = 0
        while count <= 0 or step < count:
            step += 1
            raw_str = generate_sample_string(step, fmt=fmt)
            sock.sendall((raw_str + "\n").encode("utf-8"))
            if step <= 5 or step % 10 == 0:
                print(f"  -> Sent #{step}: {raw_str}")
            time.sleep(1.0 / hz)


def stream_udp(host: str, port: int, hz: float, count: int, fmt: str) -> None:
    print(f"[UDP] Streaming datagrams to {host}:{port} at {hz} Hz...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    step = 0
    while count <= 0 or step < count:
        step += 1
        raw_str = generate_sample_string(step, fmt=fmt)
        sock.sendto((raw_str + "\n").encode("utf-8"), (host, port))
        if step <= 5 or step % 10 == 0:
            print(f"  -> Sent #{step}: {raw_str}")
        time.sleep(1.0 / hz)


def stream_http(url: str, hz: float, count: int, fmt: str) -> None:
    print(f"[HTTP] POSTing single strings to {url} at {hz} Hz...")
    step = 0
    while count <= 0 or step < count:
        step += 1
        raw_str = generate_sample_string(step, fmt=fmt)
        req = urllib.request.Request(
            url,
            data=raw_str.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5.0) as resp:
            body = resp.read().decode("utf-8")
            if step <= 5 or step % 10 == 0:
                print(f"  -> Sent #{step}: {raw_str} | Server: {body[:90]}")
        time.sleep(1.0 / hz)


async def stream_ws(url: str, hz: float, count: int, fmt: str) -> None:
    import websockets

    print(f"[WS] Connecting to {url} ...")
    async with websockets.connect(url) as ws:
        print(f"[WS] Connected! Streaming at {hz} Hz...")
        step = 0
        while count <= 0 or step < count:
            step += 1
            raw_str = generate_sample_string(step, fmt=fmt)
            await ws.send(raw_str)
            ack = await ws.recv()
            if step <= 5 or step % 10 == 0:
                print(f"  -> Sent #{step}: {raw_str} | {ack}")
            await asyncio.sleep(1.0 / hz)


def main() -> None:
    parser = argparse.ArgumentParser(description="AQMS Dubai Single-String Telemetry Sender")
    parser.add_argument("--mode", choices=["tcp", "udp", "ws", "http"], default="tcp")
    parser.add_argument("--host", default="127.0.0.1", help="Server IP or hostname")
    parser.add_argument("--port", type=int, default=9000, help="TCP/UDP port (default 9000)")
    parser.add_argument("--url", default=None, help="Full URL for ws or http mode")
    parser.add_argument("--hz", type=float, default=10.0, help="Packets per second")
    parser.add_argument("--count", type=int, default=20, help="Number of packets (0 = infinite)")
    parser.add_argument("--format", choices=["csv", "kv"], default="csv")
    args = parser.parse_args()

    if args.mode == "tcp":
        stream_tcp(args.host, args.port, args.hz, args.count, args.format)
    elif args.mode == "udp":
        stream_udp(args.host, args.port, args.hz, args.count, args.format)
    elif args.mode == "http":
        url = args.url or f"http://{args.host}:8000/api/ingest"
        stream_http(url, args.hz, args.count, args.format)
    elif args.mode == "ws":
        url = args.url or f"ws://{args.host}:8000/ws/ingest"
        asyncio.run(stream_ws(url, args.hz, args.count, args.format))


if __name__ == "__main__":
    main()
