# SPDX-License-Identifier: GPL-3.0-only
"""Seed-VC SVC adapter: fixed local weights, preserved F0, explicit reference identity."""
import argparse
import json
import os
from pathlib import Path
import sys
import time


def main():
    request = json.loads(Path(sys.argv[1]).read_text())
    engine = Path(request["engine_root"])
    from verify_engine import verify_engine
    engine_audit = verify_engine(Path(__file__).resolve().parent, engine, request["code_revision"])
    models = Path(request["models"])
    output = Path(request["output_dir"])
    output.mkdir(parents=True, exist_ok=True)
    if any(output.glob("vc_*.wav")):
        raise FileExistsError("Use a fresh output directory for every inference; refusing stale output")
    os.environ.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
                       "PYTORCH_ENABLE_MPS_FALLBACK": "1"})
    sys.path.insert(0, str(engine))
    import yaml
    import numpy as np
    import soundfile as sf
    # Validate PCM before loading multi-GB models; process exit codes alone are insufficient.
    for role, key in (("source", "input_path"), ("reference", "reference_path")):
        pcm, rate = sf.read(request[key], dtype="float32", always_2d=True)
        if len(pcm) < rate or not np.isfinite(pcm).all() or float(np.sqrt(np.mean(pcm**2))) < 0.001:
            raise ValueError(f"Invalid {role}: must contain at least 1 second of finite, audible PCM")
    import torch
    torch.manual_seed(request.get("seed", 42))
    np.random.seed(request.get("seed", 42))
    torch.set_num_threads(max(1, min(4, os.cpu_count() or 1)))
    torch.set_num_interop_threads(max(1, min(2, os.cpu_count() or 1)))
    import socket
    def deny_network(*args, **kwargs):
        raise RuntimeError("Network disabled during local conversion")
    socket.socket.connect = deny_network
    socket.socket.connect_ex = deny_network
    from device_policy import select_device, process_metrics
    selected_device, device_selection = select_device(torch, request.get("processing_mode", "auto"))
    import inference
    inference.device = selected_device
    from voice_compat import compatible_f0
    from voice_precision import prepare_cpu_whisper_encoder

    started = time.perf_counter()
    checkpoint = models / "Plachta--Seed-VC/DiT_seed_v2_uvit_whisper_base_f0_44k_bigvgan_pruned_ft_ema_v2.pth"
    config = yaml.safe_load((models / "Plachta--Seed-VC/config_dit_mel_seed_uvit_whisper_base_f0_44k.yml").read_text())
    regulator = config["model_params"]["length_regulator"]
    if regulator.get("is_discrete", False) or regulator.get("vector_quantize", False):
        raise ValueError("This package is fixed to the continuous 44.1k voice model; optional DAC models are not included")
    config["model_params"]["vocoder"]["name"] = str(models / "nvidia--bigvgan_v2_44khz_128band_512x")
    config["model_params"]["speech_tokenizer"]["name"] = str(models / "openai--whisper-small")
    local_config = output / "engine-config.yml"
    local_config.write_text(yaml.safe_dump(config))

    def local_model(repo_id, model_filename="pytorch_model.bin", config_filename=None):
        directory = models / repo_id.replace("/", "--")
        model_path = directory / model_filename
        if not model_path.is_file():
            raise FileNotFoundError("本地模型缺失: " + str(model_path))
        if config_filename:
            return str(model_path), str(directory / config_filename)
        return str(model_path)

    requested_models = []
    loaded_weights = []
    original_local_model = local_model
    def audited_local_model(repo_id, *args, **kwargs):
        if "VoiceConversionWebUI" in repo_id:
            raise RuntimeError("RMVPE excluded from the FCPE candidate")
        requested_models.append(repo_id)
        return original_local_model(repo_id, *args, **kwargs)
    inference.load_custom_model_from_hf = audited_local_model
    original_torch_load = torch.load
    def audited_torch_load(path, *args, **kwargs):
        if "rmvpe" in str(path).lower():
            raise RuntimeError("RMVPE weight access rejected")
        loaded_weights.append(str(path))
        return original_torch_load(path, *args, **kwargs)
    torch.load = audited_torch_load
    original_checkpoint = inference.load_checkpoint
    checkpoint_audit = {}

    def checked_checkpoint(model, optimizer, path, **kwargs):
        state = torch.load(path, map_location="cpu", weights_only=True)["net"]
        for name, module in model.items():
            weights = {key.removeprefix("module."): value for key, value in state.get(name, {}).items()}
            missing = [key for key, value in module.named_parameters()
                       if key not in weights or weights[key].shape != value.shape]
            checkpoint_audit[name] = {"missing_trainable_parameters": missing,
                                      "unused_checkpoint_keys": sorted(set(weights) - set(module.state_dict()))}
            if missing:
                raise ValueError(f"转换模型存在未载入的训练参数 {name}: {missing}")
        del state
        return original_checkpoint(model, optimizer, path, **kwargs)

    inference.load_checkpoint = checked_checkpoint
    original_load = inference.load_models
    feature_paths = []
    encoder_precision = {}

    def load_models(args):
        bundle = list(original_load(args))
        encoder_precision.update(prepare_cpu_whisper_encoder(bundle[1], inference.device.type))
        campplus = bundle[4]
        if request.get("reference_embedding"):
            cached = np.load(request["reference_embedding"], allow_pickle=False)
            if cached.shape != (1, 192) or not np.isfinite(cached).all():
                raise ValueError("缓存音色特征不合法")
            campplus.forward = lambda inputs: torch.from_numpy(cached).float().to(inference.device)
        original_f0 = bundle[2]
        f0_call = 0

        def f0_fn(*arguments, **keywords):
            nonlocal f0_call
            values = compatible_f0(original_f0(*arguments, **keywords), inference.device.type)
            path = output / ("reference-f0.npy" if f0_call == 0 else "source-f0.npy")
            np.save(path, values)
            feature_paths.append(str(path))
            f0_call += 1
            return values

        bundle[2] = f0_fn

        def record_embedding(module, inputs, result):
            path = output / "reference-campplus-embedding.npy"
            np.save(path, result.detach().float().cpu().numpy())
            if str(path) not in feature_paths:
                feature_paths.append(str(path))

        campplus.register_forward_hook(record_embedding)
        return bundle

    inference.load_models = load_models

    def save_audio(path, tensor, sample_rate, **kwargs):
        # torchaudio's newer save path requires TorchCodec; the official model
        # returns PCM tensors, so soundfile is used only for PCM serialization.
        samples = tensor.detach().float().cpu().numpy().T
        if not np.isfinite(samples).all():
            raise ValueError("转换模型返回非有限音频")
        sf.write(path, samples, sample_rate, subtype="PCM_24")

    inference.torchaudio.save = save_audio
    params = request.get("params", {})
    args = argparse.Namespace(source=request["input_path"], target=request["reference_path"],
                              output=str(output), diffusion_steps=params.get("steps", 30),
                              length_adjust=1.0, inference_cfg_rate=0.7, f0_condition=True,
                              auto_f0_adjust=False, semi_tone_shift=params.get("semitones", 0),
                              checkpoint=str(checkpoint), config=str(local_config), fp16=False)
    print('STUDIO_EVENT {"stage":"加载歌声音色转换模型","progress":0.05}', flush=True)
    inference.main(args)
    files = list(output.glob("vc_*.wav"))
    if len(files) != 1:
        raise ValueError("歌声转换未产生唯一有效输出")
    wave, sr = sf.read(files[0], dtype="float32", always_2d=True)
    if not len(wave) or not np.isfinite(wave).all():
        raise ValueError("转换后音频为空或含无效数值")
    metrics = process_metrics()
    report = {"files": [str(files[0])], "features": feature_paths, "engine": "Seed-VC 44.1k SVC v1",
              "code_revision": engine_audit["revision"], "engine_source_audit": engine_audit, "checkpoint_audit": checkpoint_audit,
              "device": str(inference.device), "device_selection": device_selection, "encoder_precision": encoder_precision,
              "seed": request.get("seed", 42),
              "elapsed_seconds": time.perf_counter() - started, "cpu_seconds": metrics["cpu_seconds"],
              "peak_rss_bytes": metrics["peak_rss_bytes"], "gpu_utilization": None, "duration": len(wave) / sr,
              "rms": float(np.sqrt(np.mean(wave**2))), "sample_rate": sr, "params": vars(args),
              "reference_provenance": request.get("reference_provenance"),
              "reference_embedding_reused": bool(request.get("reference_embedding")),
              "test_type":params.get('test_type','singing'),
              "speech_similarity_tested": False, "singing_similarity_tested": False,
              "note": "实际转换输出；是否像参考本人须独立试听验收，不能由生成成功推断"}
    report["pitch_backend"] = "torchfcpe 0.0.4 bundled / local_argmax / 10ms"
    report["pitch_device"] = os.environ.get("RADAR_FCPE_DEVICE", "cpu")
    report["requested_model_repositories"] = requested_models
    report["loaded_weights"] = loaded_weights
    report["rmvpe_weight_access_rejected"] = True
    (output / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
