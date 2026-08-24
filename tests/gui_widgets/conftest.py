# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Fixtures shared by the widget tests."""
from pathlib import Path

import pytest
from PySide6.QtCore import QTranslator


@pytest.fixture
def en_catalogue(qapp):
    """Installs ppm_en.qm for the duration of one test.

    Without a catalog installed, a `%n` message falls back on its neutral
    source ("Export 1 photo(s)"): that is a test artefact, never what the user
    sees -- `main()` always installs a catalog at startup, and
    ppm_en.qm exists only to carry the two real plural forms
    (cf. src/core/i18n.py and tools/update_translations.py)."""
    qm = Path(__file__).resolve().parents[2] / "translations" / "ppm_en.qm"
    tr = QTranslator()
    assert tr.load(str(qm)), f"{qm} absent — lancer tools/update_translations.py"
    qapp.installTranslator(tr)
    yield
    qapp.removeTranslator(tr)
