# SPDX-License-Identifier: GPL-3.0-only
"""Narrow MPS compatibility for RMVPE's NumPy float64 F0 output."""
import numpy as np


def compatible_f0(values, device_type):
    """MPS has no float64 tensors; preserve the CPU/CUDA path unchanged."""
    return np.asarray(values, dtype=np.float32) if device_type == "mps" else values
