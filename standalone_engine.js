/* AirWatch Dubai — Standalone Browser Telemetry Engine (v7.4, Alphasense method only)
 * Runs 100% client-side on GitHub Pages (or any static host) when /api/live is absent.
 * Connects directly to:
 *   1. Firebase RTDB: https://aqms-65300-default-rtdb.firebaseio.com/data.json
 *   2. Open-Meteo Dubai (weather panel only; not used in any formula)
 *
 * Each gas value uses a zero and a sensitivity, by Alphasense's method only. The sensitivity
 * is from the sensor's factory sheet (datasheet typical for VOC). The H2S and NH3 zeros are
 * SITE ZEROS: the station's own output in clean air, from the cleanest part of the day
 * (02:00-05:00 Dubai, five nights), as AAN 803-05 "Initial calibration" and Alphasense advise:
 *   H2S (ppb) = ((A1 - 458) - (A2 - 359)) / 1.727952   (factory-sheet zeros 395 / 362 kept for history)
 *   NH3 (ppm) = (A0 - 420) / 58.624                     (factory sheet 77840031 zero: 328)
 *   VOC (ppm) = (A3 - 70) / 55                          (datasheet typical, AAN 302-04)
 * A channel stuck at the ADC rail (>= 2550 mV or <= 5 mV) is a SENSOR_FAULT: no number.
 * No spike filter, no smoothing, no noise models, no temperature tables.
 * A negative result counts as 0, and the 10/20/30-minute averages are made from those values.
 * 10 / 20 / 30-minute values are plain averages; alarm levels use the 10-minute average.
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
  const STATUS_CODE = { FACTORY_CAL: 3, NO_CALIBRATION: 4, DATASHEET_TYPICAL: 5, SITE_ZERO: 6, SENSOR_FAULT: 7 };
  // A channel this close to either end of the 0-2560 mV ADC range is stuck (unplugged, no power, shorted).
  const RAIL_HIGH_MV = 2550, RAIL_LOW_MV = 5;
  const atRail = (mv) => mv >= RAIL_HIGH_MV || mv <= RAIL_LOW_MV;
  const ALARM_CODE = { NORMAL: 0, WARNING: 1, ACTION: 2, CRITICAL: 3 };

  // Calibration values, Alphasense method only.
  //   source "site_zero" = zero measured on this station in clean air (AAN 803-05 "Initial calibration"),
  //                        sensitivity from the sensor's factory sheet.
  //   source "factory"   = the sensor's own Alphasense factory calibration sheet.
  //   source "datasheet" = Alphasense's published typical values, used until the factory sheet is entered.
  const FACTORY = {
    // Site zero: median 02:00-05:00 Dubai, 29 Oct - 3 Nov 2026. Factory sheet: WE 395, AE 362 mV; 1.727952 mV/ppb
    h2s: { sensor: "H2S-B4", source: "site_zero", label: "Site zero, H2S-B4 factory-sheet sensitivity",
           we_zero_mv: 458, ae_zero_mv: 359, sensitivity: 1.727952 },             // mV/ppb
    // Site zero as above. Factory sheet 77840031: zero 328 mV; 73.28 nA/ppm x 0.8 mV/nA = 58.624 mV/ppm
    nh3: { sensor: "NH3-B1+", source: "site_zero", serial: "77840031", label: "Site zero, factory sheet 77840031 sensitivity",
           we_zero_mv: 420, sensitivity: 58.624 },                                // mV/ppm
    // PIDX-A-040 datasheet: offset 40-100 mV (midpoint 70 mV used), typical 55 mV/ppm, output from 40 mV
    voc: { sensor: "PIDX-A-040", source: "datasheet", label: "Alphasense PIDX-A-040 datasheet typical",
           we_zero_mv: 70, sensitivity: 55, fault_below_mv: 40 },                // mV/ppm
  };

  const CONFIG = {
    algo_version: "v7.4-web (H2S + NH3 site zero with factory sensitivity, VOC datasheet typical, stuck-channel fault, negatives as 0)",
    power_gap_reset_s: 15 * 60,
    calibration_note:
      "Alphasense method only. H₂S-B4 and NH₃-B1+ use a site zero (the station's clean-air output, 02:00–05:00 " +
      "Dubai, as Alphasense advise) with their factory-sheet sensitivity: H₂S ((A1 − 458) − (A2 − 359)) / 1.727952 ppb, " +
      "NH₃ (A0 − 420) / 58.624 ppm. PIDX-A-040 uses datasheet typical values: (A3 − 70) / 55 ppm, indicative. " +
      "A channel stuck at the ADC limit is shown as a sensor fault. A negative result counts as 0. " +
      "No filtering, smoothing or temperature tables are applied.",
    thresholds: {
      h2s_ppm: { warning: 0.5, action: 1.5, critical: 5.0 },
      nh3_ppm: { warning: 5.0, action: 15.0, critical: 35.0 },
      voc_ppm: { warning: 2.0, action: 5.0, critical: 15.0 },
    },
  };

  // ------------------------------------------------------------------ factory formulas
  const countsToMv = (counts) => (counts * 2560.0) / 32768.0;
  const h2sPpb = (we, weZero, ae, aeZero, sens) => ((we - weZero) - (ae - aeZero)) / sens;
  const nh3Ppm = (we, weZero, sens) => (we - weZero) / sens;
  const vocPpm = (v, vZero, sens) => (v - vZero) / sens;

  function factory(gas) {
    const f = FACTORY[gas];
    const need = gas === "h2s" ? ["we_zero_mv", "ae_zero_mv", "sensitivity"] : ["we_zero_mv", "sensitivity"];
    return need.every((k) => Number.isFinite(f[k])) ? f : null;
  }

  function calibrationLabel(gas) {
    const f = factory(gas);
    if (!f) return "No calibration values entered";
    const sheet = f.label;
    if (gas === "h2s") return `${sheet}: ((A1 − ${f.we_zero_mv}) − (A2 − ${f.ae_zero_mv})) / ${f.sensitivity} ppb`;
    return `${sheet}: (${gas === "nh3" ? "A0" : "A3"} mV − ${f.we_zero_mv}) / ${f.sensitivity}`;
  }

  // ------------------------------------------------------------------ helpers
  function parseIsoUnix(tsStr) {
    if (!tsStr) return null;
    const ms = Date.parse(tsStr);
    return Number.isFinite(ms) ? ms / 1000 : null;
  }
  const round = (x, n = 5) => (x == null ? null : Number(x.toFixed(n)));

  function pushBounded(arr, val, maxLen) {
    arr.push(val);
    if (arr.length > maxLen) arr.shift();
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
      // Weather: shown on the dashboard only, never used in a formula.
      head_t_c: Number(w.temp_c ?? 28.0),
      rh_pct: Number(w.rh_pct ?? 60.0),
      p_hpa: Number(w.press_hpa ?? 1013.25),
      wind_speed_ms: Number(w.wind_speed_ms ?? 2.0),
      wind_dir_deg: Number(w.wind_dir_deg ?? 0.0),
      weather_source: w.source || "Open-Meteo (Dubai)",
    };
  }

  // ------------------------------------------------------------------ engine
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
    }

    resetEngine() {
      this.rollingBuf = []; // [{ ts, h2s, nh3, voc }] up to 30 min (1800 s)
      this.powerOnUnix = null;
      this.lastSampleUnix = null;
      this.packets = 0;
      this.history = [];
    }

    _alarm(key, val) {
      if (val == null) return "NORMAL";
      const th = CONFIG.thresholds[key] || {};
      if (th.critical != null && val >= th.critical) return "CRITICAL";
      if (th.action != null && val >= th.action) return "ACTION";
      if (th.warning != null && val >= th.warning) return "WARNING";
      return "NORMAL";
    }

    process(p) {
      this.packets++;

      // Voltages: exact reported volts when present, else ADC counts.
      const rv = p.reported_volts;
      let nh3Mv, weMv, aeMv, vocMv;
      if (Array.isArray(rv) && rv.length >= 4 && rv.every((x) => Number.isFinite(x))) {
        [nh3Mv, weMv, aeMv, vocMv] = rv.map((v) => v * 1000.0);
      } else {
        [nh3Mv, weMv, aeMv, vocMv] = [p.nh3_we_counts, p.h2s_we_counts, p.h2s_ae_counts, p.voc_counts].map(countsToMv);
      }

      // Uptime (display only).
      const tsUnix = parseIsoUnix(p.ts) || Date.now() / 1000;
      const gap = this.lastSampleUnix !== null && tsUnix - this.lastSampleUnix > CONFIG.power_gap_reset_s;
      if (this.powerOnUnix === null || gap) {
        this.powerOnUnix = tsUnix;
        if (gap) this.rollingBuf = [];
      }
      this.lastSampleUnix = tsUnix;

      // The three Alphasense formulas. No calibration values -> no number. Stuck channel -> no number.
      const fH = factory("h2s"), fN = factory("nh3"), fV = factory("voc");
      const rail = { h2s: atRail(weMv) || atRail(aeMv), nh3: atRail(nh3Mv), voc: atRail(vocMv) };
      const vocFault = !!fV && vocMv < (fV.fault_below_mv ?? fV.we_zero_mv);
      const fault = { h2s: rail.h2s, nh3: rail.nh3, voc: rail.voc || vocFault };
      const value = {
        h2s: fH && !fault.h2s ? h2sPpb(weMv, fH.we_zero_mv, aeMv, fH.ae_zero_mv, fH.sensitivity) / 1000.0 : null,
        nh3: fN && !fault.nh3 ? nh3Ppm(nh3Mv, fN.we_zero_mv, fN.sensitivity) : null,
        voc: fV && !fault.voc ? vocPpm(vocMv, fV.we_zero_mv, fV.sensitivity) : null,
      };
      // A negative result counts as 0; the averages below use these floored values.
      for (const g of GASES) if (value[g] != null && value[g] < 0) value[g] = 0;

      // Plain 10 / 20 / 30-minute averages of the readings in each window.
      this.rollingBuf.push({ ts: tsUnix, ...value });
      while (this.rollingBuf.length && this.rollingBuf[0].ts < tsUnix - 1800) this.rollingBuf.shift();
      const winMean = (sec, gas) => {
        let s = 0, c = 0;
        for (const r of this.rollingBuf) {
          if (r.ts >= tsUnix - sec && r[gas] != null) { s += r[gas]; c++; }
        }
        return c > 0 ? s / c : null;
      };

      const signal = {
        h2s: `A1 WE ${weMv.toFixed(2)} mV · A2 AE ${aeMv.toFixed(2)} mV`,
        nh3: `A0 ${nh3Mv.toFixed(2)} mV`,
        voc: `A3 ${vocMv.toFixed(2)} mV`,
      };
      const report = {};
      for (const g of GASES) {
        report[g] = {
          value_ppm: round(value[g]),
          sample_ppm: round(value[g]),
          avg_10m_ppm: round(winMean(600, g)),
          avg_20m_ppm: round(winMean(1200, g)),
          avg_30m_ppm: round(winMean(1800, g)),
          status: !factory(g) ? "NO_CALIBRATION" : fault[g] ? "SENSOR_FAULT"
            : { datasheet: "DATASHEET_TYPICAL", site_zero: "SITE_ZERO" }[factory(g).source] || "FACTORY_CAL",
          calibration: calibrationLabel(g),
          signal: signal[g],
        };
      }

      const alarms = {};
      for (const g of GASES) alarms[g] = this._alarm(`${g}_ppm`, report[g].avg_10m_ppm);
      alarms.overall = GASES.reduce((best, g) => (ALARM_CODE[alarms[g]] > ALARM_CODE[best] ? alarms[g] : best), "NORMAL");

      const flags = (g) => {
        const f = factory(g);
        if (!f) return "NO_CALIBRATION";
        const base = f.source === "datasheet" ? "DATASHEET_TYPICAL" : f.source === "site_zero" ? "SITE_ZERO"
          : f.serial ? `FACTORY_CAL_${f.serial}` : "FACTORY_CAL";
        return base + (rail[g] ? "|SENSOR_FAULT_AT_RAIL" : "") + (g === "voc" && vocFault && !rail.voc ? "|SENSOR_FAULT_BELOW_RANGE" : "");
      };
      const bySource = (src) => GASES.filter((g) => factory(g)?.source === src).map((g) => g.toUpperCase());
      const calParts = [];
      if (bySource("site_zero").length) calParts.push(`Site zero + factory sensitivity: ${bySource("site_zero").join(", ")}`);
      if (bySource("factory").length) calParts.push(`Factory sheet: ${bySource("factory").join(", ")}`);
      if (bySource("datasheet").length) calParts.push(`Datasheet typical: ${bySource("datasheet").join(", ")}`);

      const recOut = {
        ts: p.ts,
        ts_unix: tsUnix,
        firebase_key: p.firebase_key,
        station_id: p.station_id,
        uptime_min: Number(((tsUnix - this.powerOnUnix) / 60.0).toFixed(1)),
        calibration_label: calParts.length ? calParts.join(" · ") : "No calibration values",
        algo_version: CONFIG.algo_version,
        raw_string: p.raw_string,
        weather_source: p.weather_source,
        report,
        alarms,
        qc: { h2s_flags: flags("h2s"), nh3_flags: flags("nh3"), voc_flags: flags("voc") },
        hardware: {
          battery_v: p.battery_v,
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
          head_t_c: Number(p.head_t_c.toFixed(2)),
          rh_pct: Number(p.rh_pct.toFixed(2)),
          p_hpa: Number(p.p_hpa.toFixed(2)),
          wind_speed_ms: Number(p.wind_speed_ms.toFixed(2)),
          wind_dir_deg: Number(p.wind_dir_deg.toFixed(1)),
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
              this.process(parseRecord(key, rec, this.weather.by_hour, this.weather));
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
              calibration_label: r.calibration_label,
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
          st: rows.map((r) => STATUS_CODE[r.report[g].status] ?? 4),
          al: rows.map((r) => ALARM_CODE[r.alarms[g]] ?? 0),
        };
      }
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
        "h2s_calibration", "nh3_calibration", "voc_calibration",
        "battery_v", "temp_c", "rh_pct", "raw_string",
      ];
      const escCsv = (v) => {
        const s = String(v ?? "");
        return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
      };
      const lines = [header.join(",")];
      for (const r of this.history) {
        const rep = r.report;
        lines.push([
          r.ts, r.firebase_key, r.station_id,
          r.raw.nh3_we_mv, r.raw.h2s_we_mv, r.raw.h2s_ae_mv, r.raw.voc_mv,
          rep.h2s.value_ppm, rep.h2s.avg_10m_ppm, rep.h2s.avg_20m_ppm, rep.h2s.avg_30m_ppm,
          rep.nh3.value_ppm, rep.nh3.avg_10m_ppm, rep.nh3.avg_20m_ppm, rep.nh3.avg_30m_ppm,
          rep.voc.value_ppm, rep.voc.avg_10m_ppm, rep.voc.avg_20m_ppm, rep.voc.avg_30m_ppm,
          rep.h2s.calibration, rep.nh3.calibration, rep.voc.calibration,
          r.hardware.battery_v ?? "", r.raw.head_t_c, r.raw.rh_pct, r.raw_string,
        ].map(escCsv).join(","));
      }
      return new Blob([lines.join("\n")], { type: "text/csv;charset=utf-8" });
    }
  }

  window.AQMSStandalone = new BrowserAQMSHub();
})();
