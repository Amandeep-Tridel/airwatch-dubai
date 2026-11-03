"""Alphasense formulas for the three AQMS gas sensors. Nothing else.

Each gas uses a zero (mV) and a sensitivity: the sensitivity from the sensor's Alphasense
factory calibration sheet, the zero from that sheet or from a site zero measured on the
station in clean air. No filtering, no smoothing.

    H2S-B4 (4 electrodes, on ISB):
        H2S (ppb) = ((WE - WE zero) - (AE - AE zero)) / sensitivity (mV/ppb)
        This is Alphasense AAN 803-05 Algorithm 3 at 20 degC, where its factor k'T is 1
        (AAN 803-05, Table 3).
    NH3-B1 (3 electrodes, on ISB):
        NH3 (ppm) = (WE - WE zero) / sensitivity (mV/ppm)
        As printed on the factory sheet, e.g. sensor 77840031: (mV - 328) / 58.624
        This is AAN 803-05 Algorithm 4 with no AE and k''T = 0 (20 degC).
    PIDX-A-040 (PID):
        VOC (ppm as isobutylene) = (V - V zero) / sensitivity (mV/ppm)

"WE zero" and "AE zero" are the TOTAL zero offsets (board offset and sensor zero
together), from the factory sheet or from a site zero (AAN 803-05, "Initial calibration").
All voltages are in millivolts (mV).

Temperature correction is at the end of this file. It needs the temperature at the top of
the sensor, which the station does not send yet. At 20 degC it changes nothing.
  - Sensitivity (all three sensors): AAN 803-05 step 5 divides by a temperature-corrected
    sensitivity, read from the datasheet chart (H2S-B4 Fig. 1, NH3-B1 Fig. 2). The
    PIDX-A-040 datasheet (Fig. 2) says its output follows the ideal gas law.
  - Zero (H2S-B4 only): AAN 803-05 Tables 1 and 3. Also needs the board's electronic
    offsets (WEe, AEe); Algorithm 4 also needs the board gain, because k''T is in nA.
"""

# The station's ADC: 16-bit, bipolar, 2.56 V full scale (as wired on the test station).
ADC_FULL_SCALE_MV = 2560.0
ADC_STEPS = 32768

# A channel this close to either end of the ADC range is stuck (unplugged, no power or
# shorted), not measuring gas. An Alphasense ISB output sits at 200-300 mV plus the sensor.
RAIL_HIGH_MV = 2550.0
RAIL_LOW_MV = 5.0


def counts_to_mv(counts, full_scale_mv=ADC_FULL_SCALE_MV, steps=ADC_STEPS):
    """Turn a raw ADC number into millivolts. Only needed if a station sends counts."""
    return counts * full_scale_mv / steps


def channel_at_rail(mv):
    """True when the voltage is stuck at the top or bottom of the ADC range."""
    return mv >= RAIL_HIGH_MV or mv <= RAIL_LOW_MV


def h2s_ppb(we_mv, we_zero_mv, ae_mv, ae_zero_mv, sensitivity_mv_per_ppb):
    """H2S (ppb) = ((WE - WE zero) - (AE - AE zero)) / sensitivity"""
    we_signal = we_mv - we_zero_mv
    ae_signal = ae_mv - ae_zero_mv
    return (we_signal - ae_signal) / sensitivity_mv_per_ppb


def nh3_ppm(we_mv, we_zero_mv, sensitivity_mv_per_ppm):
    """NH3 (ppm) = (WE - WE zero) / sensitivity"""
    return (we_mv - we_zero_mv) / sensitivity_mv_per_ppm


def voc_ppm(v_mv, v_zero_mv, sensitivity_mv_per_ppm):
    """VOC (ppm as isobutylene) = (V - V zero) / sensitivity"""
    return (v_mv - v_zero_mv) / sensitivity_mv_per_ppm


def voc_sensor_fault(v_mv, v_zero_mv):
    """A PID voltage below its zero means a faulty sensor, not clean air."""
    return v_mv < v_zero_mv


# ---- Temperature correction (only with the temperature at the top of the sensor) ----

TABLE3_TEMPS_C = (-30, -20, -10, 0, 10, 20, 30, 40, 50)

