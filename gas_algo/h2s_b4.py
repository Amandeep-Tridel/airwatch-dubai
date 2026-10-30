#!/usr/bin/env python3
# =============================================================================
#  ALPHASENSE H2S-B4  ->  HYDROGEN SULFIDE CONCENTRATION
# =============================================================================
#
#  WHAT IS THIS FILE?
#  ------------------
#  A gas sensor does not hand you a number like "300 parts per billion of
#  hydrogen sulfide". It hands you a tiny electrical signal. This file is the
#  translator: raw electrical signal in, real concentration out.
#
#
#  WHAT IS HYDROGEN SULFIDE (H2S)?
#  -------------------------------
#  A poisonous gas that smells like rotten eggs. It is produced when organic
#  matter rots without oxygen, which is why landfills, sewers and swamps make
#  it. We want to measure it in "parts per billion" (ppb): 1 ppb means that out
#  of every billion molecules of air, one of them is H2S. That is a very small
#  amount, which is why this is hard.
#
#
#  HOW DOES THE SENSOR WORK? (the chemistry, simply)
#  -------------------------------------------------
#  Inside the sensor is a small pot of liquid (an "electrolyte") with metal
#  plates (called "electrodes") dipped into it. Air diffuses in through a hole.
#
#  When an H2S molecule touches the main plate -- the WORKING ELECTRODE, or
#  "WE" -- a chemical reaction happens that releases electrons. Electrons
#  flowing is exactly what electric current is. So:
#
#         more H2S  ->  more reactions  ->  more current
#
#  The current is proportional to the concentration. That is the whole idea.
#
#
#  PROBLEM 1: THE CURRENT IS RIDICULOUSLY SMALL
#  --------------------------------------------
#  This sensor makes about 2 nanoamps per ppb of H2S. A nanoamp is one
#  billionth of an amp. For comparison, a phone charger delivers about 2 amps
#  -- a BILLION times more. You cannot plug something this small straight into
#  a computer.
#
#  So Alphasense sells a small circuit board called an ISB (Individual Sensor
#  Board). Its job is to convert current into voltage, which is much easier to
#  measure. The conversion rate is called the "gain", measured in millivolts
#  per nanoamp (mV/nA).
#
#  Then a chip called an ADC (Analogue to Digital Converter) measures that
#  voltage and turns it into a whole number ("counts") that software can read.
#  The full chain looks like this:
#
#     H2S gas -> current (nA) -> ISB -> voltage (mV) -> ADC -> counts (integer)
#                                                                    |
#                                   this file runs this direction <--+
#
#
#  PROBLEM 2: THE SENSOR LIES A LITTLE, EVEN IN CLEAN AIR
#  ------------------------------------------------------
#  Put this sensor in perfectly clean air and it still produces a small
#  current. This unwanted current is called the ZERO CURRENT or "background".
#  If you ignore it, clean air looks like polluted air.
#
#  Worse: the zero current CHANGES WITH TEMPERATURE. On a hot day it drifts by
#  about -140 nA compared to a 20 C day. At 2 nA per ppb, that is a fake
#  reading of about 70 ppb appearing out of nowhere just because the sun came
#  out. For a sensor trying to detect single-digit ppb, that is fatal.
#
#
#  THE CLEVER FIX: A SECOND ELECTRODE
#  ----------------------------------
#  The "B4" in the name means 4 electrodes. One of them is the AUXILIARY
#  ELECTRODE, or "AE". It is identical to the working electrode and sits in
#  the same liquid at the same temperature -- but it is buried where gas
#  cannot reach it.
#
#  So the AE feels the temperature but never sees the gas. It is a "control",
#  like in a school science experiment: change one thing, keep everything else
#  the same.
#
#     WE reading = real gas signal + temperature drift
#     AE reading =                   temperature drift
#     ------------------------------------------------ subtract
#     difference = real gas signal                       <-- what we want
#
#  It is not quite a straight subtraction, because the two electrodes drift by
#  slightly different amounts. So we scale the AE by a correction factor called
#  n_T (the "T" means it depends on temperature) before subtracting. Finding
#  the right n_T is what Alphasense Application Note AAN 803-05 is about, and
#  this file implements all four methods that note describes.
#
#
#  A WORKED EXAMPLE (follow the numbers)
#  -------------------------------------
#  Say the ADC reports WE = 12000 counts and AE = 4000 counts, at 25 C.
#
#    Step 1  counts -> millivolts. With a 2.048 V range on a 16-bit converter,
#            one count is about 0.0625 mV.
#               WE = 12000 * 0.0625 = 750.0 mV
#               AE =  4000 * 0.0625 = 250.0 mV
#
#    Step 2  Remove the board's own built-in offset. Alphasense deliberately
#            shifts the outputs up by 200-300 mV so that negative currents
#            still come out as positive voltages (an ADC often cannot read
#            below zero). Say the board label says WE_e = 250, AE_e = 250.
#               WE_net = 750.0 - 250 = 500.0 mV
#               AE_net = 250.0 - 250 =   0.0 mV
#
#    Step 3  Subtract the scaled AE to cancel temperature drift. Say n_T = 1.19
#            at 25 C.
#               WE_c = 500.0 - (1.19 * 0.0) = 500.0 mV
#
#    Step 4  Divide by sensitivity -- how many mV this particular sensor gives
#            per ppb. Say 0.112 mV/ppb (also adjusted for temperature).
#               concentration = 500.0 / 0.112 = 4464 ppb
#
#  That is the entire algorithm. Everything else in this file is either
#  bookkeeping or checking that the answer is trustworthy.
#
#
#  WHY THIS CODE NEVER RETURNS JUST A NUMBER
#  -----------------------------------------
#  Most ways this goes wrong are SILENT. If the ADC is maxed out, you still get
#  a number, and it still looks like a concentration. If the temperature is
#  outside what the sensor is rated for, you still get a number. Someone
#  reading a spreadsheet later cannot tell.
#
#  So every result comes back as a `Reading` object carrying QC ("quality
#  control") flags explaining anything suspicious, plus `.usable`, which is
#  False when the value should not go into an official record.
#
#
#  !! IMPORTANT WARNING BEFORE YOU USE THIS FOR REAL !!
#  ----------------------------------------------------
#  AAN 803-05 prints a table of n_T values for NO, NO2, OX, CO and SO2 sensors.
#  It has NO ROW FOR H2S. The note tells you to use algorithm 1 for this sensor
#  and then does not give you the numbers.
#
#  The table below is a PLACEHOLDER that I made up so the code runs and can be
#  tested. Any reading calculated with it is tagged COEFF_UNVERIFIED. Get the
#  real table from Alphasense, or measure it yourself by putting the sensor in
#  clean air inside a temperature-controlled box, before trusting field data.
#
#  Standalone file. Uses only Python's built-in library. Python 3.9 or newer.
# =============================================================================

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Flag, auto
from typing import Mapping, Optional, Sequence


