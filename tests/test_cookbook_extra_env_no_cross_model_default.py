"""Regression guard for the Cookbook "Env" field leaking across models.

Every Serve-panel field except Env intentionally falls back to whatever was
last used on ANY model (localStorage/`cookbook_state.json` `_lastUsed`) as a
"sensible first-run default". For custom env-var flags that's actively
harmful: a fork-specific or hardware-specific flag typed for one model (e.g.
GPU_MAX_HW_QUEUES=8) silently became the default Env value on every other
model that hadn't been configured yet, and kept reappearing after being
manually cleared because relaunching any model re-wrote the shared
`_lastUsed` fallback. The Env field must only ever read from this model's
own saved config (`svm`), never the cross-model `sv(...)` fallback.
"""
from pathlib import Path

_SRC = (Path(__file__).resolve().parents[1] / "static" / "js" / "cookbookServe.js").read_text(encoding="utf-8")


def test_extra_env_field_does_not_read_cross_model_last_used_fallback():
    assert "svm('extra_env', sv('extra_env'" not in _SRC, (
        "The Env field default must not fall through to sv('extra_env', ...) — "
        "that reintroduces the cross-model last-used leak."
    )


def test_extra_env_field_still_reads_this_models_own_saved_value():
    assert "svm('extra_env', '')" in _SRC, (
        "The Env field should still default to this model's own saved extra_env "
        "via svm() — only the cross-model sv() fallback should be excluded."
    )
