"""Factory functions and factory classes for object detectors."""

from pathlib import Path

from core.enums.object import ObjectDetectorEnum
from core.perception.base import PredictorFactory
from core.perception.object.predictors.base import ObjectDetector


class ObjectDetectorFactory(PredictorFactory[ObjectDetector]):
    """Factory that creates ObjectDetector instances from config."""

    def __init__(
        self,
        model_name: ObjectDetectorEnum,
        use_onnx: bool = True,
        model_path: Path | None = None,
        remote_url: str | None = None,
        classes_path: Path | None = None,
        threshold: float | None = None,
        batch_size: int | None = None,
    ) -> None:
        self._model_name = model_name
        self._use_onnx = use_onnx
        self._model_path = model_path
        self._remote_url = remote_url
        self._classes_path = classes_path
        self._threshold = threshold
        self._batch_size = batch_size

    def create(self) -> ObjectDetector:
        return create_object_detector(
            self._model_name,
            use_onnx=self._use_onnx,
            model_path=self._model_path,
            remote_url=self._remote_url,
            classes_path=self._classes_path,
            threshold=self._threshold,
            batch_size=self._batch_size,
        )


def create_object_detector(
    model_name: ObjectDetectorEnum,
    use_onnx: bool = True,
    model_path: Path | None = None,
    remote_url: str | None = None,
    classes_path: Path | None = None,
    threshold: float | None = None,
    batch_size: int | None = None,
) -> ObjectDetector:
    """Instantiate an ONNX object detector, or a PyTorch/HF one when use_onnx=False."""

    if model_name == ObjectDetectorEnum.YOLO_WORLD:
        if use_onnx:
            from core.perception.object.predictors.onnx.yolo_world import (
                YOLOWorldONNXDetector as detector_cls,
            )
        else:
            from core.perception.object.predictors.torch.yolo_world import (
                YOLOWorldDetector as detector_cls,
            )
    elif model_name == ObjectDetectorEnum.OWLV2:
        if use_onnx:
            from core.perception.object.predictors.onnx.owlv2 import (
                OWLv2ONNXDetector as detector_cls,
            )
        else:
            from core.perception.object.predictors.torch.owlv2 import OWLv2Detector as detector_cls
    else:
        raise ValueError(f"Unknown object detector: {model_name}")

    return detector_cls(
        model_path=model_path, remote_url=remote_url, classes_path=classes_path,
        threshold=threshold, batch_size=batch_size,
    )
