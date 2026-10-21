# SPDX-License-Identifier: GPL-3.0-only
"""CPU precision preparation for the pinned Seed-VC Whisper semantic closure.

Promote the SAME already-loaded fp16 weights and buffers, keeping model choice,
seed and diffusion settings. Different arithmetic can change PCM samples; this
is not a claim of bit-exact or perceptual equivalence. CUDA/MPS stay untouched.
"""

def prepare_cpu_whisper_encoder(semantic_fn, device_type):
    report = {"applied": False, "scope": "Whisper semantic encoder only", "device": device_type}
    if device_type != "cpu":
        report["reason"] = "non-CPU device unchanged"
        return report
    encoders = []
    for cell in semantic_fn.__closure__ or ():
        try:
            item = cell.cell_contents
        except ValueError:
            continue
        if type(item).__name__ == "WhisperModel" and hasattr(item, "encoder"):
            encoders.append(item.encoder)
    if len(encoders) != 1:
        raise RuntimeError("CPU precision adapter expected one pinned Whisper encoder; refusing an unknown engine contract")
    encoder = encoders[0]
    before = str(encoder.dtype)
    report["dtype_before"] = before
    if before not in {"torch.float16", "torch.float32"}:
        raise RuntimeError("Unsupported CPU Whisper dtype: " + before)
    if before == "torch.float16":
        encoder.float()
        report["applied"] = True
    after = str(encoder.dtype)
    if after != "torch.float32":
        raise RuntimeError("CPU Whisper encoder did not become float32")
    report["dtype_after"] = after
    report["reason"] = "CPU float32 arithmetic using the existing loaded weights"
    return report
