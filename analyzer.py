"""
Audio forensics engine.

Decodes a file through ffmpeg (streamed, so large hi-res files don't blow up
memory) and measures:

  * Effective bit depth  - how many of the container's bits actually carry data.
                           A "24-bit" file that is really zero-padded 16-bit
                           will never use the lowest 8 bits.
  * Spectral cut-off     - where the frequency content hits a brick wall.
                           An upsampled 44.1 kHz master shows a vertical cliff
                           at ~22 kHz inside a 96/192 kHz container; an MP3/AAC
                           transcode shows a cliff at ~16-20 kHz.
  * Spectrogram data     - for visual inspection in the GUI.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

AUDIO_EXTS = {
    ".flac", ".wav", ".aif", ".aiff", ".aifc", ".m4a", ".alac", ".mp4",
    ".mp3", ".ogg", ".oga", ".opus", ".wv", ".ape", ".aac", ".wma", ".dsf", ".dff",
}

LOSSY_CODECS = {
    "mp3", "mp2", "mp1", "aac", "aac_latm", "vorbis", "opus", "wmav1", "wmav2",
    "wmapro", "wmavoice", "ac3", "eac3", "atrac3", "atrac3p", "cook", "amr_nb",
    "amr_wb", "mpc7", "mpc8", "musepack7", "musepack8",
}

STANDARD_RATES = [44100, 48000, 88200, 96000, 176400, 192000, 352800, 384000]

# Below this a steep cliff means "almost certainly lossy" (MP3/AAC encoders
# low-pass at 16-20 kHz). 20.2-21 kHz is a grey zone (320k MP3 or mastering LPF).
LOSSY_CLIFF_HZ = 20200
GREY_CLIFF_HZ = 21000

_TOOL_CACHE: dict[str, Optional[str]] = {}


def find_tool(name: str) -> Optional[str]:
    """Locate ffmpeg/ffprobe on PATH, falling back to the winget install folder."""
    if name in _TOOL_CACHE:
        return _TOOL_CACHE[name]
    import sys
    path = None
    # Prefer a copy shipped next to the app/exe (bin\ffmpeg.exe or ffmpeg.exe).
    dirs = [os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))]
    if getattr(sys, "_MEIPASS", None):
        dirs.append(sys._MEIPASS)
    for d in dirs:
        for cand in (os.path.join(d, "bin", name + ".exe"), os.path.join(d, name + ".exe")):
            if os.path.isfile(cand):
                path = cand
                break
        if path:
            break
    if not path:
        path = shutil.which(name)
    if not path:
        base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "WinGet", "Packages")
        if os.path.isdir(base):
            for root, _dirs, files in os.walk(base):
                if name + ".exe" in files:
                    path = os.path.join(root, name + ".exe")
                    break
    _TOOL_CACHE[name] = path
    return path


_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW


@dataclass
class ProbeInfo:
    path: str
    codec: str
    sample_rate: int
    channels: int
    bits: Optional[int]          # container bit depth (None for lossy)
    duration: Optional[float]
    lossy: bool
    tags: dict = field(default_factory=dict)

    @property
    def artist(self) -> str:
        return self.tags.get("album_artist") or self.tags.get("albumartist") or self.tags.get("artist", "")

    @property
    def track_artist(self) -> str:
        return self.tags.get("artist", "") or self.artist

    @property
    def album(self) -> str:
        return self.tags.get("album", "")

    @property
    def title(self) -> str:
        return self.tags.get("title", "") or os.path.splitext(os.path.basename(self.path))[0]


def probe(path: str) -> ProbeInfo:
    ffprobe = find_tool("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe not found. Install FFmpeg (winget install Gyan.FFmpeg).")
    out = subprocess.run(
        [ffprobe, "-v", "error", "-print_format", "json", "-show_format",
         "-show_streams", "-select_streams", "a:0", path],
        capture_output=True, creationflags=_NO_WINDOW,
    )
    if out.returncode != 0:
        raise RuntimeError("ffprobe failed: " + out.stderr.decode(errors="replace").strip())
    data = json.loads(out.stdout.decode("utf-8", errors="replace") or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError("No audio stream found")
    s = streams[0]
    fmt = data.get("format", {})

    codec = s.get("codec_name", "?")
    sr = int(s.get("sample_rate") or 0)
    ch = int(s.get("channels") or 0)
    lossy = codec in LOSSY_CODECS

    bits = None
    if not lossy:
        for key in ("bits_per_raw_sample", "bits_per_sample"):
            try:
                v = int(s.get(key) or 0)
            except ValueError:
                v = 0
            if v:
                bits = v
                break
        if not bits:
            sf = s.get("sample_fmt", "")
            bits = {"u8": 8, "s16": 16, "s16p": 16, "s32": 32, "s32p": 32,
                    "flt": 32, "fltp": 32, "dbl": 64, "dblp": 64}.get(sf)
        if codec.startswith("dsd"):
            bits = 1

    tags = {}
    for src in (fmt.get("tags") or {}, s.get("tags") or {}):
        for k, v in src.items():
            tags.setdefault(k.lower().replace(" ", "_"), str(v))

    dur = s.get("duration") or fmt.get("duration")
    try:
        dur = float(dur) if dur else None
    except ValueError:
        dur = None

    return ProbeInfo(path, codec, sr, ch, bits, dur, lossy, tags)


@dataclass
class Analysis:
    info: ProbeInfo
    effective_bits: Optional[int] = None
    lsb_usage: dict = field(default_factory=dict)   # bit-depth -> share of samples using it
    peak_dbfs: float = -999.0
    cliff_hz: Optional[float] = None                 # detected brick-wall frequency
    cliff_drop_db: float = 0.0
    content_to_hz: Optional[float] = None            # highest freq with real content
    source_rate: Optional[int] = None                # estimated original sample rate
    lossy_suspect: bool = False
    lossy_grey: bool = False
    freqs: Optional[np.ndarray] = None
    spectrum_db: Optional[np.ndarray] = None
    spectrogram: Optional[np.ndarray] = None         # (freq_bins, time_cols) dB
    error: Optional[str] = None


def _moving_avg(x: np.ndarray, k: int) -> np.ndarray:
    if k <= 1:
        return x.copy()
    pad = k // 2
    xp = np.pad(x, (pad, k - 1 - pad), mode="edge")
    c = np.cumsum(np.insert(xp, 0, 0.0))
    return (c[k:] - c[:-k]) / k


def detect_cliff(freqs: np.ndarray, db: np.ndarray, sr: int):
    """
    Find the steepest drop-off (brick-wall low-pass) in the long-term spectrum.
    Returns (cliff_hz or None, drop_db, content_to_hz).
    """
    nyq = sr / 2.0
    df = freqs[1] - freqs[0]
    s = _moving_avg(db, max(1, int(round(120 / df))))

    top = int(np.searchsorted(freqs, nyq * 0.985))
    gap = max(1, int(round(150 / df)))
    win = max(2, int(round(800 / df)))

    # Highest frequency with content clearly above the spectral floor.
    floor = np.percentile(s[int(np.searchsorted(freqs, 1000)):top], 2)
    above = np.nonzero(s[:top] > floor + 12)[0]
    content_to = float(freqs[above[-1]]) if len(above) else None

    lo = max(int(np.searchsorted(freqs, 11000)), gap + win)
    hi = top - gap - win
    if hi <= lo:
        return None, 0.0, content_to

    c = np.cumsum(np.insert(s, 0, 0.0))
    idx = np.arange(lo, hi)
    below_mean = (c[idx - gap] - c[idx - gap - win]) / win
    above_mean = (c[idx + gap + win] - c[idx + gap]) / win
    drop = below_mean - above_mean
    k = int(np.argmax(drop))
    i = idx[k]
    best_drop = float(drop[k])

    below_level = float(below_mean[k])
    rest = s[i + gap: top]
    rest_level = float(np.percentile(rest, 95)) if len(rest) else -300.0
    ref = float(np.max(s[int(np.searchsorted(freqs, 200)):int(np.searchsorted(freqs, 5000))]))

    steep = best_drop >= 18.0
    stays_down = (below_level - rest_level) >= 15.0
    meaningful = below_level > floor + 10 and below_level > ref - 110
    if not (steep and stays_down and meaningful):
        return None, best_drop, content_to

    # Refine: where the curve crosses the midpoint between the two plateaus.
    mid = (below_level + float(above_mean[k])) / 2.0
    seg = s[i - gap - win: i + gap + win]
    crossing = np.nonzero(seg < mid)[0]
    cliff_i = (i - gap - win + int(crossing[0])) if len(crossing) else i
    return float(freqs[cliff_i]), best_drop, content_to


def rate_from_cliff(cliff_hz: float) -> int:
    """Smallest standard sample rate whose Nyquist explains a brick-wall at cliff_hz."""
    bounds = [(22600, 44100), (24600, 48000), (44600, 88200), (49200, 96000),
              (89200, 176400), (98500, 192000), (178000, 352800)]
    for limit, rate in bounds:
        if cliff_hz <= limit:
            return rate
    return 384000


def analyze(path: str, progress: Optional[Callable[[float], None]] = None,
            info: Optional[ProbeInfo] = None) -> Analysis:
    info = info or probe(path)
    res = Analysis(info=info)
    ffmpeg = find_tool("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found. Install FFmpeg (winget install Gyan.FFmpeg).")

    sr, ch = info.sample_rate, max(1, info.channels)
    if sr <= 0:
        raise RuntimeError("Unknown sample rate")

    nfft = 4096 if sr <= 50000 else (8192 if sr <= 100000 else 16384)
    window = np.hanning(nfft).astype(np.float32)
    nbins = nfft // 2 + 1
    psd_sum = np.zeros(nbins, dtype=np.float64)
    nframes = 0
    tz_counts = np.zeros(33, dtype=np.int64)
    peak = 0
    spec_cols = []
    disp_bins = 512
    group = (nbins - 1) // disp_bins

    proc = subprocess.Popen(
        [ffmpeg, "-v", "error", "-nostdin", "-i", path, "-map", "0:a:0",
         "-f", "s32le", "-acodec", "pcm_s32le", "-"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=_NO_WINDOW,
    )
    frame_bytes = 4 * ch
    block = sr * frame_bytes  # ~1 second per block
    leftover = b""
    done_samples = 0
    total_samples = (info.duration or 0) * sr

    try:
        while True:
            chunk = proc.stdout.read(block)
            if not chunk:
                break
            buf = leftover + chunk
            usable = len(buf) - (len(buf) % frame_bytes)
            leftover = buf[usable:]
            if usable == 0:
                continue
            arr = np.frombuffer(buf[:usable], dtype="<i4").reshape(-1, ch)
            done_samples += arr.shape[0]

            # ---- bit-depth: position of lowest set bit of every sample ----
            flat = arr.ravel().astype(np.int64)
            nz = flat[flat != 0]
            if nz.size:
                lsb = nz & -nz
                _, exp = np.frexp(lsb.astype(np.float64))
                tz_counts += np.bincount(exp - 1, minlength=33)[:33]
                peak = max(peak, int(np.abs(flat).max()))

            # ---- spectrum ----
            n = arr.shape[0] // nfft
            if n:
                x = arr[: n * nfft].astype(np.float32) * (1.0 / 2147483648.0)
                x = x.reshape(n, nfft, ch) * window[None, :, None]
                p = np.abs(np.fft.rfft(x, axis=1)) ** 2          # (n, bins, ch)
                p = p.sum(axis=2)
                psd_sum += p.sum(axis=0)
                nframes += n * ch
                col = p.mean(axis=0)[: group * disp_bins].reshape(disp_bins, group).mean(axis=1)
                spec_cols.append(col.astype(np.float32))

            if progress and total_samples:
                progress(min(1.0, done_samples / total_samples))
        proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()

    if nframes == 0:
        err = proc.stderr.read().decode(errors="replace").strip()
        raise RuntimeError("Could not decode audio" + (f": {err}" if err else " (file too short?)"))

    # ---- effective bit depth ----
    total_nz = tz_counts.sum()
    if total_nz:
        share = tz_counts / total_nz
        for tz in range(32):
            if share[tz] > 0.001:
                res.effective_bits = 32 - tz
                break
        # share of samples that use bits below the 16-bit and 24-bit marks
        res.lsb_usage = {
            "below16": float(share[:16].sum()),
            "below24": float(share[:8].sum()),
        }
    if info.bits and res.effective_bits:
        res.effective_bits = min(res.effective_bits, info.bits)
    res.peak_dbfs = 20 * np.log10(peak / 2147483648.0) if peak else -999.0

    # ---- spectral analysis ----
    freqs = np.fft.rfftfreq(nfft, 1.0 / sr)
    psd = psd_sum / nframes
    db = 10 * np.log10(psd / (window.sum() ** 2) + 1e-30)
    res.freqs, res.spectrum_db = freqs, db

    cliff, drop, content_to = detect_cliff(freqs, db, sr)
    res.cliff_drop_db, res.content_to_hz = drop, content_to
    if cliff is not None:
        res.cliff_hz = cliff
        src = rate_from_cliff(cliff)
        res.source_rate = src if src < sr else sr
        if cliff < LOSSY_CLIFF_HZ:
            res.lossy_suspect = True
        elif cliff < GREY_CLIFF_HZ:
            res.lossy_grey = True
    else:
        res.source_rate = sr

    if spec_cols:
        sg = np.stack(spec_cols, axis=1)
        if sg.shape[1] > 1200:  # thin out very long tracks for display
            step = int(np.ceil(sg.shape[1] / 1200))
            cut = (sg.shape[1] // step) * step
            sg = sg[:, :cut].reshape(sg.shape[0], -1, step).mean(axis=2)
        res.spectrogram = 10 * np.log10(sg / (window.sum() ** 2) + 1e-30)

    if info.lossy:
        res.effective_bits = None
    return res
