#!/usr/bin/env python3
# =============================================================================
#  ALPHASENSE PIDX-A-040  ->  VOLATILE ORGANIC COMPOUND CONCENTRATION
# =============================================================================
#
#  WHAT IS THIS FILE?
#  ------------------
#  The translator for the VOC sensor: raw electrical signal in, concentration
#  out. This one is much simpler than the two electrochemical sensors, because
#  the sensor has its own electronics built in and gives you a voltage directly.
#
#
#  WHAT ARE VOCs?
#  --------------
#  "Volatile Organic Compounds" -- carbon-based chemicals that evaporate easily
#  at room temperature. Petrol fumes, paint thinner, solvents, the smell of a
#  new car. Landfills produce hundreds of different ones as rubbish decomposes.
#  Many are harmful, and together they drive a lot of the odour complaints.
#
#
#  HOW A PID WORKS (the physics, simply)
#  -------------------------------------
#  "PID" stands for Photo Ionisation Detector. Inside is a small ultraviolet
#  lamp and two metal plates.
#
#  Step 1: The UV lamp shines on the air being sampled. UV light carries
#          energy, and if a photon carries ENOUGH energy, hitting a molecule
#          knocks an electron off it. The molecule becomes electrically charged
#          -- it is "ionised".
#
#  Step 2: Those charged fragments drift to the metal plates, which are held at
#          different voltages. Charge arriving at a plate is an electric
#          current.
#
#  Step 3: More VOC molecules -> more ions -> more current -> bigger voltage
#          out. The sensor's own electronics do the current-to-voltage part, so
#          unlike the H2S and NH3 sensors you do NOT need a separate interface
#          board.
#
#
#  WHY THE "10.6 eV" NUMBER MATTERS -- AND WHY METHANE IS INVISIBLE
#  ----------------------------------------------------------------
#  Every molecule has an "ionisation potential": the minimum energy needed to
#  knock an electron off it, measured in electron-volts (eV). The lamp in this
#  sensor delivers 10.6 eV.
#
#  So the rule is brutally simple:
#
#      ionisation potential BELOW 10.6 eV  ->  detected
#      ionisation potential ABOVE 10.6 eV  ->  completely invisible
#
#  There is no partial credit. And methane (CH4) needs 12.6 eV.
#
#      *** THIS SENSOR CANNOT SEE METHANE. AT ALL. ***
#
#  Methane is the main gas a landfill produces. On a landfill project somebody
#  WILL eventually assume the "VOC" channel covers it. Write it in the
#  parameter description, not just in a comment.
#
#  For the same reason it cannot see carbon dioxide, carbon monoxide, water
#  vapour, nitrogen or oxygen -- which is convenient, since it means ordinary
#  air produces no signal.
#
#
#  PROBLEM 1: IT CANNOT TELL YOU *WHICH* VOC
#  -----------------------------------------
#  The sensor counts ions. It has no idea whether they came from benzene or
#  from acetone -- and different chemicals ionise more or less easily, so they
#  produce different amounts of signal for the same concentration.
#
#  The industry convention: calibrate with one standard gas (isobutylene) and
#  report everything "as isobutylene equivalent". If you happen to KNOW your
#  air is pure benzene, you multiply by benzene's "response factor" to convert.
#
#  On a landfill you do not know. The mixture is hundreds of compounds and it
#  changes hourly. So leave response_factor at 1.0 and label the channel
#  honestly: "VOC as isobutylene equivalent". Applying somebody else's response
#  factor to a mixture you have not chemically analysed is guesswork dressed up
#  as precision.
#
#
#  PROBLEM 2: THE ZERO DRIFTS
#  --------------------------
#  In clean air the sensor still outputs a small voltage, the "offset". The
#  datasheet only gives a range for it -- 40 to 100 mV -- because each unit
#  differs. Worse, it MOVES over time:
#
#     * the UV lamp slowly dims over its 10,000-hour life
#     * dust and film build up on the lamp window, blocking light
#     * temperature shifts the electronics slightly
#
#  So a zero measured at the factory will not still be right a year later. This
#  file provides BaselineTracker (see below) to follow the drift, and explains
#  why the more reliable answer is a scrubbed zero-air reference.
#
#
#  PROBLEM 3: TEMPERATURE AND PRESSURE CHANGE THE ANSWER
#  -----------------------------------------------------
#  This surprises people, so here it is carefully.
#
#  "ppm" is a RATIO -- how many VOC molecules per million air molecules. But
#  the sensor does not measure a ratio; it counts ions in the small volume of
#  its chamber. And the number of molecules in a fixed volume depends on
#  temperature and pressure -- hot air is thinner, so a cubic centimetre of hot
#  air holds fewer molecules than the same volume of cold air.
#
#  So the SAME ppm produces LESS signal when hot. The datasheet confirms it in
#  Figure 2: "the temperature response follows the ideal gas law".
#
#  Across the operating range this is about a +-13 % effect -- much bigger than
#  most people expect. That is why head temperature and barometric pressure are
#  required arguments to convert(), not optional extras.
#
#  Good news: humidity needs NO correction. The datasheet gives humidity
#  sensitivity as "near zero (to 95 % RH)", which is unusually good for a PID.
#
#
#  Datasheet: PIDX-A-040 Technical specifications v1.1, doc ref PIDX-A-040/OCT25.
#  Standalone file. Uses only Python's built-in library. Python 3.9 or newer.
# =============================================================================

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from enum import Flag, auto
from typing import Any, Deque, Dict, Mapping, Optional, Tuple


