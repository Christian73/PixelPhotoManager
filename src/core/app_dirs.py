# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
import os
from pathlib import Path

APP_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PixelPhotoManager"
