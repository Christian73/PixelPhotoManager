# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
Face detection and embedding via InsightFace (buffalo_l).

Model pack buffalo_l:
  - Detection  : SCRFD-10GF    (RetinaFace-class, very fast on CPU)
  - Embedding  : ArcFace R100  (Glint360K, 512-dim, state of the art)

Replaces the former DeepFace + TensorFlow stack:
  - No more 20 s TF warmup — ONNX Runtime starts in ~3 s
  - Detection + embedding in a single pass (vs two with DeepFace)
  - No TensorFlow/Keras dependency

The models are downloaded automatically into ~/.insightface/models/buffalo_l/
at the first warmup (~380 MB, once only).

detect_and_embed() runs in the ProcessPoolExecutor worker of FaceIndexThread.
The _insight_app singleton is initialised once per worker process.
"""
import contextlib
import logging
import os
import tempfile

logger = logging.getLogger(__name__)

# Maximum dimension before downscaling for the detection.
# InsightFace internally resizes to det_size=(640,640) for the detection,
# but the embedding is extracted from the original crop — the resolution is
# still limited, to avoid oversized crops.
_MAX_DETECT_DIM = 1920

# InsightFace singleton per worker process (initialised at warmup).
_insight_app = None


def _register_nvidia_dll_dirs() -> None:
    """Adds the nvidia DLL directories (cuDNN, cuBLAS…) to the Windows PATH.

    Necessary for onnxruntime-gpu on Windows when cuDNN is installed through pip
    (nvidia-cudnn-cu12) rather than through the NVIDIA installer.  onnxruntime
    loads onnxruntime_providers_cuda.dll through LoadLibraryEx, which resolves
    its implicit dependencies (cudnn64_9.dll, etc.) through the system PATH —
    os.add_dll_directory() does not cover that case.  Without this, onnxruntime
    silently falls back to the CPU."""
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
    """Root to pass to FaceAnalysis(root=...) to find the buffalo_l pack.

    In frozen mode (PyInstaller), the pack is embedded in the bundle
    (cf. pixelphotomanager.spec) under sys._MEIPASS/insightface_root/models/
    buffalo_l, to avoid any download at the first launch (impossible without
    Internet access to github.com). In dev mode, the default insightface user
    cache (~/.insightface) is kept.
    """
    import sys
    if getattr(sys, "frozen", False):
        bundled = os.path.join(getattr(sys, "_MEIPASS", ""), "insightface_root")
        if os.path.isdir(os.path.join(bundled, "models", "buffalo_l")):
            return bundled
    return os.path.expanduser("~/.insightface")


def _get_insight_app():
    """Returns (and initialises if needed) the FaceAnalysis singleton.

    Uses CUDA if available (onnxruntime-gpu), otherwise falls back to the CPU.
    ctx_id=0 = the first GPU; ctx_id=-1 = CPU forced.
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
    Corrects the EXIF rotation and extra_rotation by writing a temporary file if
    necessary.  OpenCV (like DeepFace) rejects non-ASCII paths on Windows — an
    ASCII temp is created in that case too.

    For videos, extracts a representative frame through cv2.
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
        from src.library.exif_reader import VIDEO_EXT, ascii_safe_path
        is_video = os.path.splitext(image_path)[1].lower() in VIDEO_EXT
    except Exception:
        is_video = False

    try:
        if is_video:
            import cv2
            with ascii_safe_path(image_path) as vpath:
                cap = cv2.VideoCapture(vpath)
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
            from PIL import ImageOps
            from src.library.image_loader import RAW_EXT, open_image, safe_temp_suffix
            # RAW/HEIC: cv2.imread cannot decode them — a JPEG conversion is
            # necessary even when orientation/rotation/ascii are already
            # correct, failing which detect_and_embed receives the original
            # file, which cv2 cannot read.
            ext = os.path.splitext(image_path)[1].lower()
            needs_format_conversion = ext in RAW_EXT or ext in (".heic", ".heif")
            with open_image(image_path) as img:
                orientation = img.getexif().get(274, 1)
                needs_exif = orientation not in (None, 1)
                if needs_exif or needs_rotation or needs_ascii or needs_format_conversion:
                    corrected = ImageOps.exif_transpose(img) if needs_exif else img.copy()
                    if needs_rotation:
                        corrected = corrected.rotate(-extra_rotation, expand=True)
                    suffix = safe_temp_suffix(image_path)
                    fd, temp_path = tempfile.mkstemp(suffix=suffix)
                    os.close(fd)
                    corrected.save(temp_path, quality=95)
                    result_path = temp_path
    except Exception:
        pass

    # Fallback: if the path is non-ASCII but PIL failed silently (a corrupted
    # JPEG, a mode without alpha, etc.), copy the raw file to an ASCII temp so
    # that cv2.imread can open it.
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
    If the image exceeds _MAX_DETECT_DIM, produces a downscaled version and
    returns (downscaled_path, scale_factor).  Otherwise returns
    (original_path, 1.0).

    The factor allows the bboxes to be brought back to the coordinates of the
    original image.
    """
    temp_path = None
    result_path = image_path
    scale = 1.0
    try:
        from PIL import Image
        from src.library.image_loader import open_image, safe_temp_suffix
        with open_image(image_path) as img:
            w, h = img.size
            max_dim = max(w, h)
            if max_dim > _MAX_DETECT_DIM:
                scale = _MAX_DETECT_DIM / max_dim
                new_w, new_h = int(w * scale), int(h * scale)
                resized = img.resize((new_w, new_h), Image.LANCZOS)
                suffix = safe_temp_suffix(image_path)
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
    """Returns True if insightface is installed and usable."""
    try:
        import insightface  # noqa: F401
        import onnxruntime  # noqa: F401
        return True
    except ImportError:
        return False


def warmup_worker() -> None:
    """
    Initialises InsightFace in the ProcessPoolExecutor worker subprocess.

    Must be a MODULE-LEVEL function (not a lambda, not a method) to be picklable
    by multiprocessing on Windows (spawn).

    On the first call, downloads the buffalo_l models (~380 MB) if absent.
    The following calls are instantaneous (models already in the local cache).
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
    """CPU-forced variant — used as a fallback when CUDA gets stuck."""
    global _insight_app
    _insight_app = None  # resets any partial GPU singleton
    from insightface.app import FaceAnalysis
    _insight_app = FaceAnalysis(
        name="buffalo_l", root=_insightface_root(), providers=["CPUExecutionProvider"],
    )
    _insight_app.prepare(ctx_id=-1, det_size=(640, 640))
    logger.info("InsightFace warmup OK (buffalo_l, CPU forcé)")


def detect_and_embed_auto(image_path: str) -> "tuple[list[dict], int]":
    """Tries the necessary rotations and returns the one detecting the most faces.

    Strategy: 0° is tried first.  If faces are found, it stops immediately (the
    nominal case: ~95 % of the photos are correctly oriented).  90°/180°/270°
    are only attempted if 0° detects nothing.
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
    Detects the faces in an image and computes the ArcFace embeddings.

    Returns a list of dicts:
        {'bbox': (x, y, w, h), 'embedding': list[float]}   # x,y,w,h in pixels

    Raises RuntimeError if insightface is not installed.
    Raises FileNotFoundError if the image does not exist.
    Returns [] if no face is found.
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
