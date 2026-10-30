/* AirWatch Dubai — Standalone Browser Telemetry + Alphasense Physics Engine (v6.1)
 * Runs 100% client-side on GitHub Pages (or any static host) when /api/live is absent.
 * Connects directly to:
 *   1. Firebase RTDB: https://aqms-65300-default-rtdb.firebaseio.com/data.json
 *   2. Open-Meteo Dubai: https://api.open-meteo.com/v1/forecast (hourly + live weather)
 * Implements the exact same Alphasense datasheet conversions (H2S-B4 AAN 803 Alg 1,
 * NH3-B1 3-electrode + H2S t90 cross-sensitivity, PIDX-A-040 ideal-gas T/P density)
 * and Hampel + 1D Kalman DSP pipeline as algo_engine.py.
 */
(() => {
  "use strict";

  const FIREBASE_URL = "https://aqms-65300-default-rtdb.firebaseio.com/data.json";
  const WEATHER_URL =
    "https://api.open-meteo.com/v1/forecast?latitude=25.277&longitude=55.396" +
    "&current=temperature_2m,relative_humidity_2m,surface_pressure,wind_speed_10m,wind_direction_10m" +
    "&hourly=temperature_2m,relative_humidity_2m,surface_pressure,wind_speed_10m,wind_direction_10m" +
    "&past_days=2&forecast_days=1&wind_speed_unit=ms&timezone=UTC";

  const GASES = ["h2s", "nh3", "voc"];
  const STATUS_CODE = { WARMUP: 0, BELOW_LOD: 1, INDICATIVE: 2, QUANTITATIVE: 3 };
  const ALARM_CODE = { NORMAL: 0, WARNING: 1, ACTION: 2, CRITICAL: 3 };

  const CONFIG = {
    algo_version: "v5.1-web (Alphasense datasheets + AAN 803 n_T, live browser engine)",
    calibration_verified: false,
    sensors_prewarmed: true,
    enforce_lod: false,
    power_gap_reset_s: 15 * 60,
    calibration_note:
      "PROVISIONAL. Zeros estimated from clean-air baseline (2026-10-28). Sensitivities are nominal datasheet values. Replace with Alphasense certificate values + carbon-bag zero test when available.",
    h2s: {
      we_e_mv: 589.0,
      ae_e_mv: 302.0,
      sens_mv_per_ppb: 0.73,
      sens_rel_uncertainty: 0.4,
      zero_uncertainty_mv: 12.0,
      noise_mv_default: 4.9,
      n_t: [[-30, -0.6], [-20, -0.6], [-10, 0.1], [0, 0.8], [10, -0.7], [20, -2.5], [30, -2.5], [40, -2.2], [50, -1.8]],
      sens_tc: [[-30, 0.75], [-20, 0.835], [-10, 0.88], [0, 0.92], [10, 0.96], [20, 1.0], [30, 1.03], [40, 1.055], [50, 1.05]],
    },
    nh3: {
      we_e_mv: 405.9,
      sens_mv_per_ppm: 65.0,
      sens_rel_uncertainty: 0.5,
      zero_uncertainty_ppm: 10.0,
      noise_mv_default: 4.0,
      xs_h2s: -2.0,
      t90_match_alpha: 0.63,
      event_threshold_mv: 30.0,
      sens_tc: [[-30, 0.8], [-20, 0.84], [-10, 0.88], [0, 0.92], [10, 0.96], [20, 1.0], [30, 1.04], [40, 1.08], [50, 1.12]],
    },
    voc: {
      v_zero_mv: 95.5,
      mv_per_ppm: 55.0,
      sens_rel_uncertainty: 0.1,
      zero_uncertainty_mv: 5.0,
      noise_mv_default: 1.2,
      t_cal_c: 25.0,
      p_cal_hpa: 1013.25,
      response_factor: 1.0,
    },
    dsp: {
      hampel_window: 9,
      hampel_n_sigma: 3.5,
      kalman_q_over_r: 0.05,
      noise_window: 60,
      trend_window: 10,
      rolling_window_sec: 600,
    },
    thresholds: {
      h2s_ppm: { warning: 0.5, action: 1.5, critical: 5.0 },
      nh3_ppm: { warning: 5.0, action: 15.0, critical: 35.0 },
      voc_ppm: { warning: 2.0, action: 5.0, critical: 15.0 },
    },
  };

  // ------------------------------------------------------------------ math helpers
  function median(arr) {
    if (!arr.length) return 0;
    const s = [...arr].sort((a, b) => a - b);
    const m = s.length >> 1;
    return s.length % 2 ? s[m] : 0.5 * (s[m - 1] + s[m]);
  }

  function mean(arr) {
    if (!arr.length) return 0;
    let sum = 0;
    for (let i = 0; i < arr.length; i++) sum += arr[i];
    return sum / arr.length;
  }

  function interp(pairs, t) {
    if (t <= pairs[0][0]) return pairs[0][1];
    const last = pairs[pairs.length - 1];
    if (t >= last[0]) return last[1];
    for (let i = 1; i < pairs.length; i++) {
      const [x1, y1] = pairs[i];
      if (t <= x1) {
        const [x0, y0] = pairs[i - 1];
        if (x1 === x0) return y0;
        return y0 + ((t - x0) / (x1 - x0)) * (y1 - y0);
      }
    }
    return last[1];
  }

  function robustNoise(values) {
    if (values.length < 20) return null;
    const diffs = [];
    for (let i = 1; i < values.length; i++) diffs.push(values[i] - values[i - 1]);
    const med = median(diffs);
    const mad = median(diffs.map((d) => Math.abs(d - med)));
    return (1.4826 * mad) / Math.SQRT2;
  }

  function parseIsoUnix(tsStr) {
    if (!tsStr) return null;
    const ms = Date.parse(tsStr);
    return Number.isFinite(ms) ? ms / 1000 : null;
  }

  function countsToMv(counts) {
    return (counts / 32767.0) * 2560.0;
  }

  // ------------------------------------------------------------------ parser (v3 + v4)
  const RE_AX = /A([0-3])\s*:\s*raw\s*=\s*(-?\d+(?:\.\d+)?)(?:\s*\|\s*volts\s*=\s*(-?\d+(?:\.\d+)?))?/gi;
  const RE_DT = /^(\d{4}-\d{2}-\d{2})\s*,\s*(\d{2}:\d{2}:\d{2})/;

  function parseRecord(key, rec, weatherByHour, fallbackWeather) {
    const rawStr = String(rec.combinedString || "").trim();
    let ts = rec.timestampUtc || rec.ntpUtc || null;
    const dtMatch = RE_DT.exec(rawStr);
    if (!ts && dtMatch) ts = `${dtMatch[1]}T${dtMatch[2]}Z`;
    if (!ts) ts = new Date().toISOString();

    const hourKey = ts.slice(0, 13);
    const w = (weatherByHour && weatherByHour[hourKey])
      ? { ...weatherByHour[hourKey], source: `Open-Meteo hourly ${hourKey}:00 UTC` }
      : fallbackWeather;

    let counts = [0, 0, 0, 0];
    let volts = Array.isArray(rec.volts) && rec.volts.length >= 4 ? rec.volts.slice(0, 4).map(Number) : null;

    if (Array.isArray(rec.raw) && rec.raw.length >= 4 && rec.raw.slice(0, 4).every((x) => x != null)) {
      counts = rec.raw.slice(0, 4).map(Number);
    } else {
      RE_AX.lastIndex = 0;
      let m;
      const vMap = {};
      let found = 0;
      while ((m = RE_AX.exec(rawStr)) !== null) {
        const idx = Number(m[1]);
        counts[idx] = Number(m[2]);
        if (m[3] != null) vMap[idx] = Number(m[3]);
        found++;
      }
      if (found >= 2) {
        if (!volts && Object.keys(vMap).length === 4) volts = [vMap[0], vMap[1], vMap[2], vMap[3]];
      } else if (dtMatch) {
        const parts = rawStr.split(",").map((s) => s.trim());
        if (parts.length >= 6 && parts.slice(2, 6).every((p) => p !== "" && Number.isFinite(Number(p)))) {
          const vals = parts.slice(2, 6).map(Number);
          const isVolts = parts.slice(2, 6).some((p) => p.includes(".")) && vals.every((v) => Math.abs(v) <= 5.5);
          if (isVolts) {
            if (!volts) volts = vals;
            counts = vals.map((v) => Math.round((v * 32768.0) / 2.56));
          } else {
            counts = vals;
          }
        }
      }
    }

    let stm32Status = "NA";
    if (rec.stm32 && typeof rec.stm32 === "object") {
      stm32Status = rec.stm32.available ? "OK" : "NA";
    } else if (rawStr.includes("STM32:")) {
      stm32Status = rawStr.split("STM32:")[1].split(",")[0].trim();
    }

    return {
      firebase_key: key,
      raw_string: rawStr || JSON.stringify(rec.raw || []),
      station_id: rec.device || "aqms-monitor",
      ts,
      nh3_we_counts: counts[0],
      h2s_we_counts: counts[1],
      h2s_ae_counts: counts[2],
      voc_counts: counts[3],
      reported_volts: volts,
      battery_v: rec.batteryV != null ? Number(rec.batteryV) : null,
      sample_age_ms: rec.sampleAgeMs != null ? Number(rec.sampleAgeMs) : null,
      adc_ok: rec.adcOk !== false,
      stm32_status: stm32Status,
      head_t_c: Number(w.temp_c ?? 28.0),
      rh_pct: Number(w.rh_pct ?? 60.0),
      p_hpa: Number(w.press_hpa ?? 1013.25),
      wind_speed_ms: Number(w.wind_speed_ms ?? 2.0),
      wind_dir_deg: Number(w.wind_dir_deg ?? 0.0),
      weather_source: w.source || "Open-Meteo (Dubai)",
    };
  }

  // ------------------------------------------------------------------ engine state
  class Kalman1D {
    constructor(qOverR) {
      this.qOverR = qOverR;
      this.x = null;
      this.p = 0;
    }
    reset() {
      this.x = null;
      this.p = 0;
    }
    update(z, r, rMult = 1.0) {
      const rEff = Math.max(1e-18, r * rMult);
      if (this.x === null) {
        this.x = z;
        this.p = rEff;
        return z;
      }
      const pPred = this.p + this.qOverR * r;
      const k = pPred / (pPred + rEff);
      this.x = this.x + k * (z - this.x);
      this.p = (1 - k) * pPred;
      return this.x;
    }
  }

  function pushBounded(arr, val, maxLen) {
    arr.push(val);
    if (arr.length > maxLen) arr.shift();
  }

  class BrowserAQMSHub {
    constructor() {
      this.resetEngine();
      this.lastKey = null;
      this.weather = {
        temp_c: 28.0, rh_pct: 55.0, press_hpa: 1013.25,
        wind_speed_ms: 2.0, wind_dir_deg: 90.0,
        fetched_at: "", source: "Open-Meteo (Dubai)", by_hour: {},
      };
      this.weatherFetchedUnix = 0;
      this.firebaseConnected = false;
      this.firebaseLastCheckUnix = null;
      this.firebaseLatencyMs = 0;
      this.firebaseBytesLast = 0;
      this.firebaseError = null;
      this._initPromise = null;
    }

    resetEngine() {
      const d = CONFIG.dsp;
      this.hampel = { h2s: [], nh3: [], voc: [] };
      this.kalman = {
        h2s: new Kalman1D(d.kalman_q_over_r),
        nh3: new Kalman1D(d.kalman_q_over_r),
        voc: new Kalman1D(d.kalman_q_over_r),
      };
      this.filteredHist = { h2s: [], nh3: [], voc: [] };
      this.signalMv = { h2s: [], nh3: [], voc: [] };
      this.rollingBuf = []; // [{ ts, h2s, nh3, voc }] up to 30 min (1800 s)
      this.batteryHist = [];
      this.powerOnUnix = null;
      this.lastSampleUnix = null;
      this.h2sT90 = null;
      this.packets = 0;
      this.spikes = 0;
      this.batteryDips = 0;
      this.samplesFiltered = 0;
      this.history = [];
    }

    _noiseMv(gas) {
      const live = robustNoise(this.signalMv[gas]);
      return live !== null && live > 0 ? live : CONFIG[gas].noise_mv_default;
    }

    _despike(gas, x, noisePpm) {
      const buf = this.hampel[gas];
      let spike = false, out = x;
      if (buf.length >= 5) {
        const med = median(buf);
        const madSigma = 1.4826 * median(buf.map((b) => Math.abs(b - med)));
        const sigma = Math.max(madSigma, noisePpm);
        if (Math.abs(x - med) > CONFIG.dsp.hampel_n_sigma * sigma) {
          out = med;
          spike = true;
          this.spikes++;
        }
      }
      pushBounded(buf, x, CONFIG.dsp.hampel_window);
      return [out, spike];
    }

    _trend(gas, filtered, noisePpm) {
      const h = this.filteredHist[gas];
      const w = CONFIG.dsp.trend_window;
      pushBounded(h, filtered, 2 * w);
      if (h.length < 2 * w) return "STABLE";
      const delta = mean(h.slice(w)) - mean(h.slice(0, w));
      const thresh = 3.0 * noisePpm * Math.sqrt(2.0 / w);
      if (delta > thresh) return "RISING";
      if (delta < -thresh) return "FALLING";
      return "STABLE";
    }

    _alarm(key, val) {
      const th = CONFIG.thresholds[key] || {};
      if (th.critical != null && val >= th.critical) return "CRITICAL";
      if (th.action != null && val >= th.action) return "ACTION";
      if (th.warning != null && val >= th.warning) return "WARNING";
      return "NORMAL";
    }

    process(p) {
      this.packets++;
      const tC = p.head_t_c, rh = p.rh_pct, pHpa = p.p_hpa;

      const rv = p.reported_volts;
      let nh3Mv, weMv, aeMv, vocMv;
      if (Array.isArray(rv) && rv.length >= 4 && rv.every((x) => Number.isFinite(x))) {
        [nh3Mv, weMv, aeMv, vocMv] = rv.map((v) => v * 1000.0);
      } else {
        nh3Mv = countsToMv(p.nh3_we_counts);
        weMv = countsToMv(p.h2s_we_counts);
        aeMv = countsToMv(p.h2s_ae_counts);
        vocMv = countsToMv(p.voc_counts);
      }

      const tsUnix = parseIsoUnix(p.ts) || Date.now() / 1000;
      const gap = this.lastSampleUnix !== null && tsUnix - this.lastSampleUnix > CONFIG.power_gap_reset_s;
      if (this.powerOnUnix === null || gap) {
        this.powerOnUnix = tsUnix;
        if (gap) {
          for (const g of GASES) {
            this.kalman[g].reset();
            this.hampel[g] = [];
            this.filteredHist[g] = [];
            this.signalMv[g] = [];
          }
          this.rollingBuf = [];
          this.h2sT90 = null;
        }
      }
      this.lastSampleUnix = tsUnix;
      const uptimeMin = (tsUnix - this.powerOnUnix) / 60.0;

      let batteryDip = false;
      if (p.battery_v !== null && Number.isFinite(p.battery_v)) {
        if (this.batteryHist.length >= 3 && p.battery_v < median(this.batteryHist) - 0.025) {
          batteryDip = true;
          this.batteryDips++;
        }
        pushBounded(this.batteryHist, p.battery_v, 30);
      }
      const rMult = batteryDip ? 3.5 : 1.0;

      pushBounded(this.signalMv.h2s, weMv - aeMv, CONFIG.dsp.noise_window);
      pushBounded(this.signalMv.nh3, nh3Mv, CONFIG.dsp.noise_window);
      pushBounded(this.signalMv.voc, vocMv, CONFIG.dsp.noise_window);

      const h2sSensPpm = CONFIG.h2s.sens_mv_per_ppb * 1000.0;
      const noisePpm = {
        h2s: this._noiseMv("h2s") / h2sSensPpm,
        nh3: this._noiseMv("nh3") / CONFIG.nh3.sens_mv_per_ppm,
        voc: this._noiseMv("voc") / CONFIG.voc.mv_per_ppm,
      };
      const zeroUncPpm = {
        h2s: CONFIG.h2s.zero_uncertainty_mv / h2sSensPpm,
        nh3: CONFIG.nh3.zero_uncertainty_ppm,
        voc: CONFIG.voc.zero_uncertainty_mv / CONFIG.voc.mv_per_ppm,
      };

      // 1. H2S-B4 (AAN 803 Algorithm 1, counts_to_mv matching gas_algo/h2s_b4.py)
      const weU = countsToMv(p.h2s_we_counts);
      const aeU = countsToMv(p.h2s_ae_counts);
      const nT = interp(CONFIG.h2s.n_t, tC);
      const weC = (weU - CONFIG.h2s.we_e_mv) - nT * (aeU - CONFIG.h2s.ae_e_mv);
      const h2sSens = CONFIG.h2s.sens_mv_per_ppb * interp(CONFIG.h2s.sens_tc, tC);
      const h2sPpb = weC / h2sSens;
      const h2sPpm = h2sPpb / 1000.0;

      const alpha = CONFIG.nh3.t90_match_alpha;
      this.h2sT90 = this.h2sT90 === null ? h2sPpm : alpha * h2sPpm + (1 - alpha) * this.h2sT90;
      const h2sForNh3 = Math.max(0.0, this.h2sT90);

      // 2. NH3-B1 (3-electrode + H2S cross-sensitivity matching gas_algo/nh3_b1.py)
      const nh3U = countsToMv(p.nh3_we_counts);
      const nh3Sens = CONFIG.nh3.sens_mv_per_ppm * interp(CONFIG.nh3.sens_tc, tC);
      const nh3Apparent = (nh3U - CONFIG.nh3.we_e_mv) / nh3Sens;
      const nh3Corr = CONFIG.nh3.xs_h2s * h2sForNh3;
      const nh3Ppm = nh3Apparent - nh3Corr;

      // 3. PIDX-A-040 (clean-air zero + ideal-gas T/P density matching gas_algo/pidx_a040.py)
      const vocUMv = countsToMv(p.voc_counts);
      const density = ((tC + 273.15) / (CONFIG.voc.t_cal_c + 273.15)) * (CONFIG.voc.p_cal_hpa / pHpa);
      const vocPpm = ((vocUMv - CONFIG.voc.v_zero_mv) / CONFIG.voc.mv_per_ppm) * density * CONFIG.voc.response_factor;

      const sample = { h2s: h2sPpm, nh3: nh3Ppm, voc: vocPpm };
      const filt = {}, trend = {};
      let spikeFrame = false;
      for (const g of GASES) {
        const [x, spk] = this._despike(g, sample[g], noisePpm[g]);
        if (spk) spikeFrame = true;
        filt[g] = this.kalman[g].update(x, noisePpm[g] ** 2, rMult * (spk ? 4.0 : 1.0));
        trend[g] = this._trend(g, filt[g], noisePpm[g]);
      }
      if (spikeFrame) this.samplesFiltered++;

      // Timestamp-windowed rolling averages: 10 min (600 s), 20 min (1200 s), 30 min (1800 s)
      this.rollingBuf.push({ ts: tsUnix, h2s: filt.h2s, nh3: filt.nh3, voc: filt.voc });
      while (this.rollingBuf.length && this.rollingBuf[0].ts < tsUnix - 1800) {
        this.rollingBuf.shift();
      }
      const winMean = (sec, gas) => {
        const cut = tsUnix - sec;
        let s = 0, c = 0;
        for (let i = this.rollingBuf.length - 1; i >= 0; i--) {
          if (this.rollingBuf[i].ts < cut) break;
          s += this.rollingBuf[i][gas];
          c++;
        }
        return c > 0 ? s / c : filt[gas];
      };

      const mw = { h2s: 34.081, nh3: 17.031, voc: 56.106 };
      const report = {};
      for (const g of GASES) {
        const filtSigma = Math.sqrt(Math.max(this.kalman[g].p, 0));
        const sRel = CONFIG[g].sens_rel_uncertainty;
        const unc = Math.sqrt(zeroUncPpm[g] ** 2 + filtSigma ** 2 + (sRel * filt[g]) ** 2);
        const r10 = winMean(600, g), r20 = winMean(1200, g), r30 = winMean(1800, g);
        report[g] = {
          value_ppm: Number(filt[g].toFixed(5)),
          sample_ppm: Number(sample[g].toFixed(5)),
          avg_10m_ppm: Number(r10.toFixed(5)),
          avg_20m_ppm: Number(r20.toFixed(5)),
          avg_30m_ppm: Number(r30.toFixed(5)),
          uncertainty_ppm: Number(unc.toFixed(5)),
          lod_ppm: 0,
          noise_ppm: Number(noisePpm[g].toFixed(5)),
          zero_uncertainty_ppm: Number(zeroUncPpm[g].toFixed(5)),
          status: "INDICATIVE",
          trend: trend[g],
          ugm3: Number(((filt[g] * 1000.0 * mw[g]) / 24.465).toFixed(2)),
        };
      }
      const nh3FiltDeltaMv = filt.nh3 * CONFIG.nh3.sens_mv_per_ppm;
      const nh3Event = nh3FiltDeltaMv > Math.max(CONFIG.nh3.event_threshold_mv, 5.0 * this._noiseMv("nh3"));
      report.nh3.event = nh3Event;
      report.nh3.signal_above_zero_mv = Number((nh3Mv - CONFIG.nh3.we_e_mv).toFixed(2));

      const alarms = {
        h2s: this._alarm("h2s_ppm", filt.h2s),
        nh3: this._alarm("nh3_ppm", filt.nh3),
        voc: this._alarm("voc_ppm", filt.voc),
      };
      alarms.overall = ["h2s", "nh3", "voc"].reduce(
        (best, g) => (ALARM_CODE[alarms[g]] > ALARM_CODE[best] ? alarms[g] : best),
        "NORMAL"
      );

      const baseFlags = "CAL_PROVISIONAL|TEMP_FROM_WEATHER_API" + (batteryDip ? "|SUPPLY_DIP" : "");
      const qc = {
        h2s_flags: "COEFF_UNVERIFIED|" + baseFlags,
        nh3_flags: "TC_UNCHARACTERISED|" + baseFlags + (nh3Event ? "|NH3_EVENT" : ""),
        voc_flags: "BASELINE_UNTRACKED|" + baseFlags,
      };

      const recOut = {
        ts: p.ts,
        ts_unix: tsUnix,
        firebase_key: p.firebase_key,
        station_id: p.station_id,
        uptime_min: Number(uptimeMin.toFixed(1)),
        calibration_verified: false,
        algo_version: CONFIG.algo_version,
        raw_string: p.raw_string,
        weather_source: p.weather_source,
        report,
        alarms,
        qc,
        hardware: {
          battery_v: p.battery_v,
          battery_dip_detected: batteryDip,
          h2s_diff_v: Number(((weMv - aeMv) / 1000.0).toFixed(5)),
          sample_age_ms: p.sample_age_ms,
          adc_ok: p.adc_ok,
          stm32_status: p.stm32_status,
        },
        raw: {
          nh3_we_mv: Number(nh3Mv.toFixed(3)),
          h2s_we_mv: Number(weMv.toFixed(3)),
          h2s_ae_mv: Number(aeMv.toFixed(3)),
          voc_mv: Number(vocMv.toFixed(3)),
          head_t_c: Number(tC.toFixed(2)),
          rh_pct: Number(rh.toFixed(2)),
          p_hpa: Number(pHpa.toFixed(2)),
          wind_speed_ms: Number(p.wind_speed_ms.toFixed(2)),
          wind_dir_deg: Number(p.wind_dir_deg.toFixed(1)),
        },
        dsp: {
          spike_rate_pct: Number(((100.0 * this.spikes) / (3 * this.packets)).toFixed(2)),
          battery_dips_total: this.batteryDips,
          spikes_rejected_total: this.spikes,
        },
      };
      pushBounded(this.history, recOut, 1440);
      return recOut;
    }

    async _ensureWeather() {
      const now = Date.now() / 1000;
      if (now - this.weatherFetchedUnix < 600 && Object.keys(this.weather.by_hour).length) return;
      try {
        const r = await fetch(WEATHER_URL, { cache: "no-store" });
        if (!r.ok) return;
        const d = await r.json();
        const cur = d.current || {}, h = d.hourly || {};
        const byHour = {};
        const times = h.time || [];
        for (let i = 0; i < times.length; i++) {
          const t = h.temperature_2m?.[i], rh = h.relative_humidity_2m?.[i],
                pr = h.surface_pressure?.[i], ws = h.wind_speed_10m?.[i], wd = h.wind_direction_10m?.[i];
          if ([t, rh, pr, ws, wd].every((x) => x != null)) {
            byHour[times[i].slice(0, 13)] = { temp_c: t, rh_pct: rh, press_hpa: pr, wind_speed_ms: ws, wind_dir_deg: wd };
          }
        }
        this.weather = {
          temp_c: Number(cur.temperature_2m ?? 28.0),
          rh_pct: Number(cur.relative_humidity_2m ?? 55.0),
          press_hpa: Number(cur.surface_pressure ?? 1013.25),
          wind_speed_ms: Number(cur.wind_speed_10m ?? 2.0),
          wind_dir_deg: Number(cur.wind_direction_10m ?? 90.0),
          fetched_at: cur.time || "",
          source: "Open-Meteo (Dubai)",
          by_hour: byHour,
        };
        this.weatherFetchedUnix = now;
      } catch (e) {
        console.warn("Open-Meteo fetch warning:", e);
      }
    }

    async pollFirebase() {
      await this._ensureWeather();
      const q = this.lastKey === null
        ? `orderBy=%22%24key%22&limitToLast=1440`
        : `orderBy=%22%24key%22&startAt=${encodeURIComponent(JSON.stringify(this.lastKey))}`;
      const t0 = performance.now();
      try {
        const r = await fetch(`${FIREBASE_URL}?${q}`, { cache: "no-store" });
        if (!r.ok) throw new Error(`Firebase HTTP ${r.status}`);
        const text = await r.text();
        this.firebaseLatencyMs = Math.round(performance.now() - t0);
        this.firebaseBytesLast = text.length;
        this.firebaseConnected = true;
        this.firebaseError = null;
        this.firebaseLastCheckUnix = Date.now() / 1000;
        const records = text ? JSON.parse(text) : {};
        if (records && typeof records === "object") {
          for (const key of Object.keys(records).sort()) {
            if (this.lastKey !== null && key <= this.lastKey) continue;
            const rec = records[key];
            if (rec && typeof rec === "object" && (rec.combinedString || rec.raw)) {
              const parsed = parseRecord(key, rec, this.weather.by_hour, this.weather);
              this.process(parsed);
            }
            this.lastKey = key;
          }
        }
      } catch (e) {
        this.firebaseConnected = false;
        this.firebaseError = String(e.message || e);
        this.firebaseLastCheckUnix = Date.now() / 1000;
        throw e;
      }
    }

    async getLive() {
      await this.pollFirebase();
      const r = this.history.length ? this.history[this.history.length - 1] : null;
      const { by_hour, ...wClean } = this.weather;
      return {
        server_unix: Date.now() / 1000,
        stats: {
          algo_version: CONFIG.algo_version,
          total_received: this.packets,
          dubai_weather: wClean,
          firebase: {
            connected: this.firebaseConnected,
            url: FIREBASE_URL,
            last_check_unix: this.firebaseLastCheckUnix,
            latency_ms: this.firebaseLatencyMs,
            bytes_last_poll: this.firebaseBytesLast,
            error: this.firebaseError,
          },
        },
        thresholds: CONFIG.thresholds,
        latest: r
          ? {
              ts: r.ts,
              ts_unix: r.ts_unix,
              station_id: r.station_id,
              uptime_min: r.uptime_min,
              calibration_verified: false,
              gases: {
                h2s: { ...r.report.h2s, alarm: r.alarms.h2s, flags: r.qc.h2s_flags },
                nh3: { ...r.report.nh3, alarm: r.alarms.nh3, flags: r.qc.nh3_flags },
                voc: { ...r.report.voc, alarm: r.alarms.voc, flags: r.qc.voc_flags },
              },
              overall_alarm: r.alarms.overall,
              weather: {
                temp_c: r.raw.head_t_c,
                rh_pct: r.raw.rh_pct,
                p_hpa: r.raw.p_hpa,
                wind_speed_ms: r.raw.wind_speed_ms,
                wind_dir_deg: r.raw.wind_dir_deg,
                source: r.weather_source,
              },
              hardware: r.hardware,
              dsp: r.dsp,
            }
          : null,
      };
    }

    getHistory(hours = 24) {
      let rows = this.history;
      if (rows.length) {
        const cutoff = rows[rows.length - 1].ts_unix - hours * 3600;
        rows = rows.filter((r) => r.ts_unix >= cutoff);
      }
      const out = { t: rows.map((r) => r.ts_unix) };
      for (const g of GASES) {
        out[g] = {
          v: rows.map((r) => r.report[g].value_ppm),
          s: rows.map((r) => r.report[g].sample_ppm),
          r10: rows.map((r) => r.report[g].avg_10m_ppm),
          r20: rows.map((r) => r.report[g].avg_20m_ppm),
          r30: rows.map((r) => r.report[g].avg_30m_ppm),
          lod: rows.map(() => 0),
          st: rows.map((r) => STATUS_CODE[r.report[g].status] ?? 2),
          al: rows.map((r) => ALARM_CODE[r.alarms[g]] ?? 0),
        };
      }
      out.nh3.ev = rows.map((r) => (r.report.nh3.event ? 1 : 0));
      out.temp = rows.map((r) => r.raw.head_t_c);
      out.rh = rows.map((r) => r.raw.rh_pct);
      out.batt = rows.map((r) => r.hardware.battery_v);
      return out;
    }

    getDetail(n = 15) {
      const slice = this.history.slice(-n);
      const latest = slice.length ? slice[slice.length - 1] : null;
      return {
        latest,
        recent: [...slice].reverse().map((r) => ({
          ts: r.ts,
          a0: r.raw.nh3_we_mv,
          a1: r.raw.h2s_we_mv,
          a2: r.raw.h2s_ae_mv,
          a3: r.raw.voc_mv,
          batt: r.hardware.battery_v,
          h2s: r.report.h2s,
          nh3: r.report.nh3,
          voc: r.report.voc,
        })),
        calibration_note: CONFIG.calibration_note,
      };
    }

    buildCsvBlob() {
      const header = [
        "ts", "firebase_key", "station_id",
        "a0_nh3_mv", "a1_h2s_we_mv", "a2_h2s_ae_mv", "a3_voc_mv",
        "h2s_ppm", "h2s_10m_ppm", "h2s_20m_ppm", "h2s_30m_ppm",
        "nh3_ppm", "nh3_10m_ppm", "nh3_20m_ppm", "nh3_30m_ppm",
        "voc_ppm", "voc_10m_ppm", "voc_20m_ppm", "voc_30m_ppm",
        "battery_v", "temp_c", "rh_pct", "raw_string",
      ];
      const escCsv = (v) => {
        const s = String(v ?? "");
        return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
      };
      const lines = [header.join(",")];
      for (const r of this.history) {
        lines.push([
          r.ts, r.firebase_key, r.station_id,
          r.raw.nh3_we_mv, r.raw.h2s_we_mv, r.raw.h2s_ae_mv, r.raw.voc_mv,
          r.report.h2s.value_ppm, r.report.h2s.avg_10m_ppm, r.report.h2s.avg_20m_ppm, r.report.h2s.avg_30m_ppm,
          r.report.nh3.value_ppm, r.report.nh3.avg_10m_ppm, r.report.nh3.avg_20m_ppm, r.report.nh3.avg_30m_ppm,
          r.report.voc.value_ppm, r.report.voc.avg_10m_ppm, r.report.voc.avg_20m_ppm, r.report.voc.avg_30m_ppm,
          r.hardware.battery_v ?? "", r.raw.head_t_c, r.raw.rh_pct, r.raw_string,
        ].map(escCsv).join(","));
      }
      return new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
    }
  }

  window.AQMSStandalone = new BrowserAQMSHub();
})();
