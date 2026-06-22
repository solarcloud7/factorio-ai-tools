"""GPU/torch consistency: alert when a machine has an NVIDIA GPU but torch can't
use it (the CPU-wheel-on-a-GPU-box mistake that silently embeds on CPU)."""

import shutil

import pytest

from factorio_ai_tools.ingest import common


def test_warns_when_gpu_present_but_torch_cpu_only():
    msg = common.gpu_torch_warning(cuda_available=False, has_nvidia_smi=True)
    assert msg is not None and "make sync" in msg


def test_no_warning_when_gpu_and_cuda_ok():
    assert common.gpu_torch_warning(cuda_available=True, has_nvidia_smi=True) is None


def test_no_warning_when_no_gpu():
    assert common.gpu_torch_warning(cuda_available=False, has_nvidia_smi=False) is None
    assert common.gpu_torch_warning(cuda_available=True, has_nvidia_smi=False) is None


def test_this_machine_uses_its_gpu():
    """On a box with an NVIDIA GPU, torch MUST be able to use CUDA. Skips on
    GPU-less machines (CI), so it only fails the dev box that's misconfigured."""
    if shutil.which("nvidia-smi") is None:
        pytest.skip("no NVIDIA GPU on this machine")
    import torch

    assert torch.cuda.is_available(), (
        "NVIDIA GPU present but torch cannot use CUDA — embedding would run on CPU. "
        "Run `make sync` to install the CUDA wheel."
    )
