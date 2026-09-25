"""Expected failures: printed as one line, never as a traceback."""


class KbxError(Exception):
    """A failure the user can act on. `code` becomes the exit status."""

    def __init__(self, message: str, code: int = 1) -> None:
        super().__init__(message)
        self.code = code