# =============================================================================
#  SECTION 1: NUMBERS COPIED FROM THE DATASHEET
# =============================================================================

MW_ISOBUTYLENE = 56.106     # molar mass of isobutylene, g/mol (the reference gas)
R_GAS = 8.31446             # universal gas constant, J/(mol*K)

# The voltage window the sensor is specified to produce. A reading outside this
# means something is wrong with the sensor or the wiring, not with the air.
OUT_MIN_V, OUT_MAX_V = 0.040, 2.850

# The clean-air offset, per unit. Each sensor sits somewhere in this window.
OFFSET_MIN_MV, OFFSET_MAX_MV = 40.0, 100.0

# How much voltage per ppm of isobutylene. The datasheet says 55 mV/ppm "+-10 %",
# so an individual sensor could be anywhere from about 49.5 to 60.5. You must
# span-calibrate each unit; using the nominal 55 leaves you up to 10 % out.
NOMINAL_MV_PER_PPM = 55.0

# Above this the response stops being a straight line, so the simple
# "divide by sensitivity" maths below no longer holds.
LINEAR_RANGE_PPM = 40.0

# Operating limits. Note the sensor has two different temperature ratings: it
# OPERATES from -40 to +65 C, but its intrinsic-safety certification only holds
# from -20 to +60 C. We check the wider operating one here.
T_SPEC_MIN, T_SPEC_MAX = -40.0, 65.0
RH_SPEC_MAX = 95.0          # % relative humidity, and must be non-condensing

# Timing, from the datasheet.
WARMUP_S = 5.0              # "Warm Up Time" -- before it responds at all
STABILISATION_S = 300.0     # "Full Stabilisation Time", 5 minutes -- before the
                            # reading is trustworthy. Readings inside this window
                            # get the WARMUP flag.

# -----------------------------------------------------------------------------
# RESPONSE FACTORS, relative to isobutylene = 1.00.
#
# Different chemicals ionise more or less easily, so they give different signal
# for the same concentration. To convert:
#
#     ppm_of_that_compound = ppm_isobutylene_reading * factor
#
# Read the numbers this way: ethanol's factor of 10.0 means ethanol ionises
# POORLY -- it produces a weak signal, so a small reading actually represents a
# lot of ethanol. Hexane at 4.3 is similar. Benzene at 0.55 ionises EASILY, so
# it over-reports and you scale down.
#
# !! ONLY VALID FOR A SINGLE, KNOWN, PURE COMPOUND !!
# For a landfill's unknown mixture there is no correct single factor. Leave the
# response factor at 1.0 and report isobutylene equivalent.
# -----------------------------------------------------------------------------
RESPONSE_FACTORS: Mapping[str, float] = {
    "isobutylene": 1.00,     # the calibration reference, by definition
    "benzene": 0.55,
    "toluene": 0.55,
    "xylene": 0.45,
    "hexane": 4.30,
    "acetone": 1.10,
    "ethanol": 10.0,
    "vinyl_chloride": 2.00,
}


