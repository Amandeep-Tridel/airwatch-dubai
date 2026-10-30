#!/usr/bin/env python3
# =============================================================================
#  ALPHASENSE NH3-B1 / NH3-B1+  ->  AMMONIA CONCENTRATION
# =============================================================================
#
#  WHAT IS THIS FILE?
#  ------------------
#  The translator for the ammonia sensor: raw electrical signal in, real
#  concentration out.
#
#
#  WHAT IS AMMONIA (NH3)?
#  ----------------------
#  A sharp-smelling gas -- the smell of household cleaner, or of a badly kept
#  animal shed. It is produced when protein-rich material rots, so landfills,
#  farms and sewage works all emit it. We measure it in "parts per million"
#  (ppm): 1 ppm means one ammonia molecule per million air molecules.
#
#  Note the unit is ppm here, but ppb (parts per BILLION) for the H2S sensor.
#  That is because this ammonia sensor is a thousand times less sensitive.
#  Mixing up ppm and ppb is a factor-of-1000 error, so the code keeps them
#  clearly separated.
#
#
#  HOW IT WORKS, AND HOW IT DIFFERS FROM THE H2S SENSOR
#  ----------------------------------------------------
#  Same basic idea: air reaches a metal plate sitting in liquid, ammonia reacts
#  there, electrons flow, and that current is proportional to concentration.
#
#  But there is one huge difference, and it changes everything:
#
#         THE H2S SENSOR HAS 4 ELECTRODES. THIS ONE HAS ONLY 3.
#
#  The H2S sensor's fourth electrode is the "auxiliary electrode" -- a buried
#  twin that feels temperature but never touches the gas, used as a control to
#  cancel out temperature drift.
#
#  This sensor does not have one. So the whole clever cancellation trick is
#  simply unavailable. Of the four correction methods in Alphasense's
#  Application Note AAN 803-05, three of them subtract an auxiliary-electrode
#  term, so only the shape of the fourth can be used here:
#
#         corrected = (measured - board_offset) - sensor_zero - drift
#
#  and even that requires knowing how the sensor drifts with temperature, which
#  brings us to the first of two serious problems.
#
#
#  PROBLEM 1: NOBODY HAS MEASURED HOW IT BEHAVES WITH TEMPERATURE
#  --------------------------------------------------------------
#  Look at the NH3-B1 datasheet's temperature rows:
#
#         Sensitivity @ -20 C .......... nd
#         Sensitivity @ +40 C .......... nd
#         Zero @ -20 C ................. nd
#         Zero @ +50 C ................. nd
#
#  "nd" means NOT DETERMINED. The manufacturer has not published how this
#  sensor responds to temperature. For the H2S sensor there are graphs and
#  numbers; here there is nothing.
#
#  So unless you measure it yourself -- put the sensor in a temperature-
#  controlled box with clean air and with a known gas concentration, and record
#  what it does at each temperature -- you are guessing. This file uses an
#  obvious placeholder and raises the TC_UNCHARACTERISED flag on every single
#  reading so nobody can forget.
#
#
#  PROBLEM 2: THE SENSOR IS BARELY MORE ACCURATE THAN ITS OWN ERROR
#  ----------------------------------------------------------------
#  Two datasheet facts, side by side:
#
#     a) In perfectly clean air, the reading can be anywhere from -10 to +10 ppm.
#        That is the "zero uncertainty". Clean air might read as 7 ppm of
#        ammonia, or as -8 ppm, and both are within specification.
#
#     b) Other gases fool it, badly. The datasheet lists:
#
#              H2S ...... < -200 %      NO2 ...... < -200 %
#              SO2 ...... < -300 %      NO ....... < -300 %
#              Cl2 ...... < -400 %
#
#        Read the H2S row as: "20 ppm of hydrogen sulfide makes this sensor
#        read 40 ppm LOWER than it should". The minus sign means the reading
#        is pushed DOWN. And hydrogen sulfide is always present on a landfill.
#
#  Put those together. If the real ammonia level is, say, 3 ppm -- which is
#  what was actually measured at a wastewater treatment plant -- then the
#  genuine signal is SMALLER than the sensor's own built-in uncertainty, and
#  smaller again than the error caused by the H2S sitting next to it.
#
#  This is not a flaw in the code. It is what the part is. The UK Centre for
#  Ecology & Hydrology, which runs Britain's national ammonia monitoring
#  network, wrote in 2018 that the available ammonia sensors -- naming the
#  Alphasense NH3-A1 and NH3-B1 specifically -- are "suited to industrial
#  measurements (e.g. leak detection in refrigeration systems), but not for
#  air quality monitoring".
#
#  So this file flags it honestly. Any reading whose size falls inside the
#  +-10 ppm zero uncertainty gets BELOW_ZERO_UNCERTAINTY, and `.quantitative`
#  becomes False. Such a reading can tell you "a plume went past" but not
#  "the concentration was X". Expect that flag constantly. It is correct.
#
#
#  ONE MORE THING: THIS SENSOR NEEDS A BIAS VOLTAGE
#  ------------------------------------------------
#  The datasheet says "Bias voltage: +200 mV (Working Electrode potential is
#  above ground)". Some electrochemical sensors need their working electrode
#  held at a deliberate voltage offset for the chemistry to work at all.
#
#  In practice: the interface board you buy MUST be the biased version. An
#  ordinary unbiased board will not drive this sensor properly. Worth checking
#  before you order, which is why there is an UNBIASED_BOARD flag.
#
#
#  Datasheet: NH3-B1 Technical Specifications v1.1.
#  Standalone file. Uses only Python's built-in library. Python 3.9 or newer.
# =============================================================================

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Flag, auto
from typing import Any, Dict, Mapping, Optional, Tuple


