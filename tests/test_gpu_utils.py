# Copyright 2026 Christian Guyot
# SPDX-License-Identifier: Apache-2.0
"""Tests of src/faces/gpu_utils.py: GPU detection for TensorFlow, in pure
Python. `tensorflow` is mocked through sys.modules (a local import in every
function) and `subprocess.run` (nvidia-smi) through monkeypatch -- no real
dependency on a GPU or on TensorFlow, so that the tests stay deterministic.

`configure()` memoises its result in the `_configured`/`_device_label`
globals: every test resets them before calling it."""
import subprocess
import sys
import types

import src.faces.gpu_utils as gpu_utils_module


def _reset(monkeypatch):
    monkeypatch.setattr(gpu_utils_module, "_configured", False)
    monkeypatch.setattr(gpu_utils_module, "_device_label", "")


def _fake_tf(gpus=None, logical_devices=None, list_physical_raises=None, set_memory_growth_raises=None):
    mod = types.ModuleType("tensorflow")

    def list_physical_devices(kind):
        if list_physical_raises is not None:
            raise list_physical_raises
        return gpus or []

    def list_logical_devices():
        return logical_devices or []

    def set_memory_growth(gpu, flag):
        if set_memory_growth_raises is not None:
            raise set_memory_growth_raises

    mod.config = types.SimpleNamespace(
        list_physical_devices=list_physical_devices,
        list_logical_devices=list_logical_devices,
        experimental=types.SimpleNamespace(set_memory_growth=set_memory_growth),
    )
    return mod


class _FakeGpu:
    def __init__(self, name):
        self.name = name


class _FakeLogicalDevice:
    def __init__(self, device_type):
        self.device_type = device_type


def _raise_file_not_found(*args, **kwargs):
    raise FileNotFoundError("nvidia-smi introuvable")


class TestConfigureNoTensorflow:
    def test_returns_cpu_label_when_tensorflow_not_installed(self, monkeypatch):
        _reset(monkeypatch)
        monkeypatch.setitem(sys.modules, "tensorflow", None)  # forces ImportError

        assert gpu_utils_module.configure() == "CPU (TensorFlow not available)"


class TestConfigureMemoization:
    def test_second_call_returns_cached_label_without_recomputing(self, monkeypatch):
        _reset(monkeypatch)
        monkeypatch.setitem(sys.modules, "tensorflow", None)
        gpu_utils_module.configure()

        monkeypatch.setattr(gpu_utils_module, "_device_label", "valeur mise en cache")

        assert gpu_utils_module.configure() == "valeur mise en cache"


class TestConfigureListPhysicalDevicesRaises:
    def test_returns_cpu_label_with_exception_message(self, monkeypatch):
        _reset(monkeypatch)
        fake_tf = _fake_tf(list_physical_raises=RuntimeError("driver absent"))
        monkeypatch.setitem(sys.modules, "tensorflow", fake_tf)

        label = gpu_utils_module.configure()

        assert label == "CPU (erreur détection GPU : driver absent)"


class TestConfigureGpuFound:
    def test_gpu_label_includes_name_and_directml_backend(self, monkeypatch):
        _reset(monkeypatch)
        monkeypatch.setattr(subprocess, "run", _raise_file_not_found)
        fake_tf = _fake_tf(
            gpus=[_FakeGpu("/physical_device:GPU:0")],
            logical_devices=[_FakeLogicalDevice("DML")],
        )
        monkeypatch.setitem(sys.modules, "tensorflow", fake_tf)

        label = gpu_utils_module.configure()

        assert label == "GPU : /physical_device:GPU:0  [DirectML]"

    def test_multiple_gpus_are_joined_with_comma(self, monkeypatch):
        _reset(monkeypatch)
        monkeypatch.setattr(subprocess, "run", _raise_file_not_found)
        fake_tf = _fake_tf(
            gpus=[_FakeGpu("/physical_device:GPU:0"), _FakeGpu("/physical_device:GPU:1")],
            logical_devices=[_FakeLogicalDevice("DML")],
        )
        monkeypatch.setitem(sys.modules, "tensorflow", fake_tf)

        label = gpu_utils_module.configure()

        assert label == "GPU : /physical_device:GPU:0, /physical_device:GPU:1  [DirectML]"

    def test_memory_growth_runtime_error_is_swallowed(self, monkeypatch):
        _reset(monkeypatch)
        monkeypatch.setattr(subprocess, "run", _raise_file_not_found)
        fake_tf = _fake_tf(
            gpus=[_FakeGpu("/physical_device:GPU:0")],
            logical_devices=[_FakeLogicalDevice("DML")],
            set_memory_growth_raises=RuntimeError("déjà initialisé"),
        )
        monkeypatch.setitem(sys.modules, "tensorflow", fake_tf)

        label = gpu_utils_module.configure()

        assert label == "GPU : /physical_device:GPU:0  [DirectML]"

    def test_gpu_detail_enriched_with_vram_from_nvidia_smi(self, monkeypatch):
        _reset(monkeypatch)

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args, returncode=0, stdout="NVIDIA GeForce RTX 3070, 8192\n", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        fake_tf = _fake_tf(
            gpus=[_FakeGpu("/physical_device:GPU:0")],
            logical_devices=[_FakeLogicalDevice("DML")],
        )
        monkeypatch.setitem(sys.modules, "tensorflow", fake_tf)

        label = gpu_utils_module.configure()

        assert label == "GPU : NVIDIA GeForce RTX 3070 (8 Go)  [DirectML]"


