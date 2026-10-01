import importlib
import sys

import pytest

from hume_expression_measurement.audio_helpers import MissingDependencyError


def test_importing_without_numpy_names_the_install_command(monkeypatch: pytest.MonkeyPatch) -> None:
    # _optional stays loaded so the raised error is the MissingDependencyError imported above.
    for name in list(sys.modules):
        if (
            name.startswith("hume_expression_measurement.audio_helpers")
            and name != "hume_expression_measurement.audio_helpers._optional"
        ):
            monkeypatch.delitem(sys.modules, name)
    monkeypatch.setitem(sys.modules, "numpy", None)

    with pytest.raises(MissingDependencyError, match=r'pip install "hume-expression-measurement\[audio\]"'):
        importlib.import_module("hume_expression_measurement.audio_helpers")
