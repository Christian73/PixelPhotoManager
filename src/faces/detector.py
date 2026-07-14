# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
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


def _register_nvidia_dll_dirs() -> None:
    """Ajoute les répertoires de DLLs nvidia (cuDNN, cuBLAS…) au PATH Windows.

    Nécessaire pour onnxruntime-gpu sur Windows quand cuDNN est installé via pip
    (nvidia-cudnn-cu12) plutôt que via l'installeur NVIDIA.  onnxruntime charge
    onnxruntime_providers_cuda.dll via LoadLibraryEx, qui résout ses dépendances
    implicites (cudnn64_9.dll, etc.) via le PATH système — os.add_dll_directory()
    ne couvre pas ce cas.  Sans cela, onnxruntime tombe silencieusement sur CPU."""
    import sys
    if sys.platform != "win32":
        return
    try:
        import nvidia
        current_path = os.environ.get("PATH", "")
        dirs_to_add = []
        for base in nvidia.__path__:
            for sub in os.listdir(base):
                bin_dir = os.path.join(base, sub, "bin")
                if os.path.isdir(bin_dir) and bin_dir not in current_path:
                    dirs_to_add.append(bin_dir)
                    os.add_dll_directory(bin_dir)
        if dirs_to_add:
            prefix = os.pathsep.join(dirs_to_add)
            os.environ["PATH"] = prefix + os.pathsep + current_path
            logger.debug("nvidia DLL dirs ajoutés au PATH : %s", dirs_to_add)
    except Exception as exc:
        logger.debug("_register_nvidia_dll_dirs : %s", exc)


def _insightface_root() -> str:
    """Racine à passer à FaceAnalysis(root=...) pour trouver le pack buffalo_l.

    En mode figé (PyInstaller), le pack est embarqué dans le bundle
    (cf. pixelphotomanager.spec) sous sys._MEIPASS/insightface_root/models/
    buffalo_l, pour éviter tout téléchargement au 1er lancement (impossible
    sans accès Internet à github.com). En mode dev, on garde le cache
    utilisateur par défaut d'insightface (~/.insightface).
    """
    import sys
    if getattr(sys, "frozen", False):
        bundled = os.path.join(getattr(sys, "_MEIPASS", ""), "insightface_root")
        if os.path.isdir(os.path.join(bundled, "models", "buffalo_l")):
            return bundled
    return os.path.expanduser("~/.insightface")


def _get_insight_app():
    """Retourne (et initialise si besoin) le singleton FaceAnalysis.

    Utilise CUDA si disponible (onnxruntime-gpu), sinon CPU en fallback.
    ctx_id=0 = premier GPU ; ctx_id=-1 = CPU forcé.
    """
    global _insight_app
    if _insight_app is None:
        _register_nvidia_dll_dirs()
        from insightface.app import FaceAnalysis
        _insight_app = FaceAnalysis(
            name="buffalo_l",
            root=_insightface_root(),
            providers=["CUDAExecutionProvider", "CPUExecutionProvider"],
        )
        _insight_app.prepare(ctx_id=0, det_size=(640, 640))
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

    # Fallback : si le chemin est non-ASCII mais que PIL a échoué silencieusement
    # (JPEG corrompu, mode sans alpha, etc.), copier le fichier brut vers un temp
    # ASCII pour que cv2.imread puisse l'ouvrir.
    if needs_ascii and result_path == image_path and not is_video:
        try:
            import shutil
            if temp_path and os.path.exists(temp_path):
                os.unlink(temp_path)
                temp_path = None
            suffix = os.path.splitext(image_path)[1] or ".jpg"
            fd, temp_path = tempfile.mkstemp(suffix=suffix)
            os.close(fd)
            shutil.copy2(image_path, temp_path)
            result_path = temp_path
        except Exception:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except Exception:
                    pass
                temp_path = None

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
    try:
        app = _get_insight_app()
        import onnxruntime as _ort
        providers = []
        for model in app.models.values():
            sess = getattr(model, "session", None) or getattr(getattr(model, "model", None), "session", None)
            if sess:
                providers = sess.get_providers()
                break
        backend = "GPU" if any("CUDA" in p for p in providers) else "CPU"
        logger.info("InsightFace warmup OK (buffalo_l, %s, providers=%s)", backend, providers)
    except Exception as exc:
        logger.error("InsightFace warmup échoué : %s", exc)
        raise


def warmup_worker_cpu() -> None:
    """Variante CPU forcé — utilisée en fallback quand CUDA bloque."""
    global _insight_app
    _insight_app = None  # reset tout singleton GPU partiel
    from insightface.app import FaceAnalysis
    _insight_app = FaceAnalysis(
        name="buffalo_l", root=_insightface_root(), providers=["CPUExecutionProvider"],
    )
    _insight_app.prepare(ctx_id=-1, det_size=(640, 640))
    logger.info("InsightFace warmup OK (buffalo_l, CPU forcé)")


def detect_and_embed_auto(image_path: str) -> "tuple[list[dict], int]":
    """Essaie les rotations nécessaires et retourne celle qui détecte le plus de visages.

    Stratégie : on essaie 0° en premier.  Si des visages sont trouvés, on s'arrête
    immédiatement (cas nominal : ~95 % des photos sont correctement orientées).
    On ne tente 90°/180°/270° que si 0° ne détecte rien.
    """
    result_0 = detect_and_embed(image_path, rotation=0)
    if result_0:
        return result_0, 0
    best_result, best_rotation = [], 0
    for rotation in (90, 180, 270):
        result = detect_and_embed(image_path, rotation=rotation)
        if len(result) > len(best_result):
            best_result, best_rotation = result, rotation
    return best_result, best_rotation


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
        logger.warning("InsightFace.get() a échoué pour %s : %s", image_path, exc, exc_info=True)
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
        if inv != 1.0:
            x1 = int(x1 * inv)
            y1 = int(y1 * inv)
            w  = int(w  * inv)
            h  = int(h  * inv)
        if w < 20 or h < 20:
            continue
        result.append({
            "bbox":      (x1, y1, w, h),
            "embedding": face.embedding.tolist(),
            "det_score": float(face.det_score),
        })
    return result
