class Omit:
    """Falsy sentinel for "field not provided" in partial updates (``None`` can be meaningful)."""

    def __bool__(self):
        return False


omit = Omit()
