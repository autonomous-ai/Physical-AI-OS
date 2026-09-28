# Perception Subsystems

All perceptions live under `src/core/perception/<name>` and follow the same shape:

- a **predictor** (`predictors/`) wraps the ML model (ONNX / PyTorch / HF) and
  implements `PredictorBase[INPUT, OUTPUT]` (`src/core/perception/base/`),
- an **InputBatcher** (`base/batching.py`) queues requests from concurrent sessions
  and dispatches them to the predictor in batches for GPU efficiency,
- a **perception session** manages per-connection state, rate limiting, and submits
  inputs to the batcher,
- a **factory** picks the concrete predictor from an **enum** (`src/core/enums/`),
- result **data models** live in `src/core/models/`.

Batch size and timeout are configurable per model via `<NAME>__BATCH_SIZE` and
`<NAME>__BATCH_TIMEOUT` — see [configuration.md](configuration.md#batching).

The configured default for each subsystem comes from `src/config.py`
(`<NAME>__MODEL`, `<NAME>__ENABLED`), summarized in
[configuration.md](configuration.md).

| # | Subsystem | Default model | Enabled by default | Routes |
|---|-----------|---------------|--------------------|--------|
| 1 | Action recognition | UniformerV2 | yes | WS action-analysis |
| 2 | Facial emotion (FER) | POSTER V2 | yes | WS + HTTP emotion |
| 3 | Pose estimation | RTMPose + TCPFormer | yes | WS pose |
| 4 | Speech emotion (SER) | emotion2vec | yes | HTTP ser |
| 5 | Object detection | per-detector | no (opt-in) | WS + HTTP object |
| 6 | Audio embedder | WeSpeaker ResNet293 | no | HTTP embed |
| 7 | Face detection | YuNet | (internal) | feeds FER/pose |
| 8 | Person detection | YOLO | no (internal) | feeds action |

---

## 1. Action recognition

Classifies human actions from a rolling clip of frames into Kinetics classes.

- Enum `HumanActionRecognizerEnum` (`enums/action.py`): `x3d`, `uniformerv2`, `videomae`
- Predictors (`perception/action/predictors/`):

  | Model | File | Architecture | Weights | Input | Frames | Classes |
  |-------|------|-------------|---------|-------|--------|---------|
  | **UniformerV2** (default) | `uniformerv2.py` | UniformerV2-L ONNX | `uniformerv2-l-224-k400_fp32.onnx` | 224×224 | 8 | Kinetics |
  | X3D | `x3d.py` | X3D-M ONNX (INT8) | `x3d_m_16x5x1_int8.onnx` | 256×256 | 16 | Kinetics |
  | VideoMAE | `videomae.py` | VideoMAE ONNX | `videomae_fp32.onnx` | 224×224 | 16 | Kinetics |

- Output: `HumanActionDetection` → `actions: list[HumanAction{class_name, conf}]`.
- A per-request `whitelist` filters which classes are scored; optional YOLO person
  detection crops the largest person first (helps under camera ego-motion).

## 2. Facial emotion (FER)

Classifies emotion from a face crop.

- Enum `EmotionRecognizerEnum` (`enums/facial_emotion.py`): `posterv2`, `emonet_8`, `emonet_5`, `emoaffectnet`
- Predictors (`perception/facial_emotion/predictors/`):

  | Model | File | Architecture | Weights | Input | Output |
  |-------|------|-------------|---------|-------|--------|
  | **POSTER V2** (default) | `posterv2.py` | POSTER V2 ONNX | `posterv2_7cls.onnx` | 224×224 | 7 RAF-DB emotions |
  | EmoNet-8 | `emonet.py` | EmoNet ONNX | `emonet_8.onnx` | 256×256 | 8 emotions + valence + arousal |
  | EmoNet-5 | `emonet.py` | EmoNet ONNX | `emonet_5.onnx` | 256×256 | 5 emotions + valence + arousal |
  | Emo-AffectNet | `emoaffectnet.py` | ResNet-50 ONNX | `emoaffectnet_resnet50.onnx` | 224×224 | 7 AffectNet emotions |

- The winning label is gated by a per-label confidence threshold
  (`label_gating.py`); below its bar → `Neutral`. See
  [configuration.md](configuration.md) (`FER__LABEL_THRESHOLDS`). This gate
  only runs for requests without `raw: true`; lamps running current HAL send
  `raw: true` and gate on the device instead.

- Output: `EmotionDetection` → `emotions: list[Emotion{emotion, confidence,
  face_confidence, bbox, valence?, arousal?}]`.
- The WS endpoint detects faces with **YuNet** (subsystem 7) before classifying;
  the HTTP endpoint expects an already-cropped face.

## 3. Pose estimation

A 3-stage pipeline: 2D keypoints → optional 3D lift → optional RULA ergonomics.

- Enums (`enums/pose.py`): `GraphEnum{coco, h36m}`,
  `PoseEstimator2DEnum{rtmpose}`, `PoseLifter3DEnum{tcpformer}`, `ErgoAssessorEnum{rula}`

| Stage | Model | File | Weights | Notes |
|-------|-------|------|---------|-------|
| 2D | **RTMPose-M** | `pose/predictors/pose2d/rtmpose.py` | `rtmpose-m.onnx` | 192×256 input, COCO-17, SimCC x/y decode |
| 3D lift | **TCPFormer** | `pose/predictors/pose3d/tcpformer.py` | `tcpformer_h36m_243.onnx` | H36M-17, 243-frame temporal window |
| Ergonomics | **RULA** | `pose/predictors/ergo/rula/assessor.py` | (rule-based) | Rapid Upper Limb Assessment |

- 2D output `Pose2D{graph_type, joints:[Point2D], confs}`; 3D output
  `Pose3D{graph_type, joints:[Point3D], confs}`; combined `PoseDetection{pose_2d,
  pose_3d?, ergo?}`.
- **RULA** scores upper-limb and trunk/neck/leg posture from the 3D skeleton and
  maps to a `RiskLevel` (`models/pose.py`): `NEGLIGIBLE(1)`, `LOW(2)`, `MEDIUM(3)`,
  `HIGH(4)` — the standard ergonomics escalation for "how urgently should this
  posture change". The 3D lift and ergo stages only run when enabled in config.

## 4. Speech emotion (SER)

Classifies emotion from a speech waveform (independent of any transcript).

- Enum `SpeechEmotionRecognizerEnum` (`enums/speech_emotion_recognizer.py`): `emotion2vec`
- Predictor `perception/audio_emotion/predictors/emotion2vec.py` — **emotion2vec+
  large**, ONNX (`emotion2vec.onnx`), mono 16 kHz waveform, 9 classes (angry, disgusted, fearful, happy,
  neutral, other, sad, surprised, `<unk>`) + softmax.
- Output `AudioEmotionDetection` → `emotions: list[AudioEmotion{emotion, confidence}]`.
- The audio processor runs, in order: mono, resample, high-pass, denoise, VAD-gate,
  RMS-normalize before inference. Each step has its own toggle under
  `AUDIO_EMBEDDER__PROCESSOR__*` (e.g. `AUDIO_EMBEDDER__PROCESSOR__ENABLE_HIGH_PASS`).
  Defaults: **high-pass and denoise are OFF** (they reshape the spectrum in ways CMN
  cannot cancel, diverging from the WeSpeaker reference); mono, resample, VAD and
  RMS-normalize are ON.
- Input is bounded to **2–8 s** (`MIN_AUDIO_S` / `MAX_AUDIO_S` in `predictors/base.py`,
  applied by `length.fit_length`), and inference runs at **batch 1**. Every request
  therefore reaches TensorRT as `audio: 1 × 32000…128000`, inside the cached engine's
  shape range (prod: batch 1–4 × 32000–549120), so no request can trigger an
  in-request engine rebuild (#492). Warmup runs at both ends (2 s and 8 s), so on a
  host without a cached engine the build happens at startup, before traffic.

## 5. Object detection

Open-vocabulary / zero-shot detection. Each detector is independently enabled and
selected by URL path segment (`{detector_name}`).

- Enum `ObjectDetectorEnum` (`enums/object.py`): `yoloworld`, `owlv2`
- Predictors are split into two backend directories:
  - `predictors/torch/` — PyTorch / HuggingFace Transformers backends
  - `predictors/onnx/` — ONNX Runtime backends (default, controlled by `USE_ONNX`)

  | Detector | Enum value | Torch file | ONNX file | Default weights (ONNX) | GPU |
  |----------|-----------|------------|-----------|------------------------|-----|
  | YOLO-World | `yoloworld` | `torch/yolo_world.py` | `onnx/yolo_world.py` | auto-download from CDN | CUDA / CPU |
  | OWLv2 | `owlv2` | `torch/owlv2.py` | `onnx/owlv2.py` | auto-download from CDN | CUDA / CPU |

  ONNX predictors have `DEFAULT_MODEL_PATH` and `DEFAULT_REMOTE_URL` resolved from
  `ModelEnum` entries — models are auto-downloaded on first use via `ensure_downloaded`.

- Output `ObjectDetection` → items `{class_name, xywh:[cx,cy,w,h], confidence}`.
  All object detectors return **normalized [0,1] bbox_xywh**; the session/perception
  layer rescales coordinates to pixel dimensions before returning to the client.
- `classes` is supplied per request (the open-vocabulary prompt). All detectors are
  **disabled by default** — enable the ones you need via `OBJECT_DETECTOR__<NAME>__ENABLED`.

## 6. Audio embedder (speaker)

Produces a speaker embedding for verification/identification (matching is done by
the caller).

- Enum `AudioEmbedderEnum` (`enums/audio.py`): `resnet34`, `resnet293`, `ecapa-tdnn1024`, `campplus`
- Predictors (`perception/audio/predictors/`):

  | Model | File | Weights | Embedding dim |
  |-------|------|---------|---------------|
  | WeSpeaker ResNet34 | `resnet34.py` | `wespeaker_resnet34.onnx` | 256 |
  | **WeSpeaker ResNet293** (default) | `resnet293.py` | `voxceleb_resnet293_LM.onnx` | 256 |
  | WeSpeaker ECAPA-TDNN-1024 | `ecapa_tdnn.py` | `wespeaker_ecapa_tdnn1024.onnx` | 192 |
  | WeSpeaker CAM++ | `campplus.py` | `wespeaker_campplus.onnx` | 512 |

- All models: 16 kHz mono input → 80-bin fbank → chunking → L2-normalized embedding.
  Net speech ≤ 10 s is embedded as one whole-utterance chunk (like WeSpeaker's
  extract_embedding); longer speech is split into 6 s chunks with 4 s hop for
  per-chunk voting.
- Output `RawAudioEmbedding{embedding, chunk_embeddings}`. Disabled by default.

## 7. Face detection (internal)

- Enum `FaceDetectorEnum` (`enums/face.py`): `yunet`
- Predictor `perception/face/predictors/yunet.py` — OpenCV `FaceDetectorYN`,
  320×320, score ≥ 0.7, NMS 0.3. Returns `RawFaceDetection{bbox_xyxy, confidence,
  area}` and can `extract_crops()`. Used by the facial-emotion WS pipeline.

## 8. Person detection (internal)

- Enum `PersonDetectorEnum` (`enums/person.py`): `yolo`
- Predictors in `perception/person/predictors/` (torch and ONNX backends available).
  Ultralytics YOLO (`yolo12x.pt`) filtered to COCO class 0, confidence 0.4, bbox
  expanded 2.0x. Person detectors return **normalized [0,1] bbox_xyxy**.
  `extract_largest_crop()` feeds the action recognizer. Disabled by default
  (`PERSON_DETECTOR__ENABLED=false`).

---

## Model weights reference

All ONNX/PyTorch weights are auto-downloaded from
`https://storage.googleapis.com/autonomous-models/` into `~/.cache/perception-service/models/`
on first use. Override with `<NAME>__CKPT_PATH` (local) or `<NAME>__REMOTE_URL` (alternate CDN/HF).

| Model | Weights file | Format | CDN path |
|-------|-------------|--------|----------|
| UniformerV2-L | `uniformerv2-l-224-k400_fp32.onnx` | ONNX FP32 | `onnx_models/` |
| X3D-M | `x3d_m_16x5x1_int8.onnx` | ONNX INT8 | `onnx_models/` |
| VideoMAE | `videomae_fp32.onnx` | ONNX FP32 | `onnx_models/` |
| POSTER V2 | `posterv2_7cls.onnx` | ONNX | `onnx_models/` |
| EmoNet-8 | `emonet_8.onnx` | ONNX | `onnx_models/` |
| EmoNet-5 | `emonet_5.onnx` | ONNX | `onnx_models/` |
| Emo-AffectNet | `emoaffectnet_resnet50.onnx` | ONNX | `onnx_models/` |
| RTMPose-M | `rtmpose-m.onnx` | ONNX | `onnx_models/` |
| TCPFormer | `tcpformer_h36m_243.onnx` | ONNX | `onnx_models/` |
| emotion2vec+ | `emotion2vec.onnx` | ONNX | `onnx_models/` |
| ECAPA-TDNN-1024 | `wespeaker_ecapa_tdnn1024.onnx` | ONNX | `onnx_models/` |
| ResNet34 | `wespeaker_resnet34.onnx` | ONNX | `onnx_models/` |
| ResNet293 | `voxceleb_resnet293_LM.onnx` | ONNX | `onnx_models/` |
| CAM++ | `wespeaker_campplus.onnx` | ONNX | `onnx_models/` |
| YuNet | `face_detection_yunet_2023mar.onnx` | ONNX | `onnx_models/` |
| YOLO person (ONNX) | `yolo12x_raw.onnx` / `yolo12x.onnx` | ONNX | `onnx_models/` |
| YOLO person (PyTorch) | `yolo12x.pt` | PyTorch | `pytorch_models/` |
| YOLO-World v2 (ONNX) | `yolov8x-worldv2_raw.onnx` / `yolov8x-worldv2.onnx` | ONNX | `onnx_models/` |
| YOLO-World v2 (PyTorch) | `yolov8x-worldv2.pt` | PyTorch | `pytorch_models/` |
| OWLv2 (ONNX) | `owlv2_raw.onnx` / `owlv2.onnx` | ONNX | `onnx_models/` |
| OWLv2 (HuggingFace) | `google/owlv2-large-patch14-ensemble` | HuggingFace | (HF Hub) |

---

## ONNX export scripts

Export PyTorch/HuggingFace models to ONNX for perception-service inference. Scripts live in
`src/core/export/entries/`. After `pip install -e .` each is available as a CLI command.

```bash
export-all              # run all exports (skips missing checkpoints)
                        #   --output-dir <dir>  write all ONNX files into <dir>
export-uniformerv2      # UniformerV2 action recognition
export-posterv2         # POSTER V2 facial emotion
export-emonet           # EmoNet facial emotion (5 or 8 class)
export-emoaffectnet     # Emo-AffectNet facial emotion (ResNet-50, downloads from HF)
export-emotion2vec      # emotion2vec+ speech emotion (downloads from HF)
export-tcpformer        # TCPFormer 3D pose lifter
export-owlv2            # OWLv2 zero-shot object detection (downloads from HF)
export-yolo             # YOLO person detection
export-yolo-world       # YOLO-World zero-shot object detection
```

Detection models (OWLv2, YOLO, YOLO-World) default to `nms=False` (raw export).
`export-all` exports **both** raw and NMS-baked variants for each detection model.
Individual scripts accept `--nms` / `--no-nms` flags (default: `--no-nms`).

### Model I/O specifications

All models export with ONNX opset 17. `B` = batch, `T` = variable time, `K` = num classes, `L` = token length.

| Model | Input name(s) | Input shape | Preprocessing | Output name(s) | Output shape |
|-------|--------------|-------------|---------------|----------------|--------------|
| **UniformerV2** | `videos` | `[B,1,3,8,224,224]` | Normalize: mean=0.45, std=0.225 | `probs` | `[B, num_classes]` softmax |
| **POSTER V2** | `images` | `[B,3,224,224]` | ImageNet norm: mean=[0.485,0.456,0.406], std=[0.229,0.224,0.225] | `probs` | `[B, 7]` softmax |
| **EmoNet** | `images` | `[B,3,256,256]` | Range [0,1], no normalization | `probs`, `valence`, `arousal` | `[B,N]` softmax, `[B]`, `[B]` |
| **Emo-AffectNet** | `images` | `[B,3,224,224]` | RGB→BGR flip, ×255, VGGFace2 mean subtract — all baked into ONNX (caller passes [0,1] RGB) | `probs` | `[B, 7]` softmax |
| **emotion2vec+** | `audio` | `[B, T]` (16kHz mono) | Optional mean/var normalize | `probs` | `[B, 9]` softmax |
| **TCPFormer** | `keypoints` | `[B,243,17,3]` | Raw 2D skeleton (x,y,conf) | `poses` | `[B,243,17,3]` 3D coords |
| **OWLv2** | `images`, `class_tokens` | `[B,3,H,W]`, `[K,16]` int64 | OWLv2Processor | `boxes`, `probs`, `labels` | `[B,N,4]` xywh, `[B,N,K]`, `[B,N]` |
| **YOLO** | `images` | `[B,3,640,640]` | Raw tensor | `boxes`, `probs`, `labels` | `[B,N,4]` xywh, `[B,N]`, `[B,N]` |
| **YOLO-World** | `images`, `class_tokens` | `[B,3,640,640]`, `[K,L]` int64 | CLIP tokenize + L2 norm | `boxes`, `probs`, `labels` | `[B,N,4]` xywh, `[B,N,K]`, `[B,N]` |

**Image input convention:** All image inputs expect float32 in [0, 1] range (rescale from uint8 [0, 255] before inference). Models that require further normalization (ImageNet mean/std, custom mean/std) bake it into the ONNX graph — the caller only needs to rescale to [0, 1].

**Models with external preprocessors:** OWLv2 uses its HuggingFace processor (`Owlv2Processor`) for image preprocessing and text tokenization — do NOT manually rescale, the processor handles it. UniformerV2 bakes normalization into the ONNX wrapper. YOLO models accept raw [0, 1] tensors resized to 640×640.

**Detection outputs:** `boxes` are normalized [0,1] xywh. `labels` use -1 for padding. Export scripts default to `nms=False` (raw); the NMS variant bakes NMS into the graph. The ONNX runtime predictors apply NMS in postprocessing by default.

**Text encoders:** OWLv2 embeds the full text encoder (CLIP) in the ONNX; YOLO-World uses CLIP ViT-B/32.

`ModelEnum` entries (`enums/files.py`) use suffixes to distinguish formats:
`_ONNX` (raw ONNX), `_NMS_ONNX` (ONNX with NMS baked in), `_PTH` (PyTorch
checkpoint). For example: `YOLO_WORLD_ONNX`, `YOLO_WORLD_NMS_ONNX`,
`YOLO_WORLD_PTH`.

### Script → checkpoint mapping

| Script | Source | Pretrained checkpoint | ONNX output |
|--------|--------|-----------------------|-------------|
| `export-uniformerv2` | PyTorch `.pth` | `models/pretrained/<config>.pth` | `models/onnx/uniformerv2-*_fp32.onnx` |
| `export-posterv2` | PyTorch `.pth` | `models/pretrained/posterv2_7cls.pth` | `models/onnx/posterv2_7cls.onnx` |
| `export-emonet` | PyTorch `.pth` | `models/pretrained/emonet_{5,8}.pth` | `models/onnx/emonet_{5,8}.onnx` |
| `export-emoaffectnet` | HuggingFace | `ElenaRyumina/face_emotion_recognition` (`FER_static_ResNet50_AffectNet.pt`) | `models/onnx/emoaffectnet_resnet50.onnx` |
| `export-emotion2vec` | HuggingFace | `iic/emotion2vec_plus_large` | `models/onnx/emotion2vec.onnx` |
| `export-tcpformer` | PyTorch `.pth.tr` | `models/pretrained/TCPFormer_h36m_243_379.pth.tr` | `models/onnx/tcpformer_h36m_243.onnx` |
| `export-owlv2` | HuggingFace | `google/owlv2-large-patch14-ensemble` | `models/onnx/owlv2_raw.onnx` (raw) / `owlv2.onnx` (NMS) |
| `export-yolo` | Ultralytics `.pt` | `yolo12x.pt` | `models/onnx/yolo12x_raw.onnx` (raw) / `yolo12x.onnx` (NMS) |
| `export-yolo-world` | Ultralytics `.pt` | `yolov8x-worldv2.pt` | `models/onnx/yolov8x-worldv2_raw.onnx` (raw) / `yolov8x-worldv2.onnx` (NMS) |

Components (model definitions, preprocessing) are in `src/core/export/components/`.
Utilities (constants, evaluation, NMS, preprocessing) are in `src/core/export/utils/`.
</content>
