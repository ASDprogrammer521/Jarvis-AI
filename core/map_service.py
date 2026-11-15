"""Where the Hand Space map gets its pictures and place names: OpenStreetMap by default, nothing else is contacted.

Being a good citizen (OpenStreetMap's tile / Nominatim usage policies):
  * every request carries a User-Agent that names this app; at most 2 tile downloads run at once;
  * tiles are cached on disk (a week fresh, older ones are shown if the network is down) so a place is downloaded once;
  * only the tiles in view are requested (the list is replaced every time the view moves: old requests are dropped);
  * after an error / 429 the store stays quiet for 30 s instead of hammering the server;
  * place search is limited to one request per 1.1 s and answers are remembered.
The map's bottom corner must show "© OpenStreetMap contributors" — the window does that.

Safety (everything that comes back from the network is untrusted):
  * HTTPS only (plain http only for 127.0.0.1 in tests); no redirects are followed; no credentials in a URL;
  * a tile must be a real 256 × 256 PNG / JPEG of at most 200 kB and decode completely, or it is thrown away;
  * a place-search answer is capped at 64 kB, must be JSON with numbers in range, and its text is cleaned and shortened;
  * file names in the cache are built from three integers, never from anything the server said.
Private by design: only the tile coordinates in view and the place names you ask for leave the computer.
"""
from __future__ import annotations

import io
import json
import os
import re
import threading
import time
from collections import OrderedDict, deque
from pathlib import Path
from urllib.parse import urlparse


OSM_TEMPLATE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "Jarvis-HandSpace/1.0 (desktop assistant; personal use)"
TILE_PX = 256
MAX_TILE_BYTES = 200_000
MAX_JSON_BYTES = 64_000
FRESH_SECONDS = 7 * 86400
STALE_OK_SECONDS = 120 * 86400
COOL_DOWN = 30.0
CACHE_LIMIT = 500 * 1024 * 1024
LOCAL_HOSTS = ("127.0.0.1", "localhost")


class MapError(Exception):
    """A problem worth telling the user about (offline, nothing found …)."""


def _clean(s, n: int) -> str:
    s = "".join(ch if ch.isprintable() else " " for ch in str(s or ""))
    return re.sub(r"\s+", " ", s).strip()[:n]


def valid_template(t: str, allow_local: bool = False) -> bool:
    try:
        if not isinstance(t, str) or len(t) > 300 or any(ch.isspace() for ch in t):
            return False
        for ph in ("{z}", "{x}", "{y}"):
            if t.count(ph) != 1:
                return False
        u = urlparse(t.replace("{z}", "0").replace("{x}", "0").replace("{y}", "0"))
        if "@" in u.netloc or not u.hostname:
            return False
        if u.scheme == "https":
            return True
        return bool(allow_local and u.scheme == "http" and u.hostname in LOCAL_HOSTS)
    except ValueError:
        return False


def valid_tile(data: bytes) -> bool:
    """A genuine 256 × 256 PNG / JPEG that decodes completely."""
    if not isinstance(data, (bytes, bytearray)) or not (67 <= len(data) <= MAX_TILE_BYTES):
        return False
    if not (data.startswith(b"\x89PNG\r\n\x1a\n") or data.startswith(b"\xff\xd8\xff")):
        return False
    try:
        from PIL import Image
        with Image.open(io.BytesIO(bytes(data))) as im:
            if im.format not in ("PNG", "JPEG") or im.size != (TILE_PX, TILE_PX):
                return False
            im.load()
        return True
    except Exception:
        return False


def _key_ok(key) -> bool:
    try:
        z, x, y = key
        return all(isinstance(v, int) for v in key) and 0 <= z <= 19 and 0 <= x < (1 << z) and 0 <= y < (1 << z)
    except (TypeError, ValueError):
        return False


