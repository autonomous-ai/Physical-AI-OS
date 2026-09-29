"""Motion drivers."""

__all__ = ["MotorsService"]


def __getattr__(name):
    if name == "MotorsService":
        from .motors_service import MotorsService

        return MotorsService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
