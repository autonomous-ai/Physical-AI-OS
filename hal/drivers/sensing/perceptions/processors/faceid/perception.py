"""FacePerception — Perception state machine wrapping FaceRecognizer."""

import json
import logging
import re
import shutil
import threading
import time
from collections import deque
from copy import copy
from pathlib import Path
from typing import Any, Callable, override

import cv2
import numpy as np
import requests

import hal.config as config
from hal.drivers.sensing.perceptions.models import (
    Face,
    FaceDetectionData,
    PersonData,
    PersonKind,
)
from hal.drivers.sensing.perceptions.typing import SendEventCallable
from hal.drivers.sensing.perceptions.utils import PerceptionStateObservers
from hal.drivers.sensing.presence_service import PresenseService

from ..base import Perception
from .constants import (
    _STRANGER_SNAPSHOTS_DIR,
    _STRANGER_STATS_FILE,
    USERS_DIR,
)
from .enter_message import (
    FrameFacts,
    build_enter_message,
    frame_labels,
)
from .recognizer import FaceRecognizer
from .stranger_gaze import GazeMeasurement, StrangerGazeTick, gaze_confirmed, measure_gaze

logger = logging.getLogger(__name__)

# Presence memory sidecar — who was last seen when. tmpfs + boot_id makes it boot-scoped
# (same pattern as the scene sidecar): an in-boot HAL service restart must NOT wipe
# last_seen.
_PRESENCE_STATE_PATH = Path("/tmp/hal-presence-state.json")


def _current_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except Exception:
        return ""


_FAMILIAR_VISIT_THRESHOLD = 2

_ENROLL_IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}


