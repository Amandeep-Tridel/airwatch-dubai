# AirWatch Dubai — Live AQMS Telemetry & Alphasense Calibration Engine

**Live Web App (GitHub Pages, 24/7):** https://amandeep-tridel.github.io/airwatch-dubai/

Real-time air quality dashboard for the Dubai `aqms-monitor` station (`H₂S`, `NH₃`, `VOC`), streaming live telemetry from Firebase Realtime Database. Weather from Open-Meteo is shown for information only.

## Alphasense method only

Every gas value uses **only a zero and a sensitivity, by Alphasense's method**: the sensitivity from the sensor's factory calibration sheet (datasheet typical for VOC), the zero from a site zero (H2S, NH3) or the datasheet (VOC). No spike filter, no smoothing, no noise models, no temperature tables, no density correction. **A negative result counts as 0**, and the 10/20/30-minute averages are made from those values; the raw voltages are kept unchanged.

| Gas | Formula | Values used |
| --- | --- | --- |
| `NH3-B1+` (`A0`) | `(A0 mV − 420) / 58.624` ppm | **Site zero** 420 mV (factory sheet 77840031: 328 mV); **factory sheet** sensitivity 73.28 nA/ppm × 0.8 mV/nA = 58.624 mV/ppm |
| `H2S-B4` (`A1` WE, `A2` AE) | `((A1 − 458) − (A2 − 359)) / 1.727952` ppb | **Site zero** WE 458 mV, AE 359 mV (factory sheet: 395 / 362 mV); **factory sheet** sensitivity 1.727952 mV/ppb |
| `PIDX-A-040` (`A3`) | `(A3 mV − 70) / 55` ppm isobutylene | **Datasheet typical** (PIDX-A-040 datasheet): offset 40–100 mV, midpoint 70 mV used; 55 mV/ppm. Below 40 mV = sensor fault |

The VOC datasheet-typical values are **indicative**: an individual PID's offset can differ from the typical one by about ±30 mV (≈ ±0.55 ppm), so clean air can read a steady non-zero value. Replace them with the PID's factory sheet when it is available.

The H2S and NH3 zeros are **site zeros**: the station's own output in clean air, taken as the median from 02:00 to 05:00 Dubai time on five nights (29 Oct – 3 Nov 2026, 899 readings). This is AAN 803-05 "Initial calibration" and what Alphasense advised in November 2026 (take the zero from the cleanest part of the day). Alphasense measures its factory zeros in filtered, scrubbed, dry air at 20–25 °C on its own boards; on this station, with the factory zeros, clean air read about 1.4 ppm NH3 and 38 ppb H2S. The sensitivities stay from the factory sheets.

The station reads highest at night and lowest in office hours (a temperature effect, not gas), so with the night-time zero, daytime values show 0. Removing that swing needs temperature correction (AAN 803-05), which needs the temperature at the top of the sensor; the station does not send it yet.

**Sensor fault:** a channel at 2550 mV or more (the ADC tops out at 2560 mV) or at 5 mV or less is stuck (unplugged, no power or shorted). The gas then shows **Sensor fault** and no number. On 3 Nov 2026 from 06:47 to 12:41 UTC, A1 and A2 both sat at 2560 mV; because they cancel in the H2S formula, the old code showed 0 ppb and "Normal".

The H2S line is Alphasense AAN 803-05 Algorithm 3 at 20 °C (k'T = 1); the NH3 line is Algorithm 4 with no AE (3-electrode sensor). The VOC line follows Alphasense AAN 302-04 (no temperature correction). 10/20/30-minute values are plain averages of the readings in each window; alarm levels use the 10-minute average.

To change a zero or sensitivity (a new site zero, or the VOC factory sheet), enter it (and set `source`) in **both** places:
- `config.json` → `calibration.h2s` / `calibration.voc` (Python server mode)
- `static/standalone_engine.js` → `FACTORY` at the top (GitHub Pages mode)

## Files
- [`gas_algo/factory_formulas.py`](gas_algo/factory_formulas.py) — the three formulas, the stuck-channel check, and the AAN 803-05 temperature algorithms (not used yet). Same file as the handover package's `alphasense_formulas.py`.
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