# =============================================================================
#  SECTION 1: NUMBERS COPIED FROM THE DATASHEET
# =============================================================================

MW_NH3 = 17.031      # molar mass of ammonia, grams per mole
R_GAS = 8.31446      # universal gas constant, J/(mol*K)

# The conditions the manufacturer guarantees. Outside these, all bets are off.
T_SPEC_MIN, T_SPEC_MAX = -30.0, 50.0     # degrees Celsius
RH_SPEC_MIN, RH_SPEC_MAX = 15.0, 90.0    # % relative humidity, continuous

RANGE_PPM = 100.0            # the accuracy warranty stops applying above this
ZERO_UNCERTAINTY_PPM = 10.0  # datasheet: "< +-10 ppm equivalent in zero air"
BIAS_MV = 200.0              # required working-electrode bias, above ground

# -----------------------------------------------------------------------------
# CROSS-SENSITIVITY: how much OTHER gases fool this sensor.
#
# Each number answers: "1 ppm of this gas makes the ammonia reading change by
# how many ppm?"  So -2.0 for H2S means every 1 ppm of hydrogen sulfide drags
# the ammonia reading DOWN by 2 ppm.
#
# To undo the damage you must measure the interfering gas separately and add
# back what it stole. Since you are already fitting H2S sensors on the same
# station, you can do exactly that -- pass the H2S reading into convert().
#
# !! THESE ARE UPPER BOUNDS, NOT MEASUREMENTS !!
# The datasheet says "< -200 %", meaning "no worse than". The true value for
# your particular sensor might be -1.4, not -2.0. Correcting by too much is
# just as wrong as not correcting at all -- you would then over-report ammonia.
# Measure it properly with a gas bottle if the number matters.
# -----------------------------------------------------------------------------
CROSS_SENSITIVITY_DEFAULT: Mapping[str, float] = {
    "h2s": -2.0,     # datasheet: < -200 % at 20 ppm   <-- always present on a landfill
    "no2": -2.0,     # datasheet: < -200 % at 20 ppm
    "cl2": -4.0,     # datasheet: < -400 % at 10 ppm
    "no": -3.0,      # datasheet: < -300 % at 50 ppm
    "so2": -3.0,     # datasheet: < -300 % at 20 ppm
    "co": 0.20,      # datasheet: < 20 % at 400 ppm    (positive: pushes reading up)
    "h2": 0.15,      # datasheet: < 15 % at 400 ppm
}

