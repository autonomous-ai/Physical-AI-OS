"""FaceRecognizer — SCRFD + ONNX landmark + EdgeFace recognition & enrollment."""

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np
import numpy.typing as npt
import onnxruntime as ort

import hal.config as config
from hal.drivers.sensing.perceptions.models import Face, PersonKind

from .constants import _NO_MATCH, STRANGER_STATE_DIR, USERS_DIR
from .debug_log import FaceIdDebugLogger
from .model_store import (
    _EDGEFACE_MODEL_PATH,
    _LANDMARK_MODEL_PATH,
    _SCRFD_MODEL_PATH,
    ensure_face_models,
)
from .pipeline import _EdgeFacePipeline

logger = logging.getLogger(__name__)

# Auto-captured "extended" enrollment views persist in this per-user subfolder, i.e.
# USERS_DIR/<user>/.extended/.
_EXTENDED_SUBDIR = ".extended"
_EXTENDED_IMG_EXT = ".jpg"
_EXTENDED_EMB_EXT = ".npy"
_EXTENDED_META_EXT = ".json"

_DEBUG_KIND_COLOR: dict[PersonKind, tuple[int, int, int]] = {
    PersonKind.FRIEND: (0, 255, 0),
    PersonKind.STRANGER: (0, 0, 255),
    PersonKind.UNSURE: (0, 255, 255),
}


