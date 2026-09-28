# AgentBuild — Docker bundle

Everything runs locally in two containers: the app and Ollama, with **qwen3:4b** and **qwen3.5:9b** built in.
No Python, Ollama or model downloads are needed on this machine, and the models work offline.

## Install (once)
Requirements: x86-64 (amd64), Docker Engine with Compose 2.24+, 16 GB RAM recommended,
and ~25 GB free disk while installing (~17 GB after deleting `images.tar`).
On Windows, use Docker Desktop and run the commands in a WSL2 terminal.
```sh
./install.sh          # loads the images, creates .env, runs ./agent doctor
```

## Use
Every command from the project works through `./agent`:
```sh
./agent doctor
./agent pdf chat files/some.pdf                       # put PDFs in ./files
./agent bug new 12345 -t "Payments API order lookup fix"
./agent bug scan BUG-12345 files/asoc-report.pdf
./agent bug status BUG-12345
./agent qc run repos/my-service                       # put or clone repositories in ./repos
```

| Folder / file | What it is |
|---|---|
| `config/` | settings, guardrail policies, mail templates. Edit them freely; no rebuild needed |
| `data/` | database, drafts (`data/drafts/`), reports, traces, extracted PDFs. **Back this up** |
| `files/` | PDFs and ASoC reports you want the agent to read |
| `repos/` | repositories for `qc run` |
| `.env` | model choice, IMAP password, GPU on/off |

## CPU-only machines (no GPU)
This works out of the box. With no GPU, `./agent` automatically uses **qwen3:4b** for everything and 4× longer time limits.
Measured with CPU only (x86-64, 16 GB RAM):

| | 16 threads | 4 cores |
|---|---|---|
| PDF question (model loaded) | ~7 s | ~15 s |
| First command after start (loads the model) | ~20 s | ~80 s |
| ASoC report analysis | ~55 s | a few minutes |
| RAM used by the model container | ~4 GB | ~4 GB |

qwen3.5:9b also works on CPU (~35–40 s per question, ~7 GB RAM): set `AGENT_MODEL_MAIN=qwen3.5:9b` in `.env`.
Close other heavy apps on a 16 GB laptop when using it.

## GPU
`./agent` uses the GPU automatically when Docker has the NVIDIA runtime. Otherwise it uses the CPU.
This bundle is the **CPU variant** (smaller). Build the GPU variant with `FLAVOR=gpu scripts/export.sh 1.0-gpu`.
To enable the GPU on Linux, install the NVIDIA driver and then:
```sh
# Arch: sudo pacman -S nvidia-container-toolkit     Ubuntu/Debian: sudo apt install nvidia-container-toolkit
sudo nvidia-ctk runtime configure --runtime=docker && sudo systemctl restart docker
```
qwen3.5:9b needs about 7 GB of GPU memory. With less, or on CPU, set `AGENT_MODEL_MAIN=qwen3:4b` in `.env`.
On Windows, use Docker Desktop with the WSL2 backend (the GPU works with recent NVIDIA drivers) and run `./agent` from WSL.

## Per-machine tweaks
Create `compose.local.yaml` next to `./agent`; it is applied on top of `compose.yaml`. For example, to cap memory:
```yaml
services:
  ollama:
    mem_limit: 10g
```

## Update / uninstall
- New version: run the new bundle's `./install.sh`. Keep your `data/`, `config/` and `.env`.
- Remove: `docker compose --profile cli down -v`, then `docker image rm agentbuild-app:1.0 agentbuild-ollama:1.0`.
