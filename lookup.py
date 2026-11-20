"""
Official release-quality lookup.

Sources, in priority order:

1. Your own saved entries   - exact bit depth / sample rate you confirmed for an
                              album (e.g. from Qobuz, HDtracks, Bandcamp). Stored
                              in %APPDATA%\\FileChecker\\releases.json.
2. Qobuz catalog API        - exact per-edition max quality. Only used if you put
                              your own Qobuz API app id in Settings.
3. Apple Music              - public album badges. Tells us whether an album has
                              "Hi-Res Lossless" (24-bit, >48 kHz) or just
                              "Lossless". This is a *lower bound* only: it proves
                              hi-res exists, but not the exact rate, and a missing
                              badge doesn't prove hi-res doesn't exist elsewhere.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import threading
import unicodedata
from dataclasses import dataclass, field
from typing import Optional

import requests

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

DATA_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "FileChecker")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
RELEASES_FILE = os.path.join(DATA_DIR, "releases.json")


def _load_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _save_json(path: str, data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_settings() -> dict:
    s = {"online_lookup": True, "use_apple": True, "qobuz_app_id": "", "country": "us"}
    s.update(_load_json(SETTINGS_FILE))
    return s


def save_settings(s: dict) -> None:
    _save_json(SETTINGS_FILE, s)


# ----------------------------------------------------------------- matching --

_EDITION = re.compile(
    r"\b(remaster(ed)?|deluxe|expanded|edition|anniversary|bonus|version|mono|stereo|"
    r"explicit|clean|hi-?res|24[- ]?bit|super|special|collector'?s?|reissue|mix|"
    r"\d{4}|\d+(st|nd|rd|th))\b", re.I)


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s) if not unicodedata.combining(c))


def norm(s: str) -> str:
    s = _strip_accents(s or "").lower().replace("&", " and ")
    s = re.sub(r"[^\w\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return re.sub(r"^the ", "", s)


def base_title(s: str) -> str:
    """Title with edition decorations removed: 'Abbey Road (Remastered 2019)' -> 'abbey road'."""
    s = s or ""
    s = re.sub(r"[\(\[\{][^\)\]\}]*[\)\]\}]", " ", s)
    parts = re.split(r"\s[-–—:]\s", s)
    if len(parts) > 1 and _EDITION.search(" ".join(parts[1:])):
        s = parts[0]
    return norm(s)


def primary_artist(s: str) -> str:
    return norm(re.split(r"\s(?:feat\.?|ft\.?|featuring|with|x|vs\.?)\s|[,;/]", s or "", flags=re.I)[0])


def sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def artist_sim(a: str, b: str) -> float:
    return max(sim(norm(a), norm(b)), sim(primary_artist(a), primary_artist(b)))


def album_key(artist: str, album: str) -> str:
    return f"{primary_artist(artist)}|{base_title(album)}"


# ------------------------------------------------------------------- models --

def fmt_rate(rate: Optional[int]) -> str:
    if not rate:
        return "?"
    k = rate / 1000.0
    return f"{k:g}" if abs(k * 10 - round(k * 10)) < 1e-9 else f"{k:.2f}"


def fmt_quality(bits: Optional[int], rate: Optional[int]) -> str:
    if bits is None and rate is None:
        return "?"
    return f"{bits if bits else '?'}/{fmt_rate(rate)}"


def parse_quality(text: str) -> Optional[tuple[int, int]]:
    """'24/96', '24-bit 96kHz', '16 44.1' -> (bits, rate_hz)."""
    m = re.match(r"\s*(\d{1,2})\s*(?:-?\s*bits?)?\s*[/ ,x]\s*(\d{2,3}(?:\.\d+)?)\s*(?:k?hz)?\s*$", text or "", re.I)
    if not m:
        return None
    bits, k = int(m.group(1)), float(m.group(2))
    if bits not in (16, 20, 24, 32) or not (32 <= k <= 768):
        return None
    return bits, int(round(k * 1000))


@dataclass
class Edition:
    title: str
    artist: str
    bits: int
    rate: int                 # Hz
    exact: bool = True        # False -> bits/rate are a minimum (Apple badges)
    release_date: str = ""
    url: str = ""
    score: float = 0.0
    source: str = ""
    badge: str = ""           # human-readable description of what the source said

    @property
    def quality(self) -> tuple[int, int]:
        return (self.bits, self.rate)

    def qlabel(self) -> str:
        return fmt_quality(self.bits, self.rate) if self.exact else f"≥{fmt_quality(self.bits, self.rate)}"

    def label(self) -> str:
        yr = (self.release_date or "")[:4]
        return f"{self.qlabel()}  [{self.source}]  {self.title}" + (f" ({yr})" if yr else "")


@dataclass
class LookupResult:
    query: str = ""
    editions: list[Edition] = field(default_factory=list)   # best quality first
    error: Optional[str] = None
    notes: list[str] = field(default_factory=list)

    @property
    def best(self) -> Optional[Edition]:
        return self.editions[0] if self.editions else None


def _rank(e: Edition):
    # exact > lower-bound at the same quality; then quality; then match score
    return (e.bits, e.rate, e.exact, e.score)


# ------------------------------------------------------------ manual store --

class ReleaseDB:
    """User-confirmed official qualities, keyed by normalized artist|album."""

    def __init__(self):
        self._lock = threading.Lock()
        self.data = _load_json(RELEASES_FILE)

    def get(self, artist: str, album: str) -> Optional[Edition]:
        rec = self.data.get(album_key(artist, album))
        if not rec:
            return None
        return Edition(title=rec.get("album", album), artist=rec.get("artist", artist),
                       bits=int(rec["bits"]), rate=int(rec["rate"]), exact=True,
                       source="Saved", badge=rec.get("note", "Entered manually"))

    def put(self, artist: str, album: str, bits: int, rate: int, note: str = "") -> None:
        with self._lock:
            self.data[album_key(artist, album)] = {
                "artist": artist, "album": album, "bits": bits, "rate": rate,
                "note": note or "Entered manually"}
            _save_json(RELEASES_FILE, self.data)

    def delete(self, artist: str, album: str) -> None:
        with self._lock:
            if self.data.pop(album_key(artist, album), None) is not None:
                _save_json(RELEASES_FILE, self.data)


# -------------------------------------------------------------- Qobuz API --

class QobuzAPI:
    """Qobuz catalog search. Requires an app id that the user supplies."""
    BASE = "https://www.qobuz.com/api.json/0.2/"

    def __init__(self, session: requests.Session, app_id: str):
        self.s, self.app_id = session, app_id.strip()

    def _get(self, endpoint, params):
        r = self.s.get(self.BASE + endpoint, params=params,
                       headers={"X-App-Id": self.app_id}, timeout=20)
        if r.status_code in (400, 401, 403):
            raise RuntimeError(f"Qobuz rejected the app id (HTTP {r.status_code})")
        r.raise_for_status()
        return r.json()

    @staticmethod
    def _edition(item: dict) -> Optional[Edition]:
        try:
            bits = int(item.get("maximum_bit_depth") or 0)
            rate_k = float(item.get("maximum_sampling_rate") or 0)
        except (TypeError, ValueError):
            return None
        if not bits or not rate_k:
            return None
        title = item.get("title") or ""
        ver = item.get("version")
        if ver and ver.lower() not in title.lower():
            title = f"{title} ({ver})"
        return Edition(title=title, artist=(item.get("artist") or {}).get("name", ""),
                       bits=bits, rate=int(round(rate_k * 1000)), exact=True,
                       release_date=item.get("release_date_original") or "",
                       url=f"https://open.qobuz.com/album/{item['id']}" if item.get("id") else "",
                       source="Qobuz", badge=f"Qobuz max {fmt_quality(bits, int(round(rate_k*1000)))}")

    def find(self, artist, album, title, track_artist) -> list[Edition]:
        out = []
        if album:
            items = (self._get("album/search", {"query": f"{artist} {album}", "limit": 30})
                     .get("albums") or {}).get("items") or []
            want = base_title(album)
            for it in items:
                ed = self._edition(it)
                if not ed:
                    continue
                ts = sim(want, base_title(ed.title))
                a = max(artist_sim(artist, ed.artist), artist_sim(track_artist, ed.artist) if track_artist else 0)
                if ts >= 0.85 and a >= 0.75:
                    ed.score = ts * a
                    out.append(ed)
        if not out and title:
            items = (self._get("track/search", {"query": f"{track_artist or artist} {title}", "limit": 30})
                     .get("tracks") or {}).get("items") or []
            want, seen = base_title(title), set()
            for it in items:
                alb = it.get("album") or {}
                a = max(artist_sim(track_artist or artist, (it.get("performer") or {}).get("name", "")),
                        artist_sim(track_artist or artist, (alb.get("artist") or {}).get("name", "")))
                ts = sim(want, base_title(it.get("title", "")))
                if ts < 0.85 or a < 0.75:
                    continue
                merged = dict(alb)
                merged.setdefault("maximum_bit_depth", it.get("maximum_bit_depth"))
                merged.setdefault("maximum_sampling_rate", it.get("maximum_sampling_rate"))
                ed = self._edition(merged)
                if ed and ed.url not in seen:
                    seen.add(ed.url)
                    ed.score = ts * a
                    out.append(ed)
        return out


# ------------------------------------------------------------ Apple Music --

class AppleMusic:
    """iTunes Search API + the public album page's audio badges."""

    _BADGES = re.compile(r'"audioBadges":(\{[^{}]*\})')

    def __init__(self, session: requests.Session, country: str = "us"):
        self.s, self.country = session, (country or "us").lower()

    def _search(self, term: str, entity: str) -> list[dict]:
        r = self.s.get("https://itunes.apple.com/search",
                       params={"term": term, "entity": entity, "limit": 25, "country": self.country},
                       timeout=20)
        r.raise_for_status()
        return r.json().get("results") or []

    def _badges(self, url: str) -> Optional[dict]:
        r = self.s.get(url, timeout=20)
        if r.status_code != 200:
            return None
        m = self._BADGES.search(r.text)
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except ValueError:
            return None

    def find(self, artist, album, title, track_artist) -> list[Edition]:
        cands: list[tuple[float, dict]] = []
        if album:
            want = base_title(album)

            def take(results):
                for a in results:
                    if a.get("wrapperType") not in (None, "collection"):
                        continue
                    ts = sim(want, base_title(a.get("collectionName", "")))
                    ar = max(artist_sim(artist, a.get("artistName", "")),
                             artist_sim(track_artist, a.get("artistName", "")) if track_artist else 0)
                    if ts >= 0.85 and ar >= 0.75:
                        cands.append((ts * ar, a))

            take(self._search(f"{artist} {want}", "album"))
            if not cands:
                # Search ranking can bury the album; walk the artist's discography instead.
                for art in self._search(artist or track_artist, "musicArtist")[:3]:
                    if artist_sim(artist or track_artist, art.get("artistName", "")) < 0.85:
                        continue
                    r = self.s.get("https://itunes.apple.com/lookup",
                                   params={"id": art["artistId"], "entity": "album", "limit": 200,
                                           "country": self.country}, timeout=20)
                    if r.ok:
                        take(r.json().get("results") or [])
                    if cands:
                        break
        if not cands and title:
            want = base_title(title)
            for t in self._search(f"{track_artist or artist} {title}", "song"):
                ts = sim(want, base_title(t.get("trackName", "")))
                ar = artist_sim(track_artist or artist, t.get("artistName", ""))
                if ts >= 0.85 and ar >= 0.75:
                    cands.append((ts * ar, t))

        seen, out = set(), []
        for score, a in sorted(cands, key=lambda c: c[0], reverse=True):
            cid = a.get("collectionId")
            if cid in seen:
                continue
            seen.add(cid)
            if len(seen) > 4:      # a handful of editions is plenty
                break
            url = a.get("collectionViewUrl", "").split("?")[0]
            b = self._badges(url) if url else None
            if not b:
                continue
            if b.get("hiResLossless"):
                bits, rate, badge = 24, 88200, "Apple: Hi-Res Lossless (24-bit, above 48 kHz)"
            elif b.get("lossless"):
                bits, rate, badge = 16, 44100, "Apple: Lossless (exact resolution not published)"
            else:
                continue
            out.append(Edition(title=a.get("collectionName", ""), artist=a.get("artistName", ""),
                               bits=bits, rate=rate, exact=False,
                               release_date=a.get("releaseDate", ""), url=url, score=score,
                               source="Apple", badge=badge))
        return out


