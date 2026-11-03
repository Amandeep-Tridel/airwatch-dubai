# Alphasense Reference Data for the AQMS Algorithms

**Method: Alphasense calibration values only.** Every gas value uses only an Alphasense zero and sensitivity: the sensor's own factory calibration sheet where available, otherwise Alphasense's published datasheet typical values. No temperature tables, no values fitted to station data, no filtering or smoothing. A negative result counts as 0, also inside the averages.

Source PDFs were downloaded from the official Alphasense/AMETEK CDN on 2026-10-29 and are stored in this folder:

| File | Document | Version |
| :--- | :--- | :--- |
| `h2s-b4_datasheet.pdf` | H2S-B4 Hydrogen Sulfide Sensor, 4-Electrode | Tech. Spec. v1.0 |
| `nh3-b1_datasheet.pdf` | NH3-B1 / NH3-B1+ Ammonia Sensor, 3-Electrode | Tech. Spec. v1.1 (FEB24) |
| `pidx-a-040_datasheet.pdf` | PIDX-A-040 Photo Ionisation Detector | Tech. Spec. v1.1 (OCT25) |

| Sensor | Values in use | Source |
| :--- | :--- | :--- |
| NH3-B1+ (A0) | WE zero 328 mV; 73.28 nA/ppm × 0.8 mV/nA = 58.624 mV/ppm | **Factory sheet 77840031** |
| H2S-B4 (A1/A2) | WE zero 395 mV, AE zero 362 mV; sensitivity 1.727952 mV/ppb (1727.952 mV/ppm) | **Factory sheet** |
| PIDX-A-040 (A3) | V zero 70 mV (midpoint of the 40–100 mV offset range); 55 mV/ppm | **Datasheet typical**: PIDX-A-040 datasheet |

The VOC datasheet-typical values are indicative until the PID's own factory sheet is entered.

To switch a sensor on, copy the numbers from its factory sheet into `config.json` (`calibration.<gas>`) and into `FACTORY` at the top of `static/standalone_engine.js`.

---

## 1. H2S-B4 (channels A1 = WE / OP1, A2 = AE / OP2)

| Parameter | Datasheet value |
| :--- | :--- |
| Sensitivity | 1450 to 2600 nA/ppm (at 2 ppm H2S) |
| Zero current | −250 to +200 nA (zero air, 20 °C) |
| Noise | 1 ppb (±2 s.d., Alphasense ISB) |
| Response time t90 | < 60 s (0 → 2 ppm) |
| Zero drift | < ±100 ppb/year |
| Sensitivity drift | < 20 %/year |
| Linearity | < ±4 ppb error (0–40 ppm) |
| Range / overgas | 100 ppm / 200 ppm |
| Environment | −30 to 50 °C, 15–90 % RH, 80–120 kPa |
| Cross-sensitivity | NO2 < −10 %, Cl2 < −12 %, NO < 12 %, SO2 < 20 %, CO < 3 %, H2 < 0.5 %, NH3 < 0.1 % |

**Formula (factory sheet values only):**

```
H2S (ppb) = ((A1 − WE zero) − (A2 − AE zero)) / sensitivity        [mV, mV, mV/ppb]
```

WE zero and AE zero are the total zero offsets on the ISB factory sheet. This is Alphasense AAN 803-05 Algorithm 3 at the factory calibration temperature, where Alphasense's temperature factor k'T is 1 (AAN 803-05, Table 3), so no temperature is needed.

---

## 2. NH3-B1 (channel A0)

| Parameter | Datasheet value |
| :--- | :--- |
| Sensitivity | 20 to 60 nA/ppm (in 50 ppm NH3) |
| Zero | < ±10 ppm equivalent in zero air |
| Response | t90 < 150 s, t50 ≈ 30 s |
| Linearity | +5 to −5 ppm error (0–100 ppm) |
| Bias voltage | +200 mV (WE above ground), required |
| Zero drift / sensitivity drift | < 2 ppm/year / < 3 %/year |
| Range / overgas | 100 ppm / 200 ppm |
| Environment | −30 to 50 °C, 15–90 % RH |
| Cross-sensitivity | H2S < −200 %, NO2 < −200 %, Cl2 < −400 %, NO < −300 %, SO2 < −300 %, CO < 20 %, H2 < 15 % |

**Formula (factory sheet values only):**

```
NH3 (ppm) = (A0 − WE zero) / sensitivity
Sensor 77840031:  NH3 (ppm) = (A0 mV − 328) / 58.624
```

The datasheet's zero uncertainty is ±10 ppm, so a rise in NH3 is reliable but a small NH3 value on its own is indicative.

---

## 3. PIDX-A-040 (channel A3)

| Parameter | Datasheet value |
| :--- | :--- |
| Sensitivity | 55 mV/ppm ±10 % (isobutylene, tested at 10 ppm) |
| Offset voltage (clean air) | 40–100 mV |
| Minimum detection level | 1 ppb |
| Linear / operating range | 0–40 ppm |
| Response t90 | 6.5 s |
| Output | 0.040–2.85 V |
| Humidity sensitivity | "Near zero (to 95 % RH)", 0–95 % non-condensing |
| Lamp | 10.6 eV, ~10,000 h, user replaceable |
| Supply | 3.2–5.5 V DC, 92 mW |

**Formula (factory sheet values only):**

```
VOC (ppm isobutylene) = (A3 − V zero) / sensitivity        [mV, mV, mV/ppm]
```

Alphasense AAN 302-04 says the PID needs no temperature correction. A voltage below V zero means a sensor fault, not clean air. The PID cannot see methane.

---

## 4. Still needed

1. **Factory calibration sheet for the PIDX-A-040** fitted at the station (zero and sensitivity), to replace the datasheet typical values. The datasheet allows the PID offset to be 40–100 mV, so the typical value can leave a steady offset of about ±0.55 ppm in clean air.