# =============================================================================
#  SECTION 2: QUALITY FLAGS
# =============================================================================

class QC(Flag):
    """Labels describing anything suspicious about a reading.

    One variable holds any combination, because each flag is a separate bit.
    Combine with `|`, test with `&`.
    """
    OK = 0                          # nothing wrong

    ADC_RAILED = auto()             # converter pinned at its limit: value is fiction
    ADC_NEAR_RAIL = auto()          # within 2 % of the limit: no headroom left
    OUT_OF_OUTPUT_RANGE = auto()    # voltage outside the sensor's own 0.040-2.85 V
                                    # window, so the fault is the sensor or wiring

    TEMP_OUT_OF_SPEC = auto()       # outside -40..65 C
    RH_OUT_OF_SPEC = auto()         # above 95 % humidity, or condensing

    WARMUP = auto()                 # still inside the 5-minute stabilisation window
                                    # after power-on; the reading has not settled

    BASELINE_UNTRACKED = auto()     # using a fixed factory zero rather than
                                    # following the drift (see BaselineTracker)
    BASELINE_OUT_OF_SPEC = auto()   # the tracked zero has wandered outside the
                                    # 40-100 mV window -- usually means the lamp
                                    # is failing or the window needs cleaning

    NEGATIVE = auto()               # below zero: the zero estimate is too high
    ABOVE_LINEAR_RANGE = auto()     # past 40 ppm, where response stops being linear
    RESPONSE_FACTOR_APPLIED = auto()  # a factor other than 1.0 was used, so this
                                      # is NOT an isobutylene-equivalent number

#: Flags meaning "do not put this value in an official dataset".
FATAL = QC.ADC_RAILED | QC.OUT_OF_OUTPUT_RANGE | QC.TEMP_OUT_OF_SPEC | QC.WARMUP


def describe(flags: QC) -> str:
    """Turn flags into readable text, e.g. 'WARMUP|BASELINE_UNTRACKED'."""
    if not flags:
        return "OK"
    return "|".join(f.name for f in QC if f is not QC.OK and f & flags)


# =============================================================================
#  SECTION 3: THE NUMBERS UNIQUE TO YOUR PARTICULAR SENSOR
# =============================================================================

@dataclass(frozen=True)
class Calibration:
    """Constants for ONE specific physical sensor.

    Two of these come from calibrating the actual unit, not from the datasheet:

      * mv_per_ppm has a +-10 % spread between units. Find yours by feeding the
        sensor a known concentration of isobutylene and seeing what it outputs.

      * v_zero_mv is only a starting point. It drifts. See BaselineTracker.

    t_cal_c and p_cal_hpa record the temperature and pressure DURING that
    calibration, because the density correction later has to be measured
    relative to those exact conditions.
    """
    serial: str
    mv_per_ppm: float = NOMINAL_MV_PER_PPM   # from YOUR span calibration
    v_zero_mv: float = 70.0                  # midpoint of the datasheet 40-100 mV
    t_cal_c: float = 25.0                    # temperature when you span-calibrated
    p_cal_hpa: float = 1013.25               # pressure when you span-calibrated
    response_factor: float = 1.0             # 1.0 = report isobutylene equivalent

    # How your logger's ADC chip is configured.
    adc_fsr_v: float = 4.096     # must be bigger than 2.85 V, the sensor's maximum
    adc_bits: int = 16
    adc_bipolar: bool = False    # the PID output is always positive, so unlike the
                                 # electrochemical sensors, unipolar is fine here

    @property
    def ppb_per_count(self) -> float:
        """The smallest concentration step this ADC can resolve, in ppb.

        WHY YOU SHOULD CHECK THIS NUMBER:
        The datasheet boasts a 1 ppb minimum detection level. But your converter
        might not be able to see that finely. Work it through:

            55 mV per ppm  =  0.055 mV per ppb  =  55 microvolts per ppb

            A 16-bit converter on a 4.096 V range has steps of
                4.096 V / 32767 = 0.000125 V = 125 microvolts

            125 / 55 = about 2.3 ppb per step

        So the ELECTRONICS, not the sensor, set your resolution at roughly
        2.3 ppb. The sensor's 1 ppb capability is wasted.

        Fixes: average many samples together (averaging 16 readings roughly
        halves the noise and recovers resolution), use a smaller full-scale
        range, or fit a 24-bit converter.
        """
        full = (1 << (self.adc_bits - 1)) - 1
        lsb_mv = self.adc_fsr_v / full * 1000.0      # millivolts per ADC step
        return lsb_mv / self.mv_per_ppm * 1000.0     # -> ppb per ADC step