class TestConfigureNoGpuButHardwarePresent:
    def test_windows_message_when_hardware_present_but_unusable(self, monkeypatch):
        _reset(monkeypatch)
        monkeypatch.setattr(gpu_utils_module.sys, "platform", "win32")

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args, returncode=0, stdout="NVIDIA GeForce RTX 3070, 8192\n", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        fake_tf = _fake_tf(gpus=[])
        monkeypatch.setitem(sys.modules, "tensorflow", fake_tf)

        label = gpu_utils_module.configure()

        assert label == (
            "CPU  (GPU détecté : NVIDIA GeForce RTX 3070 (8 Go) — "
            "accélération GPU non disponible sur Windows natif avec TF ≥ 2.11)"
        )

    def test_non_windows_message_when_hardware_present_but_unusable(self, monkeypatch):
        _reset(monkeypatch)
        monkeypatch.setattr(gpu_utils_module.sys, "platform", "linux")

        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args, returncode=0, stdout="NVIDIA GeForce RTX 3070, 8192\n", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        fake_tf = _fake_tf(gpus=[])
        monkeypatch.setitem(sys.modules, "tensorflow", fake_tf)

        label = gpu_utils_module.configure()

        assert label == (
            "CPU  (GPU détecté : NVIDIA GeForce RTX 3070 (8 Go) — "
            "CUDA/cuDNN requis pour l'utiliser)"
        )


class TestConfigureNoGpuNoHardware:
    def test_returns_generic_cpu_label_when_nvidia_smi_absent(self, monkeypatch):
        _reset(monkeypatch)
        monkeypatch.setattr(subprocess, "run", _raise_file_not_found)
        fake_tf = _fake_tf(gpus=[])
        monkeypatch.setitem(sys.modules, "tensorflow", fake_tf)

        assert gpu_utils_module.configure() == "CPU  (no GPU found)"


class TestDetectBackend:
    def test_dml_logical_device_gives_directml(self, monkeypatch):
        fake_tf = _fake_tf(logical_devices=[_FakeLogicalDevice("DML")])
        monkeypatch.setitem(sys.modules, "tensorflow", fake_tf)

        assert gpu_utils_module._detect_backend() == "DirectML"

    def test_no_dml_falls_back_to_platform_windows(self, monkeypatch):
        monkeypatch.setattr(gpu_utils_module.sys, "platform", "win32")
        fake_tf = _fake_tf(logical_devices=[_FakeLogicalDevice("GPU")])
        monkeypatch.setitem(sys.modules, "tensorflow", fake_tf)

        assert gpu_utils_module._detect_backend() == "DirectML"

    def test_no_dml_falls_back_to_platform_linux(self, monkeypatch):
        monkeypatch.setattr(gpu_utils_module.sys, "platform", "linux")
        fake_tf = _fake_tf(logical_devices=[_FakeLogicalDevice("GPU")])
        monkeypatch.setitem(sys.modules, "tensorflow", fake_tf)

        assert gpu_utils_module._detect_backend() == "CUDA"

    def test_tensorflow_import_error_falls_back_to_platform(self, monkeypatch):
        monkeypatch.setattr(gpu_utils_module.sys, "platform", "linux")
        monkeypatch.setitem(sys.modules, "tensorflow", None)

        assert gpu_utils_module._detect_backend() == "CUDA"


class TestHardwareGpuName:
    def test_parses_name_and_vram_on_success(self, monkeypatch):
        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args, returncode=0, stdout="NVIDIA GeForce RTX 3070, 8192\n", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert gpu_utils_module._hardware_gpu_name() == "NVIDIA GeForce RTX 3070 (8 Go)"

    def test_returns_empty_string_when_nvidia_smi_missing(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", _raise_file_not_found)

        assert gpu_utils_module._hardware_gpu_name() == ""

    def test_returns_empty_string_on_nonzero_return_code(self, monkeypatch):
        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(args, returncode=1, stdout="", stderr="no devices")

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert gpu_utils_module._hardware_gpu_name() == ""


class TestGpuDetail:
    def test_enriches_with_vram_using_index_from_tf_name(self, monkeypatch):
        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args,
                returncode=0,
                stdout="NVIDIA GeForce RTX 3070, 8192\nNVIDIA GeForce RTX 4090, 24576\n",
                stderr="",
            )

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert gpu_utils_module._gpu_detail("/physical_device:GPU:1") == "NVIDIA GeForce RTX 4090 (24 Go)"

    def test_missing_index_in_name_defaults_to_zero(self, monkeypatch):
        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args, returncode=0, stdout="NVIDIA GeForce RTX 3070, 8192\n", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert gpu_utils_module._gpu_detail("no-index-here") == "NVIDIA GeForce RTX 3070 (8 Go)"

    def test_returns_raw_tf_name_when_nvidia_smi_fails(self, monkeypatch):
        monkeypatch.setattr(subprocess, "run", _raise_file_not_found)

        assert gpu_utils_module._gpu_detail("/physical_device:GPU:0") == "/physical_device:GPU:0"

    def test_returns_raw_tf_name_when_index_out_of_range(self, monkeypatch):
        def fake_run(*args, **kwargs):
            return subprocess.CompletedProcess(
                args, returncode=0, stdout="NVIDIA GeForce RTX 3070, 8192\n", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert gpu_utils_module._gpu_detail("/physical_device:GPU:5") == "/physical_device:GPU:5"


class TestDeviceLabel:
    def test_calls_configure_when_not_yet_configured(self, monkeypatch):
        _reset(monkeypatch)
        monkeypatch.setitem(sys.modules, "tensorflow", None)

        assert gpu_utils_module.device_label() == "CPU (TensorFlow not available)"

    def test_returns_cached_label_without_reconfiguring(self, monkeypatch):
        monkeypatch.setattr(gpu_utils_module, "_configured", True)
        monkeypatch.setattr(gpu_utils_module, "_device_label", "déjà configuré")

        assert gpu_utils_module.device_label() == "déjà configuré"