# =============================================================================
#  SECTION 1: NUMBERS COPIED FROM THE DATASHEET
# =============================================================================
# A "datasheet" is the manufacturer's official specification document. These
# constants all come from "H2S-B4 Technical Specifications v1.1".

# -----------------------------------------------------------------------------
# How the sensor's SENSITIVITY changes with temperature.
#
# Sensitivity means "how much signal per unit of gas". It is not constant --
# the chemical reaction speeds up when warm and slows down when cold, exactly
# like how food spoils faster in summer.
#
# The numbers are FRACTIONS of the sensitivity at 20 C. So:
#     1.00 at 20 C  = the reference point, by definition
#     0.835 at -20 C = at minus 20 degrees the sensor only gives 83.5 % as much
#                      signal for the same amount of gas
#     1.05 at 50 C   = at 50 degrees it gives 105 %, slightly more
#
# If we did not correct for this, the same gas would read differently on a hot
# day than a cold one.
#
# Where these came from: the datasheet states "Sensitivity @ -20 C: 77 to 90 %"
# and "@ 50 C: 100 to 110 %". I took the middle of each range and followed the
# curve shape in the datasheet's Figure 1 for the points in between.
# -----------------------------------------------------------------------------
SENSITIVITY_TC: Mapping[float, float] = {
    # temperature (C) : fraction of the 20 C sensitivity
    -30: 0.75,
    -20: 0.835,
    -10: 0.88,
      0: 0.92,
     10: 0.96,
     20: 1.00,     # reference temperature
     30: 1.03,
     40: 1.055,
     50: 1.05,
}

# -----------------------------------------------------------------------------
# The n_T correction factor for Algorithm 1  -- *** PLACEHOLDER, NOT REAL ***
#
# What n_T actually means physically: it is the ratio between how much the
# working electrode drifts and how much the auxiliary electrode drifts, at a
# given temperature.
#
#     n_T = (total WE zero) / (total AE zero)
#
# If both drifted by exactly the same amount, n_T would be 1.0 and we could
# just subtract AE directly. In reality the WE drifts a bit more, so n_T is
# slightly above 1.
#
# READ THE BIG WARNING IN THE FILE HEADER. These specific numbers are invented.
#
# UPDATE 2026-10-29: a secondary copy of AAN 803 lists an H2S-B4 algorithm-1
# row (-0.6, -0.6, 0.1, 0.8, -0.7, -2.5, -2.5, -2.2, -1.8 for -30..50 C). The
# AQMS server now passes that table in via config.json (n_t_table); this
# placeholder is only the fallback. Verify against the official AAN 803-05.
# -----------------------------------------------------------------------------
N_T_PLACEHOLDER: Mapping[float, float] = {
    -30: 1.45,
    -20: 1.42,
    -10: 1.36,
      0: 1.30,
     10: 1.24,
     20: 1.20,
     30: 1.18,
     40: 1.22,
     50: 1.32,
}