# =============================================================================
#  SECTION 4: WHAT A RESULT LOOKS LIKE
# =============================================================================

@dataclass
class Reading:
    """One converted measurement, plus everything needed to judge it."""

    ppm: float                       # the answer, after any response factor
    flags: QC = QC.OK                # anything suspicious
    ppm_isobutylene: float = 0.0     # the answer BEFORE the response factor.
                                     # Always keep this: it is the raw,
                                     # convention-standard number, and if you
                                     # later learn the real mixture you can
                                     # re-derive from it.
    v_out: float = 0.0               # the measured voltage, volts
    v_zero_used_mv: float = 0.0      # which zero was subtracted. Log this --
                                     # watching it climb over months is how you
                                     # see the lamp ageing.
    density_factor: float = 1.0      # the temperature/pressure correction applied
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        """False when this value must not enter an official dataset."""
        return not bool(self.flags & FATAL)

    def ugm3(self, t_ref_c: float = 25.0, p_ref_hpa: float = 1013.25) -> float:
        """Convert to micrograms per cubic metre, AS ISOBUTYLENE.

        Only meaningful if you are reporting isobutylene equivalent. A real
        landfill mixture does not have isobutylene's molar mass of 56.1 g/mol,
        so for a genuine mixture this number is decorative -- use it only if
        your reporting format demands a mass unit, and say what it assumes.

        At 25 C and standard pressure: 1 ppm isobutylene = 2293 ug/m3.
        """
        return (self.ppm_isobutylene * 1000.0) * MW_ISOBUTYLENE \
            * (p_ref_hpa * 100.0) / (R_GAS * (t_ref_c + 273.15)) * 1e-3

    def __str__(self) -> str:
        mark = "" if self.usable else "  [UNUSABLE]"
        return "%9.4f ppm  %s%s" % (self.ppm, describe(self.flags), mark)


# =============================================================================
#  SECTION 5: FOLLOWING THE DRIFTING ZERO
# =============================================================================

