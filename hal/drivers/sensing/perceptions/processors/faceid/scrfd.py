"""SCRFD face detector (ONNX) — returns bbox + 5 keypoints + score per face."""

import os

import cv2
import numpy as np
import onnxruntime as ort


class _SCRFDDetector:
    def __init__(
        self,
        model_path: str,
        input_size=640,
        confidence_threshold: float = 0.35,
        nms_threshold: float = 0.4,
        fp16: bool = False,
        max_num: int = 0,
        metric: str = "default",
        session_options: ort.SessionOptions | None = None,
    ):
        sess_opts = session_options or ort.SessionOptions()
        self.session = ort.InferenceSession(model_path, sess_opts)
        self.input_name = self.session.get_inputs()[0].name
        self.output_names = [o.name for o in self.session.get_outputs()]

        self.name = os.path.basename(model_path).rsplit(".", 1)[0]
        if isinstance(input_size, int):
            self.input_size = (input_size, input_size)
        else:
            self.input_size = input_size

        self.conf_thresh = confidence_threshold
        self.nms_thresh = nms_threshold
        self.fp16 = fp16
        self.max_num = max_num
        self.metric = metric

        onnx_out_num = len(self.output_names)
        self.use_kps = onnx_out_num in (9, 15)
        self.fmc = 5 if onnx_out_num >= 10 else 3
        self.num_anchors = 1 if onnx_out_num in (10, 15) else 2
        self.feat_stride_fpn = (
            [8, 16, 32, 64, 128] if self.fmc == 5 else [8, 16, 32]
        )

    @staticmethod
    def distance2bbox(points, distance, max_shape=None):
        x1 = points[:, 0] - distance[:, 0]
        y1 = points[:, 1] - distance[:, 1]
        x2 = points[:, 0] + distance[:, 2]
        y2 = points[:, 1] + distance[:, 3]
        if max_shape is not None:
            x1 = np.clip(x1, 0, max_shape[1])
            y1 = np.clip(y1, 0, max_shape[0])
            x2 = np.clip(x2, 0, max_shape[1])
            y2 = np.clip(y2, 0, max_shape[0])
        return np.stack([x1, y1, x2, y2], axis=-1)

    @staticmethod
    def distance2kps(points, distance, max_shape=None):
        preds = []
        for i in range(0, distance.shape[1], 2):
            px = points[:, i % 2] + distance[:, i]
            py = points[:, i % 2 + 1] + distance[:, i + 1]
            if max_shape is not None:
                px = np.clip(px, 0, max_shape[1])
                py = np.clip(py, 0, max_shape[0])
            preds.append(px)
            preds.append(py)
        return np.stack(preds, axis=-1)

    @staticmethod
    def preprocess_image(image, input_size):
        h0, w0 = image.shape[:2]
        w_in, h_in = input_size
        scale = min(w_in / w0, h_in / h0)
        nw, nh = int(w0 * scale), int(h0 * scale)
        img_resized = cv2.resize(image, (nw, nh))
        pad_w = (w_in - nw) // 2
        pad_h = (h_in - nh) // 2
        det_img = np.full((h_in, w_in, 3), 128, dtype=np.uint8)
        det_img[pad_h:pad_h + nh, pad_w:pad_w + nw] = img_resized
        return det_img, scale, (pad_w, pad_h)

    def decode_and_filter(self, net_outs, thresh, input_shape):
        scores_list = []
        bboxes_list = []
        kps_list = []
        center_cache: dict = {}
        _, _, h_in, w_in = input_shape
        for idx, stride in enumerate(self.feat_stride_fpn):
            scores = net_outs[idx][0]
            boxes = net_outs[idx + self.fmc][0] * stride
            if self.use_kps:
                kps = net_outs[idx + self.fmc * 2][0] * stride
            fh, fw = h_in // stride, w_in // stride

            key = (fh, fw, stride)
            if key in center_cache:
                anchor_centers = center_cache[key]
            else:
                grid = np.stack(
                    np.mgrid[:fh, :fw][::-1], axis=-1
                ).astype(np.float32)
                pts = (grid * stride).reshape(-1, 2)
                if self.num_anchors > 1:
                    pts = np.stack([pts] * self.num_anchors, 1).reshape(-1, 2)
                if len(center_cache) < 100:
                    center_cache[key] = pts
                anchor_centers = pts

            idxs = np.where(scores >= thresh)[0]
            if idxs.size == 0:
                continue
            bboxes = self.distance2bbox(anchor_centers, boxes)
            scores_list.append(scores[idxs])
            bboxes_list.append(bboxes[idxs])

            if self.use_kps:
                kpss = self.distance2kps(anchor_centers, kps)
                kps_list.append(kpss[idxs])
        return scores_list, bboxes_list, kps_list

    @staticmethod
    def nms(dets, thresh):
        x1, y1, x2, y2, s = (
            dets[:, 0], dets[:, 1], dets[:, 2], dets[:, 3], dets[:, 4]
        )
        areas = (x2 - x1 + 1) * (y2 - y1 + 1)
        order = s.argsort()[::-1]
        keep = []
        while order.size > 0:
            i = order[0]
            keep.append(i)
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])
            w = np.maximum(0, xx2 - xx1 + 1)
            h = np.maximum(0, yy2 - yy1 + 1)
            inter = w * h
            ovr = inter / (areas[i] + areas[order[1:]] - inter)
            inds = np.where(ovr <= thresh)[0]
            order = order[inds + 1]
        return keep

    def postprocess(self, scores_list, bboxes_list, kps_list, det_scale, pad,
                    original_shape=None):
        if not scores_list:
            empty_kps = (
                np.zeros((0, 5, 2), dtype=np.float32) if self.use_kps else None
            )
            return np.zeros((0, 5), dtype=np.float32), empty_kps
        pad_w, pad_h = pad
        scores = np.vstack(scores_list).ravel()
        bboxes = np.vstack(bboxes_list)
        bboxes -= np.array([pad_w, pad_h, pad_w, pad_h])
        bboxes /= det_scale
        det = np.hstack((bboxes, scores[:, None])).astype(np.float32)

        if self.use_kps:
            kpss = np.vstack(kps_list).reshape((-1, 5, 2))
            kpss -= np.array([[pad_w, pad_h]])
            kpss /= det_scale
        else:
            kpss = None

        order = det[:, 4].argsort()[::-1]
        det = det[order]
        if kpss is not None:
            kpss = kpss[order]

        keep = self.nms(det, self.nms_thresh)
        det = det[keep]
        if kpss is not None:
            kpss = kpss[keep]

        if 0 < self.max_num < det.shape[0] and original_shape is not None:
            area = (det[:, 2] - det[:, 0]) * (det[:, 3] - det[:, 1])
            if self.metric == "max":
                vals = area
            else:
                cy, cx = original_shape[0] // 2, original_shape[1] // 2
                centers = np.stack(
                    [
                        (det[:, 0] + det[:, 2]) / 2 - cx,
                        (det[:, 1] + det[:, 3]) / 2 - cy,
                    ],
                    axis=1,
                )
                dist2 = np.sum(centers ** 2, axis=1)
                vals = area - dist2 * 2.0
            idxs = np.argsort(vals)[::-1][:self.max_num]
            det = det[idxs]
            if kpss is not None:
                kpss = kpss[idxs]
        return det, kpss

    def detect(self, image: np.ndarray):
        orig_h, orig_w = image.shape[:2]
        det_img, det_scale, pad = self.preprocess_image(image, self.input_size)
        blob = cv2.dnn.blobFromImage(
            det_img,
            scalefactor=1.0 / 128,
            size=self.input_size,
            mean=(127.5, 127.5, 127.5),
            swapRB=True,
        )
        if self.fp16:
            blob = blob.astype(np.float16)
        outs = self.session.run(self.output_names, {self.input_name: blob})
        scores_list, bboxes_list, kps_list = self.decode_and_filter(
            outs, self.conf_thresh, blob.shape
        )
        return self.postprocess(
            scores_list, bboxes_list, kps_list, det_scale, pad,
            original_shape=(orig_h, orig_w),
        )

    def infer(self, image: np.ndarray) -> list:
        det, kpss = self.detect(image)
        faces = []
        for i in range(det.shape[0]):
            faces.append(
                {
                    "bbox": det[i, :4].astype(np.float32),
                    "kps": kpss[i].astype(np.float32) if kpss is not None else None,
                    "det_score": np.float32(det[i, 4]),
                }
            )
        return faces
