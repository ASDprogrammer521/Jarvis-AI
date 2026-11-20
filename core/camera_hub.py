"""One camera, several features.

A webcam can only be opened by one program at a time, so hand gestures and face recognition share this single
capture loop. The loop runs only while at least one of them is switched on (or a face is being enrolled) and
releases the camera as soon as nothing needs it.
"""
from __future__ import annotations

import os
import threading
import time

from core import face_id, gesture, hand_cursor

HAND_GAP_IDLE = 1 / 10     # no hand in view for HAND_IDLE_AFTER seconds: look for one only ~10 times a second (a hand shows up within 0.1 s)
HAND_IDLE_AFTER = 2.0
PREVIEW_GAP = 1 / 25      # the camera preview panel needs ~25 fps
HAND_GAP_GESTURES = 1 / 15    # one-shot gestures only need ~15 fps; the hand mouse and Hand Space still use every frame
FACE_EVERY = 0.4          # seconds between face checks while somebody is in view (gestures are checked on every frame)
FACE_IDLE = 0.6           # …and while nobody is: only a quick search for a face, no recogniser
FACE_EVERY_SPACE = 2.0    # …but while Hand Space is open the CPU belongs to the hand tracking (the face lock only needs a sighting now and then)
PROFILE = bool(os.environ.get("JARVIS_PROFILE"))
KNOWN_COOLDOWN = 45.0     # don't greet the same person again within this many seconds
UNKNOWN_COOLDOWN = 60.0
ENROLL_SAMPLES = 6
ENROLL_TIMEOUT = 20.0
MODEL_W = 640             # hand / face models always get a picture this wide, however big the camera frame is
SIZE_LADDER = [(1920, 1080), (1280, 720), (640, 480)]   # live view: the biggest of these the camera AND the monitor can use
IDLE_SIZE = (640, 480)       # no live view: gestures and faces don't need more



def _safe_release(obj) -> None:
    """cap.release() can raise "Unknown C++ exception from OpenCV code" when the driver is already gone (camera unplugged, GPU
    switching on laptops): that must never kill the camera thread or print a traceback."""
    try:
        obj.release()
    except Exception:
        pass


class _HandWorker(threading.Thread):
    """Runs Hand Space's hand detection off the camera thread.

    Before, the camera loop did `read frame → run the model → read the next frame`, so the camera sat idle while the model
    worked (and the other way round) — about half the possible frame rate, in visible steps. Now the camera keeps reading
    while the model works on the previous frame. Only the newest frame ever waits; if the model is still busy the camera
    thread simply skips that frame, so the picture on screen never falls behind the hand."""

    def __init__(self, reader, on_result):
        super().__init__(daemon=True, name="jarvis-hands")
        self.reader = reader
        self.on_result = on_result
        self.error: Exception | None = None
        self.ms = 0.0
        self.frames = 0
        self._frame = None
        self._busy = False
        self._quit = False
        self._cv = threading.Condition()

    def ready(self) -> bool:
        return not self._busy and self._frame is None and not self._quit

    def submit(self, rgb) -> None:
        with self._cv:
            self._frame = rgb
            self._cv.notify()

    def stop(self) -> None:
        with self._cv:
            self._quit = True
            self._frame = None
            self._cv.notify()
        if threading.current_thread() is not self:
            self.join(timeout=3)

    def run(self):
        while True:
            with self._cv:
                while self._frame is None and not self._quit:
                    self._cv.wait(0.25)
                if self._quit:
                    return
                rgb, self._frame = self._frame, None
                self._busy = True
            try:
                t0 = time.perf_counter()
                hands = self.reader.detect_all(rgb)
                self.ms = self.ms * 0.9 + (time.perf_counter() - t0) * 1000 * 0.1 if self.frames else (time.perf_counter() - t0) * 1000
                self.frames += 1
                if not self._quit:
                    self.on_result(hands)
            except Exception as e:                # surfaced by the camera loop as the camera status
                self.error = e
                return
            finally:
                self._busy = False


