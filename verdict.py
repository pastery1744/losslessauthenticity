"""Combine measurements + official release info into a verdict."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from analyzer import Analysis, GREY_CLIFF_HZ, LOSSY_CLIFF_HZ
from lookup import Edition, fmt_quality, fmt_rate

GOOD, WARN, BAD, INFO = "good", "warn", "bad", "info"


@dataclass
class Verdict:
    status: str
    level: str
    measured: str                       # e.g. "16/44.1"
    notes: list[str] = field(default_factory=list)


def true_quality(a: Analysis) -> tuple[Optional[int], Optional[int]]:
    bits = a.effective_bits if not a.info.lossy else None
    rate = a.source_rate or a.info.sample_rate
    return bits, rate


def evaluate(a: Analysis, official: Optional[Edition]) -> Verdict:
    info = a.info
    c_bits, c_rate = info.bits, info.sample_rate
    t_bits, t_rate = true_quality(a)
    measured = "lossy" if info.lossy else fmt_quality(t_bits, t_rate)
    notes: list[str] = []

    # ---------------------------------------------------- intrinsic checks --
    fake_rate = bool(a.cliff_hz and t_rate and c_rate and t_rate < c_rate)
    fake_bits = bool(c_bits and t_bits and c_bits <= 32 and t_bits < c_bits and c_bits > 16)

    if a.cliff_hz:
        notes.append(f"Brick-wall low-pass at {a.cliff_hz/1000:.1f} kHz "
                     f"({a.cliff_drop_db:.0f} dB drop) inside a {fmt_rate(c_rate)} kHz file.")
    elif a.content_to_hz:
        notes.append(f"No brick-wall below Nyquist; content extends to ~{a.content_to_hz/1000:.1f} kHz "
                     f"(Nyquist {c_rate/2000:.2f} kHz).")
    if c_bits and t_bits and not info.lossy:
        usage = a.lsb_usage.get("below24" if c_bits >= 24 else "below16", 0.0)
        if c_bits >= 24:
            notes.append(f"Effective bit depth {t_bits} of {c_bits}; "
                         f"{usage*100:.1f}% of samples use the low 8 bits.")
        else:
            notes.append(f"Effective bit depth {t_bits} of {c_bits}.")
    if a.peak_dbfs > -0.01:
        notes.append(f"Peak {a.peak_dbfs:.2f} dBFS (touches full scale, possible clipping).")

    if info.lossy:
        notes.insert(0, f"File is a lossy format ({info.codec}); it cannot match a lossless release.")
        return Verdict("LOSSY FORMAT", BAD, measured, notes + _official_notes(official))

    if a.lossy_suspect:
        notes.insert(0, f"Spectrum cuts off at {a.cliff_hz/1000:.1f} kHz, typical of an MP3/AAC "
                        f"transcode saved as {info.codec.upper()}. (An old analog master can also lack "
                        f"top end, but it rolls off gradually rather than hitting a wall.)")
        return Verdict("LIKELY LOSSY TRANSCODE", BAD, "lossy?", notes + _official_notes(official))

    if fake_rate or fake_bits:
        what = []
        if fake_rate:
            what.append(f"upsampled from {fmt_rate(t_rate)} kHz")
        if fake_bits:
            what.append(f"padded from {t_bits}-bit")
        notes.insert(0, f"Container says {fmt_quality(c_bits, c_rate)} but real resolution is "
                        f"~{fmt_quality(t_bits, t_rate)} ({', '.join(what)}).")
        if official and official.exact and (t_bits, t_rate) == official.quality \
                and (c_bits, c_rate) != official.quality:
            notes.append(f"The real resolution matches the official {official.qlabel()} release, "
                         f"so this was converted after purchase/download.")
        elif official and official.exact and (c_bits, c_rate) == official.quality:
            notes.append("The container matches the official release format, which suggests the "
                         "OFFICIAL master itself is upsampled (common for some 'hi-res' reissues).")
            return Verdict("MATCHES OFFICIAL (master is upsampled)", WARN, measured,
                           notes + _official_notes(official))
        return Verdict("UPSAMPLED / PADDED", BAD, measured, notes + _official_notes(official))

    if a.lossy_grey:
        notes.append(f"Cut-off at {a.cliff_hz/1000:.1f} kHz is a grey zone: could be a 320 kbps "
                     f"MP3 source or just a mastering low-pass. Check the spectrogram for a flat shelf.")

    # --------------------------------------------- comparison to official --
    if official is None:
        lvl = WARN if a.lossy_grey else INFO
        st = "GENUINE?" if a.lossy_grey else "LOOKS GENUINE (no release info)"
        notes.append("No official release data. Add it with 'Set official quality' to compare.")
        return Verdict(st, lvl, measured, notes)

    o_bits, o_rate = official.quality
    notes += _official_notes(official)

    if official.exact:
        if (c_bits or 0) > o_bits or c_rate > o_rate:
            notes.insert(0, f"File ({fmt_quality(c_bits, c_rate)}) exceeds the best official release "
                            f"({official.qlabel()}), so it has been upconverted.")
            return Verdict("EXCEEDS OFFICIAL (upconverted)", BAD, measured, notes)
        if (t_bits, t_rate) == (o_bits, o_rate):
            st = "AUTHENTIC (matches best release)"
            return Verdict(st if not a.lossy_grey else st + " ?", GOOD if not a.lossy_grey else WARN,
                           measured, notes)
        notes.insert(0, f"Genuine {fmt_quality(t_bits, t_rate)}, but a {official.qlabel()} release exists.")
        if c_rate != o_rate and c_rate in (44100, 48000) and o_rate > 48000:
            notes.append("Likely a CD rip or a downsampled copy of the hi-res release.")
        return Verdict(f"LOWER THAN OFFICIAL ({official.qlabel()})", WARN, measured, notes)

    # Lower-bound source (Apple badges): only "below minimum" is conclusive.
    if (t_bits or 0) < o_bits or (t_rate or 0) < o_rate:
        notes.insert(0, f"Genuine {fmt_quality(t_bits, t_rate)}, but a hi-res version "
                        f"({official.qlabel()}) is officially available.")
        return Verdict(f"LOWER THAN OFFICIAL ({official.qlabel()})", WARN, measured, notes)
    if o_rate > 48000:
        notes.append("Official hi-res exists, but the exact max resolution isn't published by this "
                     "source. Set it manually for an exact check.")
        return Verdict("GENUINE HI-RES (exact max unknown)", GOOD if not a.lossy_grey else WARN,
                       measured, notes)
    notes.append("Source only confirms a lossless release; hi-res may still exist elsewhere.")
    return Verdict("LOOKS GENUINE (max unconfirmed)", INFO, measured, notes)


def _official_notes(official: Optional[Edition]) -> list[str]:
    if not official:
        return []
    return [f"Official reference: {official.qlabel()} - {official.badge or official.source}"
            f" ({official.title})."]