# The temperature range the manufacturer guarantees the sensor works in.
# Outside this, readings are not covered by the specification at all.
T_SPEC_MIN, T_SPEC_MAX = -30.0, 50.0

# -----------------------------------------------------------------------------
# CROSS-SENSITIVITY: when the sensor reacts to the WRONG gas.
#
# The chemistry is not perfectly picky. Other gases can react at the working
# electrode too, and the sensor cannot tell the difference -- it just sees
# current. So if sulfur dioxide (SO2) is in the air, some of what looks like
# H2S is actually SO2.
#
# These numbers say how strongly. A value of 0.20 for SO2 means:
#     "100 ppb of SO2 produces a fake signal that looks like 20 ppb of H2S"
#
# A NEGATIVE value means that gas pushes the reading DOWN instead of up.
#
# To remove the error you must measure the interfering gas separately and
# subtract its contribution. That is why `compute()` accepts `interferents_ppb`.
#
# Caution: the datasheet gives these as upper bounds ("less than 20 %"), not
# exact measurements. Over-correcting is just as wrong as not correcting, so
# measure the real value for YOUR sensor if it matters.
# -----------------------------------------------------------------------------
CROSS_SENSITIVITY_DEFAULT: Mapping[str, float] = {
    "so2": 0.20,    # datasheet: < 20 % at 5 ppm -- the one that matters on a landfill
    "no2": -0.10,   # datasheet: < -10 % at 5 ppm (negative: pushes reading down)
    "no": 0.12,     # datasheet: < 12 % at 5 ppm
    "co": 0.03,     # datasheet: < 3 % at 5 ppm
}

# Physical constants, used only for converting ppb into micrograms per cubic metre.
MW_H2S = 34.081     # molar mass of H2S in grams per mole
R_GAS = 8.31446     # the universal gas constant, in J/(mol*K)


# =============================================================================
#  SECTION 2: QUALITY FLAGS
# =============================================================================

class QC(Flag):
    """Labels describing anything suspicious about a reading.

    This is a "Flag" enum, which is a neat Python trick: each flag is a
    different bit, so ONE variable can hold ANY COMBINATION of them at once.
    You combine them with `|` and test with `&`:

        flags = QC.ADC_RAILED | QC.NEGATIVE      # set two flags
        if flags & QC.NEGATIVE:                  # test for one
            print("the answer came out below zero")

    QC.OK equals zero, and zero is falsy in Python, so `if reading.flags:`
    simply means "is there anything wrong at all?".
    """
    OK = 0                       # nothing wrong

    # --- is the maths trustworthy? -------------------------------------------
    COEFF_UNVERIFIED = auto()    # using the made-up correction table, not a real one

    # --- did the electronics behave? -----------------------------------------
    ADC_RAILED = auto()          # converter maxed out or bottomed out.
                                 # Like a bathroom scale that only goes to 100 kg:
                                 # a 150 kg person still reads "100". The number
                                 # exists but it is meaningless.
    ADC_NEAR_RAIL = auto()       # within 2 % of the limit -- no headroom left,
                                 # the next gust of gas will max it out

    # --- was the environment inside spec? ------------------------------------
    TEMP_OUT_OF_SPEC = auto()    # hotter or colder than the sensor is rated for
    TEMP_EXTRAPOLATED = auto()   # temperature is outside our correction table, so
                                 # we clamped to the nearest edge rather than
                                 # inventing a value by guessing past the end

    # --- did the temperature correction misbehave? ---------------------------
    OVERCOMPENSATED = auto()     # the AE subtraction was bigger than the signal
                                 # itself, so we subtracted away the answer
    AE_SIGN_ANOMALY = auto()     # the two electrode zeros have opposite signs,
                                 # which AAN 803-05 warns breaks algorithms 2 and 3

    # --- does the final answer look sane? ------------------------------------
    NEGATIVE = auto()            # below zero by more than noise explains. You
                                 # cannot have negative gas, so something is off
    ABOVE_LINEAR_RANGE = auto()  # past 100 ppm, where the manufacturer's accuracy
                                 # warranty stops applying