# -----------------------------------------------------------------------------
# Placeholder sensitivity-vs-temperature curve.   *** NOT REAL DATA ***
#
# Values are fractions of the sensitivity at 20 C, same idea as the H2S file.
# But where the H2S datasheet gives real measured numbers, the NH3-B1 datasheet
# says "nd" -- not determined -- for every temperature row.
#
# This straight-line 0.4 %-per-degree stand-in exists ONLY so the code is not
# silently temperature-blind. It is a placeholder, not a characterisation.
# Replace it with your own chamber measurements and pass them to convert() as
# `sensitivity_tc`, which also clears the TC_UNCHARACTERISED flag.
# -----------------------------------------------------------------------------
SENSITIVITY_TC_PLACEHOLDER: Mapping[float, float] = {
    -30: 0.80,
    -20: 0.84,
    -10: 0.88,
      0: 0.92,
     10: 0.96,
     20: 1.00,     # reference temperature
     30: 1.04,
     40: 1.08,
     50: 1.12,
}


# =============================================================================
#  SECTION 2: QUALITY FLAGS
# =============================================================================

class QC(Flag):
    """Labels describing anything suspicious about a reading.

    A "Flag" enum lets one variable hold any combination of labels at once,
    because each one occupies a different bit. Combine with `|`, test with `&`:

        if reading.flags & QC.BELOW_ZERO_UNCERTAINTY:
            print("this number is not a real concentration")
    """
    OK = 0                           # nothing wrong

    TC_UNCHARACTERISED = auto()      # using the placeholder temperature curve,
                                     # because the datasheet publishes none

    ADC_RAILED = auto()              # converter pinned at a limit: value is fiction
    ADC_NEAR_RAIL = auto()           # within 2 % of a limit: no headroom left

    TEMP_OUT_OF_SPEC = auto()        # outside -30..50 C
    TEMP_EXTRAPOLATED = auto()       # beyond the correction table; we clamped
    RH_OUT_OF_SPEC = auto()          # outside 15..90 % humidity

    BELOW_ZERO_UNCERTAINTY = auto()  # THE IMPORTANT ONE. The reading is smaller
                                     # than the sensor's own +-10 ppm zero error,
                                     # so it cannot be called a concentration.
    INTERFERENT_DOMINATED = auto()   # the correction for other gases was BIGGER
                                     # than the raw signal -- meaning most of what
                                     # was measured was not ammonia at all

    NEGATIVE = auto()                # below zero by more than the zero error
    ABOVE_LINEAR_RANGE = auto()      # past the 100 ppm warranty limit
    UNBIASED_BOARD = auto()          # interface board is not supplying +200 mV,
                                     # so the sensor is not being driven correctly

#: Flags meaning "do not put this value in an official dataset".
FATAL = QC.ADC_RAILED | QC.TEMP_OUT_OF_SPEC | QC.UNBIASED_BOARD


def describe(flags: QC) -> str:
    """Turn flags into readable text, e.g. 'TC_UNCHARACTERISED|NEGATIVE'."""
    if not flags:
        return "OK"
    return "|".join(f.name for f in QC if f is not QC.OK and f & flags)


# =============================================================================
#  SECTION 3: THE NUMBERS UNIQUE TO YOUR PARTICULAR SENSOR
# =============================================================================

