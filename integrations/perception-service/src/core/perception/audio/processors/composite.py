"""Composite audio processor — chains multiple processors sequentially."""

from typing_extensions import override

from core.models.media import Audio

from .base import AudioProcessorBase


class CompositeAudioProcessor(AudioProcessorBase):
    """Chains multiple AudioProcessorBase instances sequentially (nestable)."""

    def __init__(self, processors: list[AudioProcessorBase]) -> None:
        super().__init__()
        self._processors: list[AudioProcessorBase] = processors

    @override
    def _start_impl(self) -> None:
        for processor in self._processors:
            processor.start()
        self._running = True
        self._logger.info(
            "Composite processor started with %d processors", len(self._processors)
        )

    @override
    def _stop_impl(self) -> None:
        for processor in self._processors:
            processor.stop()
        self._running = False
        self._logger.info("Composite processor stopped")

    @override
    def _is_ready_impl(self) -> bool:
        return self._running and all(p.is_ready() for p in self._processors)

    @override
    def _process_impl(self, input: Audio) -> Audio:
        result: Audio = input
        for processor in self._processors:
            result = processor.process(result)
        return result