class BaselineTracker:
    """Estimates the sensor's current clean-air zero by watching its history.

    THE PROBLEM: the zero drifts as the lamp dims and the window fouls, so the
    factory value goes stale.

    THE IDEA: keep the last N readings. Assume that at SOME point during that
    window the air was roughly clean. Then the lowest readings in the buffer
    must correspond to clean air -- so they ARE the zero.

    We take the 5th percentile rather than the outright minimum, because the
    single lowest reading might just be electrical noise. "5th percentile"
    means: sort every reading, then take the one 5 % of the way up from the
    bottom. Out of 1440 readings that is the 72nd lowest -- low, but not a
    freak value.

        window_samples=1440 at one reading per minute = 24 hours of history.

    *** THE ASSUMPTION THAT CAN BREAK THIS ***
    "At some point the air was clean." At a site that NEVER clears -- a landfill
    fence line during a long still spell, for instance -- the 5th percentile is
    not clean air, it is just the quietest polluted air. The tracker then
    subtracts real signal and silently under-reports.

    That failure is visible in this file's own self-test, where the tracked
    baseline scores WORSE than the static zero for exactly this reason.

    THE PROPER ALTERNATIVE: a solenoid valve that periodically draws air through
    a charcoal filter, which strips out VOCs. Whatever the sensor reads then is
    a genuine measured zero, not an assumption. Slower, more hardware, more
    maintenance -- and the only version that survives an audit. Use this class
    when that is not justified, or as a cross-check against it.
    """

    def __init__(self, window_samples: int = 1440, percentile: float = 0.05,
                 initial_mv: float = 70.0, min_samples: int = 60) -> None:
        # deque with maxlen automatically discards the oldest item when full,
        # which is exactly the rolling-window behaviour we want.
        self.buf: Deque[float] = deque(maxlen=window_samples)
        self.percentile = percentile        # 0.05 = 5th percentile
        self.initial_mv = initial_mv        # used until enough history exists
        self.min_samples = min_samples      # below this, do not trust the estimate

    def update(self, v_mv: float) -> None:
        """Add the latest raw reading to the history."""
        self.buf.append(v_mv)

    @property
    def zero_mv(self) -> float:
        """The current best estimate of the clean-air zero."""
        if len(self.buf) < self.min_samples:
            return self.initial_mv          # not enough history yet
        ordered = sorted(self.buf)
        index = int(self.percentile * len(ordered))
        return ordered[min(len(ordered) - 1, index)]

    @property
    def settled(self) -> bool:
        """True once enough readings have accumulated to trust the estimate."""
        return len(self.buf) >= self.min_samples


# =============================================================================
#  SECTION 6: SMALL HELPER
# =============================================================================

def counts_to_v(counts: float, cal: Calibration) -> Tuple[float, QC]:
    """Turn the ADC's whole number back into VOLTS (not millivolts here).

    Same idea as the other sensors: voltage = counts * range / 32767, plus a
    check for the converter hitting its limit and silently staying there.
    """
    full = (1 << (cal.adc_bits - 1)) - 1
    lo = -full if cal.adc_bipolar else 0
    flags = QC.OK
    if counts >= full or counts <= lo:
        flags |= QC.ADC_RAILED
    elif counts >= full * 0.98:
        flags |= QC.ADC_NEAR_RAIL
    return counts / full * cal.adc_fsr_v, flags


# =============================================================================
#  SECTION 7: THE MAIN ALGORITHM
# =============================================================================

