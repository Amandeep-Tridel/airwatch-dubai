# AirWatch Dubai — Live AQMS Telemetry & Alphasense Factory-Calibration Engine

**Live Web App (GitHub Pages, 24/7):** https://amandeep-tridel.github.io/airwatch-dubai/

Real-time air quality dashboard for the Dubai `aqms-monitor` station (`H₂S`, `NH₃`, `VOC`), streaming live telemetry from Firebase Realtime Database. Weather from Open-Meteo is shown for information only.

## Factory calibration only

Every gas value uses **only the zero and sensitivity printed on the sensor's Alphasense factory calibration sheet**. No spike filter, no smoothing, no noise models, no temperature tables, no density correction, no clamping.

| Gas | Formula | Factory sheet |
| --- | --- | --- |
| `NH3-B1+` (`A0`) | `(A0 mV − 328) / 58.624` ppm | **77840031**: WE zero 328 mV, 73.28 nA/ppm × 0.8 mV/nA = 58.624 mV/ppm |
| `H2S-B4` (`A1` WE, `A2` AE) | `((A1 − WE zero) − (A2 − AE zero)) / sensitivity` ppb | not entered yet — no value shown |
| `PIDX-A-040` (`A3`) | `(A3 − V zero) / sensitivity` ppm isobutylene | not entered yet — no value shown |

The H2S line is Alphasense AAN 803-05 Algorithm 3 at the factory calibration temperature (k'T = 1). The VOC line follows Alphasense AAN 302-04 (no temperature correction). 10/20/30-minute values are plain averages of the readings in each window; alarm levels use the 10-minute average.

To switch on H2S or VOC, enter the values from its factory sheet in **both** places:
- `config.json` → `calibration.h2s` / `calibration.voc` (Python server mode)
- `static/standalone_engine.js` → `FACTORY` at the top (GitHub Pages mode)

## Files
- [`gas_algo/factory_formulas.py`](gas_algo/factory_formulas.py) — the three factory formulas.
- [`algo_engine.py`](algo_engine.py) — applies them to each packet; plain 10/20/30-minute averages.
- [`static/standalone_engine.js`](static/standalone_engine.js) — the same, in the browser (GitHub Pages needs no backend).
- [`server.py`](server.py) — optional FastAPI backend (`/api/live`, `/api/history`, `/api/detail`, `/api/export/csv`).
- [`docs/alphasense/ALPHASENSE_REFERENCE.md`](docs/alphasense/ALPHASENSE_REFERENCE.md) — datasheet facts and formulas.

## Run Locally (Optional Python Server)
```bash
pip install -r requirements.txt
python server.py
# Open http://localhost:8000/?mode=server
```
