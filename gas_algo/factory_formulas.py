"""Alphasense factory-calibration formulas for the three AQMS gas sensors. Nothing else.

Only the numbers printed on each sensor's Alphasense factory calibration sheet are used:
a zero (mV) and a sensitivity. No temperature tables, no filtering, no smoothing.

    H2S-B4 (4 electrodes, on ISB):
        H2S (ppb) = ((WE - WE zero) - (AE - AE zero)) / sensitivity (mV/ppb)
        Alphasense AAN 803-05 Algorithm 3 at the factory calibration temperature,
        where Alphasense's factor k'T is 1 (AAN 803-05, Table 3).
    NH3-B1 (3 electrodes, on ISB):
        NH3 (ppm) = (WE - WE zero) / sensitivity (mV/ppm)
        Sensor 77840031 factory sheet: (mV - 328) / 58.624
    PIDX-A-040 (PID):
        VOC (ppm as isobutylene) = (V - V zero) / sensitivity (mV/ppm)
        Alphasense AAN 302-04; no temperature correction needed.

"WE zero" and "AE zero" are the TOTAL zero offsets on the factory sheet (board offset
and sensor zero together). All voltages are in millivolts (mV).
"""

# The station's ADC: 16-bit, bipolar, 2.56 V full scale (as wired on the test station).
ADC_FULL_SCALE_MV = 2560.0
ADC_STEPS = 32768


def counts_to_mv(counts, full_scale_mv=ADC_FULL_SCALE_MV, steps=ADC_STEPS):
    """Turn a raw ADC number into millivolts. Only needed if a station sends counts."""
    return counts * full_scale_mv / steps


def h2s_ppb(we_mv, we_zero_mv, ae_mv, ae_zero_mv, sensitivity_mv_per_ppb):
    """H2S (ppb) = ((WE - WE zero) - (AE - AE zero)) / sensitivity"""
    return ((we_mv - we_zero_mv) - (ae_mv - ae_zero_mv)) / sensitivity_mv_per_ppb


def nh3_ppm(we_mv, we_zero_mv, sensitivity_mv_per_ppm):
    """NH3 (ppm) = (WE - WE zero) / sensitivity"""
    return (we_mv - we_zero_mv) / sensitivity_mv_per_ppm


def voc_ppm(v_mv, v_zero_mv, sensitivity_mv_per_ppm):
    """VOC (ppm as isobutylene) = (V - V zero) / sensitivity"""
    return (v_mv - v_zero_mv) / sensitivity_mv_per_ppm


def voc_sensor_fault(v_mv, v_zero_mv):
    """A PID voltage below its zero means a faulty sensor, not clean air."""
    return v_mv < v_zero_mv