class TileStore:
    def __init__(self, template: str = OSM_TEMPLATE, cache_dir: Path | str | None = None, workers: int = 2,
                 limit_bytes: int = CACHE_LIMIT, allow_local: bool = False):
        self.allow_local = allow_local
        self.template = template if valid_template(template, allow_local) else OSM_TEMPLATE
        if cache_dir is None:
            base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
            cache_dir = Path(base) / "Jarvis" / "hand_space"
        self.dir = Path(cache_dir) / "tiles"
        try:
            self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.dir.is_symlink() or not self.dir.is_dir():
                self.dir = None
        except OSError:
            self.dir = None
        self.limit = limit_bytes
        self._cv = threading.Condition()
        self._wanted: deque = deque()
        self._wanted_set: set = set()
        self._inflight: set = set()
        self._done: deque = deque()
        self._failed: dict = {}
        self._cool_until = 0.0
        self._stop = False
        self._last_trim = 0.0
        self._session = None
        self.online_error = ""                                   # last network problem, for a "map is offline" hint
        self.stats = {"disk": 0, "net": 0, "bad": 0}
        self._threads = [threading.Thread(target=self._run, name=f"hs-tiles-{i}", daemon=True) for i in range(max(1, workers))]
        for t in self._threads:
            t.start()

    # ── what the window wants ────────────────────────────────────────────────────
    def request(self, keys) -> None:
        """Replaces the list of tiles to fetch (nearest to the centre first). Tiles already on their way finish."""
        keys = [k for k in keys if _key_ok(k)][:120]
        with self._cv:
            self._wanted = deque(k for k in keys if k not in self._failed or self._failed[k] < time.time())
            self._wanted_set = set(self._wanted)
            self._cv.notify_all()

    def poll(self, max_n: int = 8):
        out = []
        with self._cv:
            while self._done and len(out) < max_n:
                out.append(self._done.popleft())
        return out

    def close(self) -> None:
        with self._cv:
            self._stop = True
            self._cv.notify_all()

    # ── worker threads ───────────────────────────────────────────────────────────
    def _run(self):
        while True:
            with self._cv:
                key = None
                while key is None:
                    if self._stop:
                        return
                    for k in self._wanted:
                        if k not in self._inflight:
                            key = k
                            break
                    if key is None:
                        self._cv.wait(0.5)
                self._wanted.remove(key)
                self._wanted_set.discard(key)
                self._inflight.add(key)
            data = None
            try:
                data = self._load(key)
            except Exception:                                     # never let a worker die
                data = None
            with self._cv:
                self._inflight.discard(key)
                if data is not None:
                    self._done.append((key, data))
                else:
                    self._failed[key] = time.time() + 20.0        # try this tile again in a while
                    if len(self._failed) > 2000:
                        self._failed.clear()

    def _path(self, key) -> Path | None:
        if self.dir is None or not _key_ok(key):
            return None
        z, x, y = key
        return self.dir / f"{z}_{x}_{y}.tile"

    def _read_disk(self, key):
        f = self._path(key)
        if f is None:
            return None
        try:
            st = f.lstat()
            if not (st.st_mode & 0o170000 == 0o100000) or st.st_size > MAX_TILE_BYTES:      # a plain file only
                return None
            data = f.read_bytes()
        except OSError:
            return None
        if not valid_tile(data):
            try:
                f.unlink()
            except OSError:
                pass
            return None
        return data, time.time() - st.st_mtime

    def _write_disk(self, key, data: bytes) -> None:
        f = self._path(key)
        if f is None:
            return
        tmp = f.with_name(f".{f.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            with open(tmp, "xb") as fh:
                fh.write(data)
            os.replace(tmp, f)
        except OSError:
            try:
                tmp.unlink()
            except OSError:
                pass
            return
        now = time.time()
        if now - self._last_trim > 60:
            self._last_trim = now
            self._trim()

    def _trim(self) -> None:
        try:
            items = []
            with os.scandir(self.dir) as it:
                for e in it:
                    if e.is_file(follow_symlinks=False) and e.name.endswith(".tile"):
                        st = e.stat(follow_symlinks=False)
                        items.append((st.st_mtime, st.st_size, e.path))
            total = sum(i[1] for i in items)
            if total <= self.limit:
                return
            for _mt, sz, path in sorted(items):
                if total <= self.limit * 0.8:
                    break
                try:
                    os.unlink(path)
                    total -= sz
                except OSError:
                    pass
        except OSError:
            pass

    def _load(self, key):
        cached = self._read_disk(key)
        if cached is not None and cached[1] < FRESH_SECONDS:
            self.stats["disk"] += 1
            return cached[0]
        data = self._download(key) if time.time() >= self._cool_until else None
        if data is not None:
            self._write_disk(key, data)
            self.stats["net"] += 1
            return data
        if cached is not None and cached[1] < STALE_OK_SECONDS:      # offline / cooling down: an old tile beats no tile
            self.stats["disk"] += 1
            return cached[0]
        return None

    def _download(self, key):
        import requests
        z, x, y = key
        url = self.template.replace("{z}", str(z)).replace("{x}", str(x)).replace("{y}", str(y))
        try:
            if self._session is None:
                self._session = requests.Session()
            r = self._session.get(url, headers={"User-Agent": USER_AGENT, "Accept": "image/png,image/jpeg"},
                                  timeout=(4, 8), stream=True, allow_redirects=False)
            try:
                if r.status_code != 200:
                    if r.status_code == 429 or r.status_code >= 500:
                        self._cool_until = time.time() + COOL_DOWN
                        self.online_error = f"the map server answered {r.status_code}"
                    return None
                if r.headers.get("Content-Type", "").split(";")[0].strip().lower() not in ("image/png", "image/jpeg"):
                    self.stats["bad"] += 1
                    return None
                buf = bytearray()
                for chunk in r.iter_content(16384):
                    buf += chunk
                    if len(buf) > MAX_TILE_BYTES:
                        self.stats["bad"] += 1
                        return None
            finally:
                r.close()
        except Exception as e:                                      # offline, DNS, timeout …
            self._cool_until = time.time() + COOL_DOWN
            self.online_error = type(e).__name__
            return None
        data = bytes(buf)
        if not valid_tile(data):
            self.stats["bad"] += 1
            return None
        self.online_error = ""
        return data


class Geocoder:
    """Place name → position (OpenStreetMap Nominatim). One request per 1.1 s, answers remembered."""

    def __init__(self, url: str = NOMINATIM, lang: str = "en", allow_local: bool = False, min_interval: float = 1.1):
        u = urlparse(url)
        ok = u.scheme == "https" or (allow_local and u.scheme == "http" and u.hostname in LOCAL_HOSTS)
        self.url = url if ok and "@" not in u.netloc else NOMINATIM
        self.lang = _clean(lang, 20) or "en"
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0
        self._cache: OrderedDict = OrderedDict()

    def search(self, query: str) -> dict:
        """→ {"lat", "lon", "label", "bbox": (south, north, west, east) | None}. Raises MapError with a sentence for the user."""
        q = _clean(query, 100)
        if len(q) < 2:
            raise MapError("Which place?")
        key = q.casefold()
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        import requests
        with self._lock:                                          # one request at a time, spaced out
            wait = self._last + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                r = requests.get(self.url, params={"q": q, "format": "jsonv2", "limit": 1, "accept-language": self.lang},
                                 headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=(4, 8),
                                 stream=True, allow_redirects=False)
                try:
                    if r.status_code != 200:
                        raise MapError("The place search is busy — try again in a moment." if r.status_code in (429, 503)
                                       else "The place search didn't answer.")
                    raw = r.raw.read(MAX_JSON_BYTES + 1, decode_content=True)
                finally:
                    r.close()
            except MapError:
                raise
            except Exception:
                raise MapError("I can't reach the map service — are you online?")
        if len(raw) > MAX_JSON_BYTES:
            raise MapError("The place search gave an answer I can't use.")
        try:
            items = json.loads(raw.decode("utf-8", "replace"))
            if not isinstance(items, list):
                raise ValueError
        except ValueError:
            raise MapError("The place search gave an answer I can't use.")
        if not items:
            raise MapError(f"I can't find “{q[:40]}” on the map.")
        it = items[0]
        try:
            lat, lon = float(it["lat"]), float(it["lon"])
            if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0) or lat != lat or lon != lon:
                raise ValueError
            bbox = None
            bb = it.get("boundingbox")
            if isinstance(bb, list) and len(bb) == 4:
                s, n, w, e = (float(v) for v in bb)
                if -90 <= s <= n <= 90 and -180 <= w <= 180 and -180 <= e <= 180:
                    bbox = (s, n, w, e)
        except (KeyError, TypeError, ValueError):
            raise MapError("The place search gave an answer I can't use.")
        label = _clean(it.get("display_name") or it.get("name") or q, 140)
        res = {"lat": lat, "lon": lon, "label": label, "bbox": bbox}
        self._cache[key] = res
        while len(self._cache) > 64:
            self._cache.popitem(last=False)
        return res
