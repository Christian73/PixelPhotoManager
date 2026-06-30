# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
Détection et configuration GPU pour TensorFlow / DeepFace.

Sur Windows natif, TensorFlow >= 2.11 ne supporte pas CUDA.
L'accélération GPU sur Windows passe par le plugin DirectML :
    pip install tensorflow-directml-plugin

Ce module détecte :
  1. GPU disponible via TF (DirectML ou CUDA selon la plateforme)
  2. Matériel GPU présent via nvidia-smi (pour information même sans support TF)
"""

import logging
import platform
import sys

logger = logging.getLogger(__name__)

_configured: bool = False
_device_label: str = ""


def configure() -> str:
    """
    Configure TensorFlow pour GPU si disponible et retourne un label lisible.

    Exemples de retour :
        "GPU : NVIDIA GeForce RTX 3070 (8 Go)  [DirectML]"
        "GPU : NVIDIA GeForce RTX 3070 (8 Go)  [CUDA]"
        "CPU  (GPU détecté : RTX 3070 — installez tensorflow-directml-plugin)"
        "CPU  (aucun GPU détecté)"
    """
    global _configured, _device_label
    if _configured:
        return _device_label
    _configured = True

    try:
        import tensorflow as tf
    except ImportError:
        _device_label = "CPU (TensorFlow non disponible)"
        return _device_label

    try:
        gpus = tf.config.list_physical_devices("GPU")
    except Exception as exc:
        _device_label = f"CPU (erreur détection GPU : {exc})"
        logger.warning("gpu_utils : %s", _device_label)
        return _device_label

    if gpus:
        # GPU disponible pour TF (DirectML ou CUDA)
        for gpu in gpus:
            try:
                tf.config.experimental.set_memory_growth(gpu, True)
            except RuntimeError:
                pass

        backend = _detect_backend()
        names = [_gpu_detail(gpu.name) for gpu in gpus]
        _device_label = "GPU : " + ", ".join(names) + f"  [{backend}]"
        logger.info("gpu_utils : %s", _device_label)
        return _device_label

    # Pas de GPU utilisable par TF — chercher quand même le matériel présent
    hw = _hardware_gpu_name()
    if hw and sys.platform == "win32":
        _device_label = (
            f"CPU  (GPU détecté : {hw} — "
            f"accélération GPU non disponible sur Windows natif avec TF ≥ 2.11)"
        )
        logger.info("gpu_utils : %s", _device_label)
    elif hw:
        _device_label = (
            f"CPU  (GPU détecté : {hw} — "
            f"CUDA/cuDNN requis pour l'utiliser)"
        )
        logger.info("gpu_utils : %s", _device_label)
    else:
        _device_label = "CPU  (aucun GPU détecté)"
        logger.info("gpu_utils : %s", _device_label)

    return _device_label


def _detect_backend() -> str:
    """Détermine si TF utilise DirectML ou CUDA."""
    try:
        import tensorflow as tf
        plugins = tf.config.list_logical_devices()
        names = [d.device_type for d in plugins]
        if "DML" in names:
            return "DirectML"
    except Exception:
        pass
    return "CUDA" if sys.platform != "win32" else "DirectML"


def _hardware_gpu_name() -> str:
    """Retourne le nom du premier GPU NVIDIA via nvidia-smi (ou chaîne vide)."""
    try:
        import subprocess
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            line = r.stdout.strip().splitlines()[0]
            name, vram_mb = line.split(",", 1)
            vram_go = int(vram_mb.strip()) // 1024
            return f"{name.strip()} ({vram_go} Go)"
    except Exception:
        pass
    return ""


def _gpu_detail(tf_name: str) -> str:
    """Enrichit le nom TF avec la VRAM via nvidia-smi si possible."""
    import re
    m = re.search(r":(\d+)$", tf_name)
    idx = int(m.group(1)) if m else 0
    try:
        import subprocess
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            lines = [l.strip() for l in r.stdout.strip().splitlines() if l.strip()]
            if idx < len(lines):
                name, vram_mb = lines[idx].split(",", 1)
                vram_go = int(vram_mb.strip()) // 1024
                return f"{name.strip()} ({vram_go} Go)"
    except Exception:
        pass
    return tf_name


def device_label() -> str:
    """Retourne le label du device (configure() si nécessaire)."""
    return _device_label if _configured else configure()
