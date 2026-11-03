# Alphasense Reference Data for the AQMS Algorithms

**Method: Alphasense only.** Every gas value uses a zero and a sensitivity, by Alphasense's method: the sensitivity from the sensor's factory calibration sheet (datasheet typical for VOC); the H2S and NH3 zeros from a site zero measured on the station in clean air (AAN 803-05 "Initial calibration", and Alphasense's advice of November 2026). No temperature tables, no filtering or smoothing. A negative result counts as 0, also inside the averages.

Source PDFs were downloaded from the official Alphasense/AMETEK CDN on 2026-10-29 and are stored in this folder:

| File | Document | Version |
| :--- | :--- | :--- |
| `h2s-b4_datasheet.pdf` | H2S-B4 Hydrogen Sulfide Sensor, 4-Electrode | Tech. Spec. v1.0 |
| `nh3-b1_datasheet.pdf` | NH3-B1 / NH3-B1+ Ammonia Sensor, 3-Electrode | Tech. Spec. v1.1 (FEB24) |
| `pidx-a-040_datasheet.pdf` | PIDX-A-040 Photo Ionisation Detector | Tech. Spec. v1.1 (OCT25) |

| Sensor | Values in use | Source |
| :--- | :--- | :--- |
| NH3-B1+ (A0) | WE zero 420 mV; 73.28 nA/ppm × 0.8 mV/nA = 58.624 mV/ppm | **Site zero** (factory sheet 77840031 zero: 328 mV); sensitivity from the factory sheet |
| H2S-B4 (A1/A2) | WE zero 458 mV, AE zero 359 mV; sensitivity 1.727952 mV/ppb (1727.952 mV/ppm) | **Site zero** (factory sheet zeros: 395 / 362 mV); sensitivity from the factory sheet |
| PIDX-A-040 (A3) | V zero 70 mV (midpoint of the 40–100 mV offset range); 55 mV/ppm | **Datasheet typical**: PIDX-A-040 datasheet |

The VOC datasheet-typical values are indicative until the PID's own factory sheet is entered.

The H2S and NH3 zeros are **site zeros**: the station's own output in clean air, taken as the median from 02:00 to 05:00 Dubai time on five nights (29 Oct – 3 Nov 2026, 899 readings). This is AAN 803-05 "Initial calibration" and what Alphasense advised in November 2026 (take the zero from the cleanest part of the day). Alphasense measures its factory zeros in filtered, scrubbed, dry air at 20–25 °C on its own boards; on this station, with the factory zeros, clean air read about 1.4 ppm NH3 and 38 ppb H2S. The sensitivities stay from the factory sheets.

The station reads highest at night and lowest in office hours (a temperature effect, not gas), so with the night-time zero, daytime values show 0. Removing that swing needs temperature correction (AAN 803-05), which needs the temperature at the top of the sensor; the station does not send it yet.

**Sensor fault:** a channel at 2550 mV or more (the ADC tops out at 2560 mV) or at 5 mV or less is stuck (unplugged, no power or shorted). The gas then shows **Sensor fault** and no number. On 3 Nov 2026 from 06:47 to 12:41 UTC, A1 and A2 both sat at 2560 mV; because they cancel in the H2S formula, the old code showed 0 ppb and "Normal".

To change a sensor's values, copy them into `config.json` (`calibration.<gas>`) and into `FACTORY` at the top of `static/standalone_engine.js`.

**Alphasense's advice (November 2026):** AAN 803-05 is the latest note. Convert by dividing the corrected WE output by the sensitivity. For the NH3-B1 (3 electrodes) leave out everything about the AE (Algorithm 4). Temperature: use the AAN 803-05 algorithms as a baseline; Alphasense has no humidity algorithm. Zero: from the cleanest part of the day; before deployment in filtered, scrubbed, dry air. Then co-locate with a reference station and correct the remaining offset and gain. No pressure correction unless the site's pressure differs from the calibration pressure.

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
2. **Sensor temperature** at the top of each sensor (±1 °C), and the ISB **electronic offsets** (WEe, AEe), to switch on AAN 803-05 temperature correction. For H2S-B4, Algorithm 3's k'T is +1 at 20 °C but −1.2 at 30 °C and above.
3. **A week next to a DM reference station**, to fit an offset and a gain per gas.
