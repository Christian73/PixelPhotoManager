# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""
GPU detection and configuration for TensorFlow / DeepFace.

On native Windows, TensorFlow >= 2.11 does not support CUDA.
GPU acceleration on Windows goes through the DirectML plugin:
    pip install tensorflow-directml-plugin

This module detects:
  1. the GPU available through TF (DirectML or CUDA depending on the platform)
  2. the GPU hardware present through nvidia-smi (for information, even without TF support)
"""

import logging
import platform
import sys

from src.core.i18n import translate

logger = logging.getLogger(__name__)

_configured: bool = False
_device_label: str = ""


def configure() -> str:
    """
    Configures TensorFlow for the GPU if available and returns a readable label.

    Examples of return values:
        "GPU: NVIDIA GeForce RTX 3070 (8 GB)  [DirectML]"
        "GPU: NVIDIA GeForce RTX 3070 (8 GB)  [CUDA]"
        "CPU  (GPU found: RTX 3070 — install tensorflow-directml-plugin)"
        "CPU  (no GPU found)"
    """
    global _configured, _device_label
    if _configured:
        return _device_label
    _configured = True

    try:
        import tensorflow as tf
    except ImportError:
        _device_label = translate("GpuUtils", "CPU (TensorFlow not available)")
        return _device_label

    try:
        gpus = tf.config.list_physical_devices("GPU")
    except Exception as exc:
        _device_label = translate("GpuUtils", "CPU (GPU detection error: {err})"
                                  ).format(err=exc)
        logger.warning("gpu_utils : %s", _device_label)
        return _device_label

    if gpus:
        # a GPU available to TF (DirectML or CUDA)
        for gpu in gpus:
            try:
                tf.config.experimental.set_memory_growth(gpu, True)
            except RuntimeError:
                pass

        backend = _detect_backend()
        names = [_gpu_detail(gpu.name) for gpu in gpus]
        _device_label = (translate("GpuUtils", "GPU: ") + ", ".join(names)
                         + f"  [{backend}]")
        logger.info("gpu_utils : %s", _device_label)
        return _device_label

    # No GPU usable by TF — look for the hardware present anyway
    hw = _hardware_gpu_name()
    if hw and sys.platform == "win32":
        _device_label = translate(
            "GpuUtils",
            "CPU  (GPU found: {hw} — GPU acceleration is not available on native Windows with "
            "TF ≥ 2.11)"
        ).format(hw=hw)
        logger.info("gpu_utils : %s", _device_label)
    elif hw:
        _device_label = translate(
            "GpuUtils",
            "CPU  (GPU found: {hw} — CUDA/cuDNN required to use it)"
        ).format(hw=hw)
        logger.info("gpu_utils : %s", _device_label)
    else:
        _device_label = translate("GpuUtils", "CPU  (no GPU found)")
        logger.info("gpu_utils : %s", _device_label)

    return _device_label


def _detect_backend() -> str:
    """Determines whether TF uses DirectML or CUDA."""
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
    """Returns the name of the first NVIDIA GPU through nvidia-smi (or an empty string)."""
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
    """Enriches the TF name with the VRAM through nvidia-smi if possible."""
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
    """Returns the label of the device (calls configure() if necessary)."""
    return _device_label if _configured else configure()
