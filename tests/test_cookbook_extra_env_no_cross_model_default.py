"""Regression guard for Cookbook's "Env" and "Extra args" fields leaking
across models.

Every Serve-panel field except these two intentionally falls back to
whatever was last used on ANY model (localStorage/`cookbook_state.json`
`_lastUsed`) as a "sensible first-run default". For custom env-var flags and
raw extra CLI flags that's actively harmful: a fork-specific or
hardware-specific value typed for one model (e.g. GPU_MAX_HW_QUEUES=8, or
`-cram 51200 --jinja`) silently became the default value on every other
model that hadn't been configured yet, and kept reappearing after being
manually cleared because relaunching any model re-wrote the shared
`_lastUsed` fallback. Both fields must only ever read from this model's own
saved config (`svm`), never the cross-model `sv(...)` fallback.
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


def test_extra_args_field_does_not_read_cross_model_last_used_fallback():
    assert "data-field=\"extra\"" in _SRC and "sv('extra', '')" not in _SRC, (
        "The Extra args field default must not read sv('extra', ...) — "
        "that leaks one model's raw CLI flags into every other model's default."
    )


def test_extra_args_field_still_reads_this_models_own_saved_value():
    assert "svm('extra', '')" in _SRC, (
        "The Extra args field should still default to this model's own saved "
        "value via svm() — only the cross-model sv() fallback should be excluded."
    )
