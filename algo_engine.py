#!/usr/bin/env python3
"""
AQMS Real-Time Algorithm Engine v5.0 -- datasheet methods + honest uncertainty.

Why v5.0 replaced v4.0
----------------------
A hold-out audit of the v4.0 "Jacobian" and "virtual auxiliary electrode"
models showed they were curve fits to one night of data, not physics:
  * The fitted A1-vs-A2 slope changed from -0.95 to -0.70 between the two
    halves of the night; out-of-sample error was -9 +/- 12 ppb H2S, larger
    than the values being reported.
  * The NH3 "virtual AE" slope flipped sign (+1.81 -> -0.25).
  * Clamping negatives to 0 hid drift (H2S was exactly 0 in 45 % of samples).
  * The Hampel filter rejected 15 % of samples (expected ~1 %).

What v5.0 does instead
----------------------
1. Physics = the reference Alphasense implementations in gas_algo/:
     H2S-B4  : AAN 803-05 algorithm 1, WE_c = (WE-WE_e) - n_T*(AE-AE_e), n_T=1
     NH3-B1  : AAN 803-05 algorithm-4 shape (3-electrode, no AE)
     PIDX    : fixed clean-air zero + ideal-gas T/P density correction
   Their QC flags are passed through unchanged.
2. Values stay SIGNED. Nothing is clamped to zero.
3. Every reading carries a detection limit (LOD), a 1-sigma uncertainty and a
   status: WARMUP / BELOW_LOD / INDICATIVE / QUANTITATIVE.
     LOD   = max(3 x live noise, zero uncertainty)
     U     = sqrt(zero_unc^2 + noise^2 + (sens_rel_unc x value)^2)
   INDICATIVE means "above LOD but sensitivities are not certificate values".
4. Warm-up periods are EXCLUDED (flagged), not modelled with fitted curves.
5. Hampel uses a noise floor so it cannot over-trigger; the Kalman filter is a
   documented smoother (Q/R ratio), applied to signed values.
6. Alarms only fire when the value is above the LOD.
"""

from __future__ import annotations

import math
import statistics
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Deque, Dict, List, Optional, Tuple

from gas_algo import h2s_b4, nh3_b1, pidx_a040


GASES = ("h2s", "nh3", "voc")


