# Alphasense Reference Data for the AQMS Algorithms

Source PDFs were downloaded from the official Alphasense/AMETEK CDN on 2026-10-29 and are stored in this folder:

| File | Document | Version |
| :--- | :--- | :--- |
| `h2s-b4_datasheet.pdf` | H2S-B4 Hydrogen Sulfide Sensor, 4-Electrode | Tech. Spec. v1.0 |
| `nh3-b1_datasheet.pdf` | NH3-B1 / NH3-B1+ Ammonia Sensor, 3-Electrode | Tech. Spec. v1.1 (FEB24) |
| `pidx-a-040_datasheet.pdf` | PIDX-A-040 Photo Ionisation Detector | Tech. Spec. v1.1 (OCT25) |

Not retrieved: **AAN 803-05** (background-current correction for 4-electrode sensors). It is not on the CDN under any URL pattern tried. The H2S-B4 n_T table below comes from a secondary copy, so treat it as unverified until the official note is obtained from Alphasense.

---

## 1. H2S-B4 (channels A1 = WE / OP1, A2 = AE / OP2)

| Parameter | Datasheet value |
| :--- | :--- |
| Sensitivity | 1450 to 2600 nA/ppm (at 2 ppm H2S) |
| Zero current | −250 to +200 nA (zero air, 20 °C) |
| Noise | 1 ppb (±2 s.d., Alphasense ISB) |
| Response time t90 | < 60 s (0 → 2 ppm) |
| Sensitivity vs temperature | −20 °C: 77–90 % of 20 °C output; 50 °C: 100–110 % |
| Zero vs temperature (WE) | −20 °C: +50 to +60 nA change from 20 °C; 50 °C: −120 to −160 nA |
| Zero drift | < ±100 ppb/year |
| Sensitivity drift | < 20 %/year |
| Linearity | < ±4 ppb error (0–40 ppm) |
| Range / overgas | 100 ppm / 200 ppm |
| Environment | −30 to 50 °C, 15–90 % RH, 80–120 kPa |
| Cross-sensitivity | NO2 < −10 %, Cl2 < −12 %, NO < 12 %, SO2 < 20 %, CO < 3 %, H2 < 0.5 %, NH3 < 0.1 % |

The datasheet itself says: *"Contact Alphasense for further information on zero current correction."*

**Algorithm (AAN 803, algorithm 1):**

```
WE_c = (A1 − WE_e) − n_T(T) · (A2 − AE_e)        [mV]
ppb  = WE_c / (sens_mV_per_ppb × sens_factor(T))
```

**n_T table for H2S-B4 (secondary copy of AAN 803, unverified):**

| T (°C) | −30 | −20 | −10 | 0 | 10 | 20 | 30 | 40 | 50 |
| :--- | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: | :-: |
| n_T (alg. 1) | −0.6 | −0.6 | 0.1 | 0.8 | −0.7 | −2.5 | −2.5 | −2.2 | −1.8 |
| k_T (alg. 2) | 0.2 | 0.2 | 0.0 | −0.3 | 0.3 | 1.0 | 1.0 | 0.9 | 0.7 |

Consistency check: n_T / k_T ≈ −2.5 at every temperature, which implies WE_0 / AE_0 ≈ −2.5. The two electrode zeros have opposite signs, which is Alphasense's "AE sign anomaly" case.

**Test against the Dubai data** (603 samples after warm-up, 25.2–29.5 °C): overnight spread of the hourly-median zero

| Method | Zero spread | ≈ ppb at 0.73 mV/ppb |
| :--- | :-: | :-: |
| WE only (n_T = 0) | 33.0 mV | 45 |
| Plain A1 − A2 (n_T = +1) | 39.8 mV | 54 |
| **AAN 803 table (n_T ≈ −2.5)** | **24.2 mV** | **33** |

The AAN table gives the flattest zero, so it is now the default. The trade-off is single-sample noise: 4.9 mV, versus 2.0 mV with n_T = +1, because A2 noise is multiplied by 2.5. The Kalman filter smooths this out.

