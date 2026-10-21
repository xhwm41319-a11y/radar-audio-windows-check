# RADAR CoverLab Windows CPU candidate acceptance

This repository is a reproducible **test candidate**, not a Windows-certified release. The product is intended for separate local installations on different users' computers. It is not a shared public audio-processing server.

## What this workflow does

Only an explicit `workflow_dispatch` starts the test. It uses standard Windows Server 2025 and 2022 x64 runners, one at a time. The 2025 case also uses a short directory containing spaces and Chinese characters. Checkout and artifact actions are pinned to exact commits. Workflow permissions are read-only; checkout credentials are not retained.

1. Check the actual installation-drive space before downloading. The frozen payload is about 3.28 GiB; the product still requires 10 GiB free after reconstruction, with a 512 MiB preparation margin. Insufficient space fails clearly without removing runner software or changing the product requirement.
2. Reconstruct the exact 3,056 payload files of Windows CPU candidate 2. Small sources are committed here; 79 fixed files and four public archives are downloaded by immutable version/revision and verified size/SHA256. Downloads total about 3.37 GB. Git attributes preserve original file bytes. The original ZIP extraction is **not** tested by reconstruction.
3. Run the bundled Windows x64 Python 3.12.15, install from the fixed offline wheelhouse, and run the packaged native lock/process-tree cancellation checks.
4. Generate two three-second harmonic signals and run actual CPU voice conversion at eight diffusion steps. Check the returned device, parameters, measured time, fresh output identity, duration, PCM layout and audible RMS. No real voice or customer input is used. Runtime inference disables network access.
5. Retain bounded logs, source hashes, machine configuration and synthetic output for one day. Named installer logs are collected even after a failed step. A remaining `running` result becomes `incomplete`, never `passed`; process termination is not claimed if it was not confirmed.

The job has a 60-minute ceiling: preparation is limited to 12 minutes and native acceptance to 43 minutes. Individual installation, native-contract and conversion ceilings are 1,200, 120 and 900 seconds. A terminated runner may still prevent artifact delivery; an absent artifact is not a pass. Public standard runner execution is subject to the account's available Actions access and GitHub's service limits. This workflow does not enable billing or paid runners.

## Exact candidate

Original candidate ZIP SHA256:

`6e0bed99d74b6ac2a745dfe46533972deac2ff24d6f97d0f271c5eaaca1b33e0`

The full payload manifest is `candidate-files/payload-sha256.json`; identity is in `candidate-identity.json`. `download-plan.json` lists every public input, version/revision, archive-member mapping, size and SHA256. No model or wheel is silently substituted if an upstream URL disappears.

## Run manually on an authorized Windows x64 host

Use a fresh short local destination; the installer does not elevate privileges or alter long-path settings. Microsoft Visual C++ x64 runtime is a host prerequisite. Rebuilding downloads public dependencies; the later installation and inference are offline.

```powershell
python scripts/materialize_candidate.py --target C:\RA --cache C:\RA-cache
& C:\RA\.bootstrap\python\python.exe -X utf8 -B scripts/run_native_acceptance.py --bundle C:\RA --result C:\RA-test-result
python scripts/collect_evidence.py --bundle C:\RA --result C:\RA-test-result
```

Exit code 0 from **native acceptance**, together with its terminal `passed` summary, is a technical smoke result for the recorded host. Exit 3 is skipped, 4 from space preflight is insufficient space; other nonzero results fail. A successful materialization only proves the frozen payload bytes, not installation or native execution.

## Limits

Windows Server runs do not establish Windows 10/11 desktop-browser compatibility, low-memory operation, a full song, perceived audio quality, reference-voice similarity, NVIDIA CUDA acceleration, ARM support or customer adoption. Client Windows and hardware tiers need separate measured acceptance. The CPU package uses CPU on machines with NVIDIA, AMD or Intel graphics; a future NVIDIA build needs its own CUDA/driver compatibility evidence.

The already-tested Apple Silicon macOS build remains separate. This repository does not modify that build, any production Bot, existing web deployment, accounts or funds. It sends no mail or posts.

## Source and notices

Test scripts and project adapters are GPL-3.0-only; the GPL text is in `LICENSE`. Frozen upstream source and third-party notices are preserved under `candidate-files/`. Downloaded runtimes, wheels and models retain their own upstream terms and notices. This technical test is not a blanket rights audit or a commercial-release certificate. Historical notes inside the frozen candidate are preserved verbatim for byte identity; this README defines the scope of this test.