class FacePerception(Perception[cv2.typing.MatLike]):
    """SCRFD + ONNX-landmark + EdgeFace face recognizer. Detects friends and strangers, fires presence events."""

    FRIEND_PREFIX: str = "friend_"
    STRANGER_PREFIX: str = "stranger_"

    def __init__(
        self,
        perception_state: PerceptionStateObservers,
        send_event: SendEventCallable,
        presense_service: PresenseService | None = None,
        threshold: float = config.FACE_MATCH_THRESHOLD,
        extended_threshold: float = config.FACE_EXTENDED_THRESHOLD,
        stranger_threshold: float = config.FACE_STRANGER_THRESHOLD,
        extend_min_enroll_sim: float = config.FACE_EXTEND_MIN_ENROLL_SIM,
        negative_threshold: float | None = 0.2,
        max_strangers: int = 50,
        height_ratio_threshold: float = config.FACE_HEIGHT_RATIO_THRESHOLD,
        max_truncation: float = config.FACE_MAX_TRUNCATION,
        min_sharpness: float = config.FACE_MIN_SHARPNESS,
        stranger_min_ticks: int = config.FACE_STRANGER_MIN_TICKS,
        stranger_corroboration_s: float = config.FACE_STRANGER_CORROBORATION_S,
        owners_forget_ts: float = config.FACE_OWNER_FORGET_S,
        strangers_forget_ts: float = config.FACE_STRANGER_FORGET_S,
        max_extended_images: int = 10,
        diversity_threshold: float = 0.7,
    ):
        super().__init__(perception_state, send_event)

        self._presense_service: PresenseService | None = presense_service
        self._face_recognizer: FaceRecognizer = FaceRecognizer(
            height_ratio_threshold=height_ratio_threshold,
            max_truncation=max_truncation,
            min_sharpness=min_sharpness,
            stranger_min_ticks=stranger_min_ticks,
            stranger_corroboration_s=stranger_corroboration_s,
            threshold=threshold,
            extended_threshold=extended_threshold,
            stranger_threshold=stranger_threshold,
            extend_min_enroll_sim=extend_min_enroll_sim,
            negative_threshold=negative_threshold,
            max_strangers=max_strangers,
            max_extended_images=max_extended_images,
            diversity_threshold=diversity_threshold,
        )
        self._face_recognizer.start()
        self._owners_forget_ts: float = owners_forget_ts
        self._strangers_forget_ts: float = strangers_forget_ts

        self._faces_n: int = 0
        self._face_present: bool = False
        self._people_data_dict: dict[str, PersonData] = {}
        self._last_stranger_enter_ts: float = 0.0
        self._last_presence_save_ts: float = 0.0
        self._load_presence_state()
        self._owners: set[str] = set()
        self._strangers: set[str] = set()

        self._any_stranger_logged: bool = False
        self._stranger_visit_counts: dict[str, Any] = self._load_stranger_stats()

        # Ungreeted strangers this visit -> familiar-stranger snapshot for their greeting (#531).
        self._ungreeted_strangers: dict[str, str | None] = {}
        # Recent ticks an ungreeted stranger was in frame: snapshot, who faced the lamp, when.
        self._stranger_gaze_ticks: deque[StrangerGazeTick] = deque(
            maxlen=config.FACE_STRANGER_GAZE_TICKS
        )

        self._callbacks: set[Callable[[FaceDetectionData], None]] = set()

        self._state_lock: threading.RLock = threading.RLock()
        self._callback_lock: threading.RLock = threading.RLock()

        self._start_watcher()

    def register_callback(self, callback: Callable[[FaceDetectionData], None]):
        with self._callback_lock:
            self._callbacks.add(callback)

    def unregister_callback(self, callback: Callable[[FaceDetectionData], None]):
        with self._callback_lock:
            self._callbacks.discard(callback)

    def _start_watcher(self) -> None:
        """Poll USERS_DIR every 2s and reload embeddings when files change."""
        USERS_DIR.mkdir(parents=True, exist_ok=True)

        def _latest_mtime() -> float:
            try:
                return max(
                    (
                        e.stat().st_mtime
                        for e in USERS_DIR.rglob("*")
                        if e.is_file()
                        and e.suffix.lower() in _ENROLL_IMG_EXTS
                        and e.parent.parent == USERS_DIR
                    ),
                    default=0.0,
                )
            except OSError:
                return 0.0

        def _poll():
            last = _latest_mtime()
            while True:
                time.sleep(2)
                current = _latest_mtime()
                if current != last:
                    last = current
                    logger.info("User photos changed — reloading embeddings")
                    _ = self.load_from_disk()

        t = threading.Thread(target=_poll, daemon=True, name="owner-photos-watcher")
        t.start()
        logger.info("Watching users dir: %s", USERS_DIR)

    def train(
        self,
        images: list[cv2.typing.MatLike],
        labels: list[str],
    ) -> None:
        self._face_recognizer.register(images, labels)

    @staticmethod
    def normalize_label(label: str) -> str:
        """Lowercase folder-safe label (a-z0-9_-)."""
        s = label.strip().lower()
        s = re.sub(r"[^a-z0-9_-]+", "_", s)
        s = s.strip("_")
        return s[:64] if s else "person"

    def _clear_owner_embeddings(self) -> None:
        self._face_recognizer.reset(owners=True, strangers=False)

    @staticmethod
    def _read_metadata(person_dir: Path) -> dict[str, Any]:
        """Read metadata.json from a person's folder. Returns {} if missing."""
        meta_path = person_dir / "metadata.json"
        if meta_path.is_file():
            try:
                return json.loads(meta_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    @staticmethod
    def _write_metadata(
        person_dir: Path, telegram_username: str = "", telegram_id: str = ""
    ) -> None:
        """Write metadata.json with telegram info."""
        meta_path = person_dir / "metadata.json"
        data: dict[str, Any] = {}
        if meta_path.is_file():
            try:
                data = json.loads(meta_path.read_text())
            except (json.JSONDecodeError, OSError):
                pass
        if telegram_username:
            data["telegram_username"] = telegram_username
        if telegram_id:
            data["telegram_id"] = telegram_id
        _ = meta_path.write_text(json.dumps(data))

    def save_photo(
        self,
        image_bytes: bytes,
        label: str,
        telegram_username: str = "",
        telegram_id: str = "",
    ) -> str:
        """Write JPEG bytes under USERS_DIR/{label}/ with a timestamp name."""
        norm = self.normalize_label(label)
        dest_dir = USERS_DIR / norm
        dest_dir.mkdir(parents=True, exist_ok=True)
        if telegram_username or telegram_id:
            self._write_metadata(dest_dir, telegram_username, telegram_id)
        fname = f"{int(time.time() * 1000)}.jpg"
        path = dest_dir / fname
        _ = path.write_bytes(image_bytes)
        return str(path)

    def load_from_disk(self) -> int:
        """Re-train the owner + extended banks from all images under USERS_DIR."""
        if not USERS_DIR.is_dir():
            logger.info("No users dir at %s — skipping", USERS_DIR)
            # Empty inputs => reload clears both banks atomically.
            self._face_recognizer.reload([], [], [])
            return 0

        _IMG_EXTS = _ENROLL_IMG_EXTS
        all_images: list[cv2.typing.MatLike] = []
        all_labels: list[str] = []
        person_names: list[str] = []
        loaded_total = 0

        for person_dir in sorted(USERS_DIR.iterdir()):
            if not person_dir.is_dir():
                continue
            person_names.append(person_dir.name)

            count = 0
            for fname in sorted(person_dir.iterdir()):
                if fname.suffix.lower() not in _IMG_EXTS:
                    continue
                img = cv2.imread(str(fname))
                if img is None:
                    logger.warning("Failed to load image: %s", fname)
                    continue
                all_images.append(img)
                all_labels.append(person_dir.name)
                count += 1

            if count:
                loaded_total += count
                logger.info("Loaded %d image(s) for '%s'", count, person_dir.name)

        # Atomic rebuild of both banks (owner inference + extended disk reads all
        # happen off the lock inside reload; only the final swap is locked).
        self._face_recognizer.reload(all_images, all_labels, person_names)

        n_owners = len(self._face_recognizer.owners)
        n_strangers = len(self._face_recognizer.strangers)
        logger.info(
            "Load from disk done — %d image(s), %d enrolled owners(s), %d enrolled strangers(s)",
            loaded_total,
            n_owners,
            n_strangers,
        )
        return n_owners

    def enroll_from_bytes(
        self,
        image_bytes: bytes,
        label: str,
        telegram_username: str = "",
        telegram_id: str = "",
    ) -> str:
        """Decode image, save as JPEG on disk, and append embeddings."""
        norm = self.normalize_label(label)
        arr = np.frombuffer(image_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise ValueError("could not decode image")
        ok, buf = cv2.imencode(".jpg", img)
        if not ok:
            raise ValueError("could not encode image")
        path = self.save_photo(buf.tobytes(), norm, telegram_username, telegram_id)
        self.train([img], [norm])
        return path

    @staticmethod
    def _resolve_person_dir(label: str) -> Path | None:
        """Find the actual person directory on disk, handling case mismatches."""
        norm = FacePerception.normalize_label(label)
        direct = USERS_DIR / norm
        if direct.is_dir():
            return direct
        if not USERS_DIR.is_dir():
            return None
        for child in USERS_DIR.iterdir():
            if child.is_dir() and child.name.lower() == norm:
                return child
        return None

    def get_telegram_id(self, label: str) -> str | None:
        """Return telegram_id for a person, or None if not set."""
        person_dir = self._resolve_person_dir(label)
        if person_dir is None:
            return None
        meta = self._read_metadata(person_dir)
        return meta.get("telegram_id") or None

    def remove_photo(self, label: str, filename: str) -> bool:
        """Remove a single photo from a person's directory and re-load from disk."""
        person_dir = self._resolve_person_dir(label)
        if person_dir is None:
            return False
        photo_path = person_dir / filename
        if not photo_path.is_file():
            return False
        photo_path.unlink()
        logger.info("Removed photo %s for '%s'", filename, label)
        _IMG_EXTS = {".jpg", ".jpeg", ".png", ".bmp"}
        remaining = [f for f in person_dir.iterdir() if f.suffix.lower() in _IMG_EXTS]
        if not remaining:
            shutil.rmtree(person_dir)
            logger.info("No photos left for '%s' — removed person directory", label)
        _ = self.load_from_disk()
        return True

    def remove_person(self, label: str) -> bool:
        """Remove one person's directory and re-load remaining persons from disk."""
        person_dir = self._resolve_person_dir(label)
        if person_dir is None:
            return False
        shutil.rmtree(person_dir)
        _ = self.load_from_disk()
        return True

    def enrolled_count(self) -> int:
        return len(self._face_recognizer.owners)

    def enrolled_names(self) -> list[str]:
        return self._face_recognizer.owners

    def reset_enrolled(self) -> None:
        """Clear enrolled embeddings and delete all saved photos. Stranger bank is unchanged."""
        self._clear_owner_embeddings()
        if USERS_DIR.is_dir():
            for child in USERS_DIR.iterdir():
                if child.is_dir():
                    shutil.rmtree(child)
        logger.info("Enrolled embeddings cleared and photos removed")

    @override
    def cleanup(self) -> None:
        pass

    @override
    def _check_impl(self, data: cv2.typing.MatLike) -> None:
        frame = data
        if frame is None:
            logger.debug("[face] frame is None, skipping")
            return

        cur_ts = time.time()

        faces = self._face_recognizer.detect(frame)

        with self._state_lock:
            if not faces:
                logger.debug("[face] no faces detected")
                self._face_present = False
                self._faces_n = 0
                self._check_leaves(cur_ts)
                return
            else:
                logger.debug("[face] detected %d face(s): %s", len(faces), [f.person_id for f in faces])
                self._faces_n = len(faces)
                self._face_present = len(faces) > 0

            owners_seen = set(
                [f.person_id for f in faces if f.kind == PersonKind.FRIEND]
            )
            strangers_seen = set(
                [f.person_id for f in faces if f.kind == PersonKind.STRANGER]
            )

            logger.info(
                f"Detected friends={list(owners_seen)} and strangers={list(strangers_seen)}"
            )

            new_owners: set[str] = set()
            new_strangers: set[str] = set()

            for f in faces:
                if f.kind == PersonKind.UNSURE:
                    continue

                person_id = f.person_id
                if person_id not in self._people_data_dict:
                    self._people_data_dict[person_id] = PersonData(
                        id=person_id, kind=f.kind
                    )

                face_data = self._people_data_dict[person_id]

                if face_data.kind == PersonKind.FRIEND:
                    forget_ts = self._owners_forget_ts
                elif face_data.kind == PersonKind.STRANGER:
                    forget_ts = self._strangers_forget_ts
                else:
                    forget_ts = 0

                if (
                    face_data.last_seen is None
                    or (cur_ts - face_data.last_seen) > forget_ts
                ):
                    if face_data.kind == PersonKind.FRIEND:
                        new_owners.add(person_id)
                        self._post_wellbeing(
                            self.normalize_label(person_id), "enter"
                        )
                    elif face_data.kind == PersonKind.STRANGER:
                        new_strangers.add(person_id)

                    self._people_data_dict[person_id].last_session_time = cur_ts

                self._people_data_dict[person_id].last_seen = cur_ts

            if len(new_strangers) > 0 and not self._any_stranger_logged:
                self._post_wellbeing("unknown", "enter")
                self._any_stranger_logged = True

            if self._face_present and self._presense_service is not None:
                self._presense_service.on_motion()

            annotated_frame = self._annotate_frame(frame, faces)
            current_facts = self._frame_facts(faces, owners_seen, new_owners)
            annotated_frames_to_send: list[cv2.typing.MatLike] = []
            if len(new_owners) > 0:
                annotated_frames_to_send.append(annotated_frame)

            familiar_paths: dict[str, str] = {}
            if new_strangers:
                just_familiar = self._track_stranger_visits(new_strangers)
                if just_familiar:
                    ts_ms = int(cur_ts * 1000)
                    try:
                        _STRANGER_SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
                    except OSError as e:
                        logger.warning(
                            "[face] failed to create familiar snapshots dir: %s", e
                        )
                    for sid in just_familiar:
                        path = _STRANGER_SNAPSHOTS_DIR / f"{sid}_{ts_ms}.jpg"
                        try:
                            if cv2.imwrite(str(path), frame):
                                familiar_paths[sid] = str(path)
                            else:
                                logger.warning(
                                    "[face] cv2.imwrite returned False for %s", path
                                )
                        except cv2.error as e:
                            logger.warning(
                                "[face] failed to save familiar snapshot %s: %s",
                                path,
                                e,
                            )

            # A stranger new on the same tick as a friend rides the friend's
            # enter, as before. Any other new stranger waits until they look
            # at the lamp (#531).
            stranger_ids_to_send: set[str] = set()
            if new_owners:
                stranger_ids_to_send = set(new_strangers)
            else:
                for sid in new_strangers:
                    self._ungreeted_strangers[sid] = familiar_paths.get(sid)
                greeted, gaze_frames, familiar_paths = self._stranger_gaze_greeting(
                    frame, faces, annotated_frame, cur_ts
                )
                stranger_ids_to_send = greeted
                annotated_frames_to_send = gaze_frames

            # Stranger-only enter floor: embedding flicker mints a fresh stranger_N id
            # every few seconds for the same unrecognizable person, and a fresh id is
            # always "new".
            if (
                annotated_frames_to_send
                and not new_owners
                and stranger_ids_to_send
                and (cur_ts - self._last_stranger_enter_ts)
                < config.FACE_STRANGER_ENTER_FLOOR_S
            ):
                logger.info(
                    "[face] stranger-only enter floored: %s (last stranger enter %.0fs ago < %.0fs)",
                    sorted(stranger_ids_to_send),
                    cur_ts - self._last_stranger_enter_ts,
                    config.FACE_STRANGER_ENTER_FLOOR_S,
                )
                annotated_frames_to_send = []

            if annotated_frames_to_send:
                if not new_owners and stranger_ids_to_send:
                    self._last_stranger_enter_ts = cur_ts
                message = build_enter_message(
                    new_friends=new_owners,
                    new_strangers=stranger_ids_to_send,
                    present_friends=current_facts.present_friends,
                    frame_labels=current_facts.labels,
                )
                for sid, img_path in familiar_paths.items():
                    message += (
                        f" (familiar stranger {sid} — seen "
                        f"{_FAMILIAR_VISIT_THRESHOLD} times, ask user if they "
                        f"want to remember this face; image saved at {img_path})"
                    )
                self._send_enter_event(
                    frames=annotated_frames_to_send,
                    message=message,
                )

            self._check_leaves(cur_ts)

            face_detection_data = FaceDetectionData(
                frame=frame.copy(), faces=copy(faces)
            )

            self._perception_state.detected_faces.data = face_detection_data
            self._perception_state.current_user.data = self.current_user()

        with self._callback_lock:
            for callback in self._callbacks:
                callback(face_detection_data)

    def to_dict(self) -> dict[str, Any]:
        with self._state_lock:
            cur_ts = time.time()
            last_person: str | None = None
            last_seen: float | None = None

            for person_id, person_data in self._people_data_dict.items():
                if person_data.last_seen is None:
                    continue

                if last_seen is None or last_seen < person_data.last_seen:
                    last_seen = person_data.last_seen
                    last_person = person_id
            return {
                "type": "face",
                "face_present": self._face_present,
                "faces_count": self._faces_n,
                "visible": list(self._people_data_dict.keys()),
                "last_person": last_person,
                "last_seen_seconds_ago": (cur_ts - last_seen)
                if last_seen is not None
                else None,
                "enrolled_count": self.enrolled_count(),
                "stranger_count": len(self._face_recognizer.strangers),
            }

    def _load_presence_state(self) -> None:
        """Restore last_seen from the boot-scoped sidecar after a service restart."""
        try:
            if not _PRESENCE_STATE_PATH.exists():
                return
            data = json.loads(_PRESENCE_STATE_PATH.read_text())
            if data.get("boot_id") != _current_boot_id():
                _PRESENCE_STATE_PATH.unlink(missing_ok=True)
                return
            for pid, entry in (data.get("people") or {}).items():
                try:
                    kind = PersonKind(entry["kind"])
                    last_seen = float(entry["last_seen"])
                except (KeyError, TypeError, ValueError):
                    continue
                if kind == PersonKind.UNSURE:
                    continue
                self._people_data_dict[pid] = PersonData(
                    id=pid, kind=kind, last_seen=last_seen
                )
            if self._people_data_dict:
                logger.info(
                    "[face] presence state restored — %d people known from "
                    "before the service restart (no re-greeting)",
                    len(self._people_data_dict),
                )
        except Exception as e:
            logger.warning("[face] presence state load failed: %s", e)

    def _persist_presence_state(self, cur_ts: float, force: bool = False) -> None:
        """Throttled dump of the last_seen map.

        Must run periodically, not just on enter/leave: the restore above only
        suppresses a re-greeting when the saved timestamps are FRESH.
        """
        if not force and (cur_ts - self._last_presence_save_ts) < 30.0:
            return
        self._last_presence_save_ts = cur_ts
        try:
            people = {
                pid: {"kind": str(pd.kind), "last_seen": pd.last_seen}
                for pid, pd in self._people_data_dict.items()
                if pd.last_seen is not None
            }
            _PRESENCE_STATE_PATH.write_text(
                json.dumps({"boot_id": _current_boot_id(), "people": people})
            )
        except Exception as e:
            logger.warning("[face] presence state save failed: %s", e)

    def _check_leaves(self, cur_ts: float) -> None:
        """Fire presence.leave for anyone not seen within their forget interval."""
        deleted_ids: set[str] = set()
        with self._state_lock:
            for person_id, person_data in self._people_data_dict.items():
                if person_data.kind == PersonKind.FRIEND:
                    if (
                        person_data.last_seen is None
                        or (cur_ts - person_data.last_seen) > self._owners_forget_ts
                    ):
                        deleted_ids.add(person_id)
                        self._post_wellbeing(self.normalize_label(person_id), "leave")
                        self._send_leave_event(person_id, kind=person_data.kind)
                elif person_data.kind == PersonKind.STRANGER:
                    if (
                        person_data.last_seen is None
                        or (cur_ts - person_data.last_seen) > self._strangers_forget_ts
                    ):
                        deleted_ids.add(person_id)

            for id in deleted_ids:
                del self._people_data_dict[id]
                _ = self._ungreeted_strangers.pop(id, None)
            if not self._ungreeted_strangers:
                self._stranger_gaze_ticks.clear()

            current_strangers = [
                p
                for p in self._people_data_dict.values()
                if p.kind == PersonKind.STRANGER
            ]

            if self._any_stranger_logged and not current_strangers:
                self._post_wellbeing("unknown", "leave")
                self._any_stranger_logged = False

            self._persist_presence_state(cur_ts)

    def _send_leave_event(self, person_id: str, kind: PersonKind) -> None:
        self._send_event(
            "presence.leave",
            f"Person no longer visible — {kind} ({person_id})",
            "face",
            None,
            config.FACE_COOLDOWN_S,
        )

    def _post_wellbeing(self, user: str, action: str) -> None:
        """POST an enter/leave row to the OS server's wellbeing log.

        Fire-and-forget with a short timeout — a stuck OS server must never block face
        detection.
        """
        if not user:
            return
        try:
            resp = requests.post(
                config.OS_WELLBEING_LOG_URL,
                json={"action": action, "notes": "", "user": user},
                timeout=2,
            )
            if resp.status_code != 200:
                logger.debug(
                    "[face] wellbeing %s %s returned %d",
                    action,
                    user,
                    resp.status_code,
                )
        except requests.RequestException as e:
            logger.debug("[face] wellbeing %s %s failed: %s", action, user, e)

    @staticmethod
    def _load_stranger_stats() -> dict[str, Any]:
        try:
            return json.loads(_STRANGER_STATS_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def _save_stranger_stats(self) -> None:
        with self._state_lock:
            try:
                _STRANGER_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
                _ = _STRANGER_STATS_FILE.write_text(
                    json.dumps(self._stranger_visit_counts, indent=2)
                )
            except OSError as e:
                logger.warning("Failed to save stranger stats: %s", e)

    def _track_stranger_visits(self, stranger_ids: set[str]) -> set[str]:
        """Increment visit count for each stranger seen in this frame."""
        now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        just_familiar: set[str] = set()
        with self._state_lock:
            for sid in stranger_ids:
                rec = self._stranger_visit_counts.get(sid)
                if rec is None:
                    self._stranger_visit_counts[sid] = {
                        "count": 1,
                        "first_seen": now,
                        "last_seen": now,
                    }
                else:
                    rec["count"] += 1
                    rec["last_seen"] = now
                    if rec["count"] == _FAMILIAR_VISIT_THRESHOLD:
                        just_familiar.add(sid)

            if stranger_ids:
                self._save_stranger_stats()
        return just_familiar

    def stranger_stats(self) -> dict[str, Any]:
        """Return visit counts for all tracked stranger IDs."""
        with self._state_lock:
            return self._stranger_visit_counts

    def has_friend_present(self) -> bool:
        """Return True if any friend was seen within the forget interval."""
        with self._state_lock:
            owners = {
                p: d
                for p, d in self._people_data_dict.items()
                if d.kind == PersonKind.FRIEND
            }
            if not owners:
                return False
            now_ts = time.time()
            return any(
                (now_ts - d.last_seen) <= self._owners_forget_ts
                for d in owners.values()
                if d.last_seen is not None
            )

    def current_user(self) -> str:
        """Return the name of the person currently "in front" of the device: - Friend with
        the MOST RECENT session start (enter-after-last-leave) among friends still
        within the forget window.
        """
        return self.current_user_with_age()[0]

    def current_user_with_age(self) -> tuple[str, float]:
        """``current_user()`` plus how long ago that person was actually seen."""
        now = time.time()
        last_friend: str | None = None
        last_friend_ts: float | None = None
        last_friend_seen: float = 0.0
        newest_stranger_seen: float | None = None
        with self._state_lock:
            for person_id, person_data in self._people_data_dict.items():
                if person_data.last_seen is None:
                    continue
                if (
                    person_data.kind == PersonKind.STRANGER
                    and (now - person_data.last_seen) <= self._strangers_forget_ts
                ):
                    if (
                        newest_stranger_seen is None
                        or person_data.last_seen > newest_stranger_seen
                    ):
                        newest_stranger_seen = person_data.last_seen

                if person_data.kind != PersonKind.FRIEND:
                    continue
                if (now - person_data.last_seen) > self._owners_forget_ts:
                    continue

                session_start = person_data.last_session_time or person_data.last_seen
                if last_friend_ts is None or last_friend_ts < session_start:
                    last_friend = person_id
                    last_friend_ts = session_start
                    last_friend_seen = person_data.last_seen

            if last_friend is not None:
                return self.normalize_label(last_friend), max(0.0, now - last_friend_seen)

            if newest_stranger_seen is not None:
                return "unknown", max(0.0, now - newest_stranger_seen)

            return "", 0.0

    def cooldown_state(self) -> dict[str, Any]:
        """Return current cooldown state for all tracked persons."""
        cur_ts = time.time()
        owners = []
        strangers = []
        with self._state_lock:
            for person_id, person_data in self._people_data_dict.items():
                if person_data.last_seen is None:
                    continue

                elapsed = cur_ts - person_data.last_seen
                if person_data.kind == PersonKind.FRIEND:
                    remaining = max(0.0, self._owners_forget_ts - elapsed)
                    kind = person_data.kind
                    owners.append(
                        {
                            "person_id": person_id,
                            "kind": kind,
                            "last_seen_ago": round(elapsed, 1),
                            "cooldown_remaining": round(remaining, 1),
                            "cooldown_total": self._owners_forget_ts,
                        }
                    )
                elif person_data.kind == PersonKind.STRANGER:
                    remaining = max(0.0, self._strangers_forget_ts - elapsed)
                    strangers.append(
                        {
                            "person_id": person_id,
                            "kind": "stranger",
                            "last_seen_ago": round(elapsed, 1),
                            "cooldown_remaining": round(remaining, 1),
                            "cooldown_total": self._strangers_forget_ts,
                        }
                    )

            return {
                "owners": owners,
                "strangers": strangers,
                "owners_forget_s": self._owners_forget_ts,
                "strangers_forget_s": self._strangers_forget_ts,
            }

    def reset_cooldowns(self) -> None:
        """Clear all last-seen timestamps so next detection fires events immediately."""
        with self._state_lock:
            self._people_data_dict.clear()
            self._persist_presence_state(time.time(), force=True)
            self._last_presence_save_ts = 0.0
            self._ungreeted_strangers.clear()
            self._stranger_gaze_ticks.clear()
            # Consumers read the observable's cached copy, so blank it now, not on the next frame.
            self._perception_state.current_user.data = ""
            logger.info("Face recognition cooldowns reset")

    _FACE_COLOR: dict[PersonKind, tuple[int, int, int]] = {
        PersonKind.FRIEND: (0, 255, 0),
        PersonKind.STRANGER: (0, 0, 255),
        PersonKind.UNSURE: (0, 255, 255),
    }

    def _annotate_frame(
        self,
        frame: cv2.typing.MatLike,
        faces: list[Face],
    ) -> cv2.typing.MatLike:
        """Draw bounding boxes and labels on a frame copy."""
        annotated = frame.copy()
        for f in faces:
            x1, y1, x2, y2 = f.bbox
            color = self._FACE_COLOR.get(f.kind, (128, 128, 128))
            _ = cv2.rectangle(annotated, (x1, y1), (x2, y2), color, 2)
            display_label = f.person_id if f.kind != PersonKind.UNSURE else "unsure"
            _ = cv2.putText(
                annotated,
                display_label,
                (x1, y1 - 6),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                color,
                1,
                cv2.LINE_AA,
            )
        return annotated

    def _frame_facts(
        self, faces: list[Face], owners_seen: set[str], new_owners: set[str]
    ) -> FrameFacts:
        """Labels of every box in this frame plus the friends already present.

        ``present_friends`` are friends matched in this frame who did not just arrive.
        Never derived from current_user(): that reads the same after the user has left.
        """
        return FrameFacts(
            labels=frame_labels(faces),
            present_friends=sorted(owners_seen - new_owners),
        )

    def _stranger_gaze_greeting(
        self,
        frame: cv2.typing.MatLike,
        faces: list[Face],
        annotated_frame: cv2.typing.MatLike,
        cur_ts: float,
    ) -> tuple[set[str], list[cv2.typing.MatLike], dict[str, str]]:
        """Record this tick's gaze for ungreeted strangers; return who to greet.

        Returns (stranger ids to greet, buffered snapshots newest last,
        familiar-stranger snapshot paths). All empty until an ungreeted
        stranger has faced the lamp on enough buffered ticks. Caller holds
        _state_lock.
        """
        waiting = [
            f
            for f in faces
            if f.kind == PersonKind.STRANGER and f.person_id in self._ungreeted_strangers
        ]
        if not waiting:
            return set(), [], {}
        frame_h, frame_w = frame.shape[:2]
        # One measurement per stranger id; when two boxes share an id, the one
        # facing the lamp wins so the vote stays "any box facing" (#537).
        gazes: dict[str, GazeMeasurement] = {}
        for f in waiting:
            m = measure_gaze(f, frame_w, frame_h)
            if f.person_id not in gazes or m.facing:
                gazes[f.person_id] = m
        facing = frozenset(sid for sid, m in gazes.items() if m.facing)
        # A vote older than the window says nothing about looking now (#531).
        while (
            self._stranger_gaze_ticks
            and cur_ts - self._stranger_gaze_ticks[0].ts
            > config.FACE_STRANGER_GAZE_WINDOW_S
        ):
            _ = self._stranger_gaze_ticks.popleft()
        self._stranger_gaze_ticks.append(
            StrangerGazeTick(annotated_frame, facing, cur_ts)
        )
        ticks = [t.facing for t in self._stranger_gaze_ticks]
        in_frame = sorted(gazes)
        # The numbers behind each vote, so a greeting can be explained from the
        # log alone (#537).
        logger.info(
            "[face] stranger gaze: %s",
            "; ".join(
                f"{sid} {gazes[sid].describe()} {sum(sid in t for t in ticks)}/{len(ticks)}"
                for sid in in_frame
            ),
        )
        greet = {sid for sid in in_frame if gaze_confirmed(ticks, sid)}
        if not greet:
            return set(), [], {}
        frames = [t.frame for t in self._stranger_gaze_ticks]
        self._stranger_gaze_ticks.clear()
        familiar = {
            sid: path
            for sid in greet
            if (path := self._ungreeted_strangers.pop(sid)) is not None
        }
        return greet, frames, familiar

    def _send_enter_event(
        self,
        frames: list[cv2.typing.MatLike],
        message: str,
    ) -> None:
        """Send presence.enter with annotated frames (newest last) and the event text."""
        self._send_event(
            "presence.enter",
            message,
            "face",
            frames,
            config.FACE_COOLDOWN_S,
        )
