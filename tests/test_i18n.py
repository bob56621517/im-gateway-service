"""Tests for CLI i18n: detection, catalog coverage, and help completeness.

The language is resolved when ``im_gateway.cli.main`` is imported (Typer builds
all help strings then), so the Chinese tests reload ``i18n`` and ``main`` with
``IMGW_LANG=zh`` set, walk the Click command tree, and restore English
afterwards.  A few end-to-end tests run the real console script in a
subprocess with a scrubbed environment.
"""

from __future__ import annotations

import ast
import importlib
import os
import re
import string
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import click
import pytest
import typer.main

from im_gateway.cli import i18n

ROOT = Path(__file__).resolve().parents[1]
CLI_DIR = ROOT / "src" / "im_gateway" / "cli"
I18N_FILE = CLI_DIR / "i18n.py"
CJK_RE = re.compile(r"[\u4e00-\u9fff]")
LANGUAGE_ENVS = ("IMGW_LANG", "LC_ALL", "LC_MESSAGES", "LANG", "IMG_LANG")


# ---------------------------------------------------------------------------
# Language detection
# ---------------------------------------------------------------------------


def test_env_override_wins_over_locale() -> None:
    env = {"IMGW_LANG": "en", "LC_ALL": "zh_CN.UTF-8", "LANG": "zh_CN.UTF-8"}
    assert i18n.detect_language(env) == "en"
    env = {"IMGW_LANG": "zh", "LC_ALL": "en_US.UTF-8", "LANG": "en_US.UTF-8"}
    assert i18n.detect_language(env) == "zh"


def test_locale_priority_lc_all_then_lc_messages_then_lang() -> None:
    assert i18n.detect_language({"LC_ALL": "zh_CN.UTF-8", "LANG": "en_US.UTF-8"}) == "zh"
    assert i18n.detect_language({"LC_MESSAGES": "zh_CN.UTF-8", "LANG": "en_US.UTF-8"}) == "zh"
    assert i18n.detect_language({"LANG": "zh_CN.UTF-8"}) == "zh"
    assert i18n.detect_language({"LC_ALL": "en_US.UTF-8", "LC_MESSAGES": "zh_CN.UTF-8"}) == "en"


def test_empty_override_falls_through_to_next_variable() -> None:
    assert i18n.detect_language({"IMGW_LANG": "  ", "LC_ALL": "zh_CN.UTF-8"}) == "zh"


@pytest.mark.parametrize(
    "value",
    [
        "zh",
        "zh_CN",
        "zh-CN",
        "zh_TW.UTF-8",
        "ZH_cn",
        "Chinese (Simplified)_China",
        "Chinese (Traditional)_Taiwan",
        "936",
        "cp936",
        "GBK",
        "gb2312",
        "GB18030",
    ],
)
def test_chinese_locale_variants_normalise_to_zh(value: str) -> None:
    assert i18n.detect_language({"IMGW_LANG": value}) == "zh"


@pytest.mark.parametrize(
    "value",
    ["en", "en_US.UTF-8", "EN_us", "C", "POSIX", "fr_FR.UTF-8", "de-DE"],
)
def test_non_chinese_locale_variants_normalise_to_en(value: str) -> None:
    assert i18n.detect_language({"IMGW_LANG": value}) == "en"


