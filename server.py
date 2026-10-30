#!/usr/bin/env python3
"""
AQMS Real-Time Server v6.0 -- Firebase RTDB (aqms-65300) -> algorithm engine -> dashboard.

Data flow
---------
- Polls https://aqms-65300-default-rtdb.firebaseio.com/data.json incrementally
  (orderBy="$key" & startAt=<last key>) so only NEW records are downloaded.
  TLS is verified with Google Trust Services GTS Root R1 (certs/gts_root_r1.pem).
- Matches each sample's UTC date+hour with Open-Meteo Dubai weather.
- Runs algo_engine.AQMSAlgorithmEngine (datasheet methods + LOD/uncertainty).

Dashboard API (small, gzip-compressed, poll-friendly)
-----------------------------------------------------
- GET  /api/live            latest compact reading + connection status   (~2 KB)
- GET  /api/history?hours=N compact column arrays for charts             (~20-60 KB gz)
- GET  /api/detail          full latest record (technical drawer)
- GET  /api/export/csv      CSV log download
- POST /api/alarms          update thresholds (requires X-Admin-Token)
- GET  /api/state, WS /ws/dashboard   legacy endpoints (kept for compatibility)
"""

from __future__ import annotations

import asyncio
import csv
import json
import os
import ssl
import time
import urllib.parse
import urllib.request
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set

import uvicorn
from fastapi import FastAPI, Header, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from algo_engine import AQMSAlgorithmEngine, parse_iso_unix
from string_parser import parse_raw_string

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
CERT_PATH = BASE_DIR / "certs" / "gts_root_r1.pem"
DATA_DIR = BASE_DIR / "data"
CSV_LOG_PATH = DATA_DIR / "telemetry_log.csv"
STATIC_DIR = BASE_DIR / "static"

DUBAI_WEATHER_URL = (
    "https://api.open-meteo.com/v1/forecast?"
    "latitude=25.277&longitude=55.396"
    "&current=temperature_2m,relative_humidity_2m,surface_pressure,wind_speed_10m,wind_direction_10m"
    "&hourly=temperature_2m,relative_humidity_2m,surface_pressure,wind_speed_10m,wind_direction_10m"
    "&past_days=2&forecast_days=1"
    "&wind_speed_unit=ms&timezone=UTC"
)

STATUS_CODE = {"WARMUP": 0, "BELOW_LOD": 1, "INDICATIVE": 2, "QUANTITATIVE": 3}
ALARM_CODE = {"NORMAL": 0, "WARNING": 1, "ACTION": 2, "CRITICAL": 3}
GASES = ("h2s", "nh3", "voc")


def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def build_firebase_ssl_context() -> ssl.SSLContext:
    """System CAs plus the GTS Root R1 PEM certificate."""
    ctx = ssl.create_default_context()
    if CERT_PATH.exists():
        try:
            ctx.load_verify_locations(cafile=str(CERT_PATH))
        except Exception:
            pass
    return ctx


FIREBASE_SSL_CTX = build_firebase_ssl_context()


def fetch_dubai_weather_sync() -> Dict[str, Any]:
    req = urllib.request.Request(DUBAI_WEATHER_URL, headers={"User-Agent": "AQMS-Server/6.0"})
    with urllib.request.urlopen(req, timeout=8.0) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    cur = data.get("current", {})
    hourly = data.get("hourly", {})
    by_hour: Dict[str, Dict[str, float]] = {}
    cols = {
        "temp_c": hourly.get("temperature_2m", []),
        "rh_pct": hourly.get("relative_humidity_2m", []),
        "press_hpa": hourly.get("surface_pressure", []),
        "wind_speed_ms": hourly.get("wind_speed_10m", []),
        "wind_dir_deg": hourly.get("wind_direction_10m", []),
    }
    for i, t_str in enumerate(hourly.get("time", [])):
        row = {k: (float(v[i]) if i < len(v) and v[i] is not None else None) for k, v in cols.items()}
        if None not in row.values():
            by_hour[t_str[:13]] = row  # key "YYYY-MM-DDTHH" (UTC)
    return {
        "temp_c": float(cur.get("temperature_2m", 27.0)),
        "rh_pct": float(cur.get("relative_humidity_2m", 70.0)),
        "press_hpa": float(cur.get("surface_pressure", 1012.0)),
        "wind_speed_ms": float(cur.get("wind_speed_10m", 1.5)),
        "wind_dir_deg": float(cur.get("wind_direction_10m", 0.0)),
        "fetched_at": cur.get("time", ""),
        "source": "Open-Meteo (Dubai)",
        "by_hour": by_hour,
    }


