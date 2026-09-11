# NVIDIA llama.cpp GPU sidecar, reachable over SSH so Cookbook's existing
# remote-exec machinery drives it exactly like it would drive "Local" --
# GGUF picker, Launch/Stop, all the Advanced flags, tmux-tracked lifecycle.
# See docker/llama-cuda.yml for how this gets built and run, and
# routes/cookbook_routes.py's model_serve() for the "Local" + CUDA ->
# this sidecar redirect (ODYSSEUS_LOCAL_LLAMA_CUDA_HOST).
#
# Base is the official upstream CUDA server image
# (https://github.com/ggml-org/llama.cpp/blob/master/docs/docker.md) --
# this just adds an SSH server on top, nothing GPU-related changes.
FROM ghcr.io/ggml-org/llama.cpp:server-cuda

RUN apt-get update && apt-get install -y --no-install-recommends openssh-server \
    && rm -rf /var/lib/apt/lists/*

# The base image's own ENTRYPOINT invokes /app/llama-server by absolute path
# and never puts /app on PATH. Cookbook's remote-target detection is just
# `shutil.which("llama-server")` run over a plain non-interactive SSH command
# (routes/shell_routes.py) -- that shell never sources .bashrc/.profile, so
# an ENV PATH addition here would be invisible to it. A symlink into a
# directory already on sshd's default PATH is the only thing that works.
RUN ln -s /app/llama-server /usr/local/bin/llama-server

# Ubuntu's default sshd_config already ships `PermitRootLogin prohibit-password`
# -- pubkey auth works, password auth (root has none set) does not. No config
# change needed, only the pubkey itself (installed by the entrypoint below).
COPY docker/llama-ssh-entrypoint.sh /usr/local/bin/llama-ssh-entrypoint.sh
RUN chmod +x /usr/local/bin/llama-ssh-entrypoint.sh

EXPOSE 22
ENTRYPOINT ["/usr/local/bin/llama-ssh-entrypoint.sh"]
