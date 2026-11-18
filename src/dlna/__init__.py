"""Browse DLNA/UPnP media servers and hand their stream URLs to ordinary players."""

__version__ = "0.1.0"


class DlnaError(Exception):
    """A user-facing error: printed without a traceback by the CLI."""
