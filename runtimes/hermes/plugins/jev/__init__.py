"""Hermes native plugin entry point; installation is managed by os-server."""

from .router import Router


def register(ctx):
    router = Router()
    ctx.register_hook("pre_llm_call", router.before_turn)
