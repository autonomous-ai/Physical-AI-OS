"""FaceRecognizer — SCRFD + ONNX landmark + EdgeFace recognition & enrollment.

Owns the per-user enrollment bank (uploads + auto-captured "extended" views) and
the stranger bank, does retrieval against them, and lazily builds the ONNX
pipeline on ``start()`` — which is also where the model weights are fetched on
first use (see ``model_store.ensure_face_models``).
"""

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

# Auto-captured "extended" enrollment views persist in this per-user subfolder,
# i.e. USERS_DIR/<user>/.extended/. It is dot-prefixed so the upload loader
# (which reads image FILES directly under the user dir) never mistakes an
# extended view for an upload, and the photos watcher can skip it (the extended
# set self-manages and must not trigger a full re-embed of every upload). Each
# captured view is stored as a JPEG crop PLUS a sidecar .npy holding its
# (already L2-normalized) embedding, so a restart reloads the exact embedding
# and never has to re-detect a hard side-view — which is the very thing this
# feature exists to keep, and the thing a re-detect would be most likely to miss.
_EXTENDED_SUBDIR = ".extended"
_EXTENDED_IMG_EXT = ".jpg"
_EXTENDED_EMB_EXT = ".npy"
# Provenance sidecar: why this view was admitted, and under which rules. Written
# beside the crop and its embedding. Metadata only — nothing reads it back into
# the bank (``_read_extended_for`` globs the JPEGs), so a missing or malformed
# one costs nothing at runtime. It exists so a bank can be AUDITED after the
# fact: without it the only remedy for a rule that turned out to admit the wrong
# views is to wipe every view, which is exactly what had to be done on lamp-ac82
# when 6 of 10 turned out to be other people.
_EXTENDED_META_EXT = ".json"

# Box colour per verdict in the debug log's annotated.jpg — same convention as
# FacePerception._FACE_COLOR (BGR).
_DEBUG_KIND_COLOR: dict[PersonKind, tuple[int, int, int]] = {
    PersonKind.FRIEND: (0, 255, 0),  # green
    PersonKind.STRANGER: (0, 0, 255),  # red
    PersonKind.UNSURE: (0, 255, 255),  # yellow
}


