import contextlib
import logging
import os
import shutil
import tempfile

logger = logging.getLogger(__name__)

# Dimension maximale avant de réduire l'image pour la détection.
# RetinaFace échoue fréquemment sur les photos smartphone haute résolution
# (>10 MP) quand les visages occupent une large fraction du cadre.
_MAX_DETECT_DIM = 1920


@contextlib.contextmanager
def _exif_corrected(image_path: str, extra_rotation: int = 0):
    """
    Corrige la rotation EXIF et applique extra_rotation (degrés CW) en écrivant
    un fichier temporaire si nécessaire. DeepFace rejette les chemins non-ASCII —
    un temp est aussi créé dans ce cas.

    Pour les vidéos, extrait une frame représentative via cv2 (DeepFace ne peut
    pas traiter les fichiers vidéo directement, et rejette les chemins non-ASCII).
    """
    temp_path = None
    result_path = image_path

    needs_rotation = extra_rotation % 360 != 0
    needs_ascii = False
    try:
        image_path.encode('ascii')
    except UnicodeEncodeError:
        needs_ascii = True

    try:
        from src.library.exif_reader import VIDEO_EXT
        is_video = os.path.splitext(image_path)[1].lower() in VIDEO_EXT
    except Exception:
        is_video = False

    try:
        if is_video:
            import cv2
            cap = cv2.VideoCapture(image_path)
            if cap.isOpened():
                total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
                target = max(0, int(total * 0.1))
                cap.set(cv2.CAP_PROP_POS_FRAMES, target)
                ret, frame = cap.read()
                cap.release()
                if ret:
                    fd, temp_path = tempfile.mkstemp(suffix=".jpg")
                    os.close(fd)
                    cv2.imwrite(temp_path, frame)
                    result_path = temp_path
        else:
            from PIL import Image, ImageOps
            with Image.open(image_path) as img:
                orientation = img.getexif().get(274, 1)   # 274 = Orientation tag
                needs_exif = orientation not in (None, 1)

                if needs_exif or needs_rotation or needs_ascii:
                    corrected = ImageOps.exif_transpose(img) if needs_exif else img.copy()
                    if needs_rotation:
                        corrected = corrected.rotate(-extra_rotation, expand=True)
                    suffix = os.path.splitext(image_path)[1] or ".jpg"
                    fd, temp_path = tempfile.mkstemp(suffix=suffix)
                    os.close(fd)
                    corrected.save(temp_path, quality=95)
                    result_path = temp_path
    except Exception:
        pass

    try:
        yield result_path
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


@contextlib.contextmanager
def _resized_for_detection(image_path: str):
    """
    Si l'image dépasse _MAX_DETECT_DIM, produit une version réduite dans un
    fichier temporaire et retourne (chemin_reduit, facteur_echelle).
    Sinon retourne (chemin_original, 1.0).

    Le facteur d'échelle permet de ramener les bbox détectées aux coordonnées
    de l'image originale.
    """
    temp_path = None
    result_path = image_path
    scale = 1.0
    try:
        from PIL import Image
        with Image.open(image_path) as img:
            w, h = img.size
            max_dim = max(w, h)
            if max_dim > _MAX_DETECT_DIM:
                scale = _MAX_DETECT_DIM / max_dim
                new_w = int(w * scale)
                new_h = int(h * scale)
                resized = img.resize((new_w, new_h), Image.LANCZOS)
                suffix = os.path.splitext(image_path)[1] or ".jpg"
                fd, temp_path = tempfile.mkstemp(suffix=suffix)
                os.close(fd)
                resized.save(temp_path, quality=92)
                logger.debug(
                    "Détection sur image réduite %dx%d→%dx%d (%.2f×) pour %s",
                    w, h, new_w, new_h, scale, os.path.basename(image_path),
                )
                result_path = temp_path
    except Exception:
        pass   # en cas d'erreur de redimensionnement, on passe le fichier original
    try:
        yield result_path, scale
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


def warmup_worker() -> None:
    """
    Initialise TF/DeepFace dans le sous-processus worker de ProcessPoolExecutor.

    Doit être une fonction MODULE-LEVEL (non-lambda, non-méthode) pour être
    picklable par multiprocessing sur Windows (spawn).

    Appelée une seule fois avant la boucle de détection, ce warmup fait que
    le premier vrai appel à detect_and_embed() est rapide (modèles déjà chargés).
    """
    import numpy as np
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
    # Dans le sous-processus worker il n'y a pas d'UI à protéger :
    # on utilise la moitié des cœurs disponibles pour accélérer l'inférence.
    _ncpu = str(max(2, (os.cpu_count() or 4) // 2))
    os.environ.setdefault("TF_NUM_INTRAOP_THREADS", _ncpu)
    os.environ.setdefault("TF_NUM_INTEROP_THREADS", "2")
    try:
        from deepface import DeepFace
        # detector_backend="skip" → appelle ArcFace directement, sans détection,
        # garantissant que le graphe TF est compilé quel que soit l'image.
        dummy_face = np.zeros((112, 112, 3), dtype=np.uint8)
        DeepFace.represent(
            img_path=dummy_face,
            model_name="ArcFace",
            detector_backend="skip",
            enforce_detection=False,
            align=False,
        )
        dummy_photo = np.zeros((1080, 1920, 3), dtype=np.uint8)
        DeepFace.represent(
            img_path=dummy_photo,
            model_name="ArcFace",
            detector_backend="retinaface",
            enforce_detection=False,
            align=False,
        )
    except Exception:
        pass


def detect_and_embed(image_path: str, rotation: int = 0) -> list[dict]:
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

    # Définir AVANT le premier import de tensorflow (setdefault = ne remplace pas
    # si déjà défini par TFWarmUpThread).  Limite TF à 2 threads pour ne pas
    # saturer tous les cœurs CPU et laisser l'UI réactive.
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")
    _ncpu = str(max(2, (os.cpu_count() or 4) // 2))
    os.environ.setdefault("TF_NUM_INTRAOP_THREADS", _ncpu)
    os.environ.setdefault("TF_NUM_INTEROP_THREADS", "2")

    try:
        from deepface import DeepFace
    except ImportError:
        raise RuntimeError(
            "La reconnaissance faciale nécessite deepface. "
            "Installez-le avec : pip install deepface"
        )

    try:
        with _exif_corrected(image_path, extra_rotation=rotation) as corrected_path:
            with _resized_for_detection(corrected_path) as (detect_path, scale):
                results = DeepFace.represent(
                    img_path=detect_path,
                    model_name="ArcFace",
                    detector_backend="retinaface",
                    enforce_detection=True,
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
        if w < 20 or h < 20:
            continue
        # Ramener les coordonnées à l'échelle de l'image originale
        if scale != 1.0:
            inv = 1.0 / scale
            x = int(x * inv)
            y = int(y * inv)
            w = int(w * inv)
            h = int(h * inv)
        faces.append({"bbox": (x, y, w, h), "embedding": emb})

    return faces