@dataclass(frozen=True)
class Calibration:
    """Constants for ONE specific physical sensor.

    WHY YOU CANNOT USE AVERAGE VALUES HERE:
    The datasheet (NH3-B1 Technical Specification v1.1) says sensitivity is
    "20 to 60 nA/ppm". That is a THREE TIMES spread between the weakest and
    strongest sensor coming off the line. If you assumed the middle (40) and got
    a sensor that actually does 20, every reading would be 2 times too small.
    There is no "typical" sensor -- you must use the numbers for the exact unit
    in your hand.

    Where to find them: the calibration certificate that shipped with the
    sensor. If you bought the NH3-B1+ (the "+" / IST version), the sensor
    carries its own calibration data in a memory chip on board, which you read
    over I2C -- that is the main practical reason to pay for the + variant.

    KEEP THESE IN A CONFIG FILE, NOT HARD-CODED. Swapping a sensor should mean
    editing a file, not editing and redeploying software.
    """
    serial: str                       # which physical sensor this describes
    we_e: float                       # mV - the board's own offset with no gas.
                                      #      Boards add 200-300 mV deliberately so
                                      #      that negative currents still land in
                                      #      positive voltage territory.
    sens_mv_per_ppm: float            # mV of output per ppm of ammonia, at 20 C
    zero_ppm_equivalent: float = 0.0  # what this sensor reads in CLEAN air, at 20 C.
                                      #      Expressed in ppm rather than volts,
                                      #      because that is how the datasheet
                                      #      specifies it ("+-10 ppm equivalent").
    board_bias_mv: float = BIAS_MV    # what your interface board actually supplies.
                                      #      Must be about +200 mV. See header.

    # How your logger's ADC chip is configured.
    adc_fsr_v: float = 2.048          # largest voltage it can read
    adc_bits: int = 16                # 16 bits = 65536 steps
    adc_bipolar: bool = True          # MUST be True -- see convert() docstring


# =============================================================================
#  SECTION 4: WHAT A RESULT LOOKS LIKE
# =============================================================================

@dataclass
class Reading:
    """One converted measurement, plus everything needed to judge it."""

    ppm: float                          # the answer, parts per million
    flags: QC = QC.OK                   # anything suspicious
    we_c_mv: float = 0.0                # corrected voltage, before dividing
    sens_mv_per_ppm: float = 0.0        # the temperature-adjusted sensitivity used
    apparent_ppm: float = 0.0           # the reading BEFORE removing other gases
    interferent_ppm: float = 0.0        # how much of the signal was other gases
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """False when this value must not enter an official dataset at all."""
        return not bool(self.flags & FATAL)

    @property
    def quantitative(self) -> bool:
        """True only if this is genuinely a measured CONCENTRATION.

        THIS IS THE MOST IMPORTANT LINE IN THE FILE.

        When the reading is smaller than the sensor's own +-10 ppm zero error,
        the number is not meaningless -- a rise still indicates a plume -- but
        you cannot honestly say "the ammonia level was 4.2 ppm", because clean
        air could have produced that same 4.2 on this hardware.

        When this is False, treat the channel as a yes/no odour-event detector,
        and say exactly that in your reports. Publishing a non-quantitative
        number as though it were a measurement is how monitoring projects lose
        credibility.
        """
        return self.usable and not (self.flags & QC.BELOW_ZERO_UNCERTAINTY)

    def ugm3(self, t_ref_c: float = 25.0, p_ref_hpa: float = 1013.25) -> float:
        """Convert ppm into micrograms per cubic metre.

        ppm counts MOLECULES; ug/m3 weighs MASS. Hot air is less dense, so the
        conversion depends on temperature and pressure -- hence the arguments.

        *** USE YOUR REGULATION'S REFERENCE CONDITIONS, NOT THE WEATHER. ***
        Air quality law fixes a standard temperature for reporting. Plugging in
        the actual outdoor temperature is a classic and invisible mistake.

        At 25 C and standard pressure: 1 ppm of ammonia = 696 ug/m3.
        """
        return (self.ppm * 1000.0) * MW_NH3 * (p_ref_hpa * 100.0) \
            / (R_GAS * (t_ref_c + 273.15)) * 1e-3

    def __str__(self) -> str:
        mark = "" if self.usable else "  [UNUSABLE]"
        if self.usable and not self.quantitative:
            mark = "  [not quantitative]"
        return "%9.3f ppm  %s%s" % (self.ppm, describe(self.flags), mark)


# =============================================================================
#  SECTION 5: SMALL HELPER FUNCTIONS
# =============================================================================

