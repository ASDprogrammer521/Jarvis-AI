"""What is playing right now, in any player — for the Dynamic Island and Hand Space.

Windows: the system media session (the same one the volume flyout shows — Spotify, Chrome, YouTube, Apple Music, VLC…).
It is read through the `winrt-*` packages (the maintained successor of `winsdk`; they have wheels for Python 3.14):

    pip install winrt-runtime winrt-Windows.Foundation winrt-Windows.Foundation.Collections ^
                winrt-Windows.Media.Control winrt-Windows.Storage.Streams

`winsdk` is still used if it happens to be installed (Python <= 3.12). Without either, Jarvis falls back to Spotify's
window title — title and artist only, no cover and no progress.

One background thread owns one asyncio loop and one session manager, and refreshes a cached snapshot about once a second
(only while somebody asks for it), so the Island and Hand Space together cost one cheap poll, not two.
"""
from __future__ import annotations

import asyncio
import json
import sys
import threading
import time

_ART: dict = {"key": None, "bytes": None}                        # the cover is read once per song, not every poll
_API: dict = {}                                                  # the loaded WinRT classes (or {"ok": False})
_LOCK = threading.Lock()
_STATE: dict = {"snap": None, "t": 0.0, "asked": 0.0, "thread": None, "loop": None, "mgr": None, "stop": False}
_REFRESH = 1.0                                                   # seconds between system reads while somebody is asking
_IDLE_AFTER = 8.0                                                # nobody asked for this long: the worker sleeps


def _blank(**k) -> dict:
    d = {"state": "none", "title": "", "artist": "", "album": "", "pos": 0.0, "end": 0.0, "app": "", "thumb": None, "src": "", "t": time.time()}
    d.update(k)
    return d


def _load_api() -> bool:
    """Import WinRT once: winrt-* first (works on every current Python), winsdk as the old fallback."""
    if "ok" in _API:
        return bool(_API["ok"])
    if sys.platform != "win32":
        _API["ok"] = False
        return False
    try:
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M
        from winrt.windows.storage.streams import DataReader
        _API.update(ok=True, M=M, DataReader=DataReader, lib="winrt")
        return True
    except Exception:                                             # noqa: BLE001 - not installed / not supported here
        pass
    try:
        from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M
        from winsdk.windows.storage.streams import DataReader
        _API.update(ok=True, M=M, DataReader=DataReader, lib="winsdk")
        return True
    except Exception:                                             # noqa: BLE001
        _API["ok"] = False
        return False


async def _manager():
    mgr = _STATE["mgr"]
    if mgr is None:
        mgr = _STATE["mgr"] = await _API["M"].request_async()
    return mgr


async def _read_cover(props) -> bytes | None:
    th = props.thumbnail
    if th is None:
        return None
    st = await th.open_read_async()
    n = int(st.size)
    if not 0 < n < 6_000_000:
        return None
    rd = _API["DataReader"](st.get_input_stream_at(0))
    await rd.load_async(n)
    buf = bytearray(n)
    rd.read_bytes(buf)
    return bytes(buf)


async def _read_smtc() -> dict:
    s = (await _manager()).get_current_session()
    if s is None:
        return _blank(src="smtc")
    props = await s.try_get_media_properties_async()
    pb, tl = s.get_playback_info(), s.get_timeline_properties()
    status = int(pb.playback_status)                              # 4 = playing, 5 = paused
    title, artist = str(props.title or ""), str(props.artist or "")
    if not title and not artist:
        return _blank(src="smtc")
    key = (title, artist, str(props.album_title or ""))
    if _ART["key"] != key:
        _ART["key"], _ART["bytes"] = key, None
        try:
            _ART["bytes"] = await _read_cover(props)
        except Exception:                                         # noqa: BLE001
            pass
    end = max(0.0, tl.end_time.total_seconds() - tl.start_time.total_seconds())
    pos = max(0.0, tl.position.total_seconds() - tl.start_time.total_seconds())
    return _blank(state="playing" if status == 4 else "paused", title=title, artist=artist, album=str(props.album_title or ""),
                  pos=pos, end=end if end < 86400 else 0.0, app=str(s.source_app_user_model_id or ""), thumb=_ART["bytes"], src="smtc")


# ── the worker: one thread, one loop, one manager ───────────────────────────────────────────────────────────────────
def _worker():
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _STATE["loop"] = loop
    fails = 0
    try:
        while not _STATE["stop"]:
            now = time.time()
            if now - _STATE["asked"] > _IDLE_AFTER:               # nobody is looking: stop reading until asked again
                time.sleep(0.25)
                continue
            try:
                snap = loop.run_until_complete(asyncio.wait_for(_read_smtc(), 4.0))
                fails = 0
            except Exception:                                     # noqa: BLE001 - session service hiccup: start fresh next time
                _STATE["mgr"] = None
                fails += 1
                snap = None
            with _LOCK:
                _STATE["snap"], _STATE["t"] = snap, time.time()
            time.sleep(_REFRESH if fails < 3 else 5.0)
    finally:
        loop.close()
        _STATE["loop"] = None


def _ensure_worker():
    th = _STATE["thread"]
    if th is None or not th.is_alive():
        _STATE["stop"] = False
        th = threading.Thread(target=_worker, name="media-session", daemon=True)
        _STATE["thread"] = th
        th.start()


def _smtc() -> dict | None:
    if not _load_api():
        return None
    _STATE["asked"] = time.time()
    _ensure_worker()
    for _ in range(40):                                           # the very first call waits (at most 2 s) for the first read
        with _LOCK:
            snap, t = _STATE["snap"], _STATE["t"]
        if snap is not None or t > 0.0:
            break
        time.sleep(0.05)
    with _LOCK:
        snap = _STATE["snap"]
    return dict(snap) if snap is not None else None


def snapshot() -> dict:
    """{"state": playing|paused|none, "title", "artist", "album", "pos", "end" (seconds), "app", "thumb" (image bytes | None), "t"}."""
    d = _smtc()
    if d is not None:
        d["t"] = time.time()
        return d
    try:
        from core import space_feeds
        j = json.loads(space_feeds.nowplaying_json())
        return _blank(state=j.get("state", "none"), title=j.get("title", ""), artist=j.get("artist", ""), src="spotify")
    except Exception:                                             # noqa: BLE001
        return _blank()


def is_playing() -> bool:
    """True while something is really playing (cheap: reads the cached snapshot)."""
    return snapshot().get("state") == "playing"


def control(action: str, value: float = 0.0) -> None:
    """action: toggle | next | previous | seek (value = seconds from the start)."""
    if sys.platform == "win32" and _load_api():
        try:                                                      # a short-lived loop: the worker's loop is busy between its reads
            lp = asyncio.new_event_loop()
            try:
                mgr = lp.run_until_complete(_API["M"].request_async())

                async def go2():
                    s = mgr.get_current_session()
                    if s is None:
                        return
                    if action == "toggle":
                        await s.try_toggle_play_pause_async()
                    elif action == "next":
                        await s.try_skip_next_async()
                    elif action == "previous":
                        await s.try_skip_previous_async()
                    elif action == "seek":
                        await s.try_change_playback_position_async(int(max(0.0, value) * 10_000_000))
                lp.run_until_complete(go2())
            finally:
                lp.close()
            _STATE["asked"] = time.time()
            return
        except Exception:                                         # noqa: BLE001
            pass
    if action in ("toggle", "next", "previous"):                  # no media session: Spotify's own keys
        try:
            from actions import spotify_controller as sc
            sc._transport(action)
        except Exception:                                         # noqa: BLE001
            pass

#by ASDcuber
