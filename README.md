# AirWatch Dubai — Live AQMS Telemetry & Alphasense Sensor Engine

**Live Web App (GitHub Pages, 24/7):** https://amandeep-tridel.github.io/airwatch-dubai/

Real-time air quality monitoring dashboard and Alphasense sensor conversion engine for the Dubai `aqms-monitor` station (`H₂S`, `NH₃`, `VOC`), streaming live telemetry from Firebase Realtime Database and ambient Dubai meteorology from Open-Meteo.

## Features
- **Works Directly on GitHub Pages (Zero Backend Required):** [`static/standalone_engine.js`](static/standalone_engine.js) polls Firebase RTDB incrementally (`orderBy="$key"&startAt=...`) and Open-Meteo directly from the browser, running the full Alphasense conversion + Hampel/Kalman DSP pipeline client-side.
- **Optional FastAPI Backend ([`server.py`](server.py)):** Run locally (`python server.py`) for server-side CSV logging, WebSocket streaming, and REST APIs (`/api/live`, `/api/history`, `/api/detail`, `/api/export/csv`).
- **Alphasense Datasheet Algorithms ([`gas_algo/`](gas_algo/), [`algo_engine.py`](algo_engine.py)):**
  - **`H2S-B4`** (`A1` working / `A2` auxiliary): AAN 803-05 Algorithm 1 (`[(WE − WE_e) − n_T·(AE − AE_e)] / sensitivity(T)`).
  - **`NH3-B1`** (`A0` working): 3-electrode conversion with `t90`-matched `H₂S` cross-sensitivity compensation.
  - **`PIDX-A-040`** (`A3` 10.6 eV PID): Clean-air offset removal + ideal-gas `T/P` density correction (isobutylene equivalent).
  - **DSP:** MAD first-difference live noise estimation, Hampel spike filter, and 1D random-walk Kalman smoothing.

## Run Locally (Optional Python Server)
```bash
pip install -r requirements.txt
python server.py
# Open http://localhost:8000
```
