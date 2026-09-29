"""Hermes native plugin entry point; installation is managed by os-server."""

from .router import Router
from .dependencies import preload_dependencies


def register(ctx):
    router = Router(dependencies=preload_dependencies)
    ctx.register_hook("pre_llm_call", router.before_turn)
