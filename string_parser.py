#!/usr/bin/env python3
"""
High-speed Single-String & Firebase Telemetry Parser for AQMS Dubai (`aqms-monitor`).

Hardware ADC Channel Mapping (confirmed by user):
  - A0 : NH3            (Working Electrode, `nh3_we_counts`)
  - A1 : H2S OP1        (Working Electrode, `h2s_we_counts`)
  - A2 : H2S OP2        (Auxiliary Electrode, `h2s_ae_counts`)
  - A3 : VOC            (PIDX-A-040 output, `voc_counts`)

Natively parses:
1. Dubai `combinedString` format (from Firebase RTDB or direct TCP/HTTP/WS):
   "2026-10-28,14:19:00,A0:raw=6286|volts=0.49109,A1:raw=6025|volts=0.47070,A2:raw=4597|volts=0.35914,A3:raw=1333|volts=0.10414,STM32:NA"
2. Positional 4-channel CSV (in A0,A1,A2,A3 order: NH3, H2S_OP1, H2S_OP2, VOC)
3. Key-Value Strings
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Optional

RE_AX_CHANNEL = re.compile(
    r"A([0-3])\s*:\s*raw\s*=\s*(-?\d+(?:\.\d+)?)(?:\s*\|\s*volts\s*=\s*(-?\d+(?:\.\d+)?))?",
    re.IGNORECASE,
)
RE_DATE_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2})\s*,\s*(\d{2}:\d{2}:\d{2})")

KEY_ALIASES: Dict[str, str] = {
    "id": "station_id",
    "st": "station_id",
    "station": "station_id",
    "station_id": "station_id",
    "device": "station_id",
    "ts": "ts",
    "time": "ts",
    "timestamp": "ts",
    # A0 = NH3
    "a0": "nh3_we_counts",
    "nh3": "nh3_we_counts",
    "nh3_we": "nh3_we_counts",
    "nh3_we_counts": "nh3_we_counts",
    # A1 = H2S OP1 (Working Electrode)
    "a1": "h2s_we_counts",
    "op1": "h2s_we_counts",
    "h2s_op1": "h2s_we_counts",
    "h2s_we": "h2s_we_counts",
    "h2s_we_counts": "h2s_we_counts",
    # A2 = H2S OP2 (Auxiliary Electrode)
    "a2": "h2s_ae_counts",
    "op2": "h2s_ae_counts",
    "h2s_op2": "h2s_ae_counts",
    "h2s_ae": "h2s_ae_counts",
    "h2s_ae_counts": "h2s_ae_counts",
    # A3 = VOC
    "a3": "voc_counts",
    "voc": "voc_counts",
    "voc_counts": "voc_counts",
    "pid": "voc_counts",
}


def _is_float(val: str) -> bool:
    try:
        float(val)
        return True
    except ValueError:
        return False


def _looks_like_timestamp(val: str) -> bool:
    if "T" in val or ("-" in val and ":" in val):
        return True
    if _is_float(val):
        num = float(val)
        if 1577836800 <= num <= 4102444800 or 1577836800000 <= num <= 4102444800000:
            return True
    return False


def _normalize_timestamp(val: Optional[str]) -> str:
    if not val:
        return datetime.now(timezone.utc).isoformat()
    val = val.strip()
    if _is_float(val):
        num = float(val)
        if num > 1e11:
            num /= 1000.0
        if num >= 1577836800:
            return datetime.fromtimestamp(num, tz=timezone.utc).isoformat()
    return val


def parse_raw_string(
    raw_line: str,
    config_format: Optional[Dict[str, Any]] = None,
    live_weather: Optional[Dict[str, Any]] = None,
    firebase_meta: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Parse a single raw sensor string (including Dubai's `combinedString`) into a
    canonical dictionary using the hardware channel assignment:
      A0 = NH3, A1 = H2S OP1 (WE), A2 = H2S OP2 (AE), A3 = VOC.
    """
    if not raw_line or not raw_line.strip():
        raise ValueError("Empty telemetry string")

    original_str = raw_line.strip()
    default_station = (config_format or {}).get("default_station_id", "aqms-monitor")
    w = live_weather or {}
    fb = firebase_meta or {}

    parsed: Dict[str, Any] = {
        "raw_string": original_str,
        "station_id": fb.get("device") or default_station,
        "ts": fb.get("timestampUtc") or fb.get("ntpUtc") or None,
        # Mapped ADC counts
        "nh3_we_counts": 0.0,   # A0
        "h2s_we_counts": 0.0,   # A1 (OP1)
        "h2s_ae_counts": 0.0,   # A2 (OP2)
        "voc_counts": 0.0,      # A3
        # Reported volts directly from Dubai board if present [A0, A1, A2, A3]
        "reported_volts": fb.get("volts"),
        "battery_v": fb.get("batteryV"),
        "h2s_diff_v": fb.get("h2sDiffV"),
        "sample_age_ms": fb.get("sampleAgeMs"),
        "adc_ok": fb.get("adcOk", True),
        "stm32_status": "NA",
        "firebase_key": fb.get("firebase_key"),
        # Live Dubai weather from Open-Meteo
        "head_t_c": float(w.get("temp_c", 30.0)),
        "rh_pct": float(w.get("rh_pct", 67.0)),
        "p_hpa": float(w.get("press_hpa", 1012.0)),
        "wind_speed_ms": float(w.get("wind_speed_ms", 2.62)),
        "wind_dir_deg": float(w.get("wind_dir_deg", 342.0)),
        "weather_source": w.get("source", "Open-Meteo Live (Dubai 25.28°N, 55.40°E)"),
        "extra_raw": {},
        "format_detected": "unknown",
    }

    # -------------------------------------------------------------------------
    # 0. Structured Firebase RTDB payload (`raw` + `volts`, schemaVersion 1..4+)
    #    Handles both schemaVersion 3 (`A0:raw=...|volts=...`) and
    #    schemaVersion 4 (`2026-10-30,05:25:50,0.42328,0.46234,0.39430,0.09672,NA`)
    # -------------------------------------------------------------------------
    fb_raw = fb.get("raw")
    fb_volts = fb.get("volts")
    if isinstance(fb_raw, list) and len(fb_raw) >= 4 and all(x is not None for x in fb_raw[:4]):
        parsed["format_detected"] = f"firebase_schema_v{fb.get('schemaVersion', 'structured')}"
        parsed["nh3_we_counts"] = float(fb_raw[0])   # A0 = NH3
        parsed["h2s_we_counts"] = float(fb_raw[1])   # A1 = H2S OP1 (WE)
        parsed["h2s_ae_counts"] = float(fb_raw[2])   # A2 = H2S OP2 (AE)
        parsed["voc_counts"] = float(fb_raw[3])      # A3 = VOC

        if isinstance(fb_volts, list) and len(fb_volts) >= 4 and all(x is not None for x in fb_volts[:4]):
            parsed["reported_volts"] = [float(x) for x in fb_volts[:4]]
            if parsed["h2s_diff_v"] is None:
                parsed["h2s_diff_v"] = round(parsed["reported_volts"][1] - parsed["reported_volts"][2], 5)

        dt_match = RE_DATE_TIME.search(original_str)
        if dt_match and not parsed["ts"]:
            parsed["ts"] = f"{dt_match.group(1)}T{dt_match.group(2)}Z"

        stm_obj = fb.get("stm32")
        if isinstance(stm_obj, dict):
            parsed["stm32_status"] = "OK" if stm_obj.get("available") else "NA"
        elif "STM32:" in original_str:
            parsed["stm32_status"] = original_str.split("STM32:", 1)[1].split(",", 1)[0].strip()
        else:
            parts = [p.strip() for p in original_str.split(",")]
            if len(parts) >= 7:
                parsed["stm32_status"] = parts[6]

        parsed["ts"] = _normalize_timestamp(parsed["ts"])
        return parsed

    # -------------------------------------------------------------------------
    # 1. Detect Dubai `combinedString` v1..v3 format:
    #    "2026-10-28,14:19:00,A0:raw=6286|volts=0.49109,A1:raw=6025|volts=0.47070,..."
    #    Mapping: A0 -> NH3, A1 -> H2S OP1 (WE), A2 -> H2S OP2 (AE), A3 -> VOC
    # -------------------------------------------------------------------------
    ax_matches = RE_AX_CHANNEL.findall(original_str)
    if len(ax_matches) >= 2:
        parsed["format_detected"] = "dubai_combined_string"
        dt_match = RE_DATE_TIME.search(original_str)
        if dt_match and not parsed["ts"]:
            parsed["ts"] = f"{dt_match.group(1)}T{dt_match.group(2)}Z"

        volts_map: Dict[int, float] = {}
        for ch_str, raw_val_str, volts_val_str in ax_matches:
            ch_idx = int(ch_str)
            cnt = float(raw_val_str)
            if ch_idx == 0:
                parsed["nh3_we_counts"] = cnt   # A0 = NH3
            elif ch_idx == 1:
                parsed["h2s_we_counts"] = cnt   # A1 = H2S OP1 (WE)
            elif ch_idx == 2:
                parsed["h2s_ae_counts"] = cnt   # A2 = H2S OP2 (AE)
            elif ch_idx == 3:
                parsed["voc_counts"] = cnt      # A3 = VOC
            if volts_val_str:
                volts_map[ch_idx] = float(volts_val_str)

        if len(volts_map) == 4 and not parsed["reported_volts"]:
            parsed["reported_volts"] = [
                volts_map[0],
                volts_map[1],
                volts_map[2],
                volts_map[3],
            ]

        if parsed["h2s_diff_v"] is None and 1 in volts_map and 2 in volts_map:
            parsed["h2s_diff_v"] = round(volts_map[1] - volts_map[2], 5)

        if "STM32:" in original_str:
            stm_part = original_str.split("STM32:", 1)[1].split(",", 1)[0].strip()
            parsed["stm32_status"] = stm_part

        parsed["ts"] = _normalize_timestamp(parsed["ts"])
        return parsed

    # -------------------------------------------------------------------------
    # 1b. Detect Dubai `combinedString` v4 compact CSV format:
    #     "2026-10-30,05:25:50,0.42328,0.46234,0.39430,0.09672,NA"
    # -------------------------------------------------------------------------
    dt_lead = RE_DATE_TIME.match(original_str)
    if dt_lead:
        csv_parts = [p.strip() for p in original_str.split(",")]
        if len(csv_parts) >= 6 and all(_is_float(p) for p in csv_parts[2:6]):
            if not parsed["ts"]:
                parsed["ts"] = f"{dt_lead.group(1)}T{dt_lead.group(2)}Z"
            ch_vals = [float(p) for p in csv_parts[2:6]]
            is_volts = any("." in p for p in csv_parts[2:6]) and all(abs(v) <= 5.5 for v in ch_vals)
            if is_volts:
                parsed["format_detected"] = "dubai_combined_v4_volts"
                if not parsed["reported_volts"]:
                    parsed["reported_volts"] = ch_vals
                # 16-bit bipolar 2.56 V FSR: 1 LSB = 2.56 / 32768 V
                counts = [round(v * 32768.0 / 2.56) for v in ch_vals]
            else:
                parsed["format_detected"] = "dubai_combined_v4_counts"
                counts = ch_vals
            parsed["nh3_we_counts"] = float(counts[0])
            parsed["h2s_we_counts"] = float(counts[1])
            parsed["h2s_ae_counts"] = float(counts[2])
            parsed["voc_counts"] = float(counts[3])
            if parsed["h2s_diff_v"] is None and parsed["reported_volts"]:
                parsed["h2s_diff_v"] = round(parsed["reported_volts"][1] - parsed["reported_volts"][2], 5)
            if len(csv_parts) >= 7:
                stm_tok = csv_parts[6]
                parsed["stm32_status"] = stm_tok.split(":", 1)[1].strip() if stm_tok.upper().startswith("STM32:") else stm_tok
            parsed["ts"] = _normalize_timestamp(parsed["ts"])
            return parsed

    # -------------------------------------------------------------------------
    # 2. Key-Value or Positional CSV Formats (A0=NH3, A1=H2S_OP1, A2=H2S_OP2, A3=VOC)
    # -------------------------------------------------------------------------
    working = original_str
    if working.startswith("$"):
        working = working[1:]
        if "*" in working:
            working = working.rsplit("*", 1)[0]

    if "," in working:
        tokens = [t.strip() for t in working.split(",") if t.strip() != ""]
    elif "|" in working:
        tokens = [t.strip() for t in working.split("|") if t.strip() != ""]
    elif ";" in working:
        tokens = [t.strip() for t in working.split(";") if t.strip() != ""]
    else:
        tokens = [t.strip() for t in working.split() if t.strip() != ""]

    kv_count = sum(
        1 for t in tokens if ("=" in t) or (":" in t and not _looks_like_timestamp(t))
    )
    if kv_count >= 2:
        parsed["format_detected"] = "key_value"
        for tok in tokens:
            sep = "=" if "=" in tok else (":" if ":" in tok else None)
            if not sep:
                continue
            k_raw, v_raw = tok.split(sep, 1)
            canonical = KEY_ALIASES.get(k_raw.strip().lower(), k_raw.strip().lower())
            v_clean = v_raw.strip()
            if canonical in ("nh3_we_counts", "h2s_we_counts", "h2s_ae_counts", "voc_counts"):
                parsed[canonical] = float(v_clean)
            elif canonical == "station_id":
                parsed["station_id"] = v_clean
            elif canonical == "ts":
                parsed["ts"] = _normalize_timestamp(v_clean)
        parsed["ts"] = _normalize_timestamp(parsed["ts"])
        return parsed

    idx = 0
    n = len(tokens)
    if idx < n and not _is_float(tokens[idx]) and not _looks_like_timestamp(tokens[idx]):
        parsed["station_id"] = tokens[idx]
        idx += 1
    if idx < n and _looks_like_timestamp(tokens[idx]):
        parsed["ts"] = _normalize_timestamp(tokens[idx])
        idx += 1
    else:
        parsed["ts"] = _normalize_timestamp(parsed["ts"])

    remaining = tokens[idx:]
    if len(remaining) < 4:
        raise ValueError(
            f"Expected 4 sensor channels (A0=NH3, A1=H2S_OP1, A2=H2S_OP2, A3=VOC), got {len(remaining)} in '{original_str}'"
        )

    parsed["format_detected"] = "a0_a1_a2_a3_csv"
    parsed["nh3_we_counts"] = float(remaining[0])   # A0 = NH3
    parsed["h2s_we_counts"] = float(remaining[1])   # A1 = H2S OP1
    parsed["h2s_ae_counts"] = float(remaining[2])   # A2 = H2S OP2
    parsed["voc_counts"] = float(remaining[3])      # A3 = VOC
    return parsed
