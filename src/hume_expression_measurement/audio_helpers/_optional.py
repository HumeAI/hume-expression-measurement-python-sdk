# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

import importlib
import types

INSTALL_COMMAND = 'pip install "hume-expression-measurement[audio]"'


class MissingDependencyError(ImportError):
    """
    Raised when a library the audio helpers need is not installed.

    The message says how to install what is missing.
    """


def missing_dependency(name: str) -> MissingDependencyError:
    return MissingDependencyError(
        f"The `{name}` package is required by hume_expression_measurement.audio_helpers. "
        f"Install the audio extra: {INSTALL_COMMAND}",
        name=name,
    )


def import_optional(name: str) -> types.ModuleType:
    """
    Imports an optional library by name.

    soxr and sounddevice ship no type information, so mypy rejects an import statement for either.
    Callers cast the returned module to a Protocol that lists the calls they make.
    """
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as err:
        if err.name != name:
            raise
        raise missing_dependency(name) from err
