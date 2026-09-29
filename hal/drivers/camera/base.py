import logging
from abc import ABC
from collections.abc import Callable
from typing import Any, Self, override

from .models import IDeviceInfo, IDeviceResponse

logger = logging.getLogger(__name__)


class IDevice[DEVICE_INFO_T: IDeviceInfo, DEVICE_RESPONSE_T: IDeviceResponse](ABC):
    runable: bool = False

    def __init__(self, device_info: DEVICE_INFO_T, name: str | None = None):
        self.device_info: DEVICE_INFO_T = device_info
        self.device_name: str | None = name or device_info.device_name

        super().__init__()

        self.callbacks: set[Callable[[DEVICE_INFO_T, DEVICE_RESPONSE_T], None]] = set()
        self.running: bool = not self.runable

    def ready(self) -> bool:
        return self.running if self.runable else True

    @override
    def __hash__(self) -> int:
        return hash(self.device_info.device_id)

    @override
    def __eq__(self, other: Any) -> bool:
        if isinstance(other, IDevice):
            return str(self.device_info.device_id) == str(other.device_info.device_id)

        return False

    def register_callback(
        self, callback: Callable[[DEVICE_INFO_T, DEVICE_RESPONSE_T], None]
    ):
        self.callbacks.add(callback)

    def unregister_callback(
        self, callback: Callable[[DEVICE_INFO_T, DEVICE_RESPONSE_T], None]
    ):
        try:
            self.callbacks.remove(callback)
        except ValueError:
            logger.error(f"Callback not found: {callback}")

    def state(self) -> dict[str, Any]:
        return {"active": self.running, "device_info": self.device_info.model_dump()}

    def start(self) -> None:
        """Start the device driver or initialize the service (sync mode)"""
        raise NotImplementedError("start method is not implemented")

    def stop(self) -> None:
        """Stop the device or terminate the service"""
        if self.runable:
            self.running = False

    @classmethod
    def open(
        cls, device_info: DEVICE_INFO_T, name: str | None = None, **kwargs: Any
    ) -> Self:
        device = cls(device_info, name, **kwargs)

        logger.info(f"Opening device {cls.__name__} with device_info={device_info}")
        if device.runable:
            device.start()

        return device
