# Maintained by hand and listed in .fernignore, so SDK regeneration leaves it in place.

"""
Helpers that turn audio into frames for the realtime voice endpoint, which accepts only signed
16-bit little-endian PCM, mono, 16 kHz.

Requires the audio extra: pip install "hume-expression-measurement[audio]"
"""

from ._optional import MissingDependencyError
from .resampler import Resampler, SampleFormat
from .wav import iter_wav_frames

__all__ = ["MissingDependencyError", "Resampler", "SampleFormat", "iter_wav_frames"]