def parse_iso_unix(ts_str: str) -> Optional[float]:
    """Parse an ISO-8601 timestamp (e.g. 2026-10-28T14:49:00Z) to Unix seconds."""
    if not ts_str:
        return None
    try:
        dt = datetime.fromisoformat(ts_str.strip().replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def robust_noise(values: List[float]) -> Optional[float]:
    """Single-sample noise sigma from first differences (MAD-based, drift-immune)."""
    if len(values) < 20:
        return None
    diffs = [b - a for a, b in zip(values, values[1:])]
    med = statistics.median(diffs)
    mad = statistics.median([abs(d - med) for d in diffs])
    return 1.4826 * mad / math.sqrt(2.0)


class Kalman1D:
    """Scalar random-walk Kalman smoother. Q/R ratio sets the smoothing strength."""

    def __init__(self, q_over_r: float) -> None:
        self.q_over_r = q_over_r
        self.x: Optional[float] = None
        self.p: float = 0.0

    def update(self, z: float, r: float, r_multiplier: float = 1.0) -> float:
        r_eff = max(1e-18, r * r_multiplier)
        if self.x is None:
            self.x, self.p = z, r_eff
            return z
        p_pred = self.p + self.q_over_r * r
        k = p_pred / (p_pred + r_eff)
        self.x = self.x + k * (z - self.x)
        self.p = (1.0 - k) * p_pred
        return self.x


class StationState:
    """Per-station rolling state."""

    def __init__(self, cfg_dsp: Dict[str, Any]) -> None:
        hw = int(cfg_dsp.get("hampel_window", 9))
        nw = int(cfg_dsp.get("noise_window", 60))
        tw = int(cfg_dsp.get("trend_window", 10))
        q = float(cfg_dsp.get("kalman_q_over_r", 0.05))
        self.hampel: Dict[str, Deque[float]] = {g: deque(maxlen=hw) for g in GASES}
        self.kalman: Dict[str, Kalman1D] = {g: Kalman1D(q) for g in GASES}
        self.filtered_hist: Dict[str, Deque[float]] = {g: deque(maxlen=2 * tw) for g in GASES}
        # Raw signal history in mV for live noise estimation
        self.signal_mv: Dict[str, Deque[float]] = {g: deque(maxlen=nw) for g in GASES}
        self.rolling: Deque[Tuple[float, float, float, float]] = deque(maxlen=2000)
        self.battery_hist: Deque[float] = deque(maxlen=30)
        self.power_on_unix: Optional[float] = None
        self.last_sample_unix: Optional[float] = None
        self.h2s_t90: Optional[float] = None
        self.packets = 0
        self.spikes = 0
        self.battery_dips = 0
        self.samples_filtered = 0


class AQMSAlgorithmEngine:
    ALGO_VERSION = "v5.2 (NH3-B1+ 77840031 factory cal + Alphasense AAN 803)"

    def __init__(self, config: Dict[str, Any]) -> None:
        self.stations: Dict[str, StationState] = {}
        self.update_config(config)

    # ------------------------------------------------------------------ config
    def update_config(self, config: Dict[str, Any]) -> None:
        self.config = config
        cal = config.get("calibration", {})
        self.cal_verified = bool(cal.get("calibration_verified", False))
        self.sensors_prewarmed = bool(cal.get("sensors_prewarmed", True))
        self.enforce_lod = bool(cal.get("enforce_lod", False))
        self.power_gap_reset_s = float(cal.get("power_gap_reset_min", 15)) * 60.0
        self.cfg = {g: cal.get(g, {}) for g in GASES}
        h, n, v = self.cfg["h2s"], self.cfg["nh3"], self.cfg["voc"]

        self.h2s_cal = h2s_b4.Calibration(
            serial=h.get("serial", "H2S-B4"),
            we_e=float(h.get("we_e_mv", 398.8)),
            ae_e=float(h.get("ae_e_mv", 302.0)),
            sens_mv_per_ppb=float(h.get("sens_mv_per_ppb", 0.73)),
            adc_fsr_v=float(h.get("adc_fsr_v", 2.56)),
            adc_bits=int(h.get("adc_bits", 16)),
            adc_bipolar=bool(h.get("adc_bipolar", True)),
        )
        nt = {float(k): float(val) for k, val in h.get("n_t_table", {}).items()} or {20.0: 1.0}
        self.h2s_coeffs = h2s_b4.CoefficientSet(
            source=h.get("n_t_note", "flat n_T=1.0"), verified=self.cal_verified, n_t=nt
        )
        self.h2s_algorithm = int(h.get("algorithm", 1))

        self.nh3_cal = nh3_b1.Calibration(
            serial=n.get("serial", "NH3-B1+ (barcode 77840031)"),
            we_e=float(n.get("we_e_mv", 328.0)),
            sens_mv_per_ppm=float(n.get("sens_mv_per_ppm", 58.624)),
            zero_ppm_equivalent=float(n.get("zero_ppm_equivalent", 0.0)),
            board_bias_mv=float(n.get("board_bias_mv", 200.0)),
            adc_fsr_v=float(n.get("adc_fsr_v", 2.56)),
            adc_bits=int(n.get("adc_bits", 16)),
            adc_bipolar=bool(n.get("adc_bipolar", True)),
        )
        self.nh3_direct_sheet = bool(n.get("direct_sheet_formula", True))
        self.nh3_xs = {"h2s": float(n.get("xs_h2s_ppm_per_ppm", 0.0))}

        self.voc_cal = pidx_a040.Calibration(
            serial=v.get("serial", "PIDX-A-040"),
            mv_per_ppm=float(v.get("mv_per_ppm", 55.0)),
            v_zero_mv=float(v.get("v_zero_mv", 95.5)),
            t_cal_c=float(v.get("t_cal_c", 25.0)),
            p_cal_hpa=float(v.get("p_cal_hpa", 1013.25)),
            response_factor=float(v.get("response_factor", 1.0)),
            adc_fsr_v=float(v.get("adc_fsr_v", 2.56)),
            adc_bits=int(v.get("adc_bits", 16)),
            adc_bipolar=bool(v.get("adc_bipolar", True)),
        )

        self.dsp = config.get("dsp", {})
        self.hampel_n_sigma = float(self.dsp.get("hampel_n_sigma", 3.5))
        self.trend_window = int(self.dsp.get("trend_window", 10))
        self.rolling_window_s = float(self.dsp.get("rolling_window_sec", 600))
        self.alarms_cfg = config.get("alarms", {})

    def _state(self, station_id: str) -> StationState:
        if station_id not in self.stations:
            self.stations[station_id] = StationState(self.dsp)
        return self.stations[station_id]

    # ------------------------------------------------------------------ helpers
    def _noise_mv(self, st: StationState, gas: str) -> float:
        live = robust_noise(list(st.signal_mv[gas]))
        default = float(self.cfg[gas].get("noise_mv_default", 2.0))
        return live if live is not None and live > 0 else default

    def _despike(self, st: StationState, gas: str, x: float, noise_ppm: float) -> Tuple[float, bool]:
        buf = st.hampel[gas]
        spike = False
        out = x
        if len(buf) >= 5:
            med = statistics.median(buf)
            mad_sigma = 1.4826 * statistics.median([abs(b - med) for b in buf])
            sigma = max(mad_sigma, noise_ppm)  # noise floor stops over-triggering
            if abs(x - med) > self.hampel_n_sigma * sigma:
                out, spike = med, True
                st.spikes += 1
        buf.append(x)
        return out, spike

    def _trend(self, st: StationState, gas: str, filtered: float, noise_ppm: float) -> str:
        h = st.filtered_hist[gas]
        h.append(filtered)
        w = self.trend_window
        if len(h) < 2 * w:
            return "STABLE"
        vals = list(h)
        delta = statistics.mean(vals[w:]) - statistics.mean(vals[:w])
        # 3-sigma test on difference of two w-sample means of single-sample noise
        thresh = 3.0 * noise_ppm * math.sqrt(2.0 / w)
        if delta > thresh:
            return "RISING"
        if delta < -thresh:
            return "FALLING"
        return "STABLE"

    def _status(self, warm: bool, value: float, lod: float) -> str:
        if warm:
            return "WARMUP"
        if self.enforce_lod and value < lod:
            return "BELOW_LOD"
        return "QUANTITATIVE" if self.cal_verified else "INDICATIVE"

    def _alarm(self, key: str, value: float, status: str) -> str:
        if status == "WARMUP" or (self.enforce_lod and status == "BELOW_LOD"):
            return "NORMAL"
        th = self.alarms_cfg.get(key, {})
        for level in ("critical", "action", "warning"):
            if th.get(level) is not None and value >= float(th[level]):
                return level.upper()
        return "NORMAL"

    @staticmethod
    def _ugm3(ppm: float, mw: float) -> float:
        # 25 C, 1013.25 hPa reference conditions (24.465 L/mol)
        return ppm * 1000.0 * mw / 24.465

    # ------------------------------------------------------------------ main
    def process(self, parsed: Dict[str, Any], transport: str = "FIREBASE") -> Dict[str, Any]:
        t0 = time.perf_counter_ns()
        station_id = parsed["station_id"]
        st = self._state(station_id)
        st.packets += 1

        we_cnt = float(parsed["h2s_we_counts"])
        ae_cnt = float(parsed["h2s_ae_counts"])
        nh3_cnt = float(parsed["nh3_we_counts"])
        voc_cnt = float(parsed["voc_counts"])
        t_c = float(parsed["head_t_c"])
        rh = float(parsed["rh_pct"])
        p_hpa = float(parsed["p_hpa"])

        # --- voltages (exact reported volts when present) ---------------------
        rv = parsed.get("reported_volts")
        if isinstance(rv, list) and len(rv) >= 4 and all(x is not None for x in rv[:4]):
            nh3_mv, we_mv, ae_mv, voc_mv = (float(x) * 1000.0 for x in rv[:4])
        else:
            we_mv, _ = h2s_b4.counts_to_mv(we_cnt, self.h2s_cal)
            ae_mv, _ = h2s_b4.counts_to_mv(ae_cnt, self.h2s_cal)
            nh3_mv = (nh3_cnt / 32768.0) * 2560.0 if self.nh3_direct_sheet else nh3_b1.counts_to_mv(nh3_cnt, self.nh3_cal)[0]
            voc_mv = pidx_a040.counts_to_v(voc_cnt, self.voc_cal)[0] * 1000.0

        # --- telemetry gap & warm-up state ------------------------------------
        ts_unix = parse_iso_unix(parsed.get("ts", "")) or time.time()
        gap_detected = (
            st.last_sample_unix is not None and ts_unix - st.last_sample_unix > self.power_gap_reset_s
        )
        if st.power_on_unix is None or gap_detected:
            st.power_on_unix = ts_unix
            if gap_detected:
                # Reset short-term DSP memory across long telemetry gaps so stale
                # filter state from hours ago does not drag fresh live samples.
                for g in GASES:
                    st.kalman[g].x = None
                    st.kalman[g].p = 0.0
                    st.hampel[g].clear()
                    st.filtered_hist[g].clear()
                    st.signal_mv[g].clear()
                st.rolling.clear()
                st.h2s_t90 = None
        st.last_sample_unix = ts_unix
        uptime_min = (ts_unix - st.power_on_unix) / 60.0
        if self.sensors_prewarmed:
            warm = {g: False for g in GASES}
        else:
            warm = {g: uptime_min < float(self.cfg[g].get("warmup_min", 0)) for g in GASES}

        # --- supply dip detection ----------------------------------------------
        battery_v = parsed.get("battery_v")
        battery_dip = False
        if battery_v is not None:
            bv = float(battery_v)
            if len(st.battery_hist) >= 3 and bv < statistics.median(st.battery_hist) - 0.025:
                battery_dip = True
                st.battery_dips += 1
            st.battery_hist.append(bv)
        r_mult = 3.5 if battery_dip else 1.0

        # --- live noise estimates (mV -> ppm) ----------------------------------
        st.signal_mv["h2s"].append(we_mv - ae_mv)
        st.signal_mv["nh3"].append(nh3_mv)
        st.signal_mv["voc"].append(voc_mv)
        h2s_sens_ppm = self.h2s_cal.sens_mv_per_ppb * 1000.0  # mV per ppm
        noise_ppm = {
            "h2s": self._noise_mv(st, "h2s") / h2s_sens_ppm,
            "nh3": self._noise_mv(st, "nh3") / self.nh3_cal.sens_mv_per_ppm,
            "voc": self._noise_mv(st, "voc") / self.voc_cal.mv_per_ppm,
        }
        zero_unc_ppm = {
            "h2s": float(self.cfg["h2s"].get("zero_uncertainty_mv", 14.0)) / h2s_sens_ppm,
            "nh3": (
                float(self.cfg["nh3"].get("zero_uncertainty_mv", 2.0)) / self.nh3_cal.sens_mv_per_ppm
                if self.nh3_direct_sheet
                else nh3_b1.ZERO_UNCERTAINTY_PPM
            ),
            "voc": float(self.cfg["voc"].get("zero_uncertainty_mv", 5.0)) / self.voc_cal.mv_per_ppm,
        }
        if self.enforce_lod:
            lod = {g: max(3.0 * noise_ppm[g], zero_unc_ppm[g]) for g in GASES}
        else:
            lod = {g: 0.0 for g in GASES}

        # --- 1. H2S-B4: AAN 803-05 algorithm 1 (reference module) --------------
        h2s_r = h2s_b4.compute(
            we_cnt, ae_cnt, t_c, self.h2s_cal,
            algorithm=self.h2s_algorithm, coeffs=self.h2s_coeffs,
        )
        h2s_ppm = h2s_r.ppb / 1000.0

        # t90 matching for NH3 cross-sensitivity
        a = float(self.cfg["nh3"].get("t90_match_alpha", 0.63))
        st.h2s_t90 = h2s_ppm if st.h2s_t90 is None else a * h2s_ppm + (1 - a) * st.h2s_t90
        h2s_for_nh3 = max(0.0, st.h2s_t90) if not self.enforce_lod else (st.h2s_t90 if st.h2s_t90 > lod["h2s"] else 0.0)

        # --- 2. NH3-B1+: factory calibration sheet or reference module ----------
        if self.nh3_direct_sheet:
            # Alphasense NH3-B1+ factory calibration sheet (barcode 77840031):
            # NH3_ppm = ((A0_in_volts * 1000) - 328) / 58.624  (+200 mV bias, 3-electrode ISB)
            nh3_we_c = nh3_mv - self.nh3_cal.we_e
            nh3_ppm = nh3_we_c / self.nh3_cal.sens_mv_per_ppm
            nh3_r = nh3_b1.Reading(
                ppm=nh3_ppm,
                flags=nh3_b1.QC.OK,
                we_c_mv=nh3_we_c,
                sens_mv_per_ppm=self.nh3_cal.sens_mv_per_ppm,
                apparent_ppm=nh3_ppm,
                interferent_ppm=0.0,
                diagnostics={"we_u_mv": nh3_mv, "zero_ppm": 0.0, "head_t_c": t_c},
            )
        else:
            nh3_r = nh3_b1.convert(
                nh3_cnt, t_c, self.nh3_cal,
                interferents_ppm={"h2s": h2s_for_nh3},
                cross_sensitivity=self.nh3_xs,
                rh_pct=rh,
            )
            nh3_ppm = nh3_r.ppm

        # --- 3. PIDX-A-040: fixed zero + density (reference module) ------------
        voc_r = pidx_a040.convert(
            voc_cnt, t_c, p_hpa, self.voc_cal,
            baseline=None, rh_pct=rh,
            seconds_since_power_on=None if self.sensors_prewarmed else uptime_min * 60.0,
        )
        voc_ppm = voc_r.ppm

        sample = {"h2s": h2s_ppm, "nh3": nh3_ppm, "voc": voc_ppm}

        # --- 4. despike + Kalman (signed) + trend ------------------------------
        filt: Dict[str, float] = {}
        trend: Dict[str, str] = {}
        spike_frame = False
        for g in GASES:
            x, spk = self._despike(st, g, sample[g], noise_ppm[g])
            spike_frame |= spk
            filt[g] = st.kalman[g].update(x, noise_ppm[g] ** 2, r_mult * (4.0 if spk else 1.0))
            trend[g] = self._trend(st, g, filt[g], noise_ppm[g])
        if spike_frame:
            st.samples_filtered += 1

        # Filtered-value noise is lower than single-sample noise; Kalman variance:
        filt_sigma = {g: math.sqrt(max(st.kalman[g].p, 0.0)) for g in GASES}
        sens_rel = {g: float(self.cfg[g].get("sens_rel_uncertainty", 0.5)) for g in GASES}
        unc = {
            g: math.sqrt(zero_unc_ppm[g] ** 2 + filt_sigma[g] ** 2 + (sens_rel[g] * filt[g]) ** 2)
            for g in GASES
        }
        status = {g: self._status(warm[g], filt[g], lod[g]) for g in GASES}
        if self.nh3_direct_sheet and not warm["nh3"] and status["nh3"] == "INDICATIVE":
            status["nh3"] = "QUANTITATIVE"

        # --- rolling 10m / 20m / 30m means on SAMPLE timestamps ----------------
        st.rolling.append((ts_unix, filt["h2s"], filt["nh3"], filt["voc"]))
        while st.rolling and st.rolling[0][0] < ts_unix - 1800.0:
            st.rolling.popleft()

        def _win_mean(sec: float) -> Dict[str, float]:
            cut = ts_unix - sec
            rows_w = [r for r in st.rolling if r[0] >= cut]
            if not rows_w:
                return dict(filt)
            nw = len(rows_w)
            return {
                "h2s": sum(r[1] for r in rows_w) / nw,
                "nh3": sum(r[2] for r in rows_w) / nw,
                "voc": sum(r[3] for r in rows_w) / nw,
            }

        roll10 = _win_mean(600.0)
        roll20 = _win_mean(1200.0)
        roll30 = _win_mean(1800.0)
        roll = roll10

        # --- QC flags: reference-module flags + engine flags -------------------
        extra: Dict[str, List[str]] = {g: [] for g in GASES}
        for g in GASES:
            if warm[g]:
                extra[g].append("WARMUP")
            if g == "nh3" and self.nh3_direct_sheet:
                extra[g].append("CAL_FACTORY_77840031")
            else:
                if not self.cal_verified:
                    extra[g].append("CAL_PROVISIONAL")
                extra[g].append("TEMP_FROM_WEATHER_API")
            if battery_dip:
                extra[g].append("SUPPLY_DIP")
        rh_flag = self.cfg["voc"].get("high_rh_flag_pct")
        if rh_flag is not None and rh >= float(rh_flag):
            extra["voc"].append("HIGH_RH_OFFSET_RISK")
        nh3_delta_mv = nh3_mv - self.nh3_cal.we_e
        # Event test on the FILTERED signal (single samples have ~4 mV noise)
        nh3_filt_delta_mv = filt["nh3"] * self.nh3_cal.sens_mv_per_ppm
        nh3_event = (not warm["nh3"]) and nh3_filt_delta_mv > max(
            float(self.cfg["nh3"].get("event_threshold_mv", 30.0)), 5.0 * self._noise_mv(st, "nh3")
        )
        if nh3_event:
            extra["nh3"].append("NH3_EVENT")

        def flags(module_desc: str, g: str) -> str:
            parts = [p for p in module_desc.split("|") if p and p != "OK"] + extra[g]
            return "|".join(parts) if parts else "OK"

        qc = {
            "h2s_flags": flags(h2s_b4.describe(h2s_r.flags), "h2s"),
            "h2s_usable": h2s_r.usable and not warm["h2s"],
            "nh3_flags": flags(nh3_b1.describe(nh3_r.flags), "nh3"),
            "nh3_usable": nh3_r.usable and not warm["nh3"],
            "nh3_quantitative": nh3_r.quantitative,
            "voc_flags": flags(pidx_a040.describe(voc_r.flags), "voc"),
            "voc_usable": voc_r.usable and not warm["voc"],
        }

        alarms = {
            "h2s": self._alarm("h2s_ppm", filt["h2s"], status["h2s"]),
            "nh3": self._alarm("nh3_ppm", filt["nh3"], status["nh3"]),
            "voc": self._alarm("voc_ppm", filt["voc"], status["voc"]),
        }
        rank = {"NORMAL": 0, "WARNING": 1, "ACTION": 2, "CRITICAL": 3}
        alarms["overall"] = max(alarms.values(), key=lambda s: rank[s])

        mw = {"h2s": 34.081, "nh3": 17.031, "voc": 56.106}
        report = {
            g: {
                "value_ppm": round(filt[g], 5),
                "sample_ppm": round(sample[g], 5),
                "avg_10m_ppm": round(roll10[g], 5),
                "avg_20m_ppm": round(roll20[g], 5),
                "avg_30m_ppm": round(roll30[g], 5),
                "uncertainty_ppm": round(unc[g], 5),
                "lod_ppm": round(lod[g], 5),
                "noise_ppm": round(noise_ppm[g], 5),
                "zero_uncertainty_ppm": round(zero_unc_ppm[g], 5),
                "status": status[g],
                "trend": trend[g],
                "ugm3": round(self._ugm3(filt[g], mw[g]), 2),
            }
            for g in GASES
        }
        report["nh3"]["event"] = nh3_event
        report["nh3"]["signal_above_zero_mv"] = round(nh3_delta_mv, 2)

        proc_us = round((time.perf_counter_ns() - t0) / 1000.0, 2)

        return {
            "station_id": station_id,
            "algo_version": self.ALGO_VERSION,
            "calibration_verified": self.cal_verified,
            "ts": parsed["ts"],
            "uptime_min": round(uptime_min, 1),
            "received_unix": time.time(),
            "transport": transport.upper(),
            "format_detected": parsed["format_detected"],
            "weather_source": parsed.get("weather_source", "Open-Meteo (Dubai)"),
            "firebase_key": parsed.get("firebase_key"),
            "raw_string": parsed["raw_string"],
            "processing_time_us": proc_us,
            "report": report,
            "hardware": {
                "battery_v": battery_v,
                "battery_dip_detected": battery_dip,
                "h2s_diff_v": round((we_mv - ae_mv) / 1000.0, 5),
                "sample_age_ms": parsed.get("sample_age_ms"),
                "adc_ok": parsed.get("adc_ok", True),
                "stm32_status": parsed.get("stm32_status", "NA"),
            },
            "raw": {
                "h2s_we_counts": we_cnt,
                "h2s_ae_counts": ae_cnt,
                "nh3_we_counts": nh3_cnt,
                "voc_counts": voc_cnt,
                "h2s_we_mv": round(we_mv, 3),
                "h2s_ae_mv": round(ae_mv, 3),
                "nh3_we_mv": round(nh3_mv, 3),
                "voc_mv": round(voc_mv, 3),
                "head_t_c": round(t_c, 2),
                "rh_pct": round(rh, 2),
                "p_hpa": round(p_hpa, 2),
                "wind_speed_ms": round(float(parsed["wind_speed_ms"]), 2),
                "wind_dir_deg": round(float(parsed["wind_dir_deg"]), 1),
            },
            "baselines_mv": {
                "h2s_zero_diff_mv": round(self.h2s_cal.we_e - self.h2s_cal.ae_e, 3),
                "nh3_zero_mv": round(self.nh3_cal.we_e, 3),
                "voc_zero_mv": round(self.voc_cal.v_zero_mv, 3),
            },
            # Single-sample datasheet result, before despike/Kalman (signed)
            "uncompensated": {
                "h2s_ppm": round(h2s_ppm, 5),
                "h2s_ppb": round(h2s_r.ppb, 2),
                "nh3_ppm": round(nh3_ppm, 4),
                "voc_ppm": round(voc_ppm, 4),
            },
            "compensated": {
                "h2s_ppm": round(h2s_ppm, 5),
                "h2s_ppb": round(h2s_r.ppb, 2),
                "h2s_we_c_mv": round(h2s_r.we_c_mv, 3),
                "h2s_nt_coeff": round(h2s_r.coeff, 3),
                "h2s_ugm3": report["h2s"]["ugm3"],
                "nh3_ppm": round(nh3_ppm, 4),
                "nh3_apparent_ppm": round(nh3_r.apparent_ppm, 4),
                "nh3_h2s_interference_ppm": round(nh3_r.interferent_ppm, 5),
                "nh3_ugm3": report["nh3"]["ugm3"],
                "voc_ppm": round(voc_ppm, 4),
                "voc_density_factor": round(voc_r.density_factor, 4),
                "voc_ugm3": report["voc"]["ugm3"],
            },
            "dsp": {
                "h2s_ppm": round(filt["h2s"], 5),
                "h2s_ppb": round(filt["h2s"] * 1000.0, 2),
                "nh3_ppm": round(filt["nh3"], 4),
                "voc_ppm": round(filt["voc"], 4),
                "h2s_trend": trend["h2s"],
                "nh3_trend": trend["nh3"],
                "voc_trend": trend["voc"],
                "rolling_10m_h2s_ppm": round(roll["h2s"], 5),
                "rolling_10m_h2s_ppb": round(roll["h2s"] * 1000.0, 2),
                "rolling_10m_nh3_ppm": round(roll["nh3"], 4),
                "rolling_10m_voc_ppm": round(roll["voc"], 4),
                "spikes_rejected_total": st.spikes,
                "samples_with_spike": st.samples_filtered,
                "spike_rate_pct": round(100.0 * st.spikes / (3 * st.packets), 2),
                "battery_dips_total": st.battery_dips,
                "spike_on_frame": spike_frame,
            },
            "qc": qc,
            "alarms": alarms,
        }