def _flat_kps(kps: Any) -> list[float] | None:
    """SCRFD's (5, 2) keypoint array as the flat list Face.kps carries."""
    if kps is None:
        return None
    return [float(v) for v in np.asarray(kps).reshape(-1)]


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
        # Blur floor on the aligned crop; see FACE_MIN_SHARPNESS in hal/config.py.
        self._min_sharpness: float = min_sharpness
        # Consecutive ticks an unknown face must persist before it earns an
        # identity; see FACE_STRANGER_MIN_TICKS in hal/config.py.
        self._stranger_min_ticks: int = stranger_min_ticks
        self._stranger_corroboration_s: float = stranger_corroboration_s
        # Unknown faces seen but not yet corroborated, as
        # [embedding, ticks_seen, last_seen_ts]. Deliberately a LIST, not one
        # slot: with two unknown people in frame a single slot would be stolen
        # back and forth every tick and neither would ever reach the threshold.
        self._pending_strangers: list[
            tuple[npt.NDArray[np.float32], int, float]
        ] = []
        # Bar for a match carried by the enrolled uploads; see
        # FACE_MATCH_THRESHOLD in hal/config.py.
        self._threshold: float = threshold
        # Bar for a match carried by the extended bank alone — deliberately
        # higher than ``threshold``; see FACE_EXTENDED_THRESHOLD in hal/config.py.
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

        # --- Auto-extend enrollment config -----------------------------------
        # Max number of dynamically-captured extra views KEPT per user (on top
        # of their untouched uploaded enrollment images).
        self._max_extended_images: int = max_extended_images
        # A confidently-matched live frame is only added to a user's extended
        # set when its max cosine similarity to that user's existing views
        # (uploads + current extended) is BELOW this value. Anything above is
        # redundant (near-duplicate of a view we already have) and skipped, so
        # the extended set fills up with genuinely new poses (e.g. side-view).
        self._diversity_threshold: float = diversity_threshold

        self._app: _EdgeFacePipeline | None = None
        self._owner_embeddings: npt.NDArray[np.float32] | None = None
        self._owner_labels: npt.NDArray[np.str_] | None = None
        # Dynamically-grown per-user "extended" bank. Same FRIEND_PREFIX labels
        # as the uploads so retrieval maps a match straight back to the friend
        # id regardless of which bank it came from. Kept SEPARATE from
        # ``_owner_embeddings`` so the user's uploads are never mutated and can
        # always be rebuilt verbatim from disk. Mirrored to disk under each
        # user's ``.extended`` folder so it survives restarts (see
        # _EXTENDED_SUBDIR). ``_extended_paths`` runs parallel to the
        # embeddings/labels and holds each view's on-disk JPEG path, so an
        # eviction during pruning can delete the backing files too.
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

        # Per-detection debug capture (input crop + aligned model input + clean
        # frame + annotated frame + result.json, one folder per face).
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

        # First use: make sure the ONNX weights are present locally, fetching
        # them from the weights bucket into the model cache dir if missing.
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
                # Extended views belong to the (now-cleared) uploads; drop the
                # in-memory copy so no sample dangles on a removed user. The
                # on-disk .extended files are left intact (persistence): a
                # subsequent load_from_disk repopulates them, and only removing a
                # person/photo (which rmtrees the user dir) erases them for good.
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

    # -- Auto-extend enrollment --------------------------------------------------
    #
    # Users typically upload frontal shots, but a ceiling/desk camera mostly
    # sees them side-on. Those side views miss the frontal bank, get flagged as
    # strangers, and spawn duplicate "stranger_N" identities. To fix this each
    # user gets a second, dynamically-grown "extended" bank: a live frame may be
    # kept as an extra reference view when TWO independent things hold —
    #
    #   identity: the enrolled UPLOADS carried the match, above
    #             FACE_EXTEND_MIN_ENROLL_SIM (checked in detect(), before the
    #             candidate is queued). Never a match the extended bank made:
    #             that is evidence about a previous guess, not about the person.
    #   novelty:  it is DIFFERENT enough from what is already stored, so the
    #             bank fills with new poses instead of near-duplicates
    #             (``diversity_threshold``, checked in _maybe_extend_user).
    #
    # Both are required. Novelty alone is what let this bank fill with strangers:
    # "far from everything we have" is equally the signature of a new pose and of
    # a different person, so a novelty-only rule selects for the thing it should
    # be screening out. The set is capped at ``max_extended_images`` most-diverse
    # samples. The uploaded images are never touched.

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
        """Consider folding one confidently-matched live view into a user's
        extended set AND persisting it to disk. Manages its own locking.

        ``embedding`` is assumed L2-normalized (as produced in ``detect``).
        ``crop`` is the BGR face crop to persist; if it is None/empty the view is
        not added, keeping memory and disk in lock-step (every in-memory extended
        embedding has a backing file).

        IMPORTANT: this runs on the ``detect`` hot path, so it NEVER holds
        ``self._lock`` across disk I/O. The lock is taken only for two short,
        pure-memory critical sections (snapshot the existing views; append +
        prune the arrays); the JPEG/sidecar write and any eviction ``unlink``s
        happen with the lock released. Holding the lock across ``cv2.imwrite`` /
        ``np.save`` (as an earlier version did) lengthened every recognized
        frame's lock hold and contended with the photos-watcher reload, which
        widened the ``load_from_disk`` swap window enough to make friends
        momentarily score ``_NO_MATCH``.

        Diversity gate: compute the max cosine similarity between the new view
        and everything already stored for this user (uploads + extended). If it
        exceeds ``diversity_threshold`` (default 0.7) the view is a near-
        duplicate of one we already have -> skip. Otherwise it shows a pose the
        set lacks (e.g. a ~0.35 side-view that only just cleared the confidence
        threshold) -> persist it, append it, and prune back to the most diverse
        samples.
        """
        if crop is None or crop.size == 0:
            return

        # (1) Short lock: snapshot this user's existing views (cheap, in-memory).
        with self._lock:
            enroll = self._user_embeddings(
                self._owner_embeddings, self._owner_labels, raw_label
            )
            extended = self._user_embeddings(
                self._extended_embeddings, self._extended_labels, raw_label
            )
            existing = [e for e in (enroll, extended) if e is not None and len(e)]
            existing_stack = np.concatenate(existing) if existing else None

        # Gate 1 — cheap near-duplicate reject (no lock). A slightly stale
        # snapshot is harmless: the worst case is storing one near-duplicate,
        # which the next prune trims. This is a fast pre-filter (one matmul); the
        # more expensive diversity selection below only runs if it passes.
        max_sim: float | None = None
        if existing_stack is not None:
            max_sim = float(np.max(existing_stack @ embedding))
            if max_sim > self._diversity_threshold:
                # Redundant — we already store an almost identical view.
                logger.debug(
                    "[face] extended '%s': skip redundant view "
                    "(max_sim=%.3f > %.2f)",
                    raw_label.removeprefix(self.FRIEND_PREFIX),
                    max_sim,
                    self._diversity_threshold,
                )
                return

        # Gate 2 — decide keep/drop IN MEMORY, before touching disk. Admission
        # only competes once the set is already full: run the SAME farthest-point
        # selection the prune step uses and check whether the new view would
        # survive it. If it would merely be written and then pruned away on the
        # same frame, skip the disk write entirely. This kills the write-then-
        # immediately-delete churn on the detect hot path — clearing gate 1 is
        # far more permissive than winning a top-``max_extended_images`` slot, so
        # a newly captured view is frequently the least-diverse of the full set.
        # A stale snapshot stays harmless: the authoritative prune re-runs under
        # the lock at commit and remains correct regardless.
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

        # (2) Persist WITHOUT the lock held. A view enters the in-memory bank
        # only once it has a backing file, so the two never drift apart.
        # Fold in what only this method knows: how novel the view was against
        # everything already stored, which is the other half of the admission
        # decision (identity was decided in detect()).
        provenance = dict(meta or {})
        provenance["max_sim_to_existing"] = max_sim
        path = self._save_extended_view(raw_label, embedding, crop, provenance)
        if path is None:
            return

        # (3) Short lock: append + prune (pure array ops). Evicted files are
        # collected here and deleted AFTER the lock is released.
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

        # Only report an ADD when the new view actually stayed. Gate 2 already
        # skips the common "would be pruned immediately" case before writing, but
        # a concurrent add can still change the set between that in-memory
        # decision and the locked prune; if the authoritative prune then evicted
        # THIS view its file is in ``dropped`` (just deleted), so don't log it as
        # added.
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

        Starting from ``anchor`` (the user's fixed uploads) as reference points,
        repeatedly keep the candidate whose similarity to everything already
        kept (anchor + kept candidates) is LOWEST — the farthest / most novel
        pose. This packs the slots with views that best complement the frontal
        uploads (side-views, tilts) rather than more frontals. If ``anchor`` is
        None/empty, the newest candidate (last row) seeds the selection.
        """
        m = len(candidates)
        if m <= k:
            return list(range(m))

        if anchor is not None and len(anchor):
            selected_ref: list[npt.NDArray[np.float32]] = [anchor]
            selected_local: list[int] = []
        else:
            seed = m - 1  # newest view
            selected_ref = [candidates[seed][None, :]]
            selected_local = [seed]

        remaining = [j for j in range(m) if j not in selected_local]
        while len(selected_local) < k and remaining:
            ref = np.concatenate(selected_ref)  # (K, D)
            sims = candidates[remaining] @ ref.T  # (R, K)
            # Closeness of each candidate to its nearest already-kept view;
            # the smallest such value is the most novel candidate.
            nearest = sims.max(axis=1)
            pick = int(np.argmin(nearest))
            chosen = remaining.pop(pick)
            selected_local.append(chosen)
            selected_ref.append(candidates[chosen][None, :])
        return selected_local

    def _prune_extended_set(self, raw_label: str) -> list[str]:
        """Trim one user's extended bank to the ``max_extended_images`` most
        diverse views. Caller must hold ``self._lock``.

        Only touches the in-memory arrays; it RETURNS the on-disk paths of the
        evicted views so the caller can delete their files AFTER releasing the
        lock (disk I/O must never run under ``self._lock`` — see
        ``_maybe_extend_user``). Returns an empty list when nothing is evicted.
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

        candidates = self._extended_embeddings[idxs]  # (M, D), newest is last
        anchor = self._user_embeddings(
            self._owner_embeddings, self._owner_labels, raw_label
        )
        keep_local = self._select_diverse(candidates, anchor, self._max_extended_images)
        keep_global = idxs[np.array(sorted(keep_local))]

        dropped = [
            str(self._extended_paths[gi]) for gi in np.setdiff1d(idxs, keep_global)
        ]

        keep_mask = ~mask  # keep every OTHER user's rows untouched
        keep_mask[keep_global] = True
        self._extended_embeddings = self._extended_embeddings[keep_mask]
        self._extended_labels = self._extended_labels[keep_mask]
        self._extended_paths = self._extended_paths[keep_mask]
        return dropped

    # -- Extended-set persistence (disk) -----------------------------------------

    def _extended_dir_for(self, raw_label: str) -> Path:
        """Per-user directory holding auto-captured extended views.

        The user's on-disk folder name is exactly the friend label without the
        FRIEND_PREFIX (load_from_disk labels each user by their folder name), so
        no re-normalization is needed here.
        """
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

        Returns the JPEG path on success, or None if it could not be written (in
        which case the caller must NOT add the view to the in-memory bank). The
        sidecar embedding is what a later load trusts, so a restart reloads the
        exact vector and never has to re-detect the (possibly hard) pose.

        The .json is documentation, not state: nothing loads it back, so failing
        to write it does NOT fail the view. It records the scores and the
        thresholds that admitted this view so the bank can be audited later.
        """
        try:
            dest = self._extended_dir_for(raw_label)
            dest.mkdir(parents=True, exist_ok=True)
            # Millisecond stamp keeps names sortable; the seq suffix guarantees
            # uniqueness even for two captures within the same millisecond. The
            # counter bump is the only locked step here — the file writes below
            # run WITHOUT the lock (this method is called off the lock).
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
        """Delete an extended view's JPEG, its sidecar .npy and its provenance
        .json (best-effort). All three share a stem, so an eviction leaves
        nothing behind."""
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
        """Return the L2-normalized embedding for one persisted extended view.

        Fast path: the sidecar .npy next to the JPEG (exact, no inference). It is
        trusted as-is because it was validated when captured — crucially we do
        NOT re-gate it against the uploads, since a legitimate side-view may only
        match other extended views, not the frontal uploads. If ``expected_dim``
        is given and the sidecar's length differs (a model swap invalidated it),
        the sidecar is ignored and we re-embed the JPEG with the current model.
        Fallback: re-embed the JPEG and take the largest detected face, then
        rewrite the sidecar. Returns None if neither yields an embedding.
        """
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
        """Read one user's persisted extended views from disk. PURE reader: no
        lock, no in-memory mutation — it only touches the filesystem and returns
        ``(embeddings, paths)`` for the caller to install atomically.

        Only ``*.jpg`` is enumerated, so the ``.json`` provenance sidecar is
        ignored here by construction — it is for humans and audits, never an
        input to the bank.

        Each view's sidecar embedding is trusted as-is (it was validated at
        capture); we deliberately do NOT re-gate against the uploads, since a
        genuine side-view often matches only other extended views. A view is
        dropped (and its files removed) only when it is truly unusable — no
        usable sidecar AND no detectable face in the crop. When more than
        ``max_extended_images`` survive (e.g. after a shrunk config), the most
        diverse subset is kept (anchored on ``anchor``, the user's uploads) and
        the rest deleted.
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
                # Neither a usable sidecar nor a detectable face — drop it.
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
        """Atomically rebuild the owner AND extended banks from disk.

        Fixes the reload race: the previous flow cleared the owner bank and then
        re-appended per person, leaving a window in which ``detect`` saw a
        None/partial owner bank and scored every friend ``_NO_MATCH``. Here ALL
        heavy work — owner embedding inference and extended disk reads — happens
        WITHOUT the lock, and a single locked swap installs both banks at once,
        so ``detect`` only ever sees the complete old set or the complete new
        set, never an intermediate.

        ``owner_labels`` are the raw (folder-name) labels for ``owner_images``;
        ``person_names`` are all enrolled folder names whose ``.extended`` sets
        should be restored (a superset of the labels is fine — empty ones no-op).
        """
        if self._app is None:
            msg = f"[{self.__class__.__name__}] service must be started first"
            raise RuntimeError(msg)

        # 1. Owner embeddings (inference, no lock).
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

        # 2. Extended views (disk reads, no lock). Anchor each user's diversity
        # on their FRESHLY-computed uploads, decoupled from live state.
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

        # 3. Single atomic swap of both banks.
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
        """Count how many consecutive ticks this unknown face has now been seen
        for, remembering it for next time. Caller must hold ``self._lock``.

        Matching is by EMBEDDING, not by position: "in a row" has to mean the
        same person, and a bbox moves. A candidate that goes unseen for longer
        than ``stranger_corroboration_s`` is forgotten, so the count means
        "still here", not "here at some point today".
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

        Deliberately on the ALIGNED 112x112 rather than the detector crop: the
        latter varies in size frame to frame, and the variance of the Laplacian
        scales with resolution, so its values are not comparable between frames.
        Costs ~0.06 ms on a crop that already exists (it is the embedder input).
        """
        gray = cv2.cvtColor(aligned, cv2.COLOR_BGR2GRAY)
        return float(cv2.Laplacian(gray, cv2.CV_64F).var())

    @staticmethod
    def _crop_face(
        frame: npt.NDArray[np.uint8],
        bbox: tuple[int, int, int, int],
        margin: float = 0.3,
    ) -> npt.NDArray[np.uint8] | None:
        """BGR crop around a detection bbox with a relative margin, clamped to
        the frame. The margin gives the reloader enough context to re-detect the
        face if a sidecar embedding is ever missing. None if degenerate.
        """
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
            # Deliberately NOT debug-logged: detection ticks on every frame
            # whether or not anybody is in the room, so empty-room captures
            # would evict every real detection from the capped log directory.
            return

        embeds: npt.NDArray[np.float32] = np.stack(
            [r["embedding"] / np.linalg.norm(r["embedding"]) for r in raw_results]
        )
        det_scores: npt.NDArray[np.float32] = np.stack(
            [r["det_score"] for r in raw_results]
        )

        with self._lock:
            self._load_strangers_state()

            # Retrieve against the uploads and the extended views SEPARATELY.
            # The owner decision uses the max of the two (identical to matching a
            # single combined bank), but keeping them apart lets the debug log
            # show WHICH set made each match — in particular when the extended
            # set rescued a friend the frontal uploads alone would have missed.
            upload_scores, upload_ids = self._retrieve(
                embeds, self._owner_embeddings, self._owner_labels
            )
            ext_scores, ext_ids = self._retrieve(
                embeds, self._extended_embeddings, self._extended_labels
            )
            stranger_scores, stranger_ids = self._retrieve(
                embeds, self._stranger_embeddings, self._stranger_labels
            )

        # Best score across both owner banks. Used for the unsure / new-stranger
        # split further down, which asks "is this face unknown to us at all" —
        # a question both banks answer equally. The FRIEND decision does NOT use
        # it: the two banks carry different weight and are compared against
        # their own thresholds (see the asymmetric owner match below).
        owner_scores = np.maximum(upload_scores, ext_scores)

        new_stranger_embeds = []
        new_stranger_labels = []
        # (friend_raw_label, normalized_embedding, bbox, provenance) for
        # confidently-matched faces that may extend their user's set
        # (diversity-gated + cropped + persisted after the loop).
        extend_candidates: list[
            tuple[
                str,
                npt.NDArray[np.float32],
                tuple[int, int, int, int],
                dict[str, Any],
            ]
        ] = []
        # per-face: (bbox_pixels, face_kind, label)  face_kind: "friend"|"stranger"|"unsure"
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
            # Needed by the blur gate below, so it is fetched unconditionally
            # now — it is a reference to an array the embedder already built.
            aligned = raw_results[i].get("aligned")
            # Dense 468-point FaceMesh (full-frame pixels) and the 5 canonical
            # points the alignment warp was built from — both already computed
            # upstream, plotted into face_with_landmark.jpg.
            landmarks = raw_results[i].get("landmarks") if debug_on else None
            kps5 = raw_results[i].get("kps") if debug_on else None
            landmark_score = raw_results[i].get("landmark_score")
            height_ratio = face_h / frame_h if frame_h else 0.0

            # Height, not area — see FACE_HEIGHT_RATIO_THRESHOLD in hal/config.py.
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

            # Truncation gate. A face clipped by a frame edge is missing
            # features, not merely smaller: SCRFD still reports a plausible box
            # (the clipped edge just runs off-frame), and the landmark mesh
            # invents the part it cannot see, so the embedding describes a face
            # that was never photographed. The landmark-in-bbox check upstream
            # cannot catch this — it clamps the bbox to the frame first, so a
            # point can never be "outside" on the very edge that clipped it.
            # Rejecting here also keeps the view out of extend_candidates, so a
            # cut-off face can never be auto-added to a user's extended set.
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
                        # Per-edge overflow as a fraction of the box's own
                        # width/height — which edge clipped it, and by how much.
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

            # Blur gate. Motion blur — the lamp panning, or the person moving —
            # destroys a face without making it smaller or clipping it, so
            # neither gate above sees it. What comes out is a near-random
            # embedding that resembles nothing, and "resembles nothing" is the
            # NEW-STRANGER branch: a smeared frame of the enrolled user mints an
            # identity for him. Dropping it here protects all three downstream
            # decisions at once — no verdict, no minted identity, and no smeared
            # crop admitted to the extended set.
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

            # Debug-log fields describing HOW this face was decided; each
            # branch below overrides what applies to it.
            decision_score: float = max(o_score, s_score)
            matched_label: str | None = None
            match_source: str | None = None
            rescued_by_extended: bool = False
            is_new_stranger: bool = False

            # Asymmetric owner match. The uploads are ground truth and keep the
            # match threshold; the auto-captured extended views are a guess the
            # device made about itself, so carrying a match ALONE costs them a
            # higher bar. A single threshold has no safe value here: every
            # extended bank lifts the best stranger score into the 0.32-0.40
            # band the uploads alone never reach, while raising the bar for the
            # uploads to where the extended bank is safe would cost the frontal
            # recall the extended bank exists to recover.
            up_s = float(upload_scores[i])
            ex_s = float(ext_scores[i])
            enroll_match = up_s > self._threshold
            extended_match = ex_s > self._extended_threshold

            if enroll_match or extended_match:
                # Identity comes from the bank that AUTHORISED the match, not
                # from whichever merely scored higher. An extended view below
                # its own threshold is not trusted to carry a decision, so it
                # must not supply the name either — otherwise a rescue-shaped
                # near-miss on one user could relabel a match the uploads won.
                if enroll_match and extended_match:
                    ext_won = ex_s > up_s
                else:
                    ext_won = extended_match
                raw_id = (ext_ids[i] if ext_won else upload_ids[i]) or ""
                person_id = raw_id.removeprefix(self.FRIEND_PREFIX)
                face_kind = PersonKind.FRIEND
                # Observability: log whether the uploads or the extended set
                # carried this match. The high-signal case is an EXTENDED rescue
                # — the frontal uploads scored at/below threshold but a stored
                # side/angled view pushed it over — which is exactly the benefit
                # this feature exists to deliver.
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
                # Candidate to enrich the user's extended set — but only when
                # the UPLOADS themselves carried this match, and by a clear
                # margin. Being recognised is not enough to become a reference
                # view: a match the extended bank carried is evidence about a
                # previous guess, not about the person, so letting it add a
                # view lets one mistake breed more. Anchoring on the uploads is
                # what makes poisoning non-replicating. The pose still has to be
                # new enough to keep — see _maybe_extend_user.
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
                            # Why this view was admitted, and under which rules.
                            # The thresholds ride along so a later audit can ask
                            # "which views did the OLD rule let in" without
                            # guessing what the config was at the time.
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
                # An already-known stranger. Matched at the SAME bar as the
                # extended bank, not the upload bar: both banks are single
                # auto-captured camera views that were never re-validated, and
                # at the upload bar a stale row false-accepts a different person
                # (#429: 0.346 / 0.385 / 0.318 against three different rows,
                # id flipping with head pose).
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
                # Not anyone enrolled (both owner banks below the negative
                # bar) and not any stranger we already know (the branch above
                # consulted the stranger bank at its own bar). That is what an
                # unknown person looks like — and equally what a momentarily
                # unusable frame looks like, since a degraded crop resembles
                # nothing. The gates above drop the unusable frames they can
                # MEASURE; the rest are caught by asking what no single frame
                # can answer: is this face still here a tick later? Minting is
                # the expensive verdict (a persistent identity, a stranger
                # presence event, a row in the Unknown Faces card), so it waits
                # for that answer.
                #
                # The stranger bank is deliberately NOT part of the negative
                # test. Requiring every stranger row <= negative_threshold
                # meant that once the bank held a dozen rows some row was
                # nearly always above 0.2, so a genuinely new person was either
                # mis-assigned (above the old 0.3 bar) or parked as "?" for
                # ever (#429). A stranger score between negative_threshold and
                # stranger_threshold is "nobody we can vouch for", and the
                # honest answer to that is a new identity.
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
                # An OWNER bank scored between negative_threshold and its match
                # bar: too close to an enrolled user to call them a stranger,
                # not close enough to name them. Unsure; the next frame decides.
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
                    kps=_flat_kps(raw_results[i].get("kps")),
                )
            )

            # Every decided face → its own timestamped folder holding the frame
            # crop, the aligned model input, the clean frame, an annotated frame
            # and result.json. Folder name is "<time>_<face_id>_<similarity>" so
            # a false acceptance is spottable at a glance from the listing.
            if debug_on:
                _ = self._debug.save_decision(
                    # "UNSURE" rather than the raw "?" placeholder: it is what
                    # ends up in the folder name, and "?" slugs to nothing.
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
                    # Bank label behind the match, prefix included, and which
                    # bank produced it: "enroll" (uploads), "extended"
                    # (auto-captured views), "stranger", or None when nothing
                    # cleared a threshold.
                    matched_label=matched_label,
                    match_source=match_source,
                    # Per-bank similarities, so a wrong identity can be traced
                    # to the exact set that carried it.
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

        # Auto-extend enrollment: crop each confidently-matched view and fold it
        # into its user's extended set. _maybe_extend_user manages its own
        # locking and keeps disk I/O OFF the lock, so we deliberately do NOT wrap
        # this in `with self._lock` (that previously held the lock across every
        # frame's JPEG write and widened the reload race).
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

        The bank files are only ever written when a brand-new stranger is minted
        (see ``_save_strangers_state``), so on a device where every face it ever
        sees is enrolled they are never created at all — and this runs on the
        per-frame path inside ``detect()``. Treating "not created yet" as an
        error therefore logged a full traceback at ERROR every couple of
        seconds, from first boot, forever: journald filled up and real errors
        got buried, on precisely the devices that were working correctly.

        Absent is not corrupt. An empty bank is the right state, the caller's
        fields already hold it, and the assignment below was already skipped in
        that case — so returning early changes nothing except the noise. A bank
        that EXISTS but cannot be read is still loud, which is the case worth
        shouting about. This mirrors
        ``speaker_recognizer._load_strangers``, which has always guarded this way;
        the face path was the odd one out.
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
