"""statless-pages: cookie-free page-view tracker for Notion / Substack / README."""

from importlib import metadata

__all__ = ["__version__"]

try:
    __version__ = metadata.version("statless-pages")
except metadata.PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0.dev0"
