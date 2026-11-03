#!/usr/bin/env python3
"""
AQMS Algorithm Engine v7.0 -- Alphasense factory calibration only.

Each gas value is worked out from the sensor's voltage and the numbers printed on
that sensor's Alphasense factory calibration sheet (gas_algo/factory_formulas.py):

    H2S (ppb) = ((A1 - WE zero) - (A2 - AE zero)) / sensitivity
    NH3 (ppm) = (A0 - WE zero) / sensitivity          sensor 77840031: (A0 mV - 328) / 58.624
    VOC (ppm) = (A3 - V zero) / sensitivity

Nothing else: no spike filter, no smoothing, no noise models, no temperature
tables, no density correction, no clamping. A gas whose factory sheet has not been
entered in config.json gets no number ("NO_CALIBRATION"); its raw voltage is still
reported.

The 10/20/30-minute values are plain averages of the readings in that window.
Alarm levels are evaluated on the 10-minute average.
"""

from __future__ import annotations

import time
from collections import deque
from datetime import datetime
from typing import Any, Deque, Dict, Optional, Tuple

from gas_algo import factory_formulas as ff


GASES = ("h2s", "nh3", "voc")
ROLLING_SPAN_S = 1800.0


def parse_iso_unix(ts_str: str) -> Optional[float]:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _num(x: Any) -> Optional[float]:
    try:
        return None if x is None else float(x)
    except (TypeError, ValueError):
        return None


class StationState:
    """Per-station memory: only what the 10/20/30-minute averages and uptime need."""

    def __init__(self) -> None:
        self.rolling: Deque[Tuple[float, Optional[float], Optional[float], Optional[float]]] = deque()
        self.power_on_unix: Optional[float] = None
        self.last_sample_unix: Optional[float] = None
        self.packets = 0