def convert(
    counts: float,
    head_t_c: float,
    p_hpa: float,
    cal: Calibration,
    *,
    baseline: Optional[BaselineTracker] = None,
    rh_pct: Optional[float] = None,
    seconds_since_power_on: Optional[float] = None,
) -> Reading:
    """Convert raw ADC counts into VOC concentration in ppm.

    ARGUMENTS
      counts     - the reading from the ADC
      head_t_c   - temperature in Celsius AT THE SENSOR. Required, not optional.
      p_hpa      - barometric pressure in hectopascals. Also required.
      cal        - the Calibration for this specific sensor
      baseline   - optional BaselineTracker to follow the drifting zero

    WHY TEMPERATURE AND PRESSURE ARE REQUIRED:
    See "PROBLEM 3" in the file header. The signal depends on how many
    molecules are in the sensor's chamber, and that depends on temperature and
    pressure as well as concentration. It is a +-13 % effect across the
    operating range. Guessing "about 25 C" throws away accuracy you paid for.

    If you have no pressure sensor, passing the standard 1013.25 is better than
    nothing but adds a few percent of error -- fit a cheap BME280 at the head
    and get temperature, pressure and humidity together.
    """
    flags = QC.OK

    # --- STEP 1: ADC counts -> volts -----------------------------------------
    v_out, f = counts_to_v(counts, cal)
    flags |= f
    v_mv = v_out * 1000.0                    # also keep it in millivolts

    # --- STEP 2: sanity checks on sensor and environment ---------------------
    if not (OUT_MIN_V <= v_out <= OUT_MAX_V):
        # Outside the window the sensor is specified to produce at all. This is
        # a hardware or wiring fault, not an air quality reading.
        flags |= QC.OUT_OF_OUTPUT_RANGE
    if not (T_SPEC_MIN <= head_t_c <= T_SPEC_MAX):
        flags |= QC.TEMP_OUT_OF_SPEC
    if rh_pct is not None and rh_pct > RH_SPEC_MAX:
        flags |= QC.RH_OUT_OF_SPEC
    if seconds_since_power_on is not None and seconds_since_power_on < STABILISATION_S:
        # Datasheet: 5 minutes to full stabilisation. Readings before that are
        # the sensor settling, not the air changing.
        flags |= QC.WARMUP

    # --- STEP 3: decide what "clean air" reads right now ---------------------
    if baseline is None:
        # No tracker supplied, so use the fixed calibration value -- and warn,
        # because a factory zero does not survive lamp ageing.
        v_zero = cal.v_zero_mv
        flags |= QC.BASELINE_UNTRACKED
    else:
        baseline.update(v_mv)                # feed this reading into the history
        v_zero = baseline.zero_mv
        if not baseline.settled:
            flags |= QC.BASELINE_UNTRACKED   # not enough history yet

    if not (OFFSET_MIN_MV <= v_zero <= OFFSET_MAX_MV):
        # The zero has left the range the datasheet says it should occupy.
        # Usually the lamp is failing or the window needs cleaning.
        flags |= QC.BASELINE_OUT_OF_SPEC

    # --- STEP 4: subtract the zero, divide by sensitivity --------------------
    # This is the core conversion, and it is just a straight line:
    #     concentration = (measured voltage - clean air voltage) / (volts per ppm)
    ppm_iso = (v_mv - v_zero) / cal.mv_per_ppm

    # --- STEP 5: correct for air density (the ideal gas law) -----------------
    # Hot air is thinner, so the same ppm produces fewer ions and a smaller
    # signal. We scale the answer back up.
    #
    #   * (T_now / T_calibration) : hotter now than at calibration -> reading was
    #                               too low -> multiply up. T must be in KELVIN,
    #                               hence the +273.15; you cannot take a ratio of
    #                               Celsius values, since 0 C is not "no heat".
    #   * (P_calibration / P_now) : higher pressure now -> air denser -> reading
    #                               was too high -> divide back down.
    density = ((head_t_c + 273.15) / (cal.t_cal_c + 273.15)) * (cal.p_cal_hpa / p_hpa)
    ppm_iso *= density

    # --- STEP 6: apply the response factor, if the compound is known ---------
    ppm = ppm_iso * cal.response_factor
    if abs(cal.response_factor - 1.0) > 1e-9:
        # Someone converted to a specific compound. Flag it, because the number
        # is no longer the standard isobutylene-equivalent value and cannot be
        # compared against one without knowing the factor used.
        flags |= QC.RESPONSE_FACTOR_APPLIED

    # --- STEP 7: sanity-check the answer -------------------------------------
    if ppm_iso < -0.05:
        # Negative VOC is impossible, so the zero estimate is too high --
        # usually a stale factory zero, or a baseline tracker that has learnt
        # a zero from polluted air.
        flags |= QC.NEGATIVE
    if ppm_iso > LINEAR_RANGE_PPM:
        # Beyond 40 ppm the response curve bends, so the straight-line maths in
        # step 4 under-reports. The reading is still indicative, not accurate.
        flags |= QC.ABOVE_LINEAR_RANGE

    return Reading(ppm=ppm, flags=flags, ppm_isobutylene=ppm_iso, v_out=v_out,
                   v_zero_used_mv=v_zero, density_factor=density,
                   diagnostics={"v_mv": v_mv, "head_t_c": head_t_c, "p_hpa": p_hpa})