class _FaceWorker(threading.Thread):
    """Finds and recognises faces off the camera thread, so Hand Space and the gestures never wait for it.

    Per check: look for faces on a small picture; only when one is there does the recogniser start, and a known face that
    stays where it was is trusted for a few seconds (face_id.FaceTracker). The camera keeps reading frames meanwhile and
    hands over only the newest one; while this thread is busy, frames for it are simply skipped."""

    def __init__(self, hub):
        super().__init__(daemon=True, name="jarvis-faces")
        self.hub = hub
        self.error: Exception | None = None
        self.present = False                  # was a face in the last picture? (decides how often the camera hands one over)
        self.ms = 0.0
        self.checks = 0
        self._frame = None
        self._busy = False
        self._quit = False
        self._cv = threading.Condition()
        self._tracker = face_id.FaceTracker()
        self._last_known: dict[str, float] = {}
        self._last_unknown = 0.0
        self._unknown_streak = 0

    def ready(self) -> bool:
        return not self._busy and self._frame is None and not self._quit and self.error is None

    def submit(self, frame, now: float) -> None:
        with self._cv:
            self._frame = (frame, now)
            self._cv.notify()

    def stop(self) -> None:
        with self._cv:
            self._quit = True
            self._frame = None
            self._cv.notify()
        if threading.current_thread() is not self:
            self.join(timeout=3)

    def run(self):
        hub = self.hub
        engine = None
        while True:
            with self._cv:
                while self._frame is None and not self._quit:
                    self._cv.wait(0.25)
                if self._quit:
                    return
                (frame, now), self._frame = self._frame, None
                self._busy = True
            try:
                t0 = time.perf_counter()
                if engine is None:
                    hub._set("loading face model…")
                    engine = face_id.FaceEngine()
                    hub._set("active")
                self._check(engine, frame, now)
                ms = (time.perf_counter() - t0) * 1000
                self.ms = self.ms * 0.9 + ms * 0.1 if self.checks else ms
                self.checks += 1
                if PROFILE and self.checks % 20 == 0:
                    print(f"[camera] face check {self.ms:.1f} ms ({'face in view' if self.present else 'empty'}, "
                          f"recogniser {'on' if engine.rec_ready else 'not loaded'})")
            except Exception as e:                # surfaced by the camera loop as the camera status
                self.error = e
                return
            finally:
                self._busy = False

    def _check(self, engine, frame, now: float):
        hub = self.hub
        faces = engine.detect(frame)
        self.present = bool(faces)
        enroll = hub._enroll
        if enroll is not None:
            hub._enroll_step(engine, frame, faces, enroll, now)
        if not hub._f:
            return
        if faces and not engine.rec_ready:
            hub._set("loading face model…")                   # the recogniser starts only now that a face is in view
            engine.rec
            hub._set("active")
        names = self._tracker.resolve(faces, now, lambda f: engine.identify(engine.embed(frame, f))[0]) if faces else self._tracker.resolve([], now, None)
        seen = [n or "unknown" for n in names]
        hub.visible, hub.seen_at = seen, now
        try:
            hub.on_seen([n for n in seen if n != "unknown"])
        except Exception:
            pass
        for name in {s for s in seen if s != "unknown"}:
            if now - self._last_known.get(name, 0.0) > KNOWN_COOLDOWN:   # back after a break, not just still sitting there
                hub.on_face("known", name)
            self._last_known[name] = now
        if "unknown" in seen and face_id.names():
            self._unknown_streak += 1
            if self._unknown_streak >= 3 and now - self._last_unknown > UNKNOWN_COOLDOWN and face_id.alert_unknown():
                self._last_unknown = now
                hub.on_face("unknown", "")
        else:
            self._unknown_streak = 0