@dataclass(frozen=True)
class CoefficientSet:
    """Holds the temperature-correction tables, one per algorithm.

    WHY THIS CLASS EXISTS -- it prevents a real bug I hit while writing this.

    Each of the four algorithms needs its OWN table of correction numbers:
    algorithm 1 uses n_T, algorithm 2 uses k_T, algorithm 3 uses k'_T and
    algorithm 4 uses k''_T. They are completely different kinds of number:
    n_T is a plain ratio around 1.0, while k''_T is a voltage offset with
    values in the hundreds.

    If you accidentally hand algorithm 4 the n_T table, Python will happily do
    the arithmetic and give you a confident, wrong answer. Nothing crashes.

    So instead of passing a bare table around, you pass this object, and it
    hands out the correct table for the algorithm being run -- or raises an
    error if you never supplied one. A loud crash beats a quiet wrong number.

    `source` is free text. Fill it in honestly: once this file leaves your
    computer it is the only record of where these numbers came from.
    """
    source: str                                      # where did these come from?
    verified: bool = False                           # True ONLY if Alphasense published them
    n_t: Optional[Mapping[float, float]] = None      # for algorithm 1
    k_t: Optional[Mapping[float, float]] = None      # for algorithm 2
    k3_t: Optional[Mapping[float, float]] = None     # for algorithm 3 (k'_T)
    k4_t: Optional[Mapping[float, float]] = None     # for algorithm 4 (k''_T)

    def table_for(self, algorithm: int) -> Mapping[float, float]:
        """Return the right table for this algorithm, or refuse."""
        table = {1: self.n_t, 2: self.k_t, 3: self.k3_t, 4: self.k4_t}[algorithm]
        if table is None:
            raise ValueError(
                "CoefficientSet %r has no table for algorithm %d. AAN 803-05 "
                "gives each algorithm its own coefficients; they cannot be "
                "substituted for one another." % (self.source, algorithm))
        return table


#: The default. Deliberately marked unverified so every reading gets flagged.
PLACEHOLDER_COEFFS = CoefficientSet(
    source="PLACEHOLDER - fixture ground truth, not Alphasense data",
    verified=False,
    n_t=N_T_PLACEHOLDER,
)


# =============================================================================
#  SECTION 3: THE NUMBERS THAT ARE UNIQUE TO YOUR PARTICULAR SENSOR
# =============================================================================

@dataclass(frozen=True)
class Calibration:
    """Constants for ONE specific physical sensor, identified by serial number.

    THIS IS THE PART PEOPLE GET WRONG. Two H2S-B4 sensors from the same box are
    NOT interchangeable. The datasheet says sensitivity is "1450 to 2600 nA/ppm"
    -- that is nearly a factor of two between the weakest and strongest unit.
    Using an average would put you ~30 % out.

    Every number below is printed on paperwork that shipped with your hardware:

      * WE_e and AE_e are on the ISB circuit board's label. They are the
        board's own offset with NO SENSOR attached -- purely electronic.
        Alphasense sets these between 200 and 300 mV on purpose, so that a
        slightly negative sensor current still comes out as a positive voltage
        the ADC can read.

      * WE_0 and AE_0 are on the SENSOR's calibration certificate. They are
        what this particular sensor outputs in clean air at 20 C.

      * sens_mv_per_ppb is also on the certificate: millivolts per ppb.

    Some certificates quote "Total zero offsets" (WE_T, AE_T) instead, which
    are simply the electronic and sensor offsets added together. Pass those and
    the code works out the sensor part for you.

    KEEP THESE IN A FILE, NOT IN YOUR CODE. When you swap a sensor you change
    the file. If these are hard-coded, swapping a sensor means editing and
    redeploying software, and sooner or later someone forgets.
    """
    serial: str                    # which physical sensor this describes
    we_e: float                    # mV  - board electronic zero, working electrode
    ae_e: float                    # mV  - board electronic zero, auxiliary electrode
    sens_mv_per_ppb: float         # mV per ppb at 20 C

    # Supply EITHER these two ...
    we_0: Optional[float] = None   # mV  - this sensor's own zero, WE, in clean air
    ae_0: Optional[float] = None   # mV  - this sensor's own zero, AE, in clean air
    # ... OR these two (totals = electronic + sensor), and we derive the above.
    we_t: Optional[float] = None   # mV  - total zero offset, WE
    ae_t: Optional[float] = None   # mV  - total zero offset, AE

    # How your logger's ADC chip is wired up.
    adc_fsr_v: float = 2.048       # "full scale range": the biggest voltage it reads
    adc_bits: int = 16             # resolution: 16 bits = 65536 possible values
    adc_bipolar: bool = True       # True means it can read negative voltages too.
                                   # Electrochemical channels NEED this, because
                                   # the signal can legitimately go below zero.

    def sensor_zeros(self) -> tuple[float, float]:
        """Work out WE_0 and AE_0, whichever way the user supplied them.

        Total offset = electronic offset + sensor offset
        so            sensor offset = total offset - electronic offset
        """
        we = self.we_0 if self.we_0 is not None else (
            (self.we_t - self.we_e) if self.we_t is not None else 0.0)
        ae = self.ae_0 if self.ae_0 is not None else (
            (self.ae_t - self.ae_e) if self.ae_t is not None else 0.0)
        return we, ae


# =============================================================================
#  SECTION 4: WHAT A RESULT LOOKS LIKE
# =============================================================================

