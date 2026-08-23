# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""A global safety net: redirects %LOCALAPPDATA% towards a temporary session
folder BEFORE any test collection/import.

`src/core/app_dirs.py::APP_DATA_DIR` is a module constant computed only
once at the first import -- fixing it here, at the module level of this
conftest.py (loaded by pytest before any tests/**/*.py file), guarantees
that no test (Layer 2 in particular: `EditPanel.__init__` instantiates
`EditDatabase()` with its default path, with no injection point) can
accidentally read/write inside the user's real
%LOCALAPPDATA%\\PixelPhotoManager.

That mutation only affects the environment variable of the *pytest process
currently running* (and of its possible subprocesses) -- never the real
persistent profile of the user.

The Layer 1 tests (DB/logic) keep using `db_path=tmp_path/...`
in the constructor, which ignores that variable -- this safety net is an
extra protection, not the main isolation mechanism for those tests.
"""
import atexit
import os
import shutil
import tempfile

_SESSION_LOCALAPPDATA = tempfile.mkdtemp(prefix="ppm_pytest_localappdata_")
os.environ["LOCALAPPDATA"] = _SESSION_LOCALAPPDATA


def _cleanup() -> None:
    shutil.rmtree(_SESSION_LOCALAPPDATA, ignore_errors=True)


atexit.register(_cleanup)
