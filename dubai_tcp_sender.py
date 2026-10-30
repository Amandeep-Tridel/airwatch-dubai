#!/usr/bin/env python3
"""
=============================================================================
 DUBAI -> BARODA (HP-ZBOOK) REAL-TIME RAW SENSOR STRING SENDER (OPTION B)
=============================================================================
 Target Server (Tailscale IP): 100.96.244.94
 Target TCP/UDP Port         : 9000

 Since Temperature, Relative Humidity (RH %), Barometric Pressure, and Wind
 are fetched automatically by the server from live Dubai internet weather
 (Open-Meteo API), this script ONLY sends the 4 raw gas sensor ADC readings:

     AQMS-DXB-01,<H2S_WE>,<H2S_AE>,<NH3_WE>,<VOC>

 Zero third-party dependencies required (uses only Python standard library).
 Features automatic TCP reconnection if 4G / Wi-Fi drops in Dubai.
=============================================================================
"""

from __future__ import annotations

import argparse
import math
import random
import socket
import time
from typing import Optional

DEFAULT_SERVER_IP = "100.96.244.94"
DEFAULT_PORT = 9000
DEFAULT_STATION_ID = "AQMS-DXB-01"


class HighSpeedStringSender:
    """
    Persistent Raw TCP / UDP single-string transmitter with auto-reconnect.
    """

    def __init__(
        self,
        host: str = DEFAULT_SERVER_IP,
        port: int = DEFAULT_PORT,
        protocol: str = "tcp",
    ) -> None:
        self.host = host
        self.port = port
        self.protocol = protocol.lower()
        self.sock: Optional[socket.socket] = None

    def _ensure_connected(self) -> None:
        if self.sock is not None:
            return
        if self.protocol == "udp":
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            return

        s = socket.create_connection((self.host, self.port), timeout=5.0)
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self.sock = s
        print(f"[CONNECTED] Raw TCP link established -> {self.host}:{self.port}")

    def send_raw_string(self, single_string: str) -> bool:
        payload = (single_string.strip() + "\n").encode("utf-8")
        try:
            self._ensure_connected()
            assert self.sock is not None
            if self.protocol == "udp":
                self.sock.sendto(payload, (self.host, self.port))
            else:
                self.sock.sendall(payload)
            return True
        except Exception as exc:
            print(f"[WARN] Link interrupted ({exc}), reconnecting...")
            self.close()
            return False

    def send_sensor_reading(
        self,
        h2s_we_counts: float,
        h2s_ae_counts: float,
        nh3_we_counts: float,
        voc_counts: float,
        station_id: str = DEFAULT_STATION_ID,
    ) -> bool:
        """
        Format the 4 raw gas sensor ADC readings into a single string and transmit:
          STATION_ID,H2S_WE,H2S_AE,NH3_WE,VOC
        """
        line = f"{station_id},{h2s_we_counts},{h2s_ae_counts},{nh3_we_counts},{voc_counts}"
        return self.send_raw_string(line)

    def close(self) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except Exception:
                pass
            self.sock = None


def main() -> None:
    parser = argparse.ArgumentParser(description="Dubai -> 100.96.244.94:9000 Gas-Only String Sender")
    parser.add_argument("--host", default=DEFAULT_SERVER_IP, help="Target IP (default: 100.96.244.94)")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Target Port (default: 9000)")
    parser.add_argument("--proto", choices=["tcp", "udp"], default="tcp", help="tcp or udp")
    parser.add_argument("--hz", type=float, default=10.0, help="Send rate in packets/sec")
    parser.add_argument("--count", type=int, default=0, help="Number of packets (0 = run forever)")
    parser.add_argument("--serial", default=None, help="Optional COM port (e.g. COM4) to forward serial lines")
    parser.add_argument("--baud", type=int, default=115200, help="Serial baud rate (default: 115200)")
    args = parser.parse_args()

    sender = HighSpeedStringSender(host=args.host, port=args.port, protocol=args.proto)

    if args.serial:
        import serial  # pip install pyserial
        print(f"Opening Serial {args.serial} @ {args.baud} baud -> forwarding to {args.host}:{args.port}...")
        with serial.Serial(args.serial, args.baud, timeout=1.0) as ser:
            step = 0
            while True:
                raw_line = ser.readline().decode("utf-8", errors="replace").strip()
                if not raw_line:
                    continue
                step += 1
                if sender.send_raw_string(raw_line) and (step <= 5 or step % 20 == 0):
                    print(f"[Serial #{step}] Forwarded -> {raw_line}")
        return

    print(f"Streaming 4-channel gas sensor strings to {args.host}:{args.port} ({args.proto.upper()}) at {args.hz} Hz...")

    step = 0
    try:
        while args.count <= 0 or step < args.count:
            step += 1
            # --- YOUR FRIEND'S 4 RAW GAS ADC CHANNELS ---
            h2s_we = int(5020 + 550 * math.sin(step / 15.0) + random.gauss(0, 10))
            h2s_ae = int(4905 + random.gauss(0, 5))
            nh3_we = int(-3880 + 280 * math.sin(step / 20.0) + random.gauss(0, 12))
            voc_cnt = int(840 + 200 * math.sin(step / 25.0) + random.gauss(0, 7))
            # --------------------------------------------

            ok = sender.send_sensor_reading(
                h2s_we_counts=h2s_we,
                h2s_ae_counts=h2s_ae,
                nh3_we_counts=nh3_we,
                voc_counts=voc_cnt,
            )
            if ok and (step <= 5 or step % int(max(1, args.hz * 2)) == 0):
                print(f"[#{step}] Sent -> AQMS-DXB-01,{h2s_we},{h2s_ae},{nh3_we},{voc_cnt}")
            time.sleep(1.0 / args.hz)
    except KeyboardInterrupt:
        print("\nStopped by user.")
    finally:
        sender.close()


if __name__ == "__main__":
    main()
