"""
Face detection and embedding via InsightFace (buffalo_l).

Model pack buffalo_l :
  - Détection  : SCRFD-10GF    (RetinaFace-class, très rapide sur CPU)
  - Embedding  : ArcFace R100  (Glint360K, 512-dim, état de l'art)

Remplace l'ancienne stack DeepFace + TensorFlow :
  - Plus de warmup TF de 20 s — ONNX Runtime démarre en ~3 s
  - Détection + embedding en une seule passe (vs deux avec DeepFace)
  - Pas de dépendance TensorFlow/Keras

Les modèles sont téléchargés automatiquement dans ~/.insightface/models/buffalo_l/
lors du premier warmup (~380 Mo, une seule fois).

detect_and_embed() s'exécute dans le worker ProcessPoolExecutor de FaceIndexThread.
Le singleton _insight_app est initialisé une fois par processus worker.
"""
import contextlib
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

# Dimension maximale avant réduction pour la détection.
# InsightFace redimensionne en interne à det_size=(640,640) pour la détection,
# mais l'embedding est extrait sur le crop original — on limite tout de même
# la résolution pour éviter les crops de trop grande taille.
_MAX_DETECT_DIM = 1920

# Singleton InsightFace par processus worker (initialisé au warmup).
_insight_app = None


def _get_insight_app():
    """Retourne (et initialise si besoin) le singleton FaceAnalysis."""
    global _insight_app
    if _insight_app is None:
        from insightface.app import FaceAnalysis
        _insight_app = FaceAnalysis(
            name="buffalo_l",
            providers=["CPUExecutionProvider"],
        )
        _insight_app.prepare(ctx_id=-1, det_size=(640, 640))
    return _insight_app


# ------------------------------------------------------------------ context managers

@contextlib.contextmanager
def _exif_corrected(image_path: str, extra_rotation: int = 0):
    """
    Corrige la rotation EXIF et extra_rotation en écrivant un fichier temporaire
    si nécessaire.  OpenCV (comme DeepFace) rejette les chemins non-ASCII sur
    Windows — un temp ASCII est aussi créé dans ce cas.

    Pour les vidéos, extrait une frame représentative via cv2.
    """
    temp_path = None
    result_path = image_path

    needs_rotation = extra_rotation % 360 != 0
    needs_ascii = False
    try:
        image_path.encode("ascii")
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
                orientation = img.getexif().get(274, 1)
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
    Si l'image dépasse _MAX_DETECT_DIM, produit une version réduite et retourne
    (chemin_réduit, facteur_échelle).  Sinon retourne (chemin_original, 1.0).

    Le facteur permet de ramener les bbox aux coordonnées de l'image originale.
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
                new_w, new_h = int(w * scale), int(h * scale)
                resized = img.resize((new_w, new_h), Image.LANCZOS)
                suffix = os.path.splitext(image_path)[1] or ".jpg"
                fd, temp_path = tempfile.mkstemp(suffix=suffix)
                os.close(fd)
                resized.save(temp_path, quality=92)
                result_path = temp_path
    except Exception:
        pass
    try:
        yield result_path, scale
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)


# ------------------------------------------------------------------ public API

def is_available() -> bool:
    """Retourne True si insightface est installé et utilisable."""
    try:
        import insightface  # noqa: F401
        import onnxruntime  # noqa: F401
        return True
    except ImportError:
        return False


def warmup_worker() -> None:
    """
    Initialise InsightFace dans le sous-processus worker de ProcessPoolExecutor.

    Doit être une fonction MODULE-LEVEL (non-lambda, non-méthode) pour être
    picklable par multiprocessing sur Windows (spawn).

    Au premier appel, télécharge les modèles buffalo_l (~380 Mo) si absents.
    Les appels suivants sont instantanés (modèles déjà en cache local).
    """
    import numpy as np
    try:
        app = _get_insight_app()
        dummy = np.zeros((112, 112, 3), dtype=np.uint8)
        app.get(dummy)
        logger.info("InsightFace warmup OK (buffalo_l, CPU)")
    except Exception as exc:
        logger.error("InsightFace warmup échoué : %s", exc)
        raise


def detect_and_embed(image_path: str, rotation: int = 0) -> list[dict]:
    """
    Détecte les visages dans une image et calcule les embeddings ArcFace.

    Retourne une liste de dicts :
        {'bbox': (x, y, w, h), 'embedding': list[float]}   # x,y,w,h en pixels

    Lève RuntimeError si insightface n'est pas installé.
    Lève FileNotFoundError si l'image n'existe pas.
    Retourne [] si aucun visage n'est trouvé.
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(image_path)

    try:
        import cv2
    except ImportError:
        raise RuntimeError("opencv-python est requis pour la détection de visages.")

    if not is_available():
        raise RuntimeError(
            "La reconnaissance faciale nécessite insightface. "
            "Installez-le avec : pip install insightface onnxruntime"
        )

    try:
        with _exif_corrected(image_path, extra_rotation=rotation) as corrected_path:
            with _resized_for_detection(corrected_path) as (detect_path, scale):
                img = cv2.imread(detect_path)
                if img is None:
                    logger.warning("cv2.imread a retourné None pour %s", image_path)
                    return []
                app = _get_insight_app()
                faces = app.get(img)
    except Exception as exc:
        logger.warning("InsightFace.get() a échoué pour %s : %s", image_path, exc)
        return []

    if not faces:
        return []

    result = []
    inv = 1.0 / scale if scale != 1.0 else 1.0
    for face in faces:
        if face.det_score < 0.5:
            continue
        if face.embedding is None:
            continue
        x1, y1, x2, y2 = face.bbox.astype(int)
        w, h = x2 - x1, y2 - y1
        if w < 20 or h < 20:
            continue
        if inv != 1.0:
            x1 = int(x1 * inv)
            y1 = int(y1 * inv)
            w  = int(w  * inv)
            h  = int(h  * inv)
        result.append({
            "bbox":      (x1, y1, w, h),
            "embedding": face.embedding.tolist(),
        })
    return result