class AQMSAlgorithmEngine:
    ALGO_VERSION = "v7.0 (Alphasense factory calibration only)"

    def __init__(self, config: Dict[str, Any]) -> None:
        self.states: Dict[str, StationState] = {}
        self.update_config(config)

    def update_config(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.cal = config.get("calibration", {})
        self.thresholds = config.get("alarms", {})
        self.power_gap_reset_s = float(self.cal.get("power_gap_reset_min", 15)) * 60.0

    # ------------------------------------------------------------------ helpers
    def _state(self, station_id: str) -> StationState:
        if station_id not in self.states:
            self.states[station_id] = StationState()
        return self.states[station_id]

    def _factory(self, gas: str) -> Optional[Dict[str, float]]:
        """The gas's factory-sheet values, or None if any of them has not been entered."""
        c = self.cal.get(gas, {})
        need = ["we_zero_mv", "sensitivity"] + (["ae_zero_mv"] if gas == "h2s" else [])
        if any(_num(c.get(k)) is None for k in need):
            return None
        return {k: float(c[k]) for k in need}

    def calibration_label(self, gas: str) -> str:
        f = self._factory(gas)
        sheet = self.cal.get(gas, {}).get("factory_sheet") or "factory sheet"
        if f is None:
            return "No factory calibration sheet entered"
        if gas == "h2s":
            return f"{sheet}: ((A1 - {f['we_zero_mv']:g}) - (A2 - {f['ae_zero_mv']:g})) / {f['sensitivity']:g} ppb"
        ch = "A0" if gas == "nh3" else "A3"
        return f"{sheet}: ({ch} mV - {f['we_zero_mv']:g}) / {f['sensitivity']:g}"

    def _alarm(self, key: str, value: Optional[float]) -> str:
        th = self.thresholds.get(key, {})
        if value is None:
            return "NORMAL"
        if th.get("critical") is not None and value >= th["critical"]:
            return "CRITICAL"
        if th.get("action") is not None and value >= th["action"]:
            return "ACTION"
        if th.get("warning") is not None and value >= th["warning"]:
            return "WARNING"
        return "NORMAL"

    # ------------------------------------------------------------------ main
    def process(self, parsed: Dict[str, Any], transport: str = "FIREBASE") -> Dict[str, Any]:
        station_id = parsed["station_id"]
        st = self._state(station_id)
        st.packets += 1

        # --- voltages (exact reported volts when present, else ADC counts) ----
        rv = parsed.get("reported_volts")
        if isinstance(rv, list) and len(rv) >= 4 and all(_num(x) is not None for x in rv[:4]):
            nh3_mv, we_mv, ae_mv, voc_mv = (float(x) * 1000.0 for x in rv[:4])
        else:
            nh3_mv = ff.counts_to_mv(float(parsed["nh3_we_counts"]))
            we_mv = ff.counts_to_mv(float(parsed["h2s_we_counts"]))
            ae_mv = ff.counts_to_mv(float(parsed["h2s_ae_counts"]))
            voc_mv = ff.counts_to_mv(float(parsed["voc_counts"]))

        # --- uptime (display only) -------------------------------------------
        ts_unix = parse_iso_unix(parsed.get("ts", "")) or time.time()
        gap = st.last_sample_unix is not None and ts_unix - st.last_sample_unix > self.power_gap_reset_s
        if st.power_on_unix is None or gap:
            st.power_on_unix = ts_unix
            if gap:
                st.rolling.clear()
        st.last_sample_unix = ts_unix

        # --- the three factory formulas ----------------------------------------
        value: Dict[str, Optional[float]] = {}
        f = self._factory("h2s")
        value["h2s"] = ff.h2s_ppb(we_mv, f["we_zero_mv"], ae_mv, f["ae_zero_mv"], f["sensitivity"]) / 1000.0 if f else None
        f = self._factory("nh3")
        value["nh3"] = ff.nh3_ppm(nh3_mv, f["we_zero_mv"], f["sensitivity"]) if f else None
        f = self._factory("voc")
        voc_fault = bool(f) and ff.voc_sensor_fault(voc_mv, f["we_zero_mv"])
        value["voc"] = ff.voc_ppm(voc_mv, f["we_zero_mv"], f["sensitivity"]) if f and not voc_fault else None

        # --- plain 10 / 20 / 30-minute averages --------------------------------
        st.rolling.append((ts_unix, value["h2s"], value["nh3"], value["voc"]))
        while st.rolling and st.rolling[0][0] < ts_unix - ROLLING_SPAN_S:
            st.rolling.popleft()

        def win_mean(sec: float, idx: int) -> Optional[float]:
            vals = [r[idx] for r in st.rolling if r[0] >= ts_unix - sec and r[idx] is not None]
            return sum(vals) / len(vals) if vals else None

        signal = {
            "h2s": f"A1 WE {we_mv:.2f} mV · A2 AE {ae_mv:.2f} mV",
            "nh3": f"A0 {nh3_mv:.2f} mV",
            "voc": f"A3 {voc_mv:.2f} mV",
        }
        report: Dict[str, Dict[str, Any]] = {}
        for i, g in enumerate(GASES, start=1):
            v = value[g]
            report[g] = {
                "value_ppm": None if v is None else round(v, 5),
                "sample_ppm": None if v is None else round(v, 5),
                "avg_10m_ppm": _round(win_mean(600.0, i)),
                "avg_20m_ppm": _round(win_mean(1200.0, i)),
                "avg_30m_ppm": _round(win_mean(1800.0, i)),
                "status": "FACTORY_CAL" if self._factory(g) else "NO_CALIBRATION",
                "calibration": self.calibration_label(g),
                "signal": signal[g],
            }

        alarms = {g: self._alarm(f"{g}_ppm", report[g]["avg_10m_ppm"]) for g in GASES}
        rank = {"NORMAL": 0, "WARNING": 1, "ACTION": 2, "CRITICAL": 3}
        alarms["overall"] = max(alarms.values(), key=lambda s: rank[s])

        def flags(g: str) -> str:
            if not self._factory(g):
                return "NO_FACTORY_CALIBRATION"
            serial = self.cal.get(g, {}).get("serial") or "SHEET"
            return f"FACTORY_CAL_{serial}" + ("|SENSOR_FAULT_BELOW_ZERO" if g == "voc" and voc_fault else "")

        battery_v = _num(parsed.get("battery_v"))
        calibrated = [g.upper() for g in GASES if self._factory(g)]
        return {
            "station_id": station_id,
            "algo_version": self.ALGO_VERSION,
            "calibration_label": f"Factory sheet ({', '.join(calibrated)} only)" if calibrated else "No factory sheets",
            "ts": parsed["ts"],
            "uptime_min": round((ts_unix - st.power_on_unix) / 60.0, 1),
            "received_unix": time.time(),
            "transport": transport.upper(),
            "format_detected": parsed.get("format_detected"),
            "weather_source": parsed.get("weather_source", "Open-Meteo (Dubai)"),
            "firebase_key": parsed.get("firebase_key"),
            "raw_string": parsed["raw_string"],
            "report": report,
            "alarms": alarms,
            "qc": {f"{g}_flags": flags(g) for g in GASES},
            "hardware": {
                "battery_v": battery_v,
                "h2s_diff_v": round((we_mv - ae_mv) / 1000.0, 5),
                "sample_age_ms": parsed.get("sample_age_ms"),
                "adc_ok": parsed.get("adc_ok", True),
                "stm32_status": parsed.get("stm32_status", "NA"),
            },
            "raw": {
                "nh3_we_mv": round(nh3_mv, 3),
                "h2s_we_mv": round(we_mv, 3),
                "h2s_ae_mv": round(ae_mv, 3),
                "voc_mv": round(voc_mv, 3),
                # Weather is shown on the dashboard only; it is not used in any formula.
                "head_t_c": round(float(parsed["head_t_c"]), 2),
                "rh_pct": round(float(parsed["rh_pct"]), 2),
                "p_hpa": round(float(parsed["p_hpa"]), 2),
                "wind_speed_ms": round(float(parsed["wind_speed_ms"]), 2),
                "wind_dir_deg": round(float(parsed["wind_dir_deg"]), 1),
            },
        }


def _round(x: Optional[float], places: int = 5) -> Optional[float]:
    return None if x is None else round(x, places)
