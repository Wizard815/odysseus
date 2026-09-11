<p align="center">
  <img src="assets/branding/odysseus-wordmark.png" alt="Odysseus" width="238">
</p>

<p align="center">
  A self-hosted AI workspace for chat, agents, research, documents, email, notes, calendar, and local model workflows.
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> ·
  <a href="website/setup.md">Setup Guide</a> ·
  <a href="CONTRIBUTING.md">Contributing</a> ·
  <a href="ROADMAP.md">Roadmap</a>
</p>

<p align="center">
  <a href="https://repology.org/project/odysseus-ai/versions"><img src="https://repology.org/badge/vertical-allrepos/odysseus-ai.svg" alt="Packaging status"></a>
</p>

<p align="center">
  <img src="assets/branding/odysseus-browser.jpg" alt="Odysseus interface">
</p>

---

## Quick Start

> `dev` is the default branch and gets the newest changes first. Use [`main`](https://github.com/odysseus-dev/odysseus/tree/main) if you want the more curated branch.

```bash
git clone https://github.com/odysseus-dev/odysseus.git
cd odysseus
cp .env.example .env
docker compose up -d --build
```

Open `http://localhost:7000` when the containers are healthy. The first admin password is printed in `docker compose logs odysseus`.

Native installs, GPU notes, Windows/macOS instructions, HTTPS, and configuration live in the [setup guide](website/setup.md).

## GPU Support

Odysseus's own image is deliberately slim — it doesn't bundle a GPU toolchain. There are two ways to get GPU-accelerated inference talking to it:

**1. Recommended: run the inference engine as its own sidecar container.** Point a prebuilt, hardware-tuned `llama-server` (or vLLM, etc.) image at your GPU, then register its OpenAI-compatible URL as a **Model Endpoint** in Odysseus (Settings → Model Endpoints). Odysseus never touches the GPU directly — it just talks HTTP to the sidecar. This is faster to start (no first-serve compile), gets you hardware-tuned kernels instead of a generic from-source build, and keeps the app image free of GPU toolchains.

- **AMD gfx906 (MI50/MI60/Radeon VII):** [`zenth815/mx-llama-rocm10-gfx906`](https://hub.docker.com/r/zenth815/mx-llama-rocm10-gfx906) — a gfx906-tuned llama.cpp fork built on ROCm 10, since gfx906 isn't officially supported past ROCm 6.
- **NVIDIA:** the official [`ghcr.io/ggml-org/llama.cpp:server-cuda`](https://github.com/ggml-org/llama.cpp/blob/master/docs/docker.md) image — no custom build needed, NVIDIA is officially supported upstream.

See `docker/llama-rocm.yml` / `docker/llama-cuda.yml` for example sidecar service definitions.

**2. Fallback: let Cookbook install/compile an engine at runtime.** Cookbook can still `pip install` ROCm/CUDA-compatible vLLM or llama-cpp-python wheels, or compile llama.cpp from source, directly inside the Odysseus container — useful for a model/flag combo that doesn't have a prebuilt image yet. This needs the host GPU device(s) passed through:

| Setup | Overlay | Requirement |
|---|---|---|
| NVIDIA | `docker/gpu.nvidia.yml` | nvidia-container-toolkit |
| AMD ROCm | `docker/gpu.amd.yml` | ROCm drivers, render GID |
| AMD + NVIDIA | `docker/gpu.amd-nvidia.yml` | Both of the above |

```bash
# Find your render group GID
getent group render | cut -d: -f3   # usually 18 on Unraid

# .env
COMPOSE_FILE=docker-compose.yml:docker/gpu.amd-nvidia.yml
RENDER_GID=18
```

## Features

- **Chat + Agents** — local/API models, tools, MCP, files, shell, skills, and memory.
- **Cookbook** — hardware-aware model recommendations, downloads, and serving.
- **Deep Research** — multi-step web research with source reading and report generation.
- **Compare** — blind side-by-side model testing and synthesis.
- **Documents** — writing-first editor with AI edits, suggestions, Markdown, HTML, CSV, and syntax highlighting.
- **Email** — IMAP/SMTP inbox with triage, tags, summaries, reminders, and reply drafts.
- **Notes, Tasks + Calendar** — reminders, todos, scheduled agent tasks, and CalDAV sync.
- **Extras** — gallery/image editor, themes, uploads, web search, presets, sessions, and 2FA.

## Demo

A full hover-to-play tour lives on the [Odysseus landing page](https://odysseus-dev.github.io/odysseus/). Its source lives under [`website/`](website/).

## Contributing

Help is welcome. The best entry points are fresh-install testing, provider setup bugs, mobile/editor polish, docs, and small focused refactors. See [CONTRIBUTING.md](CONTRIBUTING.md) and [ROADMAP.md](ROADMAP.md).

## Security

Odysseus is a self-hosted workspace with powerful local tools. Keep auth enabled, keep private data out of Git, and do not expose raw model/service ports publicly.

- Keep `AUTH_ENABLED=true` for any network-accessible deployment.
- Keep `LOCALHOST_BYPASS=false` outside local development.

Deployment details are in the [setup guide](website/setup.md#security-notes).

## Star History

<a href="https://star-history.dera.page/#odysseus-dev/odysseus&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://star-history.dera.page/svg?repos=odysseus-dev/odysseus&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://star-history.dera.page/svg?repos=odysseus-dev/odysseus&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://star-history.dera.page/svg?repos=odysseus-dev/odysseus&type=date&legend=top-left" />
 </picture>
</a>

## Credits

This fork (`rocmcuda` branch) builds on the work of several contributors and community projects.

**Upstream project**

- [pewdiepie-archdaemon/odysseus](https://github.com/pewdiepie-archdaemon/odysseus) — the original Odysseus self-hosted AI workspace this fork is based on.

**Merged pull requests from upstream**

- [#4521](https://github.com/pewdiepie-archdaemon/odysseus/pull/4521) — MCP integration
- [#4250](https://github.com/pewdiepie-archdaemon/odysseus/pull/4250) — Initial ROCm support

**AMD gfx906 / MI50 llama.cpp community**

The Cookbook GPU bootstrap and build flags in this branch draw from the work of these projects:

- [iacopPBK/llama.cpp-gfx906](https://github.com/iacopPBK/llama.cpp-gfx906) — Wave64 kernel implementations for gfx906: DPP warp reductions, Q8 FlashAttention, vectorized loads, fused RoPE, and custom SGEMM/MMF kernels.
- [arte-fact/llamacpp-gfx-906-turbo](https://github.com/arte-fact/llamacpp-gfx-906-turbo) — combines iacopPBK Wave64 kernels with TurboQuant KV cache compression and 9 HIP-specific correctness fixes for gfx906.
- [moriyasujapan/llamacpp-gfx-906-turbo-gemma4](https://github.com/moriyasujapan/llamacpp-gfx-906-turbo-gemma4) — extends the above with Gemma 4 support, fused MoE kernels, turbo3 speed improvements, and TP4 ROCm split-mode fix.

## License

AGPL-3.0-or-later -- see [LICENSE](LICENSE) and [ACKNOWLEDGMENTS.md](ACKNOWLEDGMENTS.md).
