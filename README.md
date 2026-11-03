# AirWatch Dubai — Live AQMS Telemetry & Alphasense Calibration Engine

**Live Web App (GitHub Pages, 24/7):** https://amandeep-tridel.github.io/airwatch-dubai/

Real-time air quality dashboard for the Dubai `aqms-monitor` station (`H₂S`, `NH₃`, `VOC`), streaming live telemetry from Firebase Realtime Database. Weather from Open-Meteo is shown for information only.

## Alphasense calibration values only

Every gas value uses **only an Alphasense zero and sensitivity**: the sensor's own factory calibration sheet where we have it, otherwise Alphasense's published **datasheet typical** values for the sensor type. No spike filter, no smoothing, no noise models, no temperature tables, no density correction. **A negative result counts as 0**, and the 10/20/30-minute averages are made from those values; the raw voltages are kept unchanged.

| Gas | Formula | Values used |
| --- | --- | --- |
| `NH3-B1+` (`A0`) | `(A0 mV − 328) / 58.624` ppm | **Factory sheet 77840031**: WE zero 328 mV, 73.28 nA/ppm × 0.8 mV/nA = 58.624 mV/ppm |
| `H2S-B4` (`A1` WE, `A2` AE) | `((A1 − 350) − (A2 − 350)) / 1.4` ppb | **Datasheet typical** (Alphasense ISB manual, Table 2): zero 350/350 mV; 1750 nA/ppm × 0.8 mV/nA = 1.4 mV/ppb |
| `PIDX-A-040` (`A3`) | `(A3 mV − 70) / 55` ppm isobutylene | **Datasheet typical** (PIDX-A-040 datasheet): offset 40–100 mV, midpoint 70 mV used; 55 mV/ppm. Below 40 mV = sensor fault |

Datasheet-typical values are **indicative**: an individual sensor's zero can differ from the typical value (H2S-B4 zero current −250 to +200 nA ≈ −143 to +114 ppb; PID offset ±30 mV ≈ ±0.55 ppm), so clean air can read a steady non-zero value. Replace them with the sensor's factory sheet when it is available.

The H2S line is Alphasense AAN 803-05 Algorithm 3 at the factory calibration temperature (k'T = 1). The VOC line follows Alphasense AAN 302-04 (no temperature correction). 10/20/30-minute values are plain averages of the readings in each window; alarm levels use the 10-minute average.

To use a factory sheet for H2S or VOC, enter its values (and set `source` to `factory`) in **both** places:
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