@dataclass
class Reading:
    """One converted measurement, plus everything needed to judge whether to
    trust it. Deliberately NOT a plain float -- see the file header."""

    ppb: float                     # the answer, in parts per billion
    flags: QC = QC.OK              # anything suspicious (see the QC class)
    we_c_mv: float = 0.0           # the corrected voltage, before dividing.
                                   # Useful when debugging: if this is wild,
                                   # the problem is upstream of the sensitivity.
    sens_mv_per_ppb: float = 0.0   # the temperature-adjusted sensitivity used
    coeff: float = 0.0             # the n_T (or k_T etc.) value used
    algorithm: int = 1             # which of the four algorithms ran

    @property
    def usable(self) -> bool:
        """False when this value must NOT go into an official dataset.

        Some flags are warnings you can live with (NEGATIVE might just be noise
        around zero on a clean day). These three are not: a maxed-out converter,
        an out-of-spec temperature, or a broken temperature correction all mean
        the number is fiction.
        """
        fatal = QC.ADC_RAILED | QC.TEMP_OUT_OF_SPEC | QC.OVERCOMPENSATED
        return not bool(self.flags & fatal)

    def ugm3(self, t_ref_c: float = 25.0, p_ref_hpa: float = 1013.25) -> float:
        """Convert ppb into micrograms per cubic metre (ug/m3).

        WHY TWO UNITS EXIST:
          * ppb counts MOLECULES -- one H2S molecule per billion air molecules.
          * ug/m3 weighs MASS -- micrograms of H2S in a cubic metre of air.

        Air expands when heated, so one cubic metre of hot air holds fewer
        molecules than one cubic metre of cold air. That means the conversion
        between the two depends on temperature and pressure.

        *** THE CLASSIC BUG ***  Use the REFERENCE conditions your regulations
        specify here, NOT the actual outdoor conditions. Every country's air
        quality law fixes a standard temperature for reporting. Using 0 C
        instead of 25 C changes the published number by 8.4 %.

        At the common 25 C / 1013.25 hPa reference: 1 ppb of H2S = 1.393 ug/m3.
        """
        return ppb_to_ugm3(self.ppb, t_ref_c, p_ref_hpa)


# =============================================================================
#  SECTION 5: SMALL HELPER FUNCTIONS
# =============================================================================

def interpolate(table: Mapping[float, float], t_c: float) -> tuple[float, bool]:
    """Look up a value in a table, estimating between the listed points.

    Our tables only list values every 10 degrees: -30, -20, -10, 0, 10, ...
    But the real temperature might be 23.7 C. "Linear interpolation" draws a
    straight line between the two nearest entries and reads off the middle.

    Example: the table has 20 C -> 1.20 and 30 C -> 1.18. At 23 C we are
    3/10 = 30 % of the way from 20 to 30, so:
        1.20 + 0.30 * (1.18 - 1.20) = 1.20 - 0.006 = 1.194

    Returns TWO things: (value, was_it_outside_the_table).

    If the temperature is beyond the ends of the table we CLAMP -- we use the
    closest edge value and report that we did. We deliberately do NOT continue
    the line outwards ("extrapolation"), because past the last measured point
    nobody knows what the curve does. Extending a line into the unknown is how
    you produce a confident number that is completely wrong.
    """
    keys = sorted(table)                        # temperatures, lowest first
    if t_c <= keys[0]:                          # colder than the whole table
        return table[keys[0]], t_c < keys[0]
    if t_c >= keys[-1]:                         # hotter than the whole table
        return table[keys[-1]], t_c > keys[-1]

    hi = next(k for k in keys if k >= t_c)      # first listed temp at or above ours
    lo = keys[keys.index(hi) - 1]               # the one just below it
    if hi == lo:                                # exact match on a table entry
        return table[lo], False

    frac = (t_c - lo) / (hi - lo)               # how far between lo and hi, 0..1
    return table[lo] + frac * (table[hi] - table[lo]), False


def counts_to_mv(counts: float, cal: Calibration) -> tuple[float, QC]:
    """Turn the ADC's whole number back into the voltage it represents.

    An ADC splits its voltage range into equal steps. A 16-bit bipolar
    converter has 32767 steps in each direction. So with a 2.048 V range:

        one step = 2.048 V / 32767 = 0.0000625 V = 0.0625 mV

    and therefore:  voltage = counts * (range / 32767)

    RAIL DETECTION -- why this matters so much:
    If the real voltage is higher than the converter can represent, the output
    simply stops at the maximum. It does not warn you. You get a number that
    looks fine and is wrong.

    This bites hard on H2S. An ISB set up for ppb-level air quality work runs
    out of range at roughly 1.4 ppm. A landfill fence line regularly exceeds
    that. Without this check you would log a flat line and call it data.
    """
    full = (1 << (cal.adc_bits - 1)) - 1        # 2^15 - 1 = 32767 for 16 bits
    lo = -full if cal.adc_bipolar else 0        # lowest readable count
    flags = QC.OK

    if counts >= full or counts <= lo:
        flags |= QC.ADC_RAILED                  # pinned at a limit: meaningless
    elif counts >= full * 0.98 or (cal.adc_bipolar and counts <= lo * 0.98):
        flags |= QC.ADC_NEAR_RAIL               # within 2 %: about to pin

    millivolts = counts / full * cal.adc_fsr_v * 1000.0
    return millivolts, flags


