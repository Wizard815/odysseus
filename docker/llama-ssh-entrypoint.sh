#!/bin/sh
# Entrypoint for the llama.cpp GPU sidecars (docker/llama-rocm.yml,
# docker/llama-cuda.yml). Trusts whatever pubkey Cookbook already generated
# for itself (mounted read-only at /run/secrets/cookbook_ssh_pubkey, sourced
# from the host's APP_DATA_DIR/ssh/id_ed25519.pub) so Cookbook's existing
# SSH-remote-exec machinery can reach this container without any separate
# key setup -- same trust model as any other Cookbook remote target.
set -e

PUBKEY_SRC="/run/secrets/cookbook_ssh_pubkey"
mkdir -p /root/.ssh
chmod 700 /root/.ssh

if [ -f "$PUBKEY_SRC" ]; then
    cp "$PUBKEY_SRC" /root/.ssh/authorized_keys
    chmod 600 /root/.ssh/authorized_keys
else
    echo "[llama-ssh] WARNING: $PUBKEY_SRC not found -- generate Cookbook's" >&2
    echo "[llama-ssh] SSH key first (Cookbook -> Settings -> SSH key), then" >&2
    echo "[llama-ssh] recreate this container so it can pick up the pubkey." >&2
fi

# Host keys are ephemeral by default (regenerated on every container
# recreate) since this sidecar isn't meant to be reached over anything but
# the internal Docker network. ssh-keygen -A only (re)generates ones that
# are missing, so this is a no-op on a container restart that kept its layer.
ssh-keygen -A >/dev/null 2>&1 || true

mkdir -p /run/sshd

exec /usr/sbin/sshd -D -e