def interpolate(table: Mapping[float, float], x: float) -> Tuple[float, bool]:
    """Read a value from a table, estimating between the listed points.

    Our tables list a value every 10 degrees, but the real temperature might be
    23.7 C. This draws a straight line between the two nearest entries.

    Example: table has 20 C -> 1.00 and 30 C -> 1.04. At 23 C we are 30 % of
    the way along, so the answer is 1.00 + 0.30 * 0.04 = 1.012.

    Returns (value, was_it_outside_the_table).

    Beyond the ends of the table we CLAMP to the nearest edge and say so. We do
    not continue the line outwards, because past the last measured point nobody
    knows what the curve really does -- and a confident wrong number is more
    dangerous than an admitted gap.
    """
    keys = sorted(table)
    if not keys:
        raise ValueError("empty coefficient table")
    if x <= keys[0]:
        return table[keys[0]], x < keys[0]
    if x >= keys[-1]:
        return table[keys[-1]], x > keys[-1]
    hi = next(k for k in keys if k >= x)
    lo = keys[keys.index(hi) - 1]
    if hi == lo:
        return table[lo], False
    return table[lo] + (x - lo) / (hi - lo) * (table[hi] - table[lo]), False


def counts_to_mv(counts: float, cal: Calibration) -> Tuple[float, QC]:
    """Turn the ADC's whole number back into the voltage it represents.

    An ADC divides its voltage range into equal steps. A 16-bit bipolar
    converter has 32767 steps each way, so on a 2.048 V range one step is
    0.0625 mV, and:   voltage = counts * range / 32767

    We also check for "railing" -- the converter hitting its limit. When that
    happens the output just stops at the maximum and stays there, silently. The
    number still looks like a measurement. It is not.
    """
    full = (1 << (cal.adc_bits - 1)) - 1        # 32767 for a 16-bit converter
    lo = -full if cal.adc_bipolar else 0
    flags = QC.OK
    if counts >= full or counts <= lo:
        flags |= QC.ADC_RAILED
    elif counts >= full * 0.98 or (cal.adc_bipolar and counts <= lo * 0.98):
        flags |= QC.ADC_NEAR_RAIL
    return counts / full * cal.adc_fsr_v * 1000.0, flags


# =============================================================================
#  SECTION 6: THE MAIN ALGORITHM
# =============================================================================