def ppb_to_ugm3(ppb: float, t_ref_c: float = 25.0, p_ref_hpa: float = 1013.25) -> float:
    """Concentration by molecule-count -> concentration by mass.

    This is the ideal gas law, PV = nRT, rearranged. The density of air in
    moles per cubic metre is P / (R * T). Multiply by the molar mass to get
    grams, then scale the units.

    Sanity check: at 25 C and standard pressure this returns 1.393 for 1 ppb.
    """
    return ppb * MW_H2S * (p_ref_hpa * 100.0) / (R_GAS * (t_ref_c + 273.15)) * 1e-3


# =============================================================================
#  SECTION 6: THE MAIN ALGORITHM
# =============================================================================

def compute(
    we_counts: float,
    ae_counts: float,
    head_t_c: float,
    cal: Calibration,
    *,
    algorithm: int = 1,
    coeffs: CoefficientSet = PLACEHOLDER_COEFFS,
    sensitivity_tc: Mapping[float, float] = SENSITIVITY_TC,
    interferents_ppb: Optional[Mapping[str, float]] = None,
    cross_sensitivity: Mapping[str, float] = CROSS_SENSITIVITY_DEFAULT,
) -> Reading:
    """Convert raw ADC counts into H2S concentration in ppb.

    ARGUMENTS
      we_counts  - working electrode reading from the ADC (a whole number)
      ae_counts  - auxiliary electrode reading from the ADC
      head_t_c   - temperature in Celsius, measured AT THE SENSOR (see below)
      cal        - the Calibration for this specific sensor
      algorithm  - which of the four AAN 803-05 methods to use, 1 to 4

    WHICH ALGORITHM SHOULD I USE?
    AAN 803-05 Table 2 suggests algorithm 1 for H2S B-type sensors, with 2 as
    the backup. But the note openly invites you to try all four and keep
    whichever gives the flattest reading in clean air across your temperature
    range. You find that out by experiment -- putting the sensor in clean air
    in a temperature-controlled box and seeing which method stays closest to
    zero -- not by reading a docstring.

    *** WHERE YOU MEASURE TEMPERATURE MATTERS ENORMOUSLY ***
    AAN 803-05 says the temperature must be measured at the TOP OF THE SENSOR,
    accurate to within 1 degree. The temperature of the circuit board in the
    logger box is NOT good enough. If the sensor head hangs in outdoor air
    while the electronics sit in an enclosure cooled some other way, those two
    temperatures can differ by several degrees -- and every correction in this
    function would then be applied wrongly, in a way that follows the sun.
    """
    # --- check the request makes sense ---------------------------------------
    if algorithm not in (1, 2, 3, 4):
        raise ValueError("algorithm must be 1, 2, 3 or 4 (AAN 803-05 Table 1)")

    # Ask the CoefficientSet for the table matching this algorithm. If the
    # caller never supplied one, this raises rather than guessing.
    table = coeffs.table_for(algorithm)

    flags = QC.OK
    if not coeffs.verified:
        # Warn on EVERY reading that the correction numbers are not official.
        flags |= QC.COEFF_UNVERIFIED

    # --- STEP 1: ADC counts -> millivolts ------------------------------------
    we_u, f = counts_to_mv(we_counts, cal)      # "u" = uncorrected, AAN 803 naming
    flags |= f                                   # keep any rail warnings
    ae_u, f = counts_to_mv(ae_counts, cal)
    flags |= f

    # --- STEP 2: is the environment within specification? --------------------
    if not (T_SPEC_MIN <= head_t_c <= T_SPEC_MAX):
        flags |= QC.TEMP_OUT_OF_SPEC

    # --- STEP 3: look up the temperature correction factor -------------------
    coeff, extrapolated = interpolate(table, head_t_c)
    if extrapolated:
        flags |= QC.TEMP_EXTRAPOLATED

    # --- STEP 4: remove the circuit board's built-in offset ------------------
    # What is left is the sensor's genuine contribution, in millivolts.
    we_net = we_u - cal.we_e
    ae_net = ae_u - cal.ae_e
    we_0, ae_0 = cal.sensor_zeros()             # this sensor's clean-air readings

    # --- STEP 5: cancel the temperature drift --------------------------------
    # These are the four equations from AAN 803-05, Table 1, exactly as printed.
    # They all do the same job -- remove the background -- but combine the
    # available numbers differently, and each fails in a different situation.

    if algorithm == 1:
        # The straightforward one: scale the auxiliary electrode and subtract.
        #     WE_c = (WE_u - WE_e) - n_T * (AE_u - AE_e)
        we_c = we_net - coeff * ae_net

    elif algorithm == 2:
        # A refinement: instead of one scaling number, use the ratio of the two
        # electrodes' clean-air zeros, tuned by k_T.
        #     WE_c = (WE_u - WE_e) - k_T * (WE_0/AE_0) * (AE_u - AE_e)
        if abs(ae_0) < 1e-9:
            # Dividing by AE_0 explodes if AE_0 is zero. The note names this
            # exact failure and says to use algorithm 3 instead.
            raise ZeroDivisionError(
                "Algorithm 2 divides by AE_0, which is zero for %s. "
                "AAN 803-05 flags this case -- use algorithm 3." % cal.serial)
        if we_0 * ae_0 < 0:
            # Multiplying two numbers gives a negative result only when they
            # have opposite signs. The note warns this over-corrects.
            flags |= QC.AE_SIGN_ANOMALY
        we_c = we_net - coeff * (we_0 / ae_0) * ae_net

    elif algorithm == 3:
        # Avoids dividing by AE_0 entirely, by subtracting the difference of
        # the two zeros as a separate term.
        #     WE_c = (WE_u - WE_e) - (WE_0 - AE_0) - k'_T * (AE_u - AE_e)
        if we_0 * ae_0 < 0:
            flags |= QC.AE_SIGN_ANOMALY
        we_c = we_net - (we_0 - ae_0) - coeff * ae_net

    else:  # algorithm == 4
        # Ignores the auxiliary electrode completely and just subtracts a
        # temperature-dependent offset. Useful when the AE is misbehaving --
        # and it is the only shape that also fits 3-electrode sensors, which
        # have no auxiliary electrode at all.
        #     WE_c = (WE_u - WE_e) - WE_0 - k''_T
        we_c = we_net - we_0 - coeff

    # --- STEP 6: did the correction eat the signal? --------------------------
    # AAN 803-05's notes warn that if the auxiliary term is much larger than
    # the working term, you subtract away the real answer and end up with
    # nonsense -- often a large negative number. Catch it here rather than
    # letting it into the database.
    if algorithm in (1, 2, 3):
        ae_term = abs(we_net - we_c)             # how much we subtracted
        if ae_term > 3.0 * max(abs(we_net), 1e-9):
            flags |= QC.OVERCOMPENSATED

    # --- STEP 7: adjust sensitivity for temperature, then divide -------------
    # AAN 803-05 step 5 recommends this refinement: do not just divide by the
    # 20 C sensitivity, scale it for the actual temperature first.
    sens_scale, extrap_s = interpolate(sensitivity_tc, head_t_c)
    if extrap_s:
        flags |= QC.TEMP_EXTRAPOLATED
    sens = cal.sens_mv_per_ppb * sens_scale
    if sens <= 0:
        raise ValueError("non-positive sensitivity for %s" % cal.serial)

    # millivolts divided by millivolts-per-ppb leaves ppb.
    ppb = we_c / sens

    # --- STEP 8: subtract signal caused by OTHER gases -----------------------
    if interferents_ppb:
        for gas, conc in interferents_ppb.items():
            # factor * their concentration = the fake H2S they produced
            ppb -= cross_sensitivity.get(gas.lower(), 0.0) * conc

    # --- STEP 9: sanity-check the final answer -------------------------------
    if ppb < -5.0:
        # Small negatives are normal -- the datasheet quotes 1 ppb of noise, so
        # genuinely clean air wobbles either side of zero. Beyond -5 ppb though,
        # something is wrong with the zeros or the correction.
        flags |= QC.NEGATIVE
    if ppb > 100_000.0:
        # 100 ppm = 100000 ppb, the limit of the performance warranty.
        flags |= QC.ABOVE_LINEAR_RANGE

    return Reading(ppb=ppb, flags=flags, we_c_mv=we_c,
                   sens_mv_per_ppb=sens, coeff=coeff, algorithm=algorithm)