def fetch_firebase_sync(base_url: str, last_key: Optional[str], initial_limit: int) -> Dict[str, Any]:
    """Fetch only new records. Firebase REST: orderBy="$key" needs no index rule."""
    if last_key is None:
        q = {"orderBy": '"$key"', "limitToLast": str(initial_limit)}
    else:
        q = {"orderBy": '"$key"', "startAt": json.dumps(last_key)}
    url = base_url + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"User-Agent": "AQMS-Server/6.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=10.0, context=FIREBASE_SSL_CTX) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw) if raw else {}
    return data if isinstance(data, dict) else {}


class TelemetryHub:
    """Holds processed history, connection state and dashboard clients."""

    def __init__(self, config: Dict[str, Any]) -> None:
        self.config = config
        self.engine = AQMSAlgorithmEngine(config)
        max_pts = int(config.get("server", {}).get("max_history_points", 1440))
        self.history: Deque[Dict[str, Any]] = deque(maxlen=max_pts)
        self.latest_by_station: Dict[str, Dict[str, Any]] = {}
        self.dashboard_clients: Set[WebSocket] = set()

        self.last_key: Optional[str] = None
        self.firebase_connected = False
        self.firebase_last_check_unix: Optional[float] = None
        self.firebase_latency_ms = 0.0
        self.firebase_error: Optional[str] = None
        self.firebase_bytes_last = 0
        self.total_received = 0

        self.dubai_weather: Dict[str, Any] = {
            "temp_c": 27.0, "rh_pct": 70.0, "press_hpa": 1012.0,
            "wind_speed_ms": 1.5, "wind_dir_deg": 0.0,
            "fetched_at": "", "source": "Open-Meteo (Dubai)", "by_hour": {},
        }
        self.weather_task: Optional[asyncio.Task] = None
        self.firebase_task: Optional[asyncio.Task] = None

        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(CSV_LOG_PATH, "w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerow([
                "ts", "firebase_key", "station_id",
                "a0_nh3_mv", "a1_h2s_we_mv", "a2_h2s_ae_mv", "a3_voc_mv",
                "h2s_ppm", "h2s_status", "nh3_ppm", "nh3_status", "voc_ppm", "voc_status",
                "battery_v", "temp_c", "rh_pct", "raw_string",
            ])

    # ------------------------------------------------------------------ status
    def get_stats(self) -> Dict[str, Any]:
        w = {k: v for k, v in self.dubai_weather.items() if k != "by_hour"}
        return {
            "algo_version": self.engine.ALGO_VERSION,
            "total_received": self.total_received,
            "dubai_weather": w,
            "firebase": {
                "connected": self.firebase_connected,
                "url": self.config.get("firebase", {}).get("url"),
                "last_check_unix": self.firebase_last_check_unix,
                "last_check_ts": time.strftime("%H:%M:%S", time.localtime(self.firebase_last_check_unix))
                if self.firebase_last_check_unix else None,
                "latency_ms": self.firebase_latency_ms,
                "bytes_last_poll": self.firebase_bytes_last,
                "error": self.firebase_error,
            },
        }

    def _weather_for(self, raw_line: str) -> Dict[str, Any]:
        """Weather for the sample's UTC date+hour (raw_line starts 'YYYY-MM-DD,HH:MM:SS')."""
        if len(raw_line) >= 13:
            key = f"{raw_line[:10]}T{raw_line[11:13]}"
            row = self.dubai_weather.get("by_hour", {}).get(key)
            if row:
                w = dict(row)
                w["source"] = f"Open-Meteo hourly {key}:00 UTC"
                return w
        return self.dubai_weather

    # ------------------------------------------------------------------ ingest
    async def ingest(self, raw_line: str, meta: Dict[str, Any], broadcast: bool) -> Dict[str, Any]:
        parsed = parse_raw_string(
            raw_line, self.config.get("string_format"),
            live_weather=self._weather_for(raw_line), firebase_meta=meta,
        )
        result = self.engine.process(parsed, transport="FIREBASE")
        self.total_received += 1
        self.history.append(result)
        self.latest_by_station[result["station_id"]] = result
        if self.config.get("server", {}).get("log_to_csv", True):
            self._append_csv(result)
        if broadcast and self.dashboard_clients:
            await self._broadcast({"type": "telemetry", "data": result, "stats": self.get_stats()})
        return result

    async def _broadcast(self, msg: Dict[str, Any]) -> None:
        text = json.dumps(msg)
        for ws in list(self.dashboard_clients):
            try:
                await ws.send_text(text)
            except Exception:
                self.dashboard_clients.discard(ws)

    def _append_csv(self, r: Dict[str, Any]) -> None:
        try:
            raw, rep, hw = r["raw"], r["report"], r.get("hardware", {})
            with open(CSV_LOG_PATH, "a", newline="", encoding="utf-8") as fh:
                csv.writer(fh).writerow([
                    r["ts"], r.get("firebase_key", ""), r["station_id"],
                    raw["nh3_we_mv"], raw["h2s_we_mv"], raw["h2s_ae_mv"], raw["voc_mv"],
                    rep["h2s"]["value_ppm"], rep["h2s"]["status"],
                    rep["nh3"]["value_ppm"], rep["nh3"]["status"],
                    rep["voc"]["value_ppm"], rep["voc"]["status"],
                    hw.get("battery_v", ""), raw["head_t_c"], raw["rh_pct"], r["raw_string"],
                ])
        except Exception:
            pass