def convert(
    we_counts: float,
    head_t_c: float,
    cal: Calibration,
    *,
    interferents_ppm: Optional[Mapping[str, float]] = None,
    cross_sensitivity: Mapping[str, float] = CROSS_SENSITIVITY_DEFAULT,
    sensitivity_tc: Optional[Mapping[float, float]] = None,
    zero_tc_ppm: Optional[Mapping[float, float]] = None,
    rh_pct: Optional[float] = None,
) -> Reading:
    """Convert raw ADC counts into ammonia concentration in ppm.

    ARGUMENTS
      we_counts        - the working electrode reading from the ADC
      head_t_c         - temperature in Celsius, measured AT THE SENSOR HEAD,
                         not on the circuit board. If the sensor hangs in
                         outdoor air while the electronics sit in a cooled box,
                         those differ by several degrees and every temperature
                         correction below would be applied wrongly.
      cal              - the Calibration for this specific sensor

      interferents_ppm - concentrations of OTHER gases at this same instant, in
                         ppm, e.g. {"h2s": 3.2, "so2": 0.4}. On a landfill you
                         will always have H2S, so always pass it. Skip this and
                         the ammonia reading comes out roughly twice the H2S
                         concentration too LOW.

      sensitivity_tc   - YOUR measured temperature curve. Pass it and the
      zero_tc_ppm        TC_UNCHARACTERISED flag clears. Leave them out and a
                         placeholder is used and the flag is raised, because
                         the datasheet publishes nothing here.

    *** adc_bipolar MUST BE True ***
    Here is why. Suppose this sensor's zero happens to sit at -8 ppm, and there
    is 3 ppm of H2S in the air, which drags the reading down another 6 ppm. The
    output voltage then falls BELOW the board's own 200-300 mV offset -- it goes
    negative relative to the reference. A converter wired for positive voltages
    only will report 0 and keep reporting 0. You lose the channel completely and
    nothing warns you. This actually happened while building the test data: one
    sensor read zero for 100 % of a 30-day run.
    """
    flags = QC.OK

    # --- STEP 0: is the hardware even set up correctly? ----------------------
    # This sensor needs its working electrode held about 200 mV above ground.
    # If the interface board does not do that, the chemistry does not work
    # properly and no amount of arithmetic afterwards will fix it.
    if abs(cal.board_bias_mv - BIAS_MV) > 20.0:
        flags |= QC.UNBIASED_BOARD

    # --- STEP 1: ADC counts -> millivolts ------------------------------------
    we_u, f = counts_to_mv(we_counts, cal)      # "u" = uncorrected
    flags |= f

    # --- STEP 2: was the environment within specification? -------------------
    if not (T_SPEC_MIN <= head_t_c <= T_SPEC_MAX):
        flags |= QC.TEMP_OUT_OF_SPEC
    if rh_pct is not None and not (RH_SPEC_MIN <= rh_pct <= RH_SPEC_MAX):
        flags |= QC.RH_OUT_OF_SPEC

    # --- STEP 3: adjust sensitivity for temperature --------------------------
    if sensitivity_tc is None:
        # No measured curve supplied, so fall back to the placeholder AND make
        # very sure the caller knows the number is not properly compensated.
        sensitivity_tc = SENSITIVITY_TC_PLACEHOLDER
        flags |= QC.TC_UNCHARACTERISED

    scale, extrapolated = interpolate(sensitivity_tc, head_t_c)
    if extrapolated:
        flags |= QC.TEMP_EXTRAPOLATED
    sens = cal.sens_mv_per_ppm * scale
    if sens <= 0:
        raise ValueError("non-positive sensitivity for %s" % cal.serial)

    # --- STEP 4: work out the zero (what clean air would read) ---------------
    zero_ppm = cal.zero_ppm_equivalent
    if zero_tc_ppm is not None:
        # If you measured how the zero moves with temperature, apply it here.
        drift, extrap_z = interpolate(zero_tc_ppm, head_t_c)
        if extrap_z:
            flags |= QC.TEMP_EXTRAPOLATED
        zero_ppm += drift

    # --- STEP 5: the actual conversion ---------------------------------------
    # This is the algorithm-4 shape from AAN 803-05: subtract the board offset,
    # divide by sensitivity, then subtract the sensor's clean-air zero. There is
    # no auxiliary-electrode term because this sensor has no auxiliary electrode.
    we_c = we_u - cal.we_e                      # remove the board's own offset
    apparent = we_c / sens - zero_ppm           # mV / (mV per ppm) = ppm, minus zero

    # --- STEP 6: remove the contribution from OTHER gases --------------------
    correction = 0.0
    if interferents_ppm:
        for gas, conc in interferents_ppm.items():
            # factor * their concentration = how much they shifted our reading
            correction += cross_sensitivity.get(gas.lower(), 0.0) * conc
    ppm = apparent - correction

    if correction and abs(correction) > abs(apparent):
        # The correction was larger than the thing being corrected. That means
        # most of what the sensor measured was not ammonia. The answer is now
        # mostly arithmetic rather than measurement -- treat with suspicion.
        flags |= QC.INTERFERENT_DOMINATED

    # --- STEP 7: is this actually a concentration, or just noise? ------------
    if abs(ppm) < ZERO_UNCERTAINTY_PPM:
        # The whole answer fits inside the sensor's own zero error. See the
        # `quantitative` property above for what this means in practice.
        flags |= QC.BELOW_ZERO_UNCERTAINTY
    if ppm < -ZERO_UNCERTAINTY_PPM:
        # More negative than even the zero uncertainty explains. You cannot
        # have negative gas, so something upstream is wrong.
        flags |= QC.NEGATIVE
    if ppm > RANGE_PPM:
        flags |= QC.ABOVE_LINEAR_RANGE

    return Reading(ppm=ppm, flags=flags, we_c_mv=we_c, sens_mv_per_ppm=sens,
                   apparent_ppm=apparent, interferent_ppm=correction,
                   diagnostics={"we_u_mv": we_u, "zero_ppm": zero_ppm,
                                "head_t_c": head_t_c})


