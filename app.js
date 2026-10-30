/* AirWatch Dubai — dashboard logic (v6)
 * Polls small JSON endpoints instead of streaming the full history:
 *   /api/live     every 10 s   (~2 KB)
 *   /api/history  on new sample or range change
 *   /api/detail   only while the technical drawer is open
 */
(() => {
  "use strict";

  // ------------------------------------------------------------------ config
  const LIVE_MS = 10_000;
  const STALE_MIN = 5;           // no new sample for > 5 min => station "not reporting"
  const TZ = "Asia/Dubai";

  const GAS = {
    h2s: { name: "Hydrogen sulfide", formula: "H₂S", color: "--h2s", dec: { ppm: 3, ppb: 0 },
           desc: "Rotten-egg smell. From sewage, landfill and industry." },
    nh3: { name: "Ammonia", formula: "NH₃", color: "--nh3", dec: { ppm: 2, ppb: 0 },
           desc: "Sharp smell. From waste, agriculture and cleaning agents." },
    voc: { name: "Volatile organics", formula: "VOC", color: "--voc", dec: { ppm: 3, ppb: 0 },
           desc: "Fuel vapour, solvents and traffic (isobutylene equivalent)." },
  };
  const ORDER = ["h2s", "nh3", "voc"];
  const STATUS = ["WARMUP", "BELOW_LOD", "INDICATIVE", "QUANTITATIVE"];
  const ALARM = ["NORMAL", "WARNING", "ACTION", "CRITICAL"];

  // Timeline colour classes c0..c7
  const TL_LEGEND = [
    [3, "Normal"], [4, "NH₃ rise"], [5, "Elevated"], [6, "High"], [7, "Very high"], [1, "Warming up"], [0, "No data"],
  ];

  // ------------------------------------------------------------------ state
  const state = {
    unit: localStorage.getItem("aw-unit") || "ppm",
    gas: "h2s",
    hours: 24,
    live: null,
    hist: null,
    lastTs: null,
    plot: null,
    fetchFailed: false,
    // 100% standalone in-browser engine by default (no Python server required)
    standalone: new URLSearchParams(location.search).get("mode") !== "server",
  };

  // ------------------------------------------------------------------ utils
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => [...el.querySelectorAll(s)];
  const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  const fmtTime = (unix, opts = { hour: "2-digit", minute: "2-digit" }) =>
    unix ? new Intl.DateTimeFormat("en-GB", { timeZone: TZ, ...opts }).format(new Date(unix * 1000)) : "—";

  function conv(ppm) {
    if (ppm == null || !isFinite(ppm)) return null;
    const clamped = Math.max(0, ppm);
    return state.unit === "ppb" ? clamped * 1000 : clamped;
  }
  function fmtVal(gas, ppm) {
    const v = conv(ppm);
    if (v == null || !isFinite(v)) return "—";
    return v.toFixed(GAS[gas].dec[state.unit]);
  }
  function ago(sec) {
    if (sec == null) return "—";
    if (sec < 60) return `${Math.max(0, Math.round(sec))} s ago`;
    if (sec < 3600) return `${Math.round(sec / 60)} min ago`;
    return `${(sec / 3600).toFixed(1)} h ago`;
  }
  async function getJSON(url) {
    const r = await fetch(url, { cache: "no-store" });
    if (!r.ok) throw new Error(`${url} → HTTP ${r.status}`);
    return r.json();
  }

  // ------------------------------------------------------------------ status logic
  /** Plain-language state for one gas reading. tone ∈ good|ok|notice|warn|high|bad|muted */
  function gasState(gas, g) {
    if (!g) return { label: "No data", tone: "muted", rank: 0 };
    if (g.alarm === "CRITICAL") return { label: "Very high", tone: "bad", rank: 7 };
    if (g.alarm === "ACTION") return { label: "High", tone: "high", rank: 6 };
    if (g.alarm === "WARNING") return { label: "Elevated", tone: "warn", rank: 5 };
    if (g.status === "WARMUP") return { label: "Warming up", tone: "muted", rank: 1 };
    if (gas === "nh3" && g.event) return { label: "Rise detected", tone: "notice", rank: 4 };
    return { label: "Normal", tone: "good", rank: 3 };
  }

  function stationAgeSec() {
    const L = state.live?.latest;
    if (!L?.ts_unix) return null;
    return state.live.server_unix - L.ts_unix + (Date.now() / 1000 - state.liveFetchedAt);
  }

  // ------------------------------------------------------------------ renderers
  const ICONS = {
    good: '<svg viewBox="0 0 24 24"><path d="M9 16.2l-3.5-3.5L4 14.2l5 5 11-11-1.5-1.5z" fill="currentColor"/></svg>',
    ok: '<svg viewBox="0 0 24 24"><path d="M9 16.2l-3.5-3.5L4 14.2l5 5 11-11-1.5-1.5z" fill="currentColor"/></svg>',
    notice: '<svg viewBox="0 0 24 24"><path d="M11 7h2v6h-2zm0 8h2v2h-2zM12 2a10 10 0 100 20 10 10 0 000-20z" fill="currentColor"/></svg>',
    warn: '<svg viewBox="0 0 24 24"><path d="M1 21h22L12 2zm12-3h-2v-2h2zm0-4h-2v-4h2z" fill="currentColor"/></svg>',
    high: '<svg viewBox="0 0 24 24"><path d="M1 21h22L12 2zm12-3h-2v-2h2zm0-4h-2v-4h2z" fill="currentColor"/></svg>',
    bad: '<svg viewBox="0 0 24 24"><path d="M1 21h22L12 2zm12-3h-2v-2h2zm0-4h-2v-4h2z" fill="currentColor"/></svg>',
    muted: '<svg viewBox="0 0 24 24"><path d="M12 6v6l4 2" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round"/><circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2" fill="none"/></svg>',
  };

  function renderLivePill() {
    const pill = $("#livePill"), text = $("#liveText");
    const age = stationAgeSec();
    pill.className = "live-pill";
    if (state.fetchFailed) { pill.classList.add("is-down"); text.textContent = "Server unreachable — retrying"; return; }
    if (!state.live) { pill.classList.add("is-wait"); text.textContent = "Connecting…"; return; }
    if (!state.live.stats?.firebase?.connected) { pill.classList.add("is-down"); text.textContent = "Firebase disconnected"; return; }
    if (age != null && age > STALE_MIN * 60) { pill.classList.add("is-stale"); text.textContent = `No new data · ${ago(age)}`; return; }
    pill.classList.add("is-live"); text.textContent = `Live · updated ${ago(age)}`;
  }

  function renderBanner() {
    const L = state.live?.latest;
    const banner = $("#statusBanner");
    if (!L) return;
    const age = stationAgeSec();
    const states = ORDER.map((g) => ({ g, s: gasState(g, L.gases[g]) }));
    const worst = states.reduce((a, b) => (b.s.rank > a.s.rank ? b : a));
    const detected = states.filter((x) => x.s.rank === 3).map((x) => GAS[x.g].formula);
    const allWarm = states.every((x) => x.s.rank === 1);

    let tone, head, detail;
    if (age != null && age > STALE_MIN * 60) {
      tone = "muted"; head = "Station is not reporting";
      detail = `No new data from the Dubai station for ${ago(age).replace(" ago", "")}. The values below are the last ones received.`;
    } else if (worst.s.rank >= 5) {
      tone = worst.s.tone;
      head = `${worst.s.label} ${GAS[worst.g].name.toLowerCase()} level`;
      detail = `${GAS[worst.g].formula} is above its ${L.gases[worst.g].alarm.toLowerCase()} threshold. Check the source and ventilation.`;
    } else if (allWarm) {
      tone = "muted"; head = "Sensors are warming up";
      detail = "Readings stabilise after power-on (H₂S 1 h, NH₃ 3 h, VOC 5 min).";
    } else if (worst.s.rank === 4) {
      tone = "notice"; head = "Ammonia signal is rising";
      detail = "The NH₃ sensor shows a sustained rise above baseline.";
    } else {
      tone = "good"; head = "Air quality is good";
      detail = "All monitored gases (H₂S, NH₃, VOC) are within normal range and well below safety thresholds.";
    }
    banner.className = `status-banner tone-${tone}`;
    $("#statusIcon").innerHTML = ICONS[tone] || ICONS.muted;
    $("#statusHeadline").textContent = head;
    $("#statusDetail").textContent = detail;
    $("#lastReading").textContent = `${fmtTime(L.ts_unix)} GST`;
    $("#calState").textContent = L.calibration_verified ? "Certified" : "Provisional";
    $("#stationId").textContent = L.station_id;
  }

  function sparkSVG(gas, hours = 3) {
    const H = state.hist;
    if (!H || !H.t.length) return "";
    const end = H.t[H.t.length - 1], start = end - hours * 3600;
    const idx = H.t.map((t, i) => (t >= start ? i : -1)).filter((i) => i >= 0);
    if (idx.length < 2) return "";
    const vs = idx.map((i) => Math.max(0, H[gas].v[i]));
    let lo = 0, hi = Math.max(...vs);
    if (hi - lo < 1e-6) { hi = 0.01; }
    const W = 300, Hh = 44, pad = 4;
    const x = (k) => (k / (idx.length - 1)) * W;
    const y = (v) => Hh - pad - ((v - lo) / (hi - lo)) * (Hh - 2 * pad);
    const line = vs.map((v, k) => `${x(k).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
    const col = css(GAS[gas].color);
    return `<svg class="gc-spark" viewBox="0 0 ${W} ${Hh}" preserveAspectRatio="none" aria-hidden="true">
      <polyline points="${line}" fill="none" stroke="${col}" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round"/>
    </svg>`;
  }

  function renderCards() {
    const L = state.live?.latest;
    const th = state.live?.thresholds || {};
    const age = stationAgeSec();
    $(".gas-grid").classList.toggle("is-stale", age != null && age > STALE_MIN * 60);
    $$(".gas-card").forEach((card) => {
      const gas = card.dataset.gas, meta = GAS[gas];
      const g = L?.gases?.[gas];
      const s = gasState(gas, g);
      card.style.setProperty("--gas-color", css(meta.color));
      card.classList.toggle("is-selected", gas === state.gas);

      let valueHTML, sub;
      if (!g) { valueHTML = `<span class="gc-value as-text">—</span>`; sub = ""; }
      else if (g.status === "WARMUP") {
        valueHTML = `<span class="gc-value as-text">Warming up</span>`;
        sub = "Sensor is stabilising after power-on";
      } else {
        valueHTML = `<span class="gc-value">${fmtVal(gas, g.value_ppm)}</span><span class="gc-unit">${state.unit}</span>`;
        sub = `Sample ${fmtVal(gas, g.sample_ppm)} ${state.unit} · ± ${fmtVal(gas, g.noise_ppm)} ${state.unit} noise`;
      }

      const warn = th[`${gas}_ppm`]?.warning;
      const pct = warn && g ? Math.max(0, Math.min(100, (Math.max(0, g.value_ppm) / warn) * 100)) : 0;
      const pctText = !g || g.status === "WARMUP" ? "—" : `${pct < 1 && pct > 0 ? "<1" : pct.toFixed(0)}%`;
      const trend = g?.trend && g.status !== "WARMUP"
        ? { RISING: "↗ rising", FALLING: "↘ falling", STABLE: "→ steady" }[g.trend] : "";

      card.innerHTML = `
        <div class="gc-top">
          <div>
            <div class="gc-name">${meta.name}<span class="gc-formula">${meta.formula}</span></div>
            <div class="gc-desc">${meta.desc}</div>
          </div>
          <span class="pill ${s.tone}">${s.label}</span>
        </div>
        <div class="gc-reading">${valueHTML}</div>
        <div class="gc-sub">${sub} ${trend ? `<span class="muted">· ${trend}</span>` : ""}</div>
        ${sparkSVG(gas)}
        <div class="gc-bar" title="Share of the warning threshold"><span style="width:${pct}%"></span></div>
        <div class="gc-foot"><span>Warning threshold: ${warn != null ? `${fmtVal(gas, warn)} ${state.unit}` : "—"}</span><span>${pctText ? `${pctText} of threshold` : ""}</span></div>`;
      card.setAttribute("aria-label", `${meta.name}: ${s.label}`);
    });
  }

  function renderConditions() {
    const L = state.live?.latest, st = state.live?.stats;
    if (!L) return;
    const w = L.weather;
    $("#cTemp").textContent = `${w.temp_c.toFixed(1)} °C`;
    $("#cRh").textContent = `${Math.round(w.rh_pct)} %`;
    $("#cP").textContent = `${Math.round(w.p_hpa)} hPa`;
    $("#cWind").textContent = `${w.wind_speed_ms.toFixed(1)} m/s`;
    const dirs = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"];
    $("#cWindDir").textContent = `from ${dirs[Math.round(w.wind_dir_deg / 45) % 8]} (${Math.round(w.wind_dir_deg)}°)`;
    // Arrow points where the wind blows TO (meteorological direction is FROM)
    $("#windArrow").style.transform = `rotate(${(w.wind_dir_deg + 180) % 360}deg)`;

    const b = L.hardware?.battery_v;
    const bEl = $("#cBatt");
    bEl.textContent = b != null ? `${Number(b).toFixed(2)} V` : "—";
    bEl.className = "v " + (b == null ? "" : b >= 11.6 ? "good" : b >= 11.2 ? "warn" : "bad");
    $("#cBattS").textContent = L.hardware?.battery_dip_detected ? "brief dip (modem transmit)" : "battery";

    const fb = st?.firebase;
    const lEl = $("#cLink");
    lEl.textContent = fb?.connected ? `${Math.round(fb.latency_ms)} ms` : "Down";
    lEl.className = "v " + (fb?.connected ? "good" : "bad");
    $("#cLinkS").textContent = fb?.connected ? `Firebase · ${st.total_received} packets` : (fb?.error || "Firebase").slice(0, 48);
  }

  // ------------------------------------------------------------------ chart
  function chartData() {
    const H = state.hist;
    if (!H || !H.t.length) return null;
    const end = H.t[H.t.length - 1], start = end - state.hours * 3600;
    const i0 = H.t.findIndex((t) => t >= start);
    const sl = (a) => a.slice(i0).map(conv);
    const g = H[state.gas];
    return [H.t.slice(i0), sl(g.v), sl(g.s)];
  }

  function buildPlot() {
    const el = $("#chart");
    const data = chartData();
    if (state.plot) { state.plot.destroy(); state.plot = null; }
    if (!data || data[0].length < 2) { el.innerHTML = '<div class="chart-empty">Waiting for data…</div>'; return; }
    el.innerHTML = "";

    const col = css(GAS[state.gas].color);
    document.documentElement.style.setProperty("--sel-color", col);
    const axis = { stroke: css("--text-3"), grid: { stroke: css("--line"), width: 1 }, ticks: { stroke: css("--line") }, font: `12px ${css("--font") || "system-ui"}` };
    const dec = GAS[state.gas].dec[state.unit];
    const span = data[0][data[0].length - 1] - data[0][0];

    const opts = {
      width: el.clientWidth || 800,
      height: 320,
      tzDate: (ts) => uPlot.tzDate(new Date(ts * 1000), TZ),  // align ticks to Dubai hours
      cursor: { drag: { x: true, y: false }, points: { size: 7 } },
      legend: { show: false },
      scales: { x: { time: true } },
      axes: [
        {
          ...axis,
          values: (u, splits) => splits.map((s, i) => {
            const hm = fmtTime(s);
            const showDay = span > 6 * 3600 && (i === 0 || hm === "00:00");
            return showDay ? `${hm}\n${fmtTime(s, { day: "2-digit", month: "short" })}` : hm;
          }),
        },
        { ...axis, size: 64, values: (u, vals) => vals.map((v) => v.toFixed(Math.max(0, dec))), label: state.unit, labelSize: 18, labelFont: axis.font },
      ],
      series: [
        {},
        { label: "Measured", stroke: col, width: 2.2, points: { show: false } },
        { label: "Sample", stroke: col, width: 0, paths: () => null, points: { show: true, size: 3, stroke: col + "66", fill: col + "55", width: 0 } },
      ],
      hooks: { setCursor: [(u) => renderReadout(u.cursor.idx)] },
    };
    state.plot = new uPlot(opts, data, el);
    renderReadout(null);
  }

  /** Value readout under the chart: hovered point, or the latest point when idle. */
  function renderReadout(idx) {
    const p = state.plot, out = $("#chartReadout");
    if (!p || !out) return;
    const n = p.data[0].length;
    const i = idx == null ? n - 1 : idx;
    if (i < 0 || i >= n) return;
    const [t, v, s] = p.data.map((a) => a[i]);
    const dec = GAS[state.gas].dec[state.unit] + 1;
    const f = (x) => (x == null ? "—" : `${x.toFixed(dec)} ${state.unit}`);
    out.innerHTML = `<strong>${fmtTime(t, { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })} GST</strong>
      <span>Measured <b>${f(v)}</b></span>
      <span>Sample ${f(s)}</span>
      <span class="muted">${idx == null ? "latest" : "hovered"}</span>`;
  }

  function updatePlot() {
    const data = chartData();
    if (!state.plot || !data) return buildPlot();
    state.plot.setData(data);
  }

  // ------------------------------------------------------------------ timeline
  function renderTimeline() {
    const H = state.hist, box = $("#timeline");
    if (!H || !H.t.length) { box.innerHTML = '<div class="muted small">Waiting for data…</div>'; return; }
    const BUCKET = 600, N = 144;
    const end = Math.ceil(H.t[H.t.length - 1] / BUCKET) * BUCKET, start = end - N * BUCKET;

    const rows = ORDER.map((gas) => {
      const worst = new Array(N).fill(0), maxV = new Array(N).fill(null);
      H.t.forEach((t, i) => {
        const b = Math.floor((t - start) / BUCKET);
        if (b < 0 || b >= N) return;
        const st = STATUS[H[gas].st[i]], al = ALARM[H[gas].al[i]];
        const s = gasState(gas, { status: st, alarm: al, event: gas === "nh3" ? !!H.nh3.ev[i] : false });
        worst[b] = Math.max(worst[b], s.rank);
        if (st === "INDICATIVE" || st === "QUANTITATIVE") maxV[b] = Math.max(maxV[b] ?? -Infinity, H[gas].v[i]);
      });
      const cells = worst.map((c, b) => {
        const t0 = start + b * BUCKET;
        const lbl = TL_LEGEND.find(([k]) => k === c)?.[1] || "";
        const val = maxV[b] != null ? ` · peak ${fmtVal(gas, maxV[b])} ${state.unit}` : "";
        return `<span class="c${c}" data-tip="${esc(`${GAS[gas].formula} ${fmtTime(t0)}–${fmtTime(t0 + BUCKET)}: ${lbl}${val}`)}"></span>`;
      }).join("");
      return `<div class="tl-row"><div class="tl-label">${GAS[gas].formula}</div><div class="tl-cells">${cells}</div></div>`;
    });

    let ticks = "";
    for (let k = 0; k <= N; k += 18) {
      ticks += `<span style="left:${(k / N) * 100}%">${fmtTime(start + k * BUCKET)}</span>`;
    }
    box.innerHTML = rows.join("") + `<div class="tl-axis"><div></div><div class="tl-ticks">${ticks}</div></div>`;
    const age = stationAgeSec();
    $("#timelineSub").textContent = age != null && age > STALE_MIN * 60
      ? `Showing the 24 hours before the last reading (${fmtTime(H.t[H.t.length - 1], { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" })} GST). Each block is 10 minutes.`
      : "Each block is 10 minutes. Hover a block for details.";
  }

  function renderTimelineLegend() {
    $("#timelineLegend").innerHTML = TL_LEGEND.slice(0, 6).map(([c, l]) => `<span><i class="c${c}"></i>${l}</span>`).join("");
  }

  // ------------------------------------------------------------------ drawer
  async function renderDrawer() {
    if (!$("#drawer").open) return;
    let d;
    try {
      d = state.standalone && window.AQMSStandalone
        ? window.AQMSStandalone.getDetail(15)
        : await getJSON("/api/detail?n=15");
    } catch { return; }
    const r = d.latest;
    if (!r) return;
    $("#rawString").textContent = r.raw_string;
    const v = r.raw;
    $("#voltTable").innerHTML = [
      ["A0 · NH₃ working electrode", `${v.nh3_we_mv.toFixed(2)} mV`],
      ["A1 · H₂S working electrode", `${v.h2s_we_mv.toFixed(2)} mV`],
      ["A2 · H₂S auxiliary electrode", `${v.h2s_ae_mv.toFixed(2)} mV`],
      ["A3 · VOC (PID) output", `${v.voc_mv.toFixed(2)} mV`],
      ["Battery", r.hardware?.battery_v != null ? `${Number(r.hardware.battery_v).toFixed(3)} V` : "—"],
      ["Uptime since power-on", r.uptime_min != null ? `${(r.uptime_min / 60).toFixed(1)} h` : "—"],
      ["Spike filter / supply dips", `${r.dsp.spike_rate_pct}% · ${r.dsp.battery_dips_total} dips`],
      ["Algorithm", r.algo_version],
    ].map(([k, val]) => `<tr><td>${k}</td><td>${esc(val)}</td></tr>`).join("");

    $("#qualityTable").innerHTML = ORDER.map((g) => {
      const rep = r.report[g];
      return `<tr><td>${GAS[g].formula} status</td><td>${rep.status} · value ${rep.value_ppm.toFixed(4)} ppm</td></tr>
              <tr><td>${GAS[g].formula} noise / uncertainty</td><td>noise ${rep.noise_ppm.toFixed(4)} · ± ${rep.uncertainty_ppm.toFixed(4)} ppm</td></tr>
              <tr><td>${GAS[g].formula} flags</td><td>${esc(r.qc[`${g}_flags`])}</td></tr>`;
    }).join("");

    const cell = (g, rep) => rep.status === "WARMUP" ? "warm-up" : rep.value_ppm.toFixed(GAS[g].dec.ppm + 1);
    $("#recentTable").innerHTML =
      `<thead><tr><th>Time (GST)</th><th>A0 mV</th><th>A1 mV</th><th>A2 mV</th><th>A3 mV</th><th>Batt V</th><th>H₂S ppm</th><th>NH₃ ppm</th><th>VOC ppm</th></tr></thead><tbody>` +
      d.recent.map((p) => `<tr><td>${fmtTime(Date.parse(p.ts) / 1000, { hour: "2-digit", minute: "2-digit", second: "2-digit" })}</td>
        <td>${p.a0.toFixed(1)}</td><td>${p.a1.toFixed(1)}</td><td>${p.a2.toFixed(1)}</td><td>${p.a3.toFixed(1)}</td>
        <td>${p.batt != null ? Number(p.batt).toFixed(2) : "—"}</td>
        <td>${cell("h2s", p.h2s)}</td><td>${cell("nh3", p.nh3)}</td><td>${cell("voc", p.voc)}</td></tr>`).join("") + "</tbody>";
    $("#calNote").textContent = d.calibration_note || "";
  }

  // ------------------------------------------------------------------ data loop
  async function refreshLive() {
    try {
      let live;
      if (state.standalone && window.AQMSStandalone) {
        live = await window.AQMSStandalone.getLive();
      } else {
        try {
          live = await getJSON("/api/live");
        } catch (err) {
          if (window.AQMSStandalone && String(err.message || "").includes("HTTP 404")) {
            state.standalone = true;
            live = await window.AQMSStandalone.getLive();
          } else {
            throw err;
          }
        }
      }
      state.live = live;
      state.liveFetchedAt = Date.now() / 1000;
      state.fetchFailed = false;
      const ts = live.latest?.ts_unix ?? null;
      if (ts !== state.lastTs || !state.hist) {
        state.lastTs = ts;
        await refreshHistory();
        if ($("#drawer").open) renderDrawer();
      }
    } catch (e) {
      state.fetchFailed = true;
      console.warn(e);
    }
    renderAll(false);
  }

  async function refreshHistory() {
    try {
      state.hist = state.standalone && window.AQMSStandalone
        ? window.AQMSStandalone.getHistory(24)
        : await getJSON("/api/history?hours=24");
      updatePlot();
      renderTimeline();
    } catch (e) { console.warn(e); }
  }

  function renderAll(rebuildChart) {
    renderLivePill();
    renderBanner();
    renderCards();
    renderConditions();
    if (rebuildChart) { buildPlot(); renderTimeline(); }
  }

  // ------------------------------------------------------------------ events
  function selectGas(gas) {
    state.gas = gas;
    $$("#gasTabs button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.gas === gas)));
    renderCards();
    buildPlot();
  }

  function wire() {
    $$(".seg[aria-label='Units'] button").forEach((b) => {
      b.setAttribute("aria-pressed", String(b.dataset.unit === state.unit));
      b.addEventListener("click", () => {
        state.unit = b.dataset.unit;
        localStorage.setItem("aw-unit", state.unit);
        $$(".seg[aria-label='Units'] button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
        renderAll(true);
        renderDrawer();
      });
    });
    $$("#gasTabs button").forEach((b) => b.addEventListener("click", () => selectGas(b.dataset.gas)));
    $$("#rangeTabs button").forEach((b) => b.addEventListener("click", () => {
      state.hours = Number(b.dataset.hours);
      $$("#rangeTabs button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      buildPlot();
    }));
    $$(".gas-card").forEach((c) => {
      c.addEventListener("click", () => selectGas(c.dataset.gas));
      c.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); selectGas(c.dataset.gas); } });
    });
    $("#themeBtn").addEventListener("click", () => {
      const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", next);
      localStorage.setItem("aw-theme", next);
      renderAll(true);
    });
    $("#drawer").addEventListener("toggle", renderDrawer);
    const csvBtn = $("#csvBtn");
    if (csvBtn) {
      csvBtn.addEventListener("click", (e) => {
        if (state.standalone && window.AQMSStandalone) {
          e.preventDefault();
          const url = URL.createObjectURL(window.AQMSStandalone.buildCsvBlob());
          const a = document.createElement("a");
          a.href = url;
          a.download = "aqms_dubai_telemetry.csv";
          document.body.appendChild(a);
          a.click();
          a.remove();
          setTimeout(() => URL.revokeObjectURL(url), 2000);
        }
      });
    }

    // Timeline tooltips
    const tip = $("#tip");
    $("#timeline").addEventListener("mousemove", (e) => {
      const t = e.target.closest("[data-tip]");
      if (!t) { tip.hidden = true; return; }
      tip.textContent = t.dataset.tip;
      tip.hidden = false;
      const x = Math.min(window.innerWidth - tip.offsetWidth - 8, e.clientX + 12);
      tip.style.left = `${x}px`;
      tip.style.top = `${e.clientY + 14}px`;
    });
    $("#timeline").addEventListener("mouseleave", () => { tip.hidden = true; });

    // Resize chart with its container
    new ResizeObserver(() => {
      if (state.plot) state.plot.setSize({ width: $("#chart").clientWidth, height: 320 });
    }).observe($("#chart"));

    // Clock + "updated x s ago" ticker
    setInterval(() => {
      $("#dubaiClock").textContent = fmtTime(Date.now() / 1000);
      renderLivePill();
    }, 1000);
  }

  // ------------------------------------------------------------------ boot
  renderTimelineLegend();
  wire();
  $("#dubaiClock").textContent = fmtTime(Date.now() / 1000);
  renderCards();
  refreshLive();
  setInterval(refreshLive, LIVE_MS);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshLive(); });
})();