# =============================================================================
#  SECTION 7: COMPARING TWO SENSORS
# =============================================================================

def pair_agreement(a: Reading, b: Reading,
                   abs_tol_ppb: float = 10.0, rel_tol: float = 0.20) -> tuple[bool, float]:
    """Check whether two side-by-side sensors are telling the same story.

    WHY FIT TWO SENSORS AT ALL? Because a single sensor cannot tell you it has
    gone wrong. Electrochemical sensors degrade: the liquid dries out, the
    electrodes get poisoned by contaminants, sensitivity fades. All of that is
    invisible in a single reading -- the number still looks plausible.

    But two sensors breathing the same air should report the same thing. When
    they start disagreeing, at least one has drifted. That is the earliest
    warning you get without physically bringing a calibration gas bottle to
    site, and it is the entire reason for fitting a pair.

    HOW THE TOLERANCE WORKS: we allow the LARGER of
       * a fixed floor (10 ppb), which matters when both read near zero,
         because a 20 % rule around 2 ppb would be absurdly strict; and
       * a percentage (20 %), which matters at high readings, because
         demanding agreement within 10 ppb at 5000 ppb is equally absurd.

    Returns (do_they_agree, how_far_apart_in_ppb).
    """
    if not (a.usable and b.usable):
        # No point comparing if one is already known to be rubbish.
        return False, float("nan")
    diff = abs(a.ppb - b.ppb)
    tol = max(abs_tol_ppb, rel_tol * max(abs(a.ppb), abs(b.ppb)))
    return diff <= tol, diff