hub = TelemetryHub(load_config())


# ---------------------------------------------------------------------- pollers
async def poll_weather() -> None:
    while True:
        try:
            hub.dubai_weather = await asyncio.to_thread(fetch_dubai_weather_sync)
        except Exception:
            pass
        await asyncio.sleep(600)


async def poll_firebase() -> None:
    fb_cfg = hub.config.get("firebase", {})
    url = fb_cfg.get("url", "https://aqms-65300-default-rtdb.firebaseio.com/data.json")
    interval = float(fb_cfg.get("poll_interval_sec", 1.5))
    initial = int(hub.config.get("server", {}).get("max_history_points", 1440))

    try:  # weather first so history samples get the right hour
        hub.dubai_weather = await asyncio.to_thread(fetch_dubai_weather_sync)
    except Exception:
        pass

    first = True
    while True:
        t0 = time.perf_counter()
        try:
            records = await asyncio.to_thread(fetch_firebase_sync, url, hub.last_key, initial)
            hub.firebase_latency_ms = round((time.perf_counter() - t0) * 1000.0, 1)
            hub.firebase_bytes_last = len(json.dumps(records)) if records else 0
            hub.firebase_connected = True
            hub.firebase_error = None
            hub.firebase_last_check_unix = time.time()
            for key in sorted(records):
                if hub.last_key is not None and key <= hub.last_key:
                    continue  # startAt is inclusive
                rec = records[key]
                if isinstance(rec, dict) and (rec.get("combinedString") or rec.get("raw")):
                    meta = dict(rec)
                    meta["firebase_key"] = key
                    raw_str = rec.get("combinedString") or json.dumps(rec.get("raw"))
                    try:
                        await hub.ingest(raw_str, meta, broadcast=not first)
                    except Exception as parse_exc:
                        hub.firebase_error = f"Record {key}: {parse_exc}"
                hub.last_key = key
            first = False
        except Exception as exc:
            hub.firebase_connected = False
            hub.firebase_error = str(exc)
            hub.firebase_last_check_unix = time.time()
        if hub.dashboard_clients:
            await hub._broadcast({"type": "status", "stats": hub.get_stats()})
        await asyncio.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    hub.weather_task = asyncio.create_task(poll_weather())
    hub.firebase_task = asyncio.create_task(poll_firebase())
    try:
        yield
    finally:
        for t in (hub.weather_task, hub.firebase_task):
            if t:
                t.cancel()


app = FastAPI(title="AQMS Dubai Live Server", version="6.0.0", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=800)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ---------------------------------------------------------------------- helpers
def _latest() -> Optional[Dict[str, Any]]:
    return next(reversed(hub.history), None) if hub.history else None


def _compact_live(r: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "ts": r["ts"],
        "ts_unix": parse_iso_unix(r["ts"]),
        "station_id": r["station_id"],
        "uptime_min": r.get("uptime_min"),
        "calibration_verified": r.get("calibration_verified", False),
        "gases": {
            g: {
                **r["report"][g],
                "alarm": r["alarms"][g],
                "flags": r["qc"][f"{g}_flags"],
            }
            for g in GASES
        },
        "overall_alarm": r["alarms"]["overall"],
        "weather": {
            "temp_c": r["raw"]["head_t_c"],
            "rh_pct": r["raw"]["rh_pct"],
            "p_hpa": r["raw"]["p_hpa"],
            "wind_speed_ms": r["raw"]["wind_speed_ms"],
            "wind_dir_deg": r["raw"]["wind_dir_deg"],
            "source": r.get("weather_source"),
        },
        "hardware": r.get("hardware", {}),
        "dsp": {k: r["dsp"][k] for k in ("spike_rate_pct", "battery_dips_total", "spikes_rejected_total")},
    }


# ---------------------------------------------------------------------- routes
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/live")
async def api_live():
    r = _latest()
    return {
        "server_unix": time.time(),
        "stats": hub.get_stats(),
        "thresholds": hub.config.get("alarms", {}),
        "latest": _compact_live(r) if r else None,
    }