---

## 2. NH3-B1 (channel A0)

| Parameter | Datasheet value |
| :--- | :--- |
| Sensitivity | **20 to 60 nA/ppm** (in 50 ppm NH3) |
| Zero | < ±10 ppm equivalent in zero air |
| Response | t90 < 150 s, t50 ≈ 30 s |
| Linearity | +5 to −5 ppm error (0–100 ppm) |
| Bias voltage | +200 mV (WE above ground), required |
| Temperature dependence | **nd** (not determined) for both sensitivity and zero |
| Zero drift / sensitivity drift | < 2 ppm/year / < 3 %/year |
| Range / overgas | 100 ppm / 200 ppm |
| Environment | −30 to 50 °C, 15–90 % RH |
| Cross-sensitivity | H2S < −200 %, NO2 < −200 %, Cl2 < −400 %, NO < −300 %, SO2 < −300 %, CO < 20 %, H2 < 15 % |
| Load resistor | 10–47 Ω |

**Algorithm (3-electrode, no auxiliary electrode):**

```
ppm = (A0 − WE_e) / (sens_mV_per_ppm × sens_factor(T)) − zero_ppm − (−2.0 × H2S_ppm)
```

- Because the zero uncertainty is ±10 ppm, readings below 10 ppm are reported as **< LOD**. NH3 works as an event detector at ambient levels.
- H2S cross-sensitivity time matching: H2S t90 < 60 s and NH3 t90 < 150 s, so the extra lag τ ≈ √(65² − 26²) ≈ 60 s. At 1-minute sampling that gives α = 1 − e⁻¹ = **0.63**.
- **NH3-B1+ option:** if the sensor is the "+" (IST) version, it carries an I2C memory chip (M24128, address 0xA0, calibration data at 0x0B00: zero output, span, sensitivity, calibration date) and a MAX31875 temperature sensor (0x90, ±1 °C). The ESP32 could read the factory calibration and the sensor-head temperature directly.

---

## 3. PIDX-A-040 (channel A3)

| Parameter | Datasheet value |
| :--- | :--- |
| Sensitivity | 55 mV/ppm ±10 % (isobutylene, tested at 10 ppm) |
| Offset voltage (clean air) | 40–100 mV |
| Minimum detection level | 1 ppb |
| Linear / operating range | 0–40 ppm |
| Response t90 | 6.5 s |
| Warm-up / full stabilisation | 5 s / 5 min |
| Output | 0.040–2.85 V |
| **Humidity sensitivity** | **"Near zero (to 95 % RH)"**, 0–95 % non-condensing |
| **Temperature dependence** | **"Follows the ideal gas law"** |
| Lamp | 10.6 eV, ~10,000 h, user replaceable |
| Supply | 3.2–5.5 V DC, 92 mW |

**Algorithm:**

```
ppm_isobutylene = (A3 − V_zero) / 55 mV/ppm × ((T + 273.15) / (T_cal + 273.15)) × (P_cal / P)
```

- No humidity correction is applied, because the datasheet gives humidity sensitivity as near zero. (An earlier version of this server boosted readings at high humidity; that was removed.)
- Our provisional zero of 95.5 mV is inside the 40–100 mV specification.

---

## 4. What is still needed for certified values

1. **Official AAN 803-05** from Alphasense, to confirm the H2S-B4 n_T table.
2. **ISB / sensor calibration sheets** for each unit: WE_e, AE_e, WE_0, AE_0 and sensitivity (mV/ppb or nA/ppm plus the board gain in mV/nA). Without these, absolute ppm values remain indicative.
3. **Sensor-head temperature.** AAN 803 requires temperature measured at the sensor (±1 °C). A BME280/SHT31 in the enclosure, or the NH3-B1+ IST sensor, would do.
4. **A carbon-bag zero test** (about 30 min), to replace the provisional zeros.
