# Hi-Res Authenticity Checker

Drop tracks or folders onto the window. Each file is measured to find its *real*
resolution, compared against the best *official* release quality, and given a verdict.

**Start:** double-click `Lossless_Verifier.exe` (standalone, no Python needed). You can
also drag files onto the exe, or run from source with `Run File Checker.bat`.

The exe needs FFmpeg. It uses `bin\ffmpeg.exe` + `bin\ffprobe.exe` next to the exe if
present (handy when copying it to another PC), otherwise an installed FFmpeg
(`winget install Gyan.FFmpeg`).

Rebuild the exe after code changes:
```
python -m PyInstaller --noconfirm --onefile --windowed --name "HiRes Checker" --icon icon.ico --add-data "icon.ico;." --collect-all tkinterdnd2 --exclude-module scipy app.py
```

## What it measures

| Check | How | Catches |
|---|---|---|
| Real sample rate | Long-term spectrum; looks for a brick-wall low-pass well below Nyquist | 44.1/48 kHz masters upsampled to 88.2/96/192 kHz |
| Real bit depth | Which low-order bits samples actually use | 16-bit audio zero-padded into a 24-bit file |
| Lossy source | Brick-wall at 16-20 kHz | MP3/AAC re-saved as FLAC/WAV |
| Lossy container | Codec | MP3/AAC/Ogg/Opus files |

## Where "official quality" comes from (priority order)

1. **Saved entries.** Use *Set official quality…* to store the exact max for an album
   (e.g. `24/96`) after checking Qobuz/HDtracks/Bandcamp yourself (the *Search Qobuz* /
   *HDtracks* buttons open the search in your browser). Saved in
   `%APPDATA%\FileChecker\releases.json`, applied to every track of that album.
2. **Qobuz catalog API.** Exact max per edition. Only used if you enter your own
   Qobuz API app id in *Settings*.
3. **Apple Music badges** (automatic, no setup). Says whether an album is
   *Hi-Res Lossless* (24-bit, >48 kHz) or just *Lossless*. It's treated as a
   **minimum** (shown as `≥24/88.2`): it proves hi-res exists, but Apple doesn't publish
   the exact rate, and many albums that are hi-res on Qobuz are Lossless-only on Apple.

If several editions match (remasters, deluxe), the best one is used by default. Pick
another in *Compare against* (applies to the whole album).

## Verdicts

- **AUTHENTIC (matches best release)**: real quality = best official.
- **GENUINE HI-RES (exact max unknown)**: genuine hi-res; only a minimum was known.
- **LOWER THAN OFFICIAL**: genuine, but a higher-res release exists (CD rip / downsample).
- **UPSAMPLED / PADDED**: container claims more than the audio contains.
- **MATCHES OFFICIAL (master is upsampled)**: the file is as sold, but the label's "hi-res" master is itself upsampled.
- **EXCEEDS OFFICIAL**: file is higher-res than anything officially released.
- **LIKELY LOSSY TRANSCODE / LOSSY FORMAT**: MP3/AAC origin.

## Limits

- A 16-bit master converted to 24-bit *with any gain or processing* fills the low bits
  and can't be told apart by bit analysis alone.
- Real hi-res recordings often have little ultrasonic content. That's fine: only a
  steep *brick wall* counts as evidence, not a gentle roll-off.
- A 20.2–21 kHz cut-off is a grey zone (320 kbps MP3 vs. a mastering low-pass). Check the
  spectrogram: lossy files show a hard, flat shelf for the whole track.

Requires Python 3.10+ and FFmpeg (`winget install Gyan.FFmpeg`).
`pip install -r requirements.txt`
