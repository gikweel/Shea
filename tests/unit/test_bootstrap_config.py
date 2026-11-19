"""The composition root must actually consume effective configuration.

Before: `build_runtime` defaulted the filesystem roots to the workspace
*before* asking the config helper, so `SHEA_FILESYSTEM_ROOTS` (documented in
the README) and the whole layered resolver were dead code, `config_resolver`
was `None` at runtime behind a `cast`, and the helper's own fallback was the
process cwd — broader than the real default.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

import pytest

from shea.bootstrap import build_runtime
from shea.config.layers import ConfigLayer
from shea.config.resolver import ConfigResolver
from shea.security.filesystem_policy import is_path_allowed

KEY = "SHEA_FILESYSTEM_ROOTS"


def _runtime(tmp_path: Path, **kwargs: object):  # type: ignore[no-untyped-def]
    tmp_path.mkdir(parents=True, exist_ok=True)
    return build_runtime(tmp_path / "shea.db", workspace=tmp_path / "ws", **kwargs)  # type: ignore[arg-type]


def _roots(runtime: object) -> frozenset[str]:
    return runtime.filesystem_policy.allowed_roots  # type: ignore[attr-defined,no-any-return]


def test_with_no_configuration_the_roots_are_the_workspace_and_never_the_cwd(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path, config_resolver=ConfigResolver())
    assert _roots(runtime) == frozenset({str((tmp_path / "ws").resolve())})
    assert str(Path.cwd()) not in _roots(runtime)
    runtime.conn.close()


def test_the_runtime_holds_the_real_resolver_not_none(tmp_path: Path) -> None:
    injected = ConfigResolver()
    assert _runtime(tmp_path, config_resolver=injected).config_resolver is injected

    default = _runtime(tmp_path / "b")  # no resolver passed: built by the composition root
    assert isinstance(default.config_resolver, ConfigResolver)


def test_configured_roots_replace_the_workspace_default_and_are_enforced(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    resolver = ConfigResolver()
    resolver.set_layer_value(ConfigLayer.SESSION, KEY, f"{a}{os.pathsep}{b}")

    runtime = _runtime(tmp_path, config_resolver=resolver)

    assert _roots(runtime) == frozenset({str(a), str(b)})
    policy = runtime.filesystem_policy
    assert is_path_allowed(str(a / "x.txt"), policy)
    assert is_path_allowed(str(b / "deep" / "x.txt"), policy)
    assert not is_path_allowed(str(tmp_path / "ws" / "x.txt"), policy)  # workspace not listed
    assert not is_path_allowed(str(tmp_path / "other" / "x.txt"), policy)
    runtime.conn.close()


def test_a_list_from_a_config_file_layer_is_accepted(tmp_path: Path) -> None:
    resolver = ConfigResolver()
    resolver.set_layer_value(ConfigLayer.USER, KEY, [str(tmp_path / "a"), str(tmp_path / "b")])
    runtime = _runtime(tmp_path, config_resolver=resolver)
    assert _roots(runtime) == frozenset({str(tmp_path / "a"), str(tmp_path / "b")})
    runtime.conn.close()


def test_non_string_items_in_a_config_list_are_ignored_not_fatal(tmp_path: Path) -> None:
    resolver = ConfigResolver()
    resolver.set_layer_value(
        ConfigLayer.USER, KEY, [str(tmp_path / "a"), 5, None, {"x": 1}, str(tmp_path / "b")]
    )
    runtime = _runtime(tmp_path, config_resolver=resolver)
    assert _roots(runtime) == frozenset({str(tmp_path / "a"), str(tmp_path / "b")})
    runtime.conn.close()


def test_the_more_specific_layer_wins(tmp_path: Path) -> None:
    resolver = ConfigResolver()
    resolver.set_layer_value(ConfigLayer.USER, KEY, str(tmp_path / "from_user"))
    resolver.set_layer_value(ConfigLayer.SESSION, KEY, str(tmp_path / "from_session"))
    runtime = _runtime(tmp_path, config_resolver=resolver)
    assert _roots(runtime) == frozenset({str(tmp_path / "from_session")})
    runtime.conn.close()


def test_an_explicit_argument_beats_configuration(tmp_path: Path) -> None:
    resolver = ConfigResolver()
    resolver.set_layer_value(ConfigLayer.SESSION, KEY, str(tmp_path / "configured"))
    runtime = _runtime(tmp_path, config_resolver=resolver, filesystem_roots=[str(tmp_path / "chosen")])
    assert _roots(runtime) == frozenset({str(tmp_path / "chosen")})
    runtime.conn.close()


def test_an_explicitly_empty_roots_list_means_no_access_and_is_never_widened(
    tmp_path: Path,
) -> None:
    resolver = ConfigResolver()
    resolver.set_layer_value(ConfigLayer.SESSION, KEY, str(tmp_path / "configured"))
    runtime = _runtime(tmp_path, config_resolver=resolver, filesystem_roots=[])
    assert _roots(runtime) == frozenset()
    assert not is_path_allowed(str(tmp_path / "ws" / "x.txt"), runtime.filesystem_policy)
    runtime.conn.close()


def test_relative_roots_are_made_absolute_so_they_can_actually_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    resolver = ConfigResolver()
    resolver.set_layer_value(ConfigLayer.SESSION, KEY, "rel/dir")
    runtime = _runtime(tmp_path, config_resolver=resolver)
    assert _roots(runtime) == frozenset({str(tmp_path / "rel" / "dir")})
    runtime.conn.close()


@pytest.mark.parametrize("junk", [" ", os.pathsep, f" {os.pathsep} "])
def test_an_unusable_value_falls_back_to_the_workspace_and_says_so(
    junk: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    resolver = ConfigResolver()
    resolver.set_layer_value(ConfigLayer.SESSION, KEY, junk)
    with caplog.at_level(logging.WARNING, logger="shea.bootstrap"):
        runtime = _runtime(tmp_path, config_resolver=resolver)
    assert _roots(runtime) == frozenset({str((tmp_path / "ws").resolve())})
    assert "no usable paths" in caplog.text
    runtime.conn.close()


def test_the_documented_environment_variable_now_works_through_the_default_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """README: SHEA_FILESYSTEM_ROOTS = allowed roots, pathsep-separated."""
    monkeypatch.setenv(KEY, str(tmp_path / "from_env"))
    runtime = _runtime(tmp_path)  # no resolver passed: real default
    assert _roots(runtime) == frozenset({str(tmp_path / "from_env")})
    runtime.conn.close()


def test_user_config_file_is_read_and_the_environment_overrides_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    (home / ".shea").mkdir(parents=True)
    (home / ".shea" / "config.json").write_text(
        json.dumps({KEY: str(tmp_path / "from_user_file")})
    )
    monkeypatch.setattr(Path, "home", lambda: home)

    runtime = _runtime(tmp_path)
    assert _roots(runtime) == frozenset({str(tmp_path / "from_user_file")})
    runtime.conn.close()

    monkeypatch.setenv(KEY, str(tmp_path / "from_env"))
    runtime = _runtime(tmp_path / "second")
    assert _roots(runtime) == frozenset({str(tmp_path / "from_env")})
    runtime.conn.close()