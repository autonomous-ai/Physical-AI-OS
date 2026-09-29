import logging
import secrets
from abc import ABC, abstractmethod
from typing import Any, Generic, TypeVar

from core.types import Omit

INPUT_T = TypeVar("INPUT_T")
OUTPUT_T = TypeVar("OUTPUT_T")
CONFIG_T = TypeVar("CONFIG_T")


class PerceptionSessionBase(Generic[INPUT_T, OUTPUT_T, CONFIG_T], ABC):
    def __init__(self, config: CONFIG_T) -> None:
        self._logger: logging.Logger = logging.getLogger(
            f"{self.__class__.__module__}.{self.__class__.__name__}"
        )

        self._config: CONFIG_T = config
        self._last_update_ts: float = -1
        self._last_prediction: OUTPUT_T | None = None

        self._session_id: str = secrets.token_hex(8)

    @property
    def session_id(self) -> str:
        return self._session_id

    @abstractmethod
    async def start(self) -> None:
        pass

    @abstractmethod
    async def stop(self) -> None:
        pass

    @abstractmethod
    def is_ready(self) -> bool:
        pass

    def _post_config_update(self) -> None:
        pass

    def set_config(self, config: CONFIG_T) -> None:
        self._config = config
        self._post_config_update()

    def update_config(self, **kwargs: Any) -> None:
        """Update config fields; only non-Omit values are applied."""
        for k, v in kwargs.items():
            if not isinstance(v, Omit):
                setattr(self._config, k, v)
        self._post_config_update()

    @abstractmethod
    async def update(self, input: INPUT_T) -> OUTPUT_T | None:
        pass

    @property
    def last_prediction(self) -> OUTPUT_T | None:
        return self._last_prediction
