import os
from pathlib import Path

APP_DATA_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "PixelPhotoManager"
