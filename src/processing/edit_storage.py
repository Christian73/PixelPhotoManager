import json
import logging
from pathlib import Path

from src.core.models import EditInfo

logger = logging.getLogger(__name__)


class EditStorage:
    EDITS_DIR = ".pm_edits"

    @staticmethod
    def get_edit_path(photo_path: str) -> Path:
        p = Path(photo_path)
        return p.parent / EditStorage.EDITS_DIR / (p.name + ".json")

    @staticmethod
    def load(photo_path: str) -> EditInfo:
        edit_path = EditStorage.get_edit_path(photo_path)
        if not edit_path.exists():
            return EditInfo()
        try:
            data = json.loads(edit_path.read_text(encoding="utf-8"))
            return EditInfo.from_dict(data)
        except Exception as e:
            logger.error(f"Erreur lecture retouches {edit_path}: {e}")
            return EditInfo()

    @staticmethod
    def save(photo_path: str, edit: EditInfo) -> None:
        edit_path = EditStorage.get_edit_path(photo_path)
        if not edit.is_modified():
            EditStorage.delete(photo_path)
            return
        try:
            edit_path.parent.mkdir(parents=True, exist_ok=True)
            edit_path.write_text(
                json.dumps(edit.to_dict(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"Erreur sauvegarde retouches {edit_path}: {e}")

    @staticmethod
    def has_edits(photo_path: str) -> bool:
        return EditStorage.get_edit_path(photo_path).exists()

    @staticmethod
    def delete(photo_path: str) -> None:
        edit_path = EditStorage.get_edit_path(photo_path)
        try:
            if edit_path.exists():
                edit_path.unlink()
        except Exception as e:
            logger.error(f"Erreur suppression retouches {edit_path}: {e}")
