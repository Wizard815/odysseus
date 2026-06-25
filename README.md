<p align="center">
  <img src="docs/odysseus-wordmark.png" alt="Odysseus" width="238">
</p>

<p align="center">
  A self-hosted AI workspace for chat, agents, research, documents, email, notes, calendar, and local model workflows.
</p>

<p align="center">
  <a href="#quick-start">Quick Start</a> ·
  <a href="docs/setup.md">Setup Guide</a> ·
  <a href="CONTRIBUTING.md">Contributing</a> ·
  <a href="ROADMAP.md">Roadmap</a>
</p>

<p align="center">
  <a href="https://repology.org/project/odysseus-ai/versions"><img src="https://repology.org/badge/vertical-allrepos/odysseus-ai.svg" alt="Packaging status"></a>
</p>

<p align="center">
  <img src="docs/odysseus-browser.jpg" alt="Odysseus interface">
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

Native installs, GPU notes, Windows/macOS instructions, HTTPS, and configuration live in the [setup guide](docs/setup.md).

## GPU Support

Odysseus ships compose overlays for NVIDIA, AMD ROCm, and dual AMD+NVIDIA setups. Set `COMPOSE_FILE` in your `.env` to activate one — no code changes needed.

| Setup | Overlay | Requirement |
|---|---|---|
| NVIDIA | `docker/gpu.nvidia.yml` | nvidia-container-toolkit |
| AMD ROCm | `docker/rocm-overlay.gpu.amd.yml` | ROCm drivers, render GID |
| AMD + NVIDIA | `docker/gpu.amd-nvidia.yml` | Both of the above |

**AMD / dual-GPU quick start:**

```bash
# Find your render group GID
getent group render | cut -d: -f3   # usually 18 on Unraid

# .env
COMPOSE_FILE=docker-compose.yml:docker/gpu.amd-nvidia.yml
RENDER_GID=18
ROCM_VERSION=6.1.2   # 6.2.4 for RDNA2, 6.3.4 for RDNA3
```

The ROCm overlays build a custom Ubuntu 22.04 + ROCm image on top of the published Odysseus image. On first serve, the Cookbook auto-detects your GPU (gfx906/MI50, RDNA2, RDNA3, NVIDIA) and compiles the right llama.cpp backend — binaries are cached in `APP_DATA_DIR` so rebuilds and container recreates don't wipe them.

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

A full hover-to-play tour lives on the landing page: [`docs/index.html`](docs/index.html).

## Contributing

Help is welcome. The best entry points are fresh-install testing, provider setup bugs, mobile/editor polish, docs, and small focused refactors. See [CONTRIBUTING.md](CONTRIBUTING.md) and [ROADMAP.md](ROADMAP.md).

## Security

Odysseus is a self-hosted workspace with powerful local tools. Keep auth enabled, keep private data out of Git, and do not expose raw model/service ports publicly. Deployment details are in the [setup guide](docs/setup.md#security-notes).

## Star History

<a href="https://www.star-history.com/?repos=odysseus-dev%2Fodysseus&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/chart?repos=odysseus-dev/odysseus&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/chart?repos=odysseus-dev/odysseus&type=date&legend=top-left" />
   <img alt="Star History Chart" src="https://api.star-history.com/chart?repos=odysseus-dev/odysseus&type=date&legend=top-left" />
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