# =============================================================================
#  SECTION 8: SELF-TEST
# =============================================================================
# Run this file directly:
#
#     python pidx_a040.py
#
# It converts a test dataset whose true answers are known, using BOTH zeroing
# strategies, so you can compare them directly.
#
# Watch BIAS -- the average error -- which should be near zero if the maths is
# right. And read the note printed at the end about why the "smarter" baseline
# tracker performs worse here. That result is real and instructive.
# =============================================================================

def _self_test(fixture: str, calfile: str) -> int:
    import csv
    import statistics as st

    # --- load this test sensor's calibration constants -----------------------
    consts: Dict[str, Dict[str, float]] = {}
    with open(calfile, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            consts.setdefault(row["sensor"], {})[row["param"]] = float(row["value"])

    c = consts["PIDX-A-040-A"]
    cal = Calibration(serial="PIDX-A-040-A", mv_per_ppm=c["mv_per_ppm"],
                      v_zero_mv=c["v_zero"], t_cal_c=c["t_cal"], p_cal_hpa=c["p_cal"])

    # --- convert every row twice: static zero, and tracked zero --------------
    errs_static, errs_tracked = [], []
    tracker = BaselineTracker(window_samples=1440, percentile=0.05,
                              initial_mv=c["v_zero"])
    rows = 0
    flagged = 0
    with open(fixture, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rows += 1
            t_c = float(row["head_t_c"])
            p = float(row["p_hpa"])
            truth = float(row["true_voc_ppm_iso"])
            n = float(row["voc_A_counts"])

            # Method 1: fixed factory zero.
            r = convert(n, t_c, p, cal, rh_pct=float(row["rh_pct"]))
            errs_static.append(r.ppm - truth)
            # Count flags other than the expected BASELINE_UNTRACKED.
            if r.flags & ~QC.BASELINE_UNTRACKED:
                flagged += 1

            # Method 2: rolling-percentile tracked zero.
            rt = convert(n, t_c, p, cal, baseline=tracker, rh_pct=float(row["rh_pct"]))
            errs_tracked.append(rt.ppm - truth)

    def stats(name, errs):
        bias = st.mean(errs)                                    # average error
        rms = math.sqrt(sum(e * e for e in errs) / len(errs))   # typical size
        p95 = sorted(abs(e) for e in errs)[int(0.95 * len(errs))]
        print("  %-22s bias=%+8.4f  rms=%7.4f  p95|err|=%7.4f ppm"
              % (name, bias, rms, p95))
        return bias, p95

    print("rows                : %d" % rows)
    print("quantisation floor  : %.2f ppb per count (datasheet MDL is 1 ppb)"
          % cal.ppb_per_count)
    print("flagged (non-zero)  : %d" % flagged)
    print()
    b1, p1 = stats("static factory zero", errs_static)
    b2, p2 = stats("tracked baseline", errs_tracked)

    ok = abs(b1) < 0.02 and p1 < 0.10
    print("\n%s" % ("PASS" if ok else "FAIL"))
    print("\nThe tracked baseline is worse here ONLY because the fixture's VOC")
    print("series rarely returns to clean air, so the 5th percentile still")
    print("contains signal. That is the tracker's documented failure mode, and")
    print("it is exactly what a landfill fence line looks like -- which is the")
    print("argument for a scrubbed zero-air reference rather than a percentile.")
    return 0 if ok else 1


if __name__ == "__main__":
    # Runs only when this file is executed directly, not when imported.
    import argparse
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    data = os.path.join(os.path.dirname(here), "sensor raw data")
    ap = argparse.ArgumentParser(description="PIDX-A-040 conversion self-test")
    ap.add_argument("--fixture", default=os.path.join(data, "aqms_raw_fixture.csv"))
    ap.add_argument("--cal", default=os.path.join(data, "aqms_raw_fixture_cal.csv"))
    args = ap.parse_args()
    raise SystemExit(_self_test(args.fixture, args.cal))