# AAN 803-05 Table 3, H2S-B4 row, copied exactly. Factors at -30, -20, ... 50 degC.
# nT, kT and k'T have no unit. k''T is a current in nA: it matches the H2S-B4 datasheet's
# Fig. 2 zero shift (-65 / -125 / -180 nA at 30 / 40 / 50 degC), so on a board it is
# multiplied by the board gain (mV/nA) before it is taken from a voltage.
H2S_B4_FACTORS = {
    1: (-0.6, -0.6, 0.1, 0.8, -0.7, -2.5, -2.5, -2.2, -1.8),   # nT
    2: (0.2, 0.2, 0, -0.3, 0.3, 1, 1, 0.9, 0.7),               # kT
    3: (-14, -14, 3, 3, 2, 1, -1.2, -1.2, -1.2),               # k'T
    4: (52, 51, 48, 45, 26, 0, -65, -125, -180),               # k''T (nA)
}
# AAN 803-05 Table 2 suggests Algorithm 1 for B-type H2S (alternative: 2). Table 3 has no
# NH3-B1 row, and the NH3-B1 datasheet gives no zero-vs-temperature data ("nd"), so the NH3
# zero gets no temperature correction.

# Sensitivity as % of the 20 degC value, at -30, -20, ... 50 degC. Read from the datasheet
# charts (middle of the typical batch shown), so they are approximate: replace them with
# Alphasense's tabulated values if they send them.
SENSITIVITY_PCT = {
    "H2S-B4": (77, 82, 87, 92, 96, 100, 103, 105.5, 105.5),     # H2S-B4 datasheet Fig. 1
    "NH3-B1": (116, 112, 104, 100, 98.5, 100, 103, 110, 125),   # NH3-B1 datasheet Fig. 2
}
IDEAL_GAS_REF_K = 293.15   # 20 degC: the PIDX-A-040 is specified at room temperature


def table3_factor(factors, temp_c):
    """Value at temp_c, linearly interpolated between the Table 3 temperatures (AAN 803-05).
    Outside -30 to 50 degC the end value is used."""
    if temp_c <= TABLE3_TEMPS_C[0]:
        return factors[0]
    if temp_c >= TABLE3_TEMPS_C[-1]:
        return factors[-1]
    for i in range(len(TABLE3_TEMPS_C) - 1):
        t0, t1 = TABLE3_TEMPS_C[i], TABLE3_TEMPS_C[i + 1]
        if t0 <= temp_c <= t1:
            return factors[i] + (factors[i + 1] - factors[i]) * (temp_c - t0) / (t1 - t0)


def aan803_factor(algorithm, temp_c, gain_mv_per_na=None):
    """The H2S-B4 Table 3 factor for this algorithm and temperature, ready for
    aan803_corrected_we. Algorithm 4's k''T (nA) is turned into mV with the board gain;
    without a gain it returns None (cannot be used)."""
    factor = table3_factor(H2S_B4_FACTORS[algorithm], temp_c)
    if algorithm == 4:
        return None if gain_mv_per_na is None else factor * gain_mv_per_na
    return factor


def aan803_corrected_we(algorithm, factor, we_u, we_e, we_t, ae_u=None, ae_e=None, ae_t=None):
    """Corrected WE output WEc (mV), AAN 803-05 Table 1.

    we_u, ae_u: raw WE and AE outputs.  we_e, ae_e: electronic offsets on the board.
    we_t, ae_t: total zero offsets (sheet or site zero).  Sensor zeros: WEo = WEt - WEe, AEo = AEt - AEe.
    factor: from aan803_factor (for Algorithm 4 already in mV).
    """
    we_o = we_t - we_e
    if algorithm == 4:
        return (we_u - we_e) - we_o - factor
    ae_o = ae_t - ae_e
    if algorithm == 1:
        return (we_u - we_e) - factor * (ae_u - ae_e)
    if algorithm == 2:
        return (we_u - we_e) - factor * (we_o / ae_o) * (ae_u - ae_e)
    if algorithm == 3:
        return (we_u - we_e) - (we_o - ae_o) - factor * (ae_u - ae_e)
    raise ValueError(f"unknown AAN 803-05 algorithm {algorithm}")


def sensitivity_pct(sensor_model, temp_c):
    """Sensitivity at temp_c as % of the 20 degC value, or None for an unknown sensor.

    H2S-B4 and NH3-B1: the datasheet chart (AAN 803-05 step 5). PIDX-A-040: the ideal gas
    law, as its datasheet says: 100 x 293.15 / (273.15 + T)."""
    model = (sensor_model or "").rstrip("+")
    if model in SENSITIVITY_PCT:
        return table3_factor(SENSITIVITY_PCT[model], temp_c)
    if model == "PIDX-A-040":
        return 100.0 * IDEAL_GAS_REF_K / (273.15 + temp_c)
    return None