def test_falls_back_to_windows_ui_language(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(i18n, "_windows_ui_language", lambda: "zh")
    monkeypatch.setattr(i18n.locale, "getlocale", lambda: ("en_US", "UTF-8"))
    assert i18n.detect_language({}) == "zh"


def test_falls_back_to_locale_getlocale(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(i18n, "_windows_ui_language", lambda: None)
    monkeypatch.setattr(i18n.locale, "getlocale", lambda: ("zh_CN", "UTF-8"))
    assert i18n.detect_language({}) == "zh"


def test_defaults_to_english_when_nothing_is_detected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(i18n, "_windows_ui_language", lambda: None)
    monkeypatch.setattr(i18n.locale, "getlocale", lambda: (None, None))
    assert i18n.detect_language({}) == "en"


def test_locale_getlocale_failure_defaults_to_english(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> tuple[str, str]:
        raise ValueError("no locale")

    monkeypatch.setattr(i18n, "_windows_ui_language", lambda: None)
    monkeypatch.setattr(i18n.locale, "getlocale", boom)
    assert i18n.detect_language({}) == "en"


def test_img_lang_typo_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(i18n, "_windows_ui_language", lambda: None)
    monkeypatch.setattr(i18n.locale, "getlocale", lambda: (None, None))
    assert i18n.detect_language({"IMG_LANG": "zh"}) == "en"
    # The typo must not shadow a real locale variable either.
    assert i18n.detect_language({"IMG_LANG": "zh", "LANG": "zh_CN.UTF-8"}) == "zh"


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


def _zh_catalog() -> dict[str, str]:
    return i18n.CATALOGS["zh"]


def _t_literals() -> list[tuple[Path, int, str]]:
    """Every ``t("literal")`` call in the CLI package, via AST."""
    literals: list[tuple[Path, int, str]] = []
    for path in sorted(CLI_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if not isinstance(node.func, ast.Name) or node.func.id != "t":
                continue
            assert node.args, f"t() without arguments at {path}:{node.lineno}"
            first = node.args[0]
            assert isinstance(first, ast.Constant) and isinstance(first.value, str), (
                f"t() message id must be a string literal at {path}:{node.lineno}"
            )
            literals.append((path, node.lineno, first.value))
    return literals


def test_every_t_literal_has_a_chinese_translation() -> None:
    catalog = _zh_catalog()
    literals = _t_literals()
    assert len(literals) > 100
    missing = sorted({msgid for _, _, msgid in literals if msgid not in catalog})
    assert not missing, f"missing Chinese translations: {missing[:5]}"
    extra = sorted(set(catalog) - {msgid for _, _, msgid in literals})
    assert not extra, f"catalog entries no longer used: {extra[:5]}"


def test_catalog_is_large_enough_for_full_help_coverage() -> None:
    assert len(_zh_catalog()) >= 150


def test_english_and_chinese_catalogs_stay_in_parity() -> None:
    assert set(i18n.CATALOGS["en"]) == set(i18n.CATALOGS["zh"])
    assert all(msgid == value for msgid, value in i18n.CATALOGS["en"].items())


def test_catalog_has_no_duplicate_keys() -> None:
    tree = ast.parse(I18N_FILE.read_text(encoding="utf-8"))
    dict_node: ast.Dict | None = None
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "_ZH_CATALOG"
            and isinstance(node.value, ast.Dict)
        ):
            dict_node = node.value
    assert dict_node is not None, "could not find _ZH_CATALOG in i18n.py"
    keys: list[str] = []
    for key in dict_node.keys:
        assert isinstance(key, ast.Constant) and isinstance(key.value, str)
        keys.append(key.value)
    duplicates = {key for key in keys if keys.count(key) > 1}
    assert not duplicates, f"duplicate msgids: {duplicates}"


def test_catalog_keeps_format_placeholders_and_is_translated() -> None:
    catalog = _zh_catalog()
    fmt = string.Formatter().parse
    translated = 0
    for msgid, value in catalog.items():
        en_fields = [name for _, name, _, _ in fmt(msgid) if name]
        zh_fields = [name for _, name, _, _ in fmt(value) if name]
        assert en_fields == zh_fields, (msgid, en_fields, zh_fields)
        assert value != msgid or CJK_RE.search(value) is not None, msgid
        if CJK_RE.search(value):
            translated += 1
    # A handful of short identifier-only labels ("feishu/qq: app_id.") stay
    # ASCII; all real prose is translated.
    assert translated >= len(catalog) - 10


def test_unknown_msgid_falls_back_to_msgid_in_both_languages() -> None:
    for language in ("en", "zh"):
        i18n.set_language(cast("i18n.Language", language))
        assert i18n.t("no such message id") == "no such message id"
    i18n.set_language("en")


def test_t_formats_only_when_kwargs_are_given() -> None:
    i18n.set_language("en")
    assert i18n.t("status: {status}", status="confirmed") == "status: confirmed"
    assert i18n.t("literal {braces} stay") == "literal {braces} stay"
    i18n.set_language("zh")
    assert i18n.t("status: {status}", status="confirmed") == "状态：confirmed"
    i18n.set_language("en")


def test_set_and_get_language_round_trip() -> None:
    try:
        i18n.set_language("zh")
        assert i18n.get_language() == "zh"
        i18n.set_language("en")
        assert i18n.get_language() == "en"
    finally:
        i18n.set_language("en")


def test_set_language_rejects_unknown_language() -> None:
    with pytest.raises(ValueError):
        i18n.set_language(cast("i18n.Language", "fr"))


# ---------------------------------------------------------------------------
# Help completeness through the Click command tree
# ---------------------------------------------------------------------------


def _main_module() -> Any:
    return importlib.import_module("im_gateway.cli.main")


def _root_command(module: Any) -> click.Command:
    return cast(click.Command, typer.main.get_command(module.app))


def _walk(command: click.Command) -> Iterator[click.Command]:
    yield command
    sub_commands = getattr(command, "commands", None)
    if sub_commands:
        for sub_command in sub_commands.values():
            yield from _walk(sub_command)


def _documented_params(command: click.Command) -> list[click.Parameter]:
    # Click adds its own ``--help`` option; keep it out of the completeness
    # assertion because it is framework chrome, not our prose.
    return [param for param in command.params if param.name != "help"]


def _find(root: click.Command, path: str) -> click.Command:
    current = root
    for part in path.split():
        sub_commands = getattr(current, "commands", None)
        assert sub_commands is not None, f"{path!r}: {part!r} is not a group"
        current = sub_commands[part]
    return current


def _assert_help_language(root: click.Command, language: str) -> None:
    for command in _walk(root):
        label = command.name or "<root>"
        assert command.help, f"{language}: {label} has no help text"
        assert command.epilog, f"{language}: {label} has no example epilog"
        texts = [command.help, command.epilog]
        texts.extend(param.help or "" for param in _documented_params(command))
        cjk = [CJK_RE.search(text) is not None for text in texts]
        if language == "zh":
            assert all(cjk), f"zh: untranslated help for {label}"
        else:
            assert not any(cjk), f"en: unexpected CJK in help for {label}"
        if command.name == "imgw":
            # The root command uses "Quick start" / "快速开始" rather than an
            # examples header; leaf commands and sub-groups use "Examples:".
            continue
        if language == "zh":
            assert "示例" in (command.epilog or ""), f"zh: {label} has no Chinese examples header"
        else:
            assert "Examples:" in (command.epilog or ""), f"en: {label} has no examples header"


def test_english_help_is_complete() -> None:
    assert i18n.get_language() == "en"
    root = _root_command(_main_module())
    _assert_help_language(root, "en")


def test_english_root_help_documents_env_vars_and_exit_codes() -> None:
    root = _root_command(_main_module())
    text = f"{root.help}\n{root.epilog}"
    for token in ("IMGW_URL", "IMGW_TOKEN", "IMGW_TOKEN_FILE", "IMGW_LANG", "Exit codes", "Quick start"):
        assert token in text


def test_english_channel_login_help_covers_every_mode_and_platform() -> None:
    login = _find(_root_command(_main_module()), "channel login")
    text = f"{login.help}\n{login.epilog}"
    for token in (
        "--rescan",
        "--no-wait",
        "--session-id",
        "--app-id",
        "--app-secret",
        "--bot-id",
        "--app-key",
        "--account-id",
        "--base-url",
        "feishu",
        "wecom",
        "dingtalk",
        "qq",
        "weixin",
        "QR login",
        "silent registration",
        "reconnect",
    ):
        assert token in text, token


def test_english_channel_delete_help_mentions_logout_and_purge() -> None:
    delete = _find(_root_command(_main_module()), "channel delete")
    text = f"{delete.help}\n{delete.epilog}"
    for token in ("logged out first", "409", "--purge", "--yes"):
        assert token in text, token


@pytest.fixture()
def zh_cli_module() -> Iterator[Any]:
    """Reload the CLI with ``IMGW_LANG=zh`` and restore English afterwards."""
    previous = os.environ.get("IMGW_LANG")
    os.environ["IMGW_LANG"] = "zh"
    importlib.reload(i18n)
    module = importlib.reload(_main_module())
    try:
        yield module
    finally:
        if previous is None:
            os.environ.pop("IMGW_LANG", None)
        else:
            os.environ["IMGW_LANG"] = previous
        importlib.reload(i18n)
        importlib.reload(_main_module())
        i18n.set_language("en")


def test_chinese_help_is_complete(zh_cli_module: Any) -> None:
    assert i18n.get_language() == "zh"
    root = _root_command(zh_cli_module)
    _assert_help_language(root, "zh")


def test_chinese_help_representative_samples(zh_cli_module: Any) -> None:
    root = _root_command(zh_cli_module)
    login = _find(root, "channel login")
    login_text = f"{login.help}\n{login.epilog}"
    for token in ("扫码登录", "--rescan", "--no-wait", "--session-id", "feishu", "weixin", "示例"):
        assert token in login_text, token
    delete_text = f"{_find(root, 'channel delete').help}\n{_find(root, 'channel delete').epilog}"
    assert "409" in delete_text
    assert "登出" in delete_text
    root_text = f"{root.help}\n{root.epilog}"
    for token in ("快速开始", "环境变量", "退出码", "IMGW_LANG"):
        assert token in root_text, token
    # Data identifiers/headers stay English in the Chinese build.
    assert zh_cli_module.CHANNEL_TABLE_HEADERS == [
        "CHANNEL_ID",
        "KIND",
        "TENANT",
        "ALIAS",
        "ENABLED",
        "STATE",
        "NAME",
    ]
    assert zh_cli_module.HISTORY_TABLE_HEADERS[0] == "TS"


# ---------------------------------------------------------------------------
# End-to-end: the real console script with a scrubbed environment
# ---------------------------------------------------------------------------


def _cli_argv() -> list[str]:
    executable = Path(sys.executable).with_name("imgw.exe" if os.name == "nt" else "imgw")
    if executable.exists():
        return [str(executable)]
    return [sys.executable, "-c", "from im_gateway.cli.main import main; main()"]


def _closed_env(**overrides: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in LANGUAGE_ENVS}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(overrides)
    return env


def _run_cli(arguments: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*_cli_argv(), *arguments],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=180,
        check=False,
    )


def test_subprocess_help_is_chinese_with_imgw_lang_zh() -> None:
    result = _run_cli(["--help"], _closed_env(IMGW_LANG="zh"))
    assert result.returncode == 0, result.stderr
    assert CJK_RE.search(result.stdout)
    assert "快速开始" in result.stdout
    assert "Examples:" not in result.stdout


def test_subprocess_help_is_english_with_imgw_lang_en() -> None:
    result = _run_cli(["--help"], _closed_env(IMGW_LANG="en"))
    assert result.returncode == 0, result.stderr
    assert "Quick start" in result.stdout
    assert not CJK_RE.search(result.stdout)


def test_subprocess_help_detects_chinese_locale() -> None:
    result = _run_cli(["--help"], _closed_env(LC_ALL="zh_CN.UTF-8"))
    assert result.returncode == 0, result.stderr
    assert "快速开始" in result.stdout


def test_subprocess_channel_login_help_is_chinese() -> None:
    result = _run_cli(["channel", "login", "--help"], _closed_env(IMGW_LANG="zh_CN"))
    assert result.returncode == 0, result.stderr
    assert "扫码登录" in result.stdout
    assert "--session-id" in result.stdout
    # The no-wrap example block must survive the Chinese build.
    assert "$ imgw channel login weixin:wx --qr-png ./weixin-qr.png" in result.stdout


def test_subprocess_img_lang_typo_is_ignored() -> None:
    result = _run_cli(["--help"], _closed_env(IMG_LANG="zh", LANG="en_US.UTF-8"))
    assert result.returncode == 0, result.stderr
    assert "Quick start" in result.stdout
    assert not CJK_RE.search(result.stdout)