# =============================================================================
#  SECTION 7: SELF-TEST
# =============================================================================
# Run this file directly:
#
#     python nh3_b1.py
#
# It converts a test dataset whose true answers are already known, and reports
# how close it got. The test data was made by running this physics BACKWARDS.
#
# WATCH THE BIAS -- the average error. Random noise cancels out over thousands
# of rows, so bias should be nearly zero if the maths is right.
#
# BUT ALSO WATCH THE FLAG PERCENTAGES. They are the real story for this sensor.
# The arithmetic passing does not mean the sensor is fit for purpose.
# =============================================================================

def _self_test(fixture: str, calfile: str) -> int:
    import csv
    import statistics as st

    # --- load this test sensor's calibration constants -----------------------
    consts: Dict[str, Dict[str, float]] = {}
    with open(calfile, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            consts.setdefault(row["sensor"], {})[row["param"]] = float(row["value"])

    c = consts["NH3-B1-A"]
    cal = Calibration(
        serial="NH3-B1-A",
        we_e=c["WE_e"],
        # nA/ppm * mV/nA = mV/ppm
        sens_mv_per_ppm=c["S20"] * c["gain"],
        zero_ppm_equivalent=c["zero_ppm"],
    )

    # The test data was generated with a known temperature curve, so feed the
    # same one back in. In the field you would have to measure this yourself.
    tc = {t: 1.0 + 0.004 * (t - 20.0) for t in range(-30, 51, 10)}
    xs = {"h2s": c["xs_h2s"], "so2": c["xs_so2"]}

    # --- convert every row, compare against the known truth ------------------
    errs = []
    counts = {"below_zero": 0, "interf": 0, "unusable": 0}
    rows = 0
    with open(fixture, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rows += 1
            r = convert(
                float(row["nh3_A_we_counts"]),
                float(row["head_t_c"]),
                cal,
                # Pass the true H2S and SO2 so cross-sensitivity can be removed.
                # (true_h2s_ppb is in ppb, so divide by 1000 to get ppm.)
                interferents_ppm={"h2s": float(row["true_h2s_ppb"]) / 1000.0,
                                  "so2": float(row["true_so2_ppm"])},
                cross_sensitivity=xs,
                sensitivity_tc=tc,
                rh_pct=float(row["rh_pct"]),
            )
            errs.append(r.ppm - float(row["true_nh3_ppm"]))
            if r.flags & QC.BELOW_ZERO_UNCERTAINTY:
                counts["below_zero"] += 1
            if r.flags & QC.INTERFERENT_DOMINATED:
                counts["interf"] += 1
            if not r.usable:
                counts["unusable"] += 1

    # --- report ---------------------------------------------------------------
    bias = st.mean(errs)
    rms = math.sqrt(sum(e * e for e in errs) / len(errs))
    p95 = sorted(abs(e) for e in errs)[int(0.95 * len(errs))]
    print("rows                     : %d" % rows)
    print("bias                     : %+.4f ppm" % bias)
    print("rms                      : %.4f ppm" % rms)
    print("p95 |error|              : %.4f ppm" % p95)
    print("BELOW_ZERO_UNCERTAINTY   : %d (%.1f %%)" % (
        counts["below_zero"], 100 * counts["below_zero"] / rows))
    print("INTERFERENT_DOMINATED    : %d (%.1f %%)" % (
        counts["interf"], 100 * counts["interf"] / rows))
    print("unusable                 : %d (%.1f %%)" % (
        counts["unusable"], 100 * counts["unusable"] / rows))

    ok = abs(bias) < 0.05 and p95 < 1.0
    print("\n%s  (arithmetic; the flag rates above are the real story)"
          % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    # Runs only when this file is executed directly, not when imported.
    import argparse
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    data = os.path.join(os.path.dirname(here), "sensor raw data")
    ap = argparse.ArgumentParser(description="NH3-B1 conversion self-test")
    ap.add_argument("--fixture", default=os.path.join(data, "aqms_raw_fixture.csv"))
    ap.add_argument("--cal", default=os.path.join(data, "aqms_raw_fixture_cal.csv"))
    args = ap.parse_args()
    raise SystemExit(_self_test(args.fixture, args.cal))
