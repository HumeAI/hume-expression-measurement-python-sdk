# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

"""
Helpers that turn audio into frames for the realtime voice endpoint, which accepts only signed
16-bit little-endian PCM, mono, 16 kHz.

Requires the audio extra: pip install "hume-expression-measurement[audio]"
"""

from ._optional import MissingDependencyError, import_optional

# Every helper module imports numpy at load time, so check once here to raise the error that
# names the install command.
import_optional("numpy")

from .microphone import Microphone
from .resampler import Resampler, SampleFormat
from .wav import iter_wav_frames

__all__ = ["Microphone", "MissingDependencyError", "Resampler", "SampleFormat", "iter_wav_frames"]