class CameraHub:
    """Callbacks run on the camera thread — the UI must hand them over to the Qt thread.

    on_gesture(action_id, gesture_id) · on_face(kind, name) · on_status(text)
    kind: known | unknown | enrolled | enroll_failed
    """

    def __init__(self, on_gesture, on_face, on_status=None, on_cursor=None, on_cursor_panic=None, on_seen=None, on_hand=None, on_space=None):
        self.on_seen = on_seen or (lambda known: None)               # every face check: list of saved names in view
        self.on_gesture = on_gesture
        self.on_cursor = on_cursor or (lambda x, y, s: None)          # hand-cursor overlay feed: (x, y, state)
        self.on_cursor_panic = on_cursor_panic or (lambda: None)
        self.on_hand = on_hand or (lambda pts, pinch: None)           # hand skeleton for the overlay
        self.on_space = on_space or (lambda hands: None)              # Hand Space: landmarks of every hand in view
        self.space = False                                            # set by the window while Hand Space is open
        self._preview = False                                         # set while the camera preview panel is open: frames are shared, not opened twice
        self._claims: set[str] = set()                                # other users of the picture (the workout counter): same HD frames, one camera
        self.max_size: tuple[int, int] | None = None                  # monitor size: the live view never asks the camera for more than fits it
        self.latest = None                                            # newest BGR frame, for the preview panel (read-only: never change it)
        self._hand_seen = 0.0                                         # last time any hand was in view
        self.on_face = on_face
        self.on_status = on_status or (lambda s: None)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._g = False
        self._c = False
        self._f = False
        self._enroll: dict | None = None
        self._lock = threading.Lock()
        self.status = "off"
        self.current_gesture: str | None = None
        self.visible: list[str] = []        # names (or "unknown") seen in the last face check
        self.seen_at = 0.0

    @property
    def preview(self) -> bool:
        """True while anyone needs the shared HD picture (the preview panel or a claim, e.g. the workout counter)."""
        return self._preview or bool(self._claims)

    @preview.setter
    def preview(self, on: bool):
        self._preview = bool(on)

    def claim(self, who: str) -> None:
        """Ask for the shared picture (hub.latest) without opening a second capture: Windows lets one program own a camera."""
        with self._lock:
            self._claims.add(who)

    def release(self, who: str) -> None:
        with self._lock:
            self._claims.discard(who)

    def snapshot_jpeg(self, max_w: int = 1280, quality: int = 88) -> bytes | None:
        """The newest preview frame as a JPEG, or None while the live view has no picture yet (used by the vision tool)."""
        frame = self.latest
        if frame is None:
            return None
        import cv2
        h, w = frame.shape[:2]
        if w > max_w:
            frame = cv2.resize(frame, (max_w, max(1, int(h * max_w / w))), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
        return buf.tobytes() if ok else None

    # ── control ──────────────────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def refresh(self):
        """Re-read the on/off switches and start or stop the camera accordingly."""
        self._g = gesture.is_enabled()
        self._c = hand_cursor.is_enabled()
        self._f = face_id.is_enabled()
        if self._wanted():
            if not self.running:
                self._stop.clear()
                self._thread = threading.Thread(target=self._run, daemon=True, name="jarvis-camera")
                self._thread.start()
        elif self.running:
            self.stop()
        else:
            self._set("off")

    def stop(self):
        self._stop.set()
        t = self._thread
        if t and t is not threading.current_thread():
            t.join(timeout=4)
        self._thread = None
        self._set("off")

    def enroll(self, name: str):
        with self._lock:
            self._enroll = {"name": name.strip()[:40], "vecs": [], "t0": time.time(), "last": 0.0}
        self.refresh()          # starts the camera if it was off

    def _wanted(self) -> bool:
        return self._g or self._c or self.space or self._f or self.preview or self._enroll is not None

    def _space_result(self, hands):
        if hands:
            self._hand_seen = time.time()
        self.on_space(hands)

    def _set(self, s: str):
        self.status = s
        try:
            self.on_status(s)
        except Exception:
            pass

    # ── loop ─────────────────────────────────────────────────────────────────────
    def _sizes(self, hd: bool):
        if not hd:
            return [IDLE_SIZE]
        mw = max(1280, (self.max_size or (1280, 720))[0])
        return [sz for sz in SIZE_LADDER if sz[0] <= mw] or [SIZE_LADDER[-1]]

    def _open(self, hd: bool = False):
        import cv2
        idx = gesture.get_camera()
        cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(idx)
        if not cap.isOpened():
            _safe_release(cap)
            raise RuntimeError("camera is busy or not found")
        try:
            cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))   # much faster than raw frames on most webcams
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)                              # always the newest frame, no lag
            # Ask for the biggest size first and keep the first one the camera really delivers (a driver may accept the
            # request and still send something smaller, so the size is checked on a real frame, not on cap.get()).
            for w, h in self._sizes(hd):
                cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
                cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
                got = 0
                for _ in range(4):
                    ok, f = cap.read()
                    if ok and f is not None:
                        got = f.shape[1]
                if got >= w * 0.9:
                    break
        except Exception:
            pass
        return cap

    def _run(self):
        cap = hands = faceworker = worker = None
        hd = False
        tracker = gesture.GestureTracker()
        cursor = None
        last_face = 0.0
        last_hand = 0.0
        _prof = [0.0, 0]
        last_prev = 0.0
        self._hand_seen = time.time()
        try:
            self._set("starting…")
            import cv2
            hd = self.preview                                  # open at the right size at once (no open → close → reopen)
            cap = self._open(hd)
            self._set("active")
            while not self._stop.is_set():
                if not self._wanted():
                    break
                if self.preview != hd:                 # live view opened / closed: reopen the camera at the right size
                    hd = self.preview
                    _safe_release(cap)
                    cap = self._open(hd)
                if faceworker is not None and faceworker.error is not None:
                    raise faceworker.error             # the face thread failed (missing model, old OpenCV…): show it as the camera status
                # Skip frames nobody needs: grab() drops a frame without decoding it, so the hand / face models stay idle
                t_now = time.time()
                hands_on = self._g or self._c or self.space
                hand_gap = 0.0 if (self._c or self.space) else HAND_GAP_GESTURES
                if t_now - self._hand_seen > HAND_IDLE_AFTER:
                    hand_gap = max(hand_gap, HAND_GAP_IDLE)                 # nobody is gesturing: keep the CPU for something else
                hand_due = hands_on and t_now - last_hand >= hand_gap
                preview_due = self.preview and t_now - last_prev >= PREVIEW_GAP
                if self._enroll is not None or (not self.space and faceworker is not None and faceworker.present):
                    face_gap = FACE_EVERY
                elif self.space:
                    face_gap = FACE_EVERY_SPACE
                else:
                    face_gap = FACE_IDLE if faceworker is not None else FACE_EVERY
                face_due = ((self._f or self._enroll is not None) and t_now - last_face >= face_gap
                            and (faceworker is None or faceworker.ready()))
                cleanup = (not hands_on and (hands is not None or cursor is not None)) or (not self._f and faceworker is not None and self._enroll is None)
                if not (hand_due or face_due or cleanup or preview_due):
                    cap.grab()
                    time.sleep(0.008)
                    continue
                ok, frame = cap.read()
                if not ok:
                    time.sleep(0.03)
                    continue
                now = time.time()
                if self.preview:
                    self.latest = frame
                    last_prev = now
                # the models never see more than MODEL_W pixels across: HD frames are for the picture on screen only
                fw = frame.shape[1]
                work = frame
                if fw > MODEL_W + 60 and (hand_due or face_due):
                    work = cv2.resize(frame, (MODEL_W, max(1, int(frame.shape[0] * MODEL_W / fw))), interpolation=cv2.INTER_AREA)

                # ── hands: Hand Space (two hands) > cursor mode (hand = mouse) > one-shot gestures ──
                if hands_on and not hand_due:
                    pass                                   # this frame was read for the preview / face only: the hand models rest
                elif self._g or self._c or self.space:
                    last_hand = now
                    need = 2 if self.space else 1
                    if hands is not None and getattr(hands, "num_hands", 1) != need:
                        if worker is not None:
                            worker.stop()
                            worker = None
                        hands.close()
                        hands = None
                    if hands is None:
                        hands = gesture.HandReader(need)
                    if worker is not None and not self.space:
                        worker.stop()                  # left Hand Space: the reader is used directly again
                        worker = None
                    if cursor is not None and (self.space or not self._c):
                        cursor.release()
                        cursor = None
                        (self.on_hand(None, 0.0), self.on_cursor(0, 0, "lost"))
                    if self.space:
                        if worker is None or worker.reader is not hands:
                            if worker is not None:
                                worker.stop()
                            worker = _HandWorker(hands, self._space_result)
                            worker.start()
                        if worker.error is not None:
                            raise worker.error
                        if worker.ready():
                            worker.submit(cv2.cvtColor(work, cv2.COLOR_BGR2RGB))
                        self.current_gesture = None
                        if PROFILE:
                            _prof[1] += 1
                            if _prof[1] >= 90:
                                print(f"[camera] hand detection {worker.ms:.1f} ms/frame on {hands.backend} "
                                      f"({worker.frames} processed)")
                                _prof[1] = 0
                    else:
                        rgb = cv2.cvtColor(work, cv2.COLOR_BGR2RGB)
                        lm = hands.detect(rgb)
                        if lm:
                            self._hand_seen = now
                        if self._c:
                            if cursor is None:
                                cursor = hand_cursor.HandCursor(on_state=self.on_cursor, on_panic=self.on_cursor_panic, on_hand=self.on_hand)
                            cursor.update(lm, now)
                            self.current_gesture = "cursor:" + cursor.state
                        else:
                            fired = tracker.update(gesture.classify(lm) if lm else None, now)
                            self.current_gesture = tracker.current
                            if fired:
                                self.on_gesture(fired[1], fired[0])
                else:
                    if cursor is not None:
                        cursor.release()
                        cursor = None
                        (self.on_hand(None, 0.0), self.on_cursor(0, 0, "lost"))
                    if worker is not None:
                        worker.stop()
                        worker = None
                    if hands is not None:
                        hands.close()
                        hands = None
                    self.current_gesture = None

                # ── faces: handed to their own thread; this loop only passes on the newest frame ──
                enroll = self._enroll
                if self._f or enroll:
                    if faceworker is not None and faceworker.error is not None:
                        raise faceworker.error
                    if face_due:
                        if faceworker is None:
                            faceworker = _FaceWorker(self)
                            faceworker.start()
                        last_face = now
                        faceworker.submit(work, now)
                elif faceworker is not None:
                    faceworker.stop()
                    faceworker = None
                    self.visible = []
                time.sleep(0.005)
        except ImportError as e:
            self._set(f"missing package: pip install opencv-python mediapipe ({e.name})")
        except Exception as e:
            self._set(f"error: {e}")
            if self._enroll is not None:
                name, self._enroll = self._enroll["name"], None
                self.on_face("enroll_failed", f"{name}: {e}")
        finally:
            self.latest = None
            if cap is not None:
                _safe_release(cap)
            if cursor is not None:
                _safe_release(cursor)
                (self.on_hand(None, 0.0), self.on_cursor(0, 0, "lost"))
            if worker is not None:
                worker.stop()
            if faceworker is not None:
                faceworker.stop()
            if hands is not None:
                hands.close()
            self.current_gesture = None
            self.visible = []
            if not self.status.startswith(("error", "missing")):
                self._set("off")

    def _enroll_step(self, engine, frame, faces, enroll, now):                  # runs on the face thread
        if now - enroll["t0"] > ENROLL_TIMEOUT:
            self._enroll = None
            self.on_face("enroll_failed", f"{enroll['name']}: I didn't see a clear face — look straight at the camera")
            return
        if len(faces) != 1 or float(faces[0][-1]) < 0.9 or now - enroll["last"] < 0.5:
            return
        enroll["last"] = now
        enroll["vecs"].append(engine.embed(frame, faces[0]))
        if len(enroll["vecs"]) >= ENROLL_SAMPLES:
            face_id.add_person(enroll["name"], enroll["vecs"])
            self._enroll = None
            self.on_face("enrolled", enroll["name"])

#by ASDcuber
