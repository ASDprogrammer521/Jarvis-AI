"""What is playing right now, in any player — for the Dynamic Island.

Windows: the system media session (the same one the volume flyout shows — Spotify, Chrome, YouTube, Apple Music, VLC…) through
the optional `winsdk` package: title, artist, album, cover picture, position / length, and the transport buttons.
    pip install winsdk
Without it (or on another system) Jarvis falls back to Spotify's window title — title and artist only, no cover and no progress.
"""
from __future__ import annotations

import json
import sys
import time

_ART: dict = {"key": None, "bytes": None}                        # the cover is read once per song, not every poll


def _blank(**k) -> dict:
    d = {"state": "none", "title": "", "artist": "", "album": "", "pos": 0.0, "end": 0.0, "app": "", "thumb": None, "src": "", "t": time.time()}
    d.update(k)
    return d


async def _manager():
    from winsdk.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M
    return await M.request_async()


async def _read_smtc() -> dict | None:
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
            th = props.thumbnail
            if th is not None:
                st = await th.open_read_async()
                n = int(st.size)
                if 0 < n < 6_000_000:
                    from winsdk.windows.storage.streams import DataReader
                    rd = DataReader(st.get_input_stream_at(0))
                    await rd.load_async(n)
                    buf = bytearray(n)
                    rd.read_bytes(buf)
                    _ART["bytes"] = bytes(buf)
        except Exception:                                         # noqa: BLE001
            pass
    end = max(0.0, tl.end_time.total_seconds() - tl.start_time.total_seconds())
    pos = max(0.0, tl.position.total_seconds() - tl.start_time.total_seconds())
    return _blank(state="playing" if status == 4 else "paused", title=title, artist=artist, album=str(props.album_title or ""),
                  pos=pos, end=end if end < 86400 else 0.0, app=str(s.source_app_user_model_id or ""), thumb=_ART["bytes"], src="smtc")


def _smtc() -> dict | None:
    if sys.platform != "win32":
        return None
    try:
        import asyncio
        return asyncio.run(_read_smtc())
    except Exception:                                             # noqa: BLE001 - winsdk missing or no session service
        return None


def snapshot() -> dict:
    """{"state": playing|paused|none, "title", "artist", "album", "pos", "end" (seconds), "app", "thumb" (image bytes | None), "t"}."""
    d = _smtc()
    if d is not None:
        return d
    try:
        from core import space_feeds
        j = json.loads(space_feeds.nowplaying_json())
        return _blank(state=j.get("state", "none"), title=j.get("title", ""), artist=j.get("artist", ""), src="spotify")
    except Exception:                                             # noqa: BLE001
        return _blank()


def control(action: str, value: float = 0.0) -> None:
    """action: toggle | next | previous | seek (value = seconds from the start)."""
    if sys.platform == "win32":
        try:
            import asyncio

            async def go():
                s = (await _manager()).get_current_session()
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
            asyncio.run(go())
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
