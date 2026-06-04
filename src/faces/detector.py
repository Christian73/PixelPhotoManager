import contextlib
import logging
import os
import tempfile

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _exif_corrected(image_path: str):
    """
    Corrige la rotation EXIF en écrivant un fichier temporaire uniquement si
    nécessaire (orientation ≠ 1 / normale). DeepFace lit ensuite ce fichier via
    son chemin habituel — aucun changement de code path interne.
    """
    temp_path = None
    try:
        from PIL import Image, ImageOps
        with Image.open(image_path) as img:
            orientation = img.getexif().get(274, 1)   # 274 = Orientation tag
            if orientation in (None, 1):
                yield image_path
                return
            corrected = ImageOps.exif_transpose(img)
        suffix = os.path.splitext(image_path)[1] or ".jpg"
        fd, temp_path = tempfile.mkstemp(suffix=suffix)
        os.close(fd)
        corrected.save(temp_path, quality=95)
        yield temp_path
    except Exception:
        yield image_path   # en cas d'erreur, on passe le fichier original
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


def is_available() -> bool:
    """Return True if deepface is installed and usable."""
    try:
        import deepface  # noqa: F401
        return True
    except ImportError:
        return False


def detect_and_embed(image_path: str) -> list[dict]:
    """
    Detect faces in an image and compute ArcFace embeddings.

    Returns a list of dicts:
        {'bbox': (x, y, w, h), 'embedding': list[float]}

    Raises RuntimeError if deepface is not installed.
    Raises FileNotFoundError if the image does not exist.
    Returns [] if no face is found.
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(image_path)

    try:
        from deepface import DeepFace
    except ImportError:
        raise RuntimeError(
            "La reconnaissance faciale nécessite deepface. "
            "Installez-le avec : pip install deepface"
        )

    try:
        os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

        with _exif_corrected(image_path) as path:
            results = DeepFace.represent(
                img_path=path,
                model_name="ArcFace",
                detector_backend="retinaface",
                enforce_detection=False,
                align=True,
            )
    except Exception as e:
        logger.warning("DeepFace.represent() a échoué pour %s : %s", image_path, e)
        return []

    if not results:
        return []

    faces = []
    for r in results:
        area = r.get("facial_area") or {}
        x = int(area.get("x", 0))
        y = int(area.get("y", 0))
        w = int(area.get("w", 0))
        h = int(area.get("h", 0))
        emb: list[float] = r.get("embedding") or []
        # Skip trivial detections (enforce_detection=False returns dummy area when no face)
        if w < 20 or h < 20:
            continue
        faces.append({"bbox": (x, y, w, h), "embedding": emb})

    return faces