# --------------------------------------------------------------- facade ----

class ReleaseLookup:
    def __init__(self, settings: dict):
        self.settings = settings
        self.db = ReleaseDB()
        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA
        self._lock = threading.Lock()
        self._cache: dict[str, LookupResult] = {}
        self._key_locks: dict[str, threading.Lock] = {}

    def clear_cache(self):
        with self._lock:
            self._cache.clear()

    def find(self, artist: str, album: str, title: str = "", track_artist: str = "") -> LookupResult:
        key = album_key(artist, album) if album else "track|" + album_key(track_artist or artist, title)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            klock = self._key_locks.setdefault(key, threading.Lock())
        with klock:   # tracks from the same album share one lookup
            with self._lock:
                if key in self._cache:
                    return self._cache[key]
            res = self._find(artist, album, title, track_artist)
            with self._lock:
                self._cache[key] = res
            return res

    def _find(self, artist, album, title, track_artist) -> LookupResult:
        res = LookupResult(query=" - ".join(x for x in (artist or track_artist, album or title) if x))
        eds: list[Edition] = []

        saved = self.db.get(artist, album) if album else None
        if saved:
            eds.append(saved)

        if not (artist or track_artist) or not (album or title):
            res.error = "File has no artist/album tags to search with"
        elif self.settings.get("online_lookup", True):
            errors = []
            app_id = (self.settings.get("qobuz_app_id") or "").strip()
            if app_id:
                try:
                    eds += QobuzAPI(self.session, app_id).find(artist, album, title, track_artist)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"Qobuz: {e}")
            if self.settings.get("use_apple", True):
                try:
                    eds += AppleMusic(self.session, self.settings.get("country", "us")).find(
                        artist, album, title, track_artist)
                except Exception as e:  # noqa: BLE001
                    errors.append(f"Apple: {e}")
            if errors and not eds:
                res.error = "; ".join(errors)
            res.notes += errors

        eds.sort(key=_rank, reverse=True)
        # A saved entry is authoritative: put it first regardless of rank.
        if saved:
            eds.remove(saved)
            eds.insert(0, saved)
        res.editions = eds
        return res