class FaceRecognizer:
    FRIEND_PREFIX: str = "friend_"
    STRANGER_PREFIX: str = "stranger_"

    def __init__(
        self,
        height_ratio_threshold: float = config.FACE_HEIGHT_RATIO_THRESHOLD,
        max_truncation: float = config.FACE_MAX_TRUNCATION,
        min_sharpness: float = config.FACE_MIN_SHARPNESS,
        stranger_min_ticks: int = config.FACE_STRANGER_MIN_TICKS,
        stranger_corroboration_s: float = config.FACE_STRANGER_CORROBORATION_S,
        threshold: float = config.FACE_MATCH_THRESHOLD,
        extended_threshold: float = config.FACE_EXTENDED_THRESHOLD,
        stranger_threshold: float = config.FACE_STRANGER_THRESHOLD,
        extend_min_enroll_sim: float = config.FACE_EXTEND_MIN_ENROLL_SIM,
        negative_threshold: float | None = 0.2,
        max_strangers: int = 50,
        scrfd_model_path: str = _SCRFD_MODEL_PATH,
        edgeface_model_path: str = _EDGEFACE_MODEL_PATH,
        landmark_model_path: str = _LANDMARK_MODEL_PATH,
        max_extended_images: int = 5,
        diversity_threshold: float = 0.7,
    ):
        self._height_ratio_threshold: float = height_ratio_threshold
        self._max_truncation: float = max_truncation
        self._min_sharpness: float = min_sharpness
        # Consecutive ticks an unknown face must persist before it earns an
        # identity; see FACE_STRANGER_MIN_TICKS in hal/config.py.
        self._stranger_min_ticks: int = stranger_min_ticks
        self._stranger_corroboration_s: float = stranger_corroboration_s
        self._pending_strangers: list[
            tuple[npt.NDArray[np.float32], int, float]
        ] = []
        self._threshold: float = threshold
        self._extended_threshold: float = extended_threshold
        # Bar for matching an already-known stranger_N. Same kind of evidence as
        # the extended bank (auto-captured, same camera, one view), same bar;
        # see FACE_STRANGER_THRESHOLD in hal/config.py and #429.
        self._stranger_threshold: float = stranger_threshold
        # Bar the UPLOADS must clear before a live view may be auto-captured
        # into the extended bank; see FACE_EXTEND_MIN_ENROLL_SIM.
        self._extend_min_enroll_sim: float = extend_min_enroll_sim
        self._negative_threshold: float | None = negative_threshold
        self._max_strangers: int = max_strangers
        self._scrfd_model_path: str = scrfd_model_path
        self._edgeface_model_path: str = edgeface_model_path
        self._landmark_model_path: str = landmark_model_path

        self._max_extended_images: int = max_extended_images
        self._diversity_threshold: float = diversity_threshold

        self._app: _EdgeFacePipeline | None = None
        self._owner_embeddings: npt.NDArray[np.float32] | None = None
        self._owner_labels: npt.NDArray[np.str_] | None = None
        # Dynamically-grown per-user "extended" bank. Kept SEPARATE from
        # ``_owner_embeddings`` so the user's uploads are never mutated and can always
        # be rebuilt verbatim from disk.
        self._extended_embeddings: npt.NDArray[np.float32] | None = None
        self._extended_labels: npt.NDArray[np.str_] | None = None
        self._extended_paths: npt.NDArray[np.object_] | None = None
        # Monotonic counter appended to extended-view filenames so two views
        # captured in the same millisecond never collide (which would overwrite
        # a file and desync disk from memory).
        self._extended_save_seq: int = 0
        self._stranger_counter: int = 0
        self._stranger_embeddings: npt.NDArray[np.float32] | None = None
        self._stranger_labels: npt.NDArray[np.str_] | None = None

        self._lock: threading.RLock = threading.RLock()
        self._running: bool = False
        self._logger: logging.Logger = logging.getLogger(self.__class__.__name__)

        self._debug: FaceIdDebugLogger = FaceIdDebugLogger(
            root_dir=config.FACEID_LOG_DIR,
            enabled=config.FACEID_DEBUG_LOG_ENABLED,
            max_triggers=config.FACEID_LOG_MAX_TRIGGERS,
        )
        if config.FACEID_DEBUG_LOG_ENABLED:
            self._logger.info("[face] debug logging → %s", config.FACEID_LOG_DIR)

    @property
    def owners(self) -> list[str]:
        with self._lock:
            if self._owner_labels is None:
                return []
            unique: set[str] = set()
            for lbl in self._owner_labels:
                s = str(lbl)
                unique.add(s.removeprefix(self.FRIEND_PREFIX))
            return list(unique)

    @property
    def strangers(self) -> list[str]:
        with self._lock:
            if self._stranger_labels is None:
                return []
            unique: set[str] = set()
            for lbl in self._stranger_labels:
                s = str(lbl)
                unique.add(s.removeprefix(self.STRANGER_PREFIX))
            return list(unique)

    def start(self):
        if self._running:
            self._logger.info(
                "[%s] service has been already started", self.__class__.__name__
            )
            return

        ensure_face_models(
            self._scrfd_model_path,
            self._edgeface_model_path,
            self._landmark_model_path,
        )

        sess_opts = ort.SessionOptions()
        sess_opts.intra_op_num_threads = 1
        sess_opts.inter_op_num_threads = 1

        self._app = _EdgeFacePipeline(
            scrfd_model_path=self._scrfd_model_path,
            edgeface_model_path=self._edgeface_model_path,
            landmark_model_path=self._landmark_model_path,
            l2_normalize=False,
            session_options=sess_opts,
        )
        self._running = True

    def reset(self, owners: bool = True, strangers: bool = True):
        with self._lock:
            if owners:
                self._owner_embeddings = None
                self._owner_labels = None
                self._extended_embeddings = None
                self._extended_labels = None
                self._extended_paths = None

            if strangers:
                self._stranger_embeddings = None
                self._stranger_labels = None
                self._stranger_counter = 0

    def register(
        self,
        images: list[cv2.typing.MatLike],
        labels: list[str],
    ) -> None:
        if self._app is None:
            msg = f"[{self.__class__.__name__}] service must be started first"
            raise RuntimeError(msg)

        prefixed_labels = [self.FRIEND_PREFIX + str(lbl) for lbl in labels]
        new_embeddings = []
        new_labels = []
        for image, label in zip(images, prefixed_labels):
            results = self._app.get(image)
            for r in results:
                emb = r["embedding"]
                new_embeddings.append(emb / np.linalg.norm(emb))
                new_labels.append(label)

        if new_embeddings:
            stacked_e = np.stack(new_embeddings, axis=0)
            stacked_l = np.stack(new_labels, axis=0)

            with self._lock:
                self._owner_embeddings = (
                    np.concatenate([self._owner_embeddings, stacked_e])
                    if self._owner_embeddings is not None
                    else stacked_e
                )
                self._owner_labels = (
                    np.concatenate([self._owner_labels, stacked_l])
                    if self._owner_labels is not None
                    else stacked_l
                )
                logger.info(
                    "Added %d faces — total enrolled: %d, total strangers: %d",
                    len(new_embeddings),
                    len(self._owner_embeddings),
                    len(self._stranger_embeddings)
                    if self._stranger_embeddings is not None
                    else 0,
                )

    def _retrieve(
        self,
        embeds: npt.NDArray[np.float32],
        bank: npt.NDArray[np.float32] | None,
        labels: npt.NDArray[np.str_] | None,
    ) -> tuple[npt.NDArray[np.float32], list[str | None]]:
        scores: npt.NDArray[np.float32] = np.empty(0, dtype=np.float32)
        ids: list[str | None] = []

        if bank is not None and labels is not None:
            sim = embeds @ bank.T
            best = sim.argmax(axis=-1)
            scores = np.array([sim[i, best[i]] for i in range(len(embeds))])
            ids = [str(labels[best[i]]) for i in range(len(embeds))]
        else:
            scores = np.full(embeds.shape[0], _NO_MATCH)
            ids = [None] * embeds.shape[0]

        return scores, ids

    @staticmethod
    def _user_embeddings(
        bank: npt.NDArray[np.float32] | None,
        labels: npt.NDArray[np.str_] | None,
        raw_label: str,
    ) -> npt.NDArray[np.float32] | None:
        """Rows of ``bank`` whose label equals ``raw_label`` (a friend_* id), or
        None if the bank is empty or holds nothing for that user."""
        if bank is None or labels is None:
            return None
        mask = labels == raw_label
        if not np.any(mask):
            return None
        return bank[mask]

    def _maybe_extend_user(
        self,
        raw_label: str,
        embedding: npt.NDArray[np.float32],
        crop: npt.NDArray[np.uint8] | None,
        meta: dict[str, Any] | None = None,
    ) -> None:
        """Consider folding one confidently-matched live view into a user's extended set
        AND persisting it to disk. Manages its own locking.

        IMPORTANT: this runs on the ``detect`` hot path, so it NEVER holds
        ``self._lock`` across disk I/O.
        """
        if crop is None or crop.size == 0:
            return

        with self._lock:
            enroll = self._user_embeddings(
                self._owner_embeddings, self._owner_labels, raw_label
            )
            extended = self._user_embeddings(
                self._extended_embeddings, self._extended_labels, raw_label
            )
            existing = [e for e in (enroll, extended) if e is not None and len(e)]
            existing_stack = np.concatenate(existing) if existing else None

        # Gate 1 — cheap near-duplicate reject (no lock).
        max_sim: float | None = None
        if existing_stack is not None:
            max_sim = float(np.max(existing_stack @ embedding))
            if max_sim > self._diversity_threshold:
                logger.debug(
                    "[face] extended '%s': skip redundant view "
                    "(max_sim=%.3f > %.2f)",
                    raw_label.removeprefix(self.FRIEND_PREFIX),
                    max_sim,
                    self._diversity_threshold,
                )
                return

        # Gate 2 — decide keep/drop IN MEMORY, before touching disk. A stale snapshot
        # stays harmless: the authoritative prune re-runs under the lock at commit and
        # remains correct regardless.
        n_existing_ext = 0 if extended is None else len(extended)
        if n_existing_ext + 1 > self._max_extended_images:
            candidates = (
                np.concatenate([extended, embedding[None, :]])
                if extended is not None
                else embedding[None, :]
            )
            keep_local = self._select_diverse(
                candidates, enroll, self._max_extended_images
            )
            if (len(candidates) - 1) not in keep_local:
                logger.debug(
                    "[face] extended '%s': skip view — not among the %d most "
                    "diverse (max_sim=%s)",
                    raw_label.removeprefix(self.FRIEND_PREFIX),
                    self._max_extended_images,
                    "n/a" if max_sim is None else f"{max_sim:.3f}",
                )
                return

        provenance = dict(meta or {})
        provenance["max_sim_to_existing"] = max_sim
        path = self._save_extended_view(raw_label, embedding, crop, provenance)
        if path is None:
            return

        with self._lock:
            self._extended_embeddings = (
                np.concatenate([self._extended_embeddings, embedding[None, :]])
                if self._extended_embeddings is not None
                else embedding[None, :].copy()
            )
            self._extended_labels = (
                np.concatenate([self._extended_labels, np.array([raw_label])])
                if self._extended_labels is not None
                else np.array([raw_label])
            )
            self._extended_paths = (
                np.concatenate([self._extended_paths, np.array([path], dtype=object)])
                if self._extended_paths is not None
                else np.array([path], dtype=object)
            )
            dropped = self._prune_extended_set(raw_label)
            kept = self._user_embeddings(
                self._extended_embeddings, self._extended_labels, raw_label
            )
            n_kept = 0 if kept is None else len(kept)

        for dropped_path in dropped:
            self._delete_extended_view(dropped_path)

        # Only report an ADD when the new view actually stayed.
        if path in dropped:
            logger.debug(
                "[face] extended '%s': view pruned on commit (race) -> %s",
                raw_label.removeprefix(self.FRIEND_PREFIX),
                path,
            )
            return
        logger.info(
            "[face] extended '%s': ADDED view (%d/%d kept, "
            "max_sim_to_existing=%s) -> %s",
            raw_label.removeprefix(self.FRIEND_PREFIX),
            n_kept,
            self._max_extended_images,
            "n/a" if max_sim is None else f"{max_sim:.3f}",
            path,
        )

    @staticmethod
    def _select_diverse(
        candidates: npt.NDArray[np.float32],
        anchor: npt.NDArray[np.float32] | None,
        k: int,
    ) -> list[int]:
        """Greedy farthest-point selection: return up to ``k`` indices of
        ``candidates`` (each row an embedding) that are most diverse.
        """
        m = len(candidates)
        if m <= k:
            return list(range(m))

        if anchor is not None and len(anchor):
            selected_ref: list[npt.NDArray[np.float32]] = [anchor]
            selected_local: list[int] = []
        else:
            seed = m - 1
            selected_ref = [candidates[seed][None, :]]
            selected_local = [seed]

        remaining = [j for j in range(m) if j not in selected_local]
        while len(selected_local) < k and remaining:
            ref = np.concatenate(selected_ref)
            sims = candidates[remaining] @ ref.T
            nearest = sims.max(axis=1)
            pick = int(np.argmin(nearest))
            chosen = remaining.pop(pick)
            selected_local.append(chosen)
            selected_ref.append(candidates[chosen][None, :])
        return selected_local

    def _prune_extended_set(self, raw_label: str) -> list[str]:
        """Trim one user's extended bank to the ``max_extended_images`` most diverse views.
        Caller must hold ``self._lock``.
        """
        if (
            self._extended_embeddings is None
            or self._extended_labels is None
            or self._extended_paths is None
        ):
            return []

        mask = self._extended_labels == raw_label
        idxs = np.nonzero(mask)[0]
        if len(idxs) <= self._max_extended_images:
            return []

        candidates = self._extended_embeddings[idxs]
        anchor = self._user_embeddings(
            self._owner_embeddings, self._owner_labels, raw_label
        )
        keep_local = self._select_diverse(candidates, anchor, self._max_extended_images)
        keep_global = idxs[np.array(sorted(keep_local))]

        dropped = [
            str(self._extended_paths[gi]) for gi in np.setdiff1d(idxs, keep_global)
        ]

        keep_mask = ~mask
        keep_mask[keep_global] = True
        self._extended_embeddings = self._extended_embeddings[keep_mask]
        self._extended_labels = self._extended_labels[keep_mask]
        self._extended_paths = self._extended_paths[keep_mask]
        return dropped

    def _extended_dir_for(self, raw_label: str) -> Path:
        """Per-user directory holding auto-captured extended views."""
        folder = raw_label.removeprefix(self.FRIEND_PREFIX)
        return USERS_DIR / folder / _EXTENDED_SUBDIR

    def _save_extended_view(
        self,
        raw_label: str,
        embedding: npt.NDArray[np.float32],
        crop: npt.NDArray[np.uint8],
        meta: dict[str, Any] | None = None,
    ) -> str | None:
        """Persist one extended view: a JPEG crop, a sidecar .npy embedding, and
        a .json provenance record.

        Returns the JPEG path on success, or None if it could not be written (in which
        case the caller must NOT add the view to the in-memory bank).
        """
        try:
            dest = self._extended_dir_for(raw_label)
            dest.mkdir(parents=True, exist_ok=True)
            # Millisecond stamp keeps names sortable; the seq suffix guarantees
            # uniqueness even for two captures within the same millisecond.
            with self._lock:
                self._extended_save_seq += 1
                seq = self._extended_save_seq
            stem = f"ext_{int(time.time() * 1000)}_{seq}"
            img_path = dest / f"{stem}{_EXTENDED_IMG_EXT}"
            emb_path = dest / f"{stem}{_EXTENDED_EMB_EXT}"
            if not cv2.imwrite(str(img_path), crop):
                logger.warning("[face-v2] cv2.imwrite failed for %s", img_path)
                return None
            np.save(emb_path, embedding.astype(np.float32))
            # Best-effort, and deliberately last: the view is already valid
            # without it, so a failure here must not orphan the JPEG/.npy pair.
            try:
                record: dict[str, Any] = {
                    "captured_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%S", time.localtime()
                    ),
                    "ts": time.time(),
                    "label": raw_label,
                }
                record.update(meta or {})
                _ = (dest / f"{stem}{_EXTENDED_META_EXT}").write_text(
                    json.dumps(record, indent=2), encoding="utf-8"
                )
            except (OSError, TypeError, ValueError) as e:
                logger.debug("[face-v2] provenance sidecar not written: %s", e)
            return str(img_path)
        except (OSError, cv2.error) as e:
            logger.warning("[face-v2] failed to save extended view: %s", e)
            return None

    @staticmethod
    def _delete_extended_view(img_path: str) -> None:
        """Delete an extended view's JPEG, its sidecar .npy and its provenance .json
        (best-effort).
        """
        try:
            p = Path(img_path)
            p.unlink(missing_ok=True)
            p.with_suffix(_EXTENDED_EMB_EXT).unlink(missing_ok=True)
            p.with_suffix(_EXTENDED_META_EXT).unlink(missing_ok=True)
        except OSError as e:
            logger.warning(
                "[face-v2] failed to delete extended view %s: %s", img_path, e
            )

    def _load_extended_embedding(
        self, img_path: Path, expected_dim: int | None = None
    ) -> npt.NDArray[np.float32] | None:
        """Return the L2-normalized embedding for one persisted extended view."""
        emb_path = img_path.with_suffix(_EXTENDED_EMB_EXT)
        if emb_path.is_file():
            try:
                emb = np.load(emb_path).astype(np.float32).reshape(-1)
                n = float(np.linalg.norm(emb))
                if n > 0 and (expected_dim is None or emb.shape[0] == expected_dim):
                    return emb / n
            except (OSError, ValueError) as e:
                logger.warning("[face-v2] bad extended sidecar %s: %s", emb_path, e)

        if self._app is None:
            return None
        img = cv2.imread(str(img_path))
        if img is None:
            return None
        results = self._app.get(img)
        if not results:
            return None
        best = max(
            results,
            key=lambda r: max(r["bbox"][2] - r["bbox"][0], 0)
            * max(r["bbox"][3] - r["bbox"][1], 0),
        )
        emb = best["embedding"].astype(np.float32)
        n = float(np.linalg.norm(emb))
        if n == 0:
            return None
        emb = emb / n
        try:
            np.save(emb_path, emb)
        except OSError:
            pass
        return emb

    def _read_extended_for(
        self,
        person_name: str,
        expected_dim: int | None,
        anchor: npt.NDArray[np.float32] | None,
    ) -> tuple[list[npt.NDArray[np.float32]], list[str]]:
        """Read one user's persisted extended views from disk. PURE reader: no lock, no
        in-memory mutation — it only touches the filesystem and returns ``(embeddings,
        paths)`` for the caller to install atomically.

        Only ``*.jpg`` is enumerated, so the ``.json`` provenance sidecar is ignored
        here by construction — it is for humans and audits, never an input to the bank.
        """
        raw_label = self.FRIEND_PREFIX + person_name
        dest = self._extended_dir_for(raw_label)
        if not dest.is_dir():
            return [], []

        embeds: list[npt.NDArray[np.float32]] = []
        paths: list[str] = []
        for img_path in sorted(dest.glob(f"*{_EXTENDED_IMG_EXT}")):
            emb = self._load_extended_embedding(img_path, expected_dim=expected_dim)
            if emb is None:
                self._delete_extended_view(str(img_path))
                continue
            embeds.append(emb)
            paths.append(str(img_path))

        if len(embeds) > self._max_extended_images:
            keep = set(
                self._select_diverse(
                    np.stack(embeds), anchor, self._max_extended_images
                )
            )
            for i in range(len(embeds)):
                if i not in keep:
                    self._delete_extended_view(paths[i])
            embeds = [embeds[i] for i in sorted(keep)]
            paths = [paths[i] for i in sorted(keep)]
        return embeds, paths

    def reload(
        self,
        owner_images: list[cv2.typing.MatLike],
        owner_labels: list[str],
        person_names: list[str],
    ) -> None:
        """Atomically rebuild the owner AND extended banks from disk."""
        if self._app is None:
            msg = f"[{self.__class__.__name__}] service must be started first"
            raise RuntimeError(msg)

        prefixed = [self.FRIEND_PREFIX + str(lbl) for lbl in owner_labels]
        o_embeds: list[npt.NDArray[np.float32]] = []
        o_labels: list[str] = []
        for image, label in zip(owner_images, prefixed):
            for r in self._app.get(image):
                emb = r["embedding"]
                o_embeds.append(emb / np.linalg.norm(emb))
                o_labels.append(label)
        new_owner_e = np.stack(o_embeds, axis=0) if o_embeds else None
        new_owner_l = np.array(o_labels) if o_labels else None
        expected_dim = int(new_owner_e.shape[1]) if new_owner_e is not None else None

        x_embeds: list[npt.NDArray[np.float32]] = []
        x_labels: list[str] = []
        x_paths: list[str] = []
        for name in person_names:
            raw = self.FRIEND_PREFIX + name
            anchor = (
                new_owner_e[new_owner_l == raw] if new_owner_e is not None else None
            )
            es, ps = self._read_extended_for(name, expected_dim, anchor)
            for e, p in zip(es, ps):
                x_embeds.append(e)
                x_labels.append(raw)
                x_paths.append(p)
        new_ext_e = np.stack(x_embeds, axis=0) if x_embeds else None
        new_ext_l = np.array(x_labels) if x_labels else None
        new_ext_p = np.array(x_paths, dtype=object) if x_paths else None

        with self._lock:
            self._owner_embeddings = new_owner_e
            self._owner_labels = new_owner_l
            self._extended_embeddings = new_ext_e
            self._extended_labels = new_ext_l
            self._extended_paths = new_ext_p
        logger.info(
            "Reloaded banks — %d owner view(s), %d extended view(s)",
            0 if new_owner_e is None else len(new_owner_e),
            0 if new_ext_e is None else len(new_ext_e),
        )

    def _corroborate_stranger(self, embedding: npt.NDArray[np.float32]) -> int:
        """Count how many consecutive ticks this unknown face has now been seen for,
        remembering it for next time. Caller must hold ``self._lock``.
        """
        now = time.time()
        self._pending_strangers = [
            p for p in self._pending_strangers
            if now - p[2] <= self._stranger_corroboration_s
        ]
        best_i, best_sim = -1, self._threshold
        for idx, (emb, _ticks, _ts) in enumerate(self._pending_strangers):
            sim = float(emb @ embedding)
            if sim > best_sim:
                best_i, best_sim = idx, sim
        if best_i < 0:
            self._pending_strangers.append((embedding, 1, now))
            return 1
        ticks = self._pending_strangers[best_i][1] + 1
        self._pending_strangers[best_i] = (embedding, ticks, now)
        return ticks

    @staticmethod
    def _sharpness(aligned: npt.NDArray[np.uint8]) -> float:
        """Variance of the Laplacian of the aligned crop — the standard blur
        measure, high for detail, near zero for a smear.
        """
        gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())

    @staticmethod
    def _crop_face(
        frame: npt.NDArray[np.uint8],
        bbox: tuple[int, int, int, int],
        margin: float = 0.3,
    ) -> npt.NDArray[np.uint8] | None:
        """BGR crop around a detection bbox with a relative margin, clamped to the frame."""
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = bbox
        bw, bh = x2 - x1, y2 - y1
        if bw <= 0 or bh <= 0:
            return None
        mx, my = int(bw * margin), int(bh * margin)
        x1 = max(0, x1 - mx)
        y1 = max(0, y1 - my)
        x2 = min(w, x2 + mx)
        y2 = min(h, y2 + my)
        if x2 <= x1 or y2 <= y1:
            return None
        return frame[y1:y2, x1:x2].copy()

    def detect(self, frame: cv2.typing.MatLike):
        if self._app is None:
            msg = f"[{self.__class__.__name__}] service must be started first"
            raise RuntimeError(msg)

        frame_h, frame_w = frame.shape[:2]

        raw_results = self._app.get(frame)
        n_faces = len(raw_results)

        if n_faces == 0:
            return

        embeds: npt.NDArray[np.float32] = np.stack(
            [r["embedding"] / np.linalg.norm(r["embedding"]) for r in raw_results]
        )
        det_scores: npt.NDArray[np.float32] = np.stack(
            [r["det_score"] for r in raw_results]
        )

        with self._lock:
            self._load_strangers_state()

            upload_scores, upload_ids = self._retrieve(
                embeds, self._owner_embeddings, self._owner_labels
            )
            ext_scores, ext_ids = self._retrieve(
                embeds, self._extended_embeddings, self._extended_labels
            )
            stranger_scores, stranger_ids = self._retrieve(
                embeds, self._stranger_embeddings, self._stranger_labels
            )

        owner_scores = np.maximum(upload_scores, ext_scores)

        new_stranger_embeds = []
        new_stranger_labels = []
        extend_candidates: list[
            tuple[
                str,
                npt.NDArray[np.float32],
                tuple[int, int, int, int],
                dict[str, Any],
            ]
        ] = []
        faces: list[Face] = []

        for i in range(n_faces):
            o_score = float(owner_scores[i])
            s_score = float(stranger_scores[i])
            bbox = [int(v) for v in raw_results[i]["bbox"]]
            x1, y1, x2, y2 = bbox
            face_h = max(y2 - y1, 0)

            # Debug capture inputs: the face cut straight out of the original
            # frame (clamped to its bounds, so crop_box below reproduces it),
            # and the 112x112 crop the embedder actually saw.
            debug_on = self._debug.enabled
            cx1, cy1 = max(0, x1), max(0, y1)
            cx2, cy2 = min(frame_w, x2), min(frame_h, y2)
            face_crop = (
                frame[cy1:cy2, cx1:cx2].copy()
                if debug_on and cx2 > cx1 and cy2 > cy1
                else None
            )
            aligned = raw_results[i].get("aligned")
            landmarks = raw_results[i].get("landmarks") if debug_on else None
            kps5 = raw_results[i].get("kps") if debug_on else None
            landmark_score = raw_results[i].get("landmark_score")
            height_ratio = face_h / frame_h if frame_h else 0.0

            if face_h / frame_h < self._height_ratio_threshold:
                if debug_on:
                    _ = self._debug.save_failure(
                        "too-small",
                        face_crop=face_crop,
                        aligned=aligned,
                        frame=frame,
                        bbox=bbox,
                        landmarks=landmarks,
                        kps5=kps5,
                        landmark_score=landmark_score,
                        crop_box=[cx1, cy1, cx2, cy2],
                        frame_size=[frame_w, frame_h],
                        face_height_ratio=height_ratio,
                        height_ratio_threshold=self._height_ratio_threshold,
                        det_score=det_scores[i],
                        enroll_similarity=float(upload_scores[i]),
                        extended_similarity=float(ext_scores[i]),
                        stranger_similarity=s_score,
                        detail="bbox height below FACE_HEIGHT_RATIO_THRESHOLD",
                    )
                continue

            # Truncation gate. A face clipped by a frame edge is missing features, not
            # merely smaller.
            vis_w = max(0, min(frame_w, x2) - max(0, x1))
            vis_h = max(0, min(frame_h, y2) - max(0, y1))
            box_area = (x2 - x1) * face_h
            truncation = (
                1.0 - (vis_w * vis_h) / box_area if box_area > 0 else 1.0
            )
            if truncation > self._max_truncation:
                logger.info(
                    "[face] dropped: bbox %.0f%% off-frame (max %.0f%%) — "
                    "bbox=%s frame=%dx%d",
                    truncation * 100, self._max_truncation * 100,
                    bbox, frame_w, frame_h,
                )
                if debug_on:
                    _ = self._debug.save_failure(
                        "truncated",
                        face_crop=face_crop,
                        aligned=aligned,
                        frame=frame,
                        bbox=bbox,
                        landmarks=landmarks,
                        kps5=kps5,
                        landmark_score=landmark_score,
                        crop_box=[cx1, cy1, cx2, cy2],
                        frame_size=[frame_w, frame_h],
                        truncation=truncation,
                        max_truncation=self._max_truncation,
                        truncation_edges={
                            "left": max(0, -x1) / (x2 - x1) if x2 > x1 else 0.0,
                            "right": max(0, x2 - frame_w) / (x2 - x1)
                            if x2 > x1
                            else 0.0,
                            "top": max(0, -y1) / face_h if face_h else 0.0,
                            "bottom": max(0, y2 - frame_h) / face_h
                            if face_h
                            else 0.0,
                        },
                        det_score=det_scores[i],
                        enroll_similarity=float(upload_scores[i]),
                        extended_similarity=float(ext_scores[i]),
                        stranger_similarity=s_score,
                        detail="bbox clipped by the frame edge beyond FACE_MAX_TRUNCATION",
                    )
                continue

            sharpness = (
                self._sharpness(aligned) if aligned is not None else float("inf")
            )
            if sharpness < self._min_sharpness:
                logger.info(
                    "[face] dropped: too blurred (sharpness %.0f < %.0f)",
                    sharpness, self._min_sharpness,
                )
                if debug_on:
                    _ = self._debug.save_failure(
                        "blurred",
                        face_crop=face_crop,
                        aligned=aligned,
                        frame=frame,
                        bbox=bbox,
                        landmarks=landmarks,
                        kps5=kps5,
                        landmark_score=landmark_score,
                        crop_box=[cx1, cy1, cx2, cy2],
                        frame_size=[frame_w, frame_h],
                        sharpness=sharpness,
                        min_sharpness=self._min_sharpness,
                        face_height_ratio=height_ratio,
                        truncation=truncation,
                        det_score=det_scores[i],
                        enroll_similarity=float(upload_scores[i]),
                        extended_similarity=float(ext_scores[i]),
                        stranger_similarity=s_score,
                        detail="aligned-crop sharpness below FACE_MIN_SHARPNESS",
                    )
                continue

            det_score = det_scores[i]

            decision_score: float = max(o_score, s_score)
            matched_label: str | None = None
            match_source: str | None = None
            rescued_by_extended: bool = False
            is_new_stranger: bool = False

            # Asymmetric owner match. A single threshold has no safe value here.
            up_s = float(upload_scores[i])
            ex_s = float(ext_scores[i])
            enroll_match = up_s > self._threshold
            extended_match = ex_s > self._extended_threshold

            if enroll_match or extended_match:
                # Identity comes from the bank that AUTHORISED the match, not from
                # whichever merely scored higher. An extended view below its own
                # threshold is not trusted to carry a decision, so it must not supply
                # the name either.
                if enroll_match and extended_match:
                    ext_won = ex_s > up_s
                else:
                    ext_won = extended_match
                raw_id = (ext_ids[i] if ext_won else upload_ids[i]) or ""
                person_id = raw_id.removeprefix(self.FRIEND_PREFIX)
                face_kind = PersonKind.FRIEND
                decision_score = ex_s if ext_won else up_s
                matched_label = raw_id or None
                match_source = "extended" if ext_won else "enroll"
                rescued_by_extended = extended_match and not enroll_match
                if rescued_by_extended:
                    logger.info(
                        "[face] '%s' RESCUED by extended set "
                        "(enroll_sim=%.3f <= thr=%.2f, extended_sim=%.3f > "
                        "ext_thr=%.2f)",
                        person_id, up_s, self._threshold,
                        ex_s, self._extended_threshold,
                    )
                else:
                    logger.debug(
                        "[face] '%s' matched via %s "
                        "(enroll_sim=%.3f, extended_sim=%.3f, thr=%.2f, "
                        "ext_thr=%.2f)",
                        person_id, match_source, up_s, ex_s,
                        self._threshold, self._extended_threshold,
                    )
                if (
                    raw_id
                    and match_source == "enroll"
                    and up_s > self._extend_min_enroll_sim
                ):
                    extend_candidates.append(
                        (
                            raw_id,
                            embeds[i],
                            (x1, y1, x2, y2),
                            {
                                "enroll_similarity": up_s,
                                "extended_similarity": ex_s,
                                "match_source": match_source,
                                "det_score": float(det_score),
                                "landmark_score": (
                                    None
                                    if landmark_score is None
                                    else float(landmark_score)
                                ),
                                "face_height_ratio": float(height_ratio),
                                "truncation": float(truncation),
                                "bbox": [int(v) for v in bbox],
                                "frame_size": [int(frame_w), int(frame_h)],
                                "thresholds": {
                                    "threshold": self._threshold,
                                    "extended_threshold": self._extended_threshold,
                                    "extend_min_enroll_sim": (
                                        self._extend_min_enroll_sim
                                    ),
                                    "diversity_threshold": self._diversity_threshold,
                                },
                            },
                        )
                    )
            elif s_score > self._stranger_threshold:
                # An already-known stranger. Matched at the SAME bar as the extended
                # bank, not the upload bar.
                raw_id = stranger_ids[i] or ""
                person_id = raw_id.removeprefix(self.STRANGER_PREFIX)
                face_kind = PersonKind.STRANGER
                decision_score = s_score
                matched_label = raw_id or None
                match_source = "stranger"
            elif (
                self._negative_threshold is None
                or o_score <= self._negative_threshold
            ):
                # Not anyone enrolled (both owner banks below the negative bar) and not
                # any stranger we already know (the branch above consulted the stranger
                # bank at its own bar).
                with self._lock:
                    ticks = self._corroborate_stranger(embeds[i])
                if ticks < self._stranger_min_ticks:
                    logger.debug(
                        "[face] unknown face seen %d/%d tick(s) — holding as "
                        "unsure before minting an identity",
                        ticks, self._stranger_min_ticks,
                    )
                    person_id = "?"
                    face_kind = PersonKind.UNSURE
                else:
                    with self._lock:
                        self._stranger_counter += 1
                        self._stranger_counter %= int(1e6)

                        raw_id = (
                            f"{self.STRANGER_PREFIX}stranger_{self._stranger_counter}"
                        )
                    person_id = raw_id.removeprefix(self.STRANGER_PREFIX)
                    face_kind = PersonKind.STRANGER
                    matched_label = raw_id
                    is_new_stranger = True
                    logger.info(
                        "[face] minted '%s' after %d consecutive tick(s)",
                        person_id, ticks,
                    )

                    new_stranger_embeds.append(embeds[i])
                    new_stranger_labels.append(raw_id)
            else:
                person_id = "?"
                face_kind = PersonKind.UNSURE

            faces.append(
                Face(
                    bbox=bbox,
                    kind=face_kind,
                    person_id=person_id,
                    confidence=det_score,
                    # Re-centered face-mesh box (get_box over the 468 landmarks)
                    # computed during alignment above; reused by the emotion
                    # pipeline so it never re-runs the mesh. None if unavailable.
                    emotion_box=raw_results[i].get("emotion_box"),
                )
            )

            if debug_on:
                _ = self._debug.save_decision(
                    face_id=(
                        person_id if face_kind != PersonKind.UNSURE else "UNSURE"
                    ),
                    similarity=decision_score,
                    face_crop=face_crop,
                    aligned=aligned,
                    frame=frame,
                    bbox=bbox,
                    landmarks=landmarks,
                    kps5=kps5,
                    color=_DEBUG_KIND_COLOR.get(face_kind, (128, 128, 128)),
                    # Clamped [x1, y1, x2, y2] actually cut for input.jpg — apply
                    # it to frame.jpg to reproduce the crop (bbox above is the
                    # raw detector box, which may extend past the frame edges).
                    crop_box=[cx1, cy1, cx2, cy2],
                    frame_size=[frame_w, frame_h],
                    kind=str(face_kind),
                    person_id=person_id,
                    matched_label=matched_label,
                    match_source=match_source,
                    owner_similarity=o_score,
                    enroll_similarity=float(upload_scores[i]),
                    extended_similarity=float(ext_scores[i]),
                    stranger_similarity=s_score,
                    threshold=self._threshold,
                    extended_threshold=self._extended_threshold,
                    stranger_threshold=self._stranger_threshold,
                    negative_threshold=self._negative_threshold,
                    det_score=det_score,
                    landmark_score=landmark_score,
                    sharpness=sharpness,
                    face_height_ratio=height_ratio,
                    height_ratio_threshold=self._height_ratio_threshold,
                    rescued_by_extended=rescued_by_extended,
                    new_stranger=is_new_stranger,
                    face_index=i,
                    n_faces=n_faces,
                )

        if new_stranger_embeds:
            stacked_e = np.stack(new_stranger_embeds, axis=0)
            stacked_l = np.stack(new_stranger_labels, axis=0)
            with self._lock:
                self._stranger_embeddings = (
                    np.concatenate([self._stranger_embeddings, stacked_e])
                    if self._stranger_embeddings is not None
                    else stacked_e
                )
                self._stranger_labels = (
                    np.concatenate([self._stranger_labels, stacked_l])
                    if self._stranger_labels is not None
                    else stacked_l
                )
                self._evict_oldest_strangers()
                self._save_strangers_state()

        # Auto-extend enrollment: crop each confidently-matched view and fold it into
        # its user's extended set.
        if extend_candidates:
            for raw_label, emb, bbox, meta in extend_candidates:
                crop = self._crop_face(frame, bbox)
                self._maybe_extend_user(raw_label, emb, crop, meta)

        return faces

    def _evict_oldest_strangers(self) -> None:
        if self._stranger_embeddings is None or self._stranger_labels is None:
            return

        count = len(self._stranger_embeddings)
        if count <= self._max_strangers:
            return
        drop = count - self._max_strangers
        logger.debug("Evicting %d oldest stranger(s)", drop)
        self._stranger_embeddings = self._stranger_embeddings[drop:]
        self._stranger_labels = self._stranger_labels[drop:]

    def _save_strangers_state(self):
        if self._stranger_embeddings is not None and self._stranger_labels is not None:
            try:
                np.save(STRANGER_STATE_DIR / "embeds.npy", self._stranger_embeddings)
                np.save(STRANGER_STATE_DIR / "labels.npy", self._stranger_labels)
                np.save(
                    STRANGER_STATE_DIR / "counter.npy", np.array(self._stranger_counter)
                )
                logger.debug("Saved strangers' state")
            except Exception as e:
                logger.error(f"Failed to save strangers' state due to {e}")

    def _load_strangers_state(self):
        """Re-read the stranger bank from disk. SILENT when it does not exist.

        The bank files are only ever written when a brand-new stranger is minted (see
        ``_save_strangers_state``), so on a device where every face it ever sees is
        enrolled they are never created at all.
        """
        embeds_path = STRANGER_STATE_DIR / "embeds.npy"
        labels_path = STRANGER_STATE_DIR / "labels.npy"
        if not (embeds_path.exists() and labels_path.exists()):
            return

        try:
            stranger_embeddings = np.load(embeds_path, allow_pickle=True)
            stranger_labels = np.load(labels_path, allow_pickle=True)
            stranger_counter = int(
                np.load(STRANGER_STATE_DIR / "counter.npy", allow_pickle=True)
            )
        except Exception:
            logger.exception("Failed to load strangers' state")
            stranger_embeddings = None
            stranger_labels = None
            stranger_counter = 0

        if stranger_embeddings is not None and stranger_labels is not None:
            self._stranger_embeddings = stranger_embeddings
            self._stranger_labels = stranger_labels
            self._stranger_counter = stranger_counter