@app.get("/api/history")
async def api_history(hours: float = Query(24.0, ge=0.25, le=48.0)):
    rows = list(hub.history)
    if rows:
        end = parse_iso_unix(rows[-1]["ts"]) or time.time()
        cutoff = end - hours * 3600.0
        rows = [r for r in rows if (parse_iso_unix(r["ts"]) or 0) >= cutoff]
    out: Dict[str, Any] = {"t": [parse_iso_unix(r["ts"]) for r in rows]}
    for g in GASES:
        out[g] = {
            "v": [r["report"][g]["value_ppm"] for r in rows],
            "s": [r["report"][g]["sample_ppm"] for r in rows],
            "r10": [r["report"][g].get("avg_10m_ppm", r["report"][g]["value_ppm"]) for r in rows],
            "r20": [r["report"][g].get("avg_20m_ppm", r["report"][g]["value_ppm"]) for r in rows],
            "r30": [r["report"][g].get("avg_30m_ppm", r["report"][g]["value_ppm"]) for r in rows],
            "lod": [r["report"][g]["lod_ppm"] for r in rows],
            "st": [STATUS_CODE.get(r["report"][g]["status"], 1) for r in rows],
            "al": [ALARM_CODE.get(r["alarms"][g], 0) for r in rows],
        }
    out["nh3"]["ev"] = [1 if r["report"]["nh3"].get("event") else 0 for r in rows]
    out["temp"] = [r["raw"]["head_t_c"] for r in rows]
    out["rh"] = [r["raw"]["rh_pct"] for r in rows]
    out["batt"] = [r.get("hardware", {}).get("battery_v") for r in rows]
    return out


@app.get("/api/detail")
async def api_detail(n: int = Query(15, ge=1, le=60)):
    rows = list(hub.history)[-n:]
    latest = rows[-1] if rows else None
    return {
        "latest": latest,
        "recent": [
            {
                "ts": r["ts"],
                "a0": r["raw"]["nh3_we_mv"], "a1": r["raw"]["h2s_we_mv"],
                "a2": r["raw"]["h2s_ae_mv"], "a3": r["raw"]["voc_mv"],
                "batt": r.get("hardware", {}).get("battery_v"),
                "h2s": r["report"]["h2s"], "nh3": r["report"]["nh3"], "voc": r["report"]["voc"],
            }
            for r in reversed(rows)
        ],
        "calibration": {
            g: {k: v for k, v in hub.config.get("calibration", {}).get(g, {}).items() if k != "datasheet"}
            for g in GASES
        },
        "calibration_note": hub.config.get("calibration", {}).get("calibration_note"),
    }


@app.get("/api/export/csv")
async def export_csv():
    return FileResponse(CSV_LOG_PATH, media_type="text/csv", filename="aqms_dubai_telemetry.csv")


@app.post("/api/alarms")
async def update_alarms(payload: Dict[str, Any], x_admin_token: Optional[str] = Header(None)):
    token = os.environ.get("AQMS_ADMIN_TOKEN") or hub.config.get("server", {}).get("admin_token")
    if not token or x_admin_token != token:
        raise HTTPException(status_code=403, detail="Admin token required")
    if isinstance(payload.get("alarms"), dict):
        hub.config["alarms"] = payload["alarms"]
        hub.engine.update_config(hub.config)
        with open(CONFIG_PATH, "w", encoding="utf-8") as fh:
            json.dump(hub.config, fh, indent=2)
    return {"ok": True, "alarms": hub.config.get("alarms", {})}


# ---------------------------------------------------------------- legacy routes
@app.get("/api/state")
async def get_state(limit: int = Query(120, ge=1, le=1440)):
    return {"stats": hub.get_stats(), "latest": hub.latest_by_station, "history": list(hub.history)[-limit:]}


@app.websocket("/ws/dashboard")
async def websocket_dashboard(ws: WebSocket):
    await ws.accept()
    hub.dashboard_clients.add(ws)
    try:
        await ws.send_text(json.dumps({"type": "snapshot", "history": list(hub.history)[-60:],
                                       "latest": hub.latest_by_station, "stats": hub.get_stats()}))
        while True:
            if await ws.receive_text() == "ping":
                await ws.send_text(json.dumps({"type": "pong"}))
    except WebSocketDisconnect:
        pass
    finally:
        hub.dashboard_clients.discard(ws)


# Serve relative static assets (./app.css, ./app.js, ./vendor/...) so the exact
# same static/ directory works on both FastAPI and GitHub Pages.
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="root_static")


if __name__ == "__main__":
    port = int(load_config().get("server", {}).get("http_port", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
