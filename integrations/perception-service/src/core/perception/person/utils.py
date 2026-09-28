from pathlib import Path

from core.enums.person import PersonDetectorEnum
from core.perception.base import PredictorFactory
from core.perception.person.predictors.base import PersonDetector


class PersonDetectorFactory(PredictorFactory[PersonDetector]):
    """Factory that creates PersonDetector instances from config."""

    def __init__(
        self,
        model_name: PersonDetectorEnum,
        model_path: str | None = None,
        threshold: float | None = None,
        bbox_expand_scale: float | None = None,
        batch_size: int | None = None,
    ) -> None:
        self._model_name = model_name
        self._model_path = model_path
        self._threshold = threshold
        self._bbox_expand_scale = bbox_expand_scale
        self._batch_size = batch_size

    def create(self) -> PersonDetector:
        return create_person_detector(
            self._model_name, self._model_path,
            threshold=self._threshold, bbox_expand_scale=self._bbox_expand_scale,
            batch_size=self._batch_size,
        )


def create_person_detector(
    model_name: PersonDetectorEnum,
    model_path: str | None = None,
    threshold: float | None = None,
    bbox_expand_scale: float | None = None,
    batch_size: int | None = None,
) -> PersonDetector:
    """Instantiate an ONNX person detector if an ONNX path is given, else ultralytics."""
    use_onnx = model_path is not None and str(model_path).endswith(".onnx")

    if model_name == PersonDetectorEnum.YOLO:
        if use_onnx:
            from core.perception.person.predictors.yolo_onnx import YOLOONNXPersonDetector

            return YOLOONNXPersonDetector(
                model_path=Path(model_path),
                threshold=threshold,
                bbox_expand_scale=bbox_expand_scale,
                batch_size=batch_size,
            )
        from core.perception.person.predictors.yolo import YOLOPersonDetector

        return YOLOPersonDetector(
            model_path, threshold=threshold,
            bbox_expand_scale=bbox_expand_scale, batch_size=batch_size,
        )

    msg = f"Unknown person detector model: {model_name}"
    raise ValueError(msg)
