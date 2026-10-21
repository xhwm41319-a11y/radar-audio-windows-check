# SPDX-License-Identifier: GPL-3.0-only
"""Choose a usable inference device before loading models; never install drivers."""
import os
import sys
import time


def select_device(torch, mode="auto"):
    if mode not in ("auto", "cpu", "gpu"):
        raise ValueError("处理方式必须是 auto、cpu 或 gpu")
    failures = []
    if mode != "cpu":
        candidates = []
        try:
            if torch.cuda.is_available():
                candidates.append("cuda")
        except (RuntimeError, AssertionError, OSError) as exc:
            failures.append("cuda discovery: " + type(exc).__name__)
        mps = getattr(getattr(torch, "backends", None), "mps", None)
        if sys.platform == "darwin" and mps is not None and mps.is_available():
            candidates.append("mps")
        for device in candidates:
            try:
                # Availability alone does not prove kernels support the installed driver/GPU.
                sample = torch.ones((16, 16), device=device)
                actual = (sample @ sample).sum().item()
                if actual != 4096:
                    raise RuntimeError("GPU calculation failed validation")
                del sample
                if device == "cuda":
                    torch.cuda.synchronize()
                    torch.cuda.empty_cache()
                return torch.device(device), {"requested": mode, "selected": device,
                    "fallback": False, "probe": "small matrix multiplication only"}
            except (RuntimeError, AssertionError, OSError) as exc:
                failures.append(device + ": " + type(exc).__name__)
        if mode == "gpu":
            raise RuntimeError("本安装包或当前显卡未通过加速检查，请选择 CPU。" + " ".join(failures))
    return torch.device("cpu"), {"requested": mode, "selected": "cpu",
        "fallback": mode == "auto", "gpu_probe_failures": failures,
        "note": "CPU selected; CPU-only packages do not enable GPU acceleration"}


def process_metrics():
    result = {"cpu_seconds": time.process_time(), "peak_rss_bytes": None}
    if os.name == "posix":
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF)
        result["peak_rss_bytes"] = usage.ru_maxrss * (1 if sys.platform == "darwin" else 1024)
    # No Windows RSS estimate is fabricated when that counter is unavailable.
    return result