def describe(flags: QC) -> str:
    """Turn the flags into readable text, e.g. 'ADC_RAILED|NEGATIVE'."""
    if flags is QC.OK or not flags:
        return "OK"
    return "|".join(f.name for f in QC if f is not QC.OK and f & flags)


# =============================================================================
#  SECTION 8: SELF-TEST
# =============================================================================
# Run this file directly and it checks itself against a test dataset where the
# true answer is already known:
#
#     python h2s_b4.py
#
# The test data was built by running this physics BACKWARDS -- starting from
# known concentrations and generating the raw counts a sensor would produce.
# So if this code is correct, converting those counts must give back the
# original concentrations. That is called a "round trip" test.
#
# The number to watch is BIAS -- the average error. Random noise cancels out
# when you average, so bias should be almost exactly zero. If it is not, there
# is a systematic mistake in the maths. Individual errors being non-zero is
# fine and expected: that is sensor noise plus the sensor's response lag.
# =============================================================================

def _self_test(fixture: str, calfile: str) -> int:
    import csv
    import statistics as st

    # --- load the calibration constants for the test sensors -----------------
    consts: dict = {}
    with open(calfile, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            consts.setdefault(row["sensor"], {})[row["param"]] = float(row["value"])

    # The test file records the n_T values used to BUILD it, so we can feed the
    # same ones back in. In the real world you would never have this luxury.
    truth_table = {}
    for key, val in consts.get("H2S-B4-*", {}).items():
        if key.startswith("n_T@"):
            truth_table[float(key[4:-1])] = val      # "n_T@20C" -> 20.0

    # --- build a Calibration for each of the two test sensors ----------------
    cals = {}
    for ch in ("A", "B"):
        c = consts["H2S-B4-%s" % ch]
        cals[ch] = Calibration(
            serial="H2S-B4-%s" % ch,
            we_e=c["WE_e"], ae_e=c["AE_e"],
            # The test file stores the zero as a current (nA), so multiply by
            # the board gain (mV/nA) to get the voltage this code expects.
            we_0=c["we_zero_20"] * c["isb_gain"],
            ae_0=c["we_zero_20"] * c["isb_gain"] / truth_table.get(20.0, 1.2),
            # nA/ppm * mV/nA = mV/ppm, then / 1000 to get mV/ppb.
            sens_mv_per_ppb=c["S20"] * c["isb_gain"] / 1000.0,
        )

    # --- convert every row and compare against the known truth ---------------
    errs: list[float] = []
    flagged = 0
    disagree = 0
    rows = 0
    with open(fixture, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            rows += 1
            t_c = float(row["head_t_c"])
            res = {}
            for ch in ("A", "B"):
                res[ch] = compute(
                    float(row["h2s_%s_we_counts" % ch]),
                    float(row["h2s_%s_ae_counts" % ch]),
                    t_c, cals[ch], algorithm=1,
                    coeffs=CoefficientSet(source="fixture ground truth",
                                          verified=True, n_t=truth_table))
            errs.append(res["A"].ppb - float(row["true_h2s_ppb"]))
            if res["A"].flags:
                flagged += 1
            ok, _ = pair_agreement(res["A"], res["B"])
            if not ok:
                disagree += 1

    # --- report --------------------------------------------------------------
    bias = st.mean(errs)                                    # average error
    rms = math.sqrt(sum(e * e for e in errs) / len(errs))   # typical error size
    p95 = sorted(abs(e) for e in errs)[int(0.95 * len(errs))]  # 95 % are below this
    print("rows              : %d" % rows)
    print("bias              : %+.4f ppb" % bias)
    print("rms               : %.4f ppb" % rms)
    print("p95 |error|       : %.4f ppb" % p95)
    print("flagged (any)     : %d (%.2f %%)" % (flagged, 100 * flagged / rows))
    print("A/B disagreements : %d (%.2f %%)" % (disagree, 100 * disagree / rows))

    # Bias must be near zero (proves the maths). p95 is allowed to be larger
    # because the sensor physically lags behind sudden changes in gas level.
    ok = abs(bias) < 0.5 and p95 < 40.0
    print("\n%s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    # This block runs ONLY when you execute this file directly, not when another
    # file imports it.
    import argparse
    import os

    here = os.path.dirname(os.path.abspath(__file__))
    data = os.path.join(os.path.dirname(here), "sensor raw data")
    ap = argparse.ArgumentParser(description="H2S-B4 conversion self-test")
    ap.add_argument("--fixture", default=os.path.join(data, "aqms_raw_fixture.csv"))
    ap.add_argument("--cal", default=os.path.join(data, "aqms_raw_fixture_cal.csv"))
    args = ap.parse_args()
    raise SystemExit(_self_test(args.fixture, args.cal))
