# Windows CPU candidate build notes

Runtime: CPython 3.12.15, Astral python-build-standalone release 20261020, Windows x64. Source and upstream digest are in binary-provenance.json. https://github.com/astral-sh/python-build-standalone/releases/tag/20261020

Inference: PyTorch and torchaudio 2.8.0 CPU wheels from https://download.pytorch.org/whl/cpu ; other pinned Windows wheels from https://pypi.org/simple . windows-requirements.lock lists exact offline wheel hashes. Full metadata closure was checked against Python 3.12.15 / Windows AMD64, including pyreadline3's Windows-only requirement. This is not a native import or audio acceptance claim.

FFmpeg 9.0.2 essentials: https://www.gyan.dev/ffmpeg/builds/ ; copied license and documentation under native-tools. Source reference: https://github.com/FFmpeg/FFmpeg/commit/946fcce07b . Keep notices with this internal candidate.

Model assets and base FCPE integration come from the verified Mac candidate. This Windows candidate changes one engine source file: modules/length_regulator.py imports DAC only when optional vector quantization is requested. The selected fixed model uses continuous input with vector_quantize=false. Its parameter tensors and forward outputs matched before/after on the Mac regression interpreter; the active import chain passed while DAC imports were forbidden. The diff is in patches; engine-code-sha256.json records the new local revision. No model weights changed. Optional vector-quantized models are rejected by this package's adapter.

Windows native execution has not yet been performed. Check-RADAR.cmd performs real host installation/import checks plus Windows process-lock/Job Object checks when run on a Windows machine. Before release, also run short dry/mixed conversion, cancellation, restart/output preservation, then a full song on a CPU-only machine and a second hardware configuration. Generated output does not certify voice likeness.
