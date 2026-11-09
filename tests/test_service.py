"""Tests for the system-service layer (``im_gateway.cli.service``).

No real systemd / launchd / NSSM is used: every platform is selected through
the injectable ``platform`` / ``runner`` seams. The CLI tests also inject the
platform and either run in ``--dry-run`` mode (no side effects) or replace
``ServiceController`` with a fake.
"""

from __future__ import annotations

import json
import plistlib
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from typer.testing import CliRunner

from im_gateway.cli import main as cli
from im_gateway.cli import service

SPEC = service.ServiceSpec(exec_path="/opt/imgw/bin/imgw", workdir="/srv/im-gateway")

runner = CliRunner()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeRunner:
    """Records argv lists and pretends every command succeeded."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, command: list[str]) -> subprocess.CompletedProcess[str]:
        self.calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, "", "")


@pytest.fixture()
def fake_runner() -> FakeRunner:
    return FakeRunner()


def _failing_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    raise subprocess.CalledProcessError(3, command, "", "Unit im-gateway.service not found.")


def _missing_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    raise FileNotFoundError(2, "No such file or directory", command[0])


# ---------------------------------------------------------------------------
# systemd unit
# ---------------------------------------------------------------------------


def test_systemd_unit_user_scope() -> None:
    unit = service.systemd_unit(SPEC)
    assert "[Unit]" in unit
    assert "[Service]" in unit
    assert "Type=simple" in unit
    assert f"ExecStart={SPEC.exec_path} run" in unit
    assert f"WorkingDirectory={SPEC.workdir}" in unit
    assert "Restart=on-failure" in unit
    assert "RestartSec=5" in unit
    assert "[Install]" in unit
    # Empty user => user-scoped manager, so default.target is the right target.
    assert "WantedBy=default.target" in unit
    assert "User=" not in unit


def test_systemd_unit_system_scope_has_user_and_multi_user_target() -> None:
    unit = service.systemd_unit(replace(SPEC, user="imgw"))
    assert "User=imgw" in unit
    assert "WantedBy=multi-user.target" in unit
    assert "WantedBy=default.target" not in unit


def test_systemd_unit_env_file_and_overrides() -> None:
    spec = replace(SPEC, env_file="/etc/im-gateway.env", log_level="debug", restart_seconds=10)
    unit = service.systemd_unit(spec)
    assert "EnvironmentFile=/etc/im-gateway.env" in unit
    assert "RestartSec=10" in unit
    assert "IMGW_LOG_LEVEL=debug" in unit


def test_systemd_unit_quotes_exec_path_with_spaces() -> None:
    spec = replace(SPEC, exec_path="/opt/im gateway/bin/imgw")
    unit = service.systemd_unit(spec)
    assert 'ExecStart="/opt/im gateway/bin/imgw" run' in unit


# ---------------------------------------------------------------------------
# launchd plist
# ---------------------------------------------------------------------------


def test_launchd_plist_is_parseable_and_complete() -> None:
    payload = plistlib.loads(service.launchd_plist(SPEC).encode("utf-8"))
    assert payload["Label"] == "com.imgw.gateway"
    arguments = payload["ProgramArguments"]
    assert arguments[0] == SPEC.exec_path
    assert arguments[-1] == "run"
    assert payload["RunAtLoad"] is True
    assert payload["KeepAlive"]["SuccessfulExit"] is False
    assert payload["WorkingDirectory"] == SPEC.workdir
    # No log directory supplied => the optional paths are omitted.
    assert "StandardOutPath" not in payload
    assert "StandardErrorPath" not in payload


def test_launchd_plist_log_paths_point_into_target() -> None:
    log_dir = str(Path("/tmp/launchd"))
    payload = plistlib.loads(service.launchd_plist(SPEC, log_dir=log_dir).encode("utf-8"))
    assert payload["StandardOutPath"] == str(Path(log_dir) / "im-gateway.out.log")
    assert payload["StandardErrorPath"] == str(Path(log_dir) / "im-gateway.err.log")


def test_launchd_plist_system_scope_sets_user_name() -> None:
    payload = plistlib.loads(service.launchd_plist(replace(SPEC, user="imgw")).encode("utf-8"))
    assert payload["UserName"] == "imgw"


# ---------------------------------------------------------------------------
# NSSM
# ---------------------------------------------------------------------------


def test_nssm_commands_install_configure_and_autostart() -> None:
    commands = service.nssm_commands(SPEC)
    assert commands[0] == ["nssm", "install", "im-gateway", SPEC.exec_path, "run"]
    assert ["nssm", "set", "im-gateway", "AppDirectory", SPEC.workdir] in commands
    assert ["nssm", "set", "im-gateway", "Start", "SERVICE_AUTO_START"] in commands
    assert all(command[0] == "nssm" for command in commands)


def test_nssm_script_records_each_command() -> None:
    script = service.nssm_script(SPEC)
    lines = script.splitlines()
    assert len(lines) == len(service.nssm_commands(SPEC))
    assert lines[0].startswith("nssm install im-gateway")
    assert any("SERVICE_AUTO_START" in line for line in lines)


# ---------------------------------------------------------------------------
# Rendering / writing definitions
# ---------------------------------------------------------------------------


def test_render_command_quotes_spaces() -> None:
    assert (
        service.render_command(["systemctl", "--user", "daemon-reload"])
        == "systemctl --user daemon-reload"
    )
    assert service.render_command(["imgw", "/opt/im gateway/imgw"]) == 'imgw "/opt/im gateway/imgw"'


def test_write_service_file_linux(tmp_path: Path) -> None:
    target = tmp_path / "systemd"
    path = service.write_service_file(SPEC, platform="linux", target=target)
    assert path == target / "im-gateway.service"
    assert path.read_text(encoding="utf-8") == service.systemd_unit(SPEC)
    assert target.is_dir()


def test_write_service_file_darwin(tmp_path: Path) -> None:
    target = tmp_path / "launchd"
    path = service.write_service_file(SPEC, platform="darwin", target=target)
    assert path == target / "com.imgw.gateway.plist"
    assert path.read_text(encoding="utf-8") == service.render_service_file(
        SPEC, platform="darwin", target=target
    )
    plistlib.loads(path.read_bytes())


def test_write_service_file_win32_records_nssm_commands(tmp_path: Path) -> None:
    target = tmp_path / "nssm"
    path = service.write_service_file(SPEC, platform="win32", target=target)
    assert path == target / "im-gateway-nssm.txt"
    text = path.read_text(encoding="utf-8")
    assert "nssm install im-gateway" in text
    assert "SERVICE_AUTO_START" in text


def test_write_service_file_creates_parents(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b" / "c"
    path = service.write_service_file(SPEC, platform="linux", target=target)
    assert path.is_file()


def test_render_service_file_rejects_unknown_platform() -> None:
    with pytest.raises(service.ServiceError, match="unsupported platform"):
        service.render_service_file(SPEC, platform="freebsd")
    with pytest.raises(service.ServiceError, match="unsupported platform"):
        service.write_service_file(SPEC, platform="freebsd", target=Path("/tmp"))


def test_default_targets() -> None:
    user_linux = service.default_target(platform="linux", spec=SPEC)
    assert user_linux.name == "user"
    assert user_linux.parent.name == "systemd"
    system_linux = service.default_target(platform="linux", spec=replace(SPEC, user="imgw"))
    assert system_linux.name == "system"
    darwin_target = service.default_target(platform="darwin", spec=SPEC)
    assert darwin_target.name == "LaunchAgents"
    win32_target = service.default_target(platform="win32", spec=SPEC)
    assert win32_target.name == "im-gateway"


# ---------------------------------------------------------------------------
# ServiceController: install
# ---------------------------------------------------------------------------


def test_install_linux_user_scope(fake_runner: FakeRunner, tmp_path: Path) -> None:
    controller = service.ServiceController(platform="linux", runner=fake_runner, spec=SPEC)
    commands = controller.install(target=tmp_path)
    assert commands == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "im-gateway"],
    ]
    assert fake_runner.calls == commands
    assert (tmp_path / "im-gateway.service").is_file()


def test_install_linux_system_scope(fake_runner: FakeRunner, tmp_path: Path) -> None:
    spec = replace(SPEC, user="imgw")
    controller = service.ServiceController(platform="linux", runner=fake_runner, spec=spec)
    commands = controller.install(target=tmp_path)
    assert commands == [
        ["systemctl", "daemon-reload"],
        ["systemctl", "enable", "--now", "im-gateway"],
    ]
    unit = (tmp_path / "im-gateway.service").read_text(encoding="utf-8")
    assert "User=imgw" in unit


def test_install_darwin_loads_plist(fake_runner: FakeRunner, tmp_path: Path) -> None:
    controller = service.ServiceController(platform="darwin", runner=fake_runner, spec=SPEC)
    commands = controller.install(target=tmp_path)
    plist = tmp_path / "com.imgw.gateway.plist"
    assert commands == [["launchctl", "load", str(plist)]]
    assert fake_runner.calls == commands
    assert plist.is_file()


def test_install_win32_runs_nssm_commands(fake_runner: FakeRunner, tmp_path: Path) -> None:
    controller = service.ServiceController(platform="win32", runner=fake_runner, spec=SPEC)
    commands = controller.install(target=tmp_path)
    assert commands == service.nssm_commands(SPEC)
    assert fake_runner.calls == commands
    assert (tmp_path / "im-gateway-nssm.txt").is_file()


def test_install_dry_run_does_not_write_or_execute(fake_runner: FakeRunner, tmp_path: Path) -> None:
    target = tmp_path / "systemd"
    controller = service.ServiceController(platform="linux", runner=fake_runner, spec=SPEC)
    commands = controller.install(target=target, dry_run=True)
    assert commands == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", "im-gateway"],
    ]
    assert fake_runner.calls == []
    assert not target.exists()


# ---------------------------------------------------------------------------
# ServiceController: uninstall
# ---------------------------------------------------------------------------


def test_uninstall_linux_disables_then_removes_unit(fake_runner: FakeRunner, tmp_path: Path) -> None:
    service.write_service_file(SPEC, platform="linux", target=tmp_path)
    controller = service.ServiceController(platform="linux", runner=fake_runner, spec=SPEC)
    commands = controller.uninstall(target=tmp_path)
    assert commands == [
        ["systemctl", "--user", "disable", "--now", "im-gateway"],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert fake_runner.calls == commands
    assert not (tmp_path / "im-gateway.service").exists()


def test_uninstall_darwin_unloads_then_removes_plist(fake_runner: FakeRunner, tmp_path: Path) -> None:
    service.write_service_file(SPEC, platform="darwin", target=tmp_path)
    controller = service.ServiceController(platform="darwin", runner=fake_runner, spec=SPEC)
    commands = controller.uninstall(target=tmp_path)
    assert commands == [["launchctl", "unload", str(tmp_path / "com.imgw.gateway.plist")]]
    assert fake_runner.calls == commands
    assert not (tmp_path / "com.imgw.gateway.plist").exists()


def test_uninstall_win32_stops_and_removes_service(fake_runner: FakeRunner, tmp_path: Path) -> None:
    service.write_service_file(SPEC, platform="win32", target=tmp_path)
    controller = service.ServiceController(platform="win32", runner=fake_runner, spec=SPEC)
    commands = controller.uninstall(target=tmp_path)
    assert commands == [
        ["nssm", "stop", "im-gateway"],
        ["nssm", "remove", "im-gateway", "confirm"],
    ]
    assert fake_runner.calls == commands
    assert not (tmp_path / "im-gateway-nssm.txt").exists()


def test_uninstall_dry_run_keeps_files_and_runs_nothing(fake_runner: FakeRunner, tmp_path: Path) -> None:
    unit = service.write_service_file(SPEC, platform="linux", target=tmp_path)
    controller = service.ServiceController(platform="linux", runner=fake_runner, spec=SPEC)
    commands = controller.uninstall(target=tmp_path, dry_run=True)
    assert commands
    assert fake_runner.calls == []
    assert unit.is_file()


# ---------------------------------------------------------------------------
# ServiceController: lifecycle
# ---------------------------------------------------------------------------


def test_lifecycle_linux_user_scope(fake_runner: FakeRunner) -> None:
    controller = service.ServiceController(platform="linux", runner=fake_runner, spec=SPEC)
    assert controller.start() == [["systemctl", "--user", "start", "im-gateway"]]
    assert controller.stop() == [["systemctl", "--user", "stop", "im-gateway"]]
    assert controller.restart() == [["systemctl", "--user", "restart", "im-gateway"]]
    assert controller.status() == [["systemctl", "--user", "status", "im-gateway"]]
    assert fake_runner.calls == [
        ["systemctl", "--user", "start", "im-gateway"],
        ["systemctl", "--user", "stop", "im-gateway"],
        ["systemctl", "--user", "restart", "im-gateway"],
        ["systemctl", "--user", "status", "im-gateway"],
    ]


def test_lifecycle_linux_system_scope(fake_runner: FakeRunner) -> None:
    controller = service.ServiceController(
        platform="linux", runner=fake_runner, spec=replace(SPEC, user="imgw")
    )
    assert controller.start() == [["systemctl", "start", "im-gateway"]]


def test_lifecycle_darwin(fake_runner: FakeRunner) -> None:
    controller = service.ServiceController(platform="darwin", runner=fake_runner, spec=SPEC)
    assert controller.start() == [["launchctl", "start", "com.imgw.gateway"]]
    assert controller.stop() == [["launchctl", "stop", "com.imgw.gateway"]]
    assert controller.restart() == [
        ["launchctl", "stop", "com.imgw.gateway"],
        ["launchctl", "start", "com.imgw.gateway"],
    ]
    assert controller.status() == [["launchctl", "list", "com.imgw.gateway"]]


def test_lifecycle_win32(fake_runner: FakeRunner) -> None:
    controller = service.ServiceController(platform="win32", runner=fake_runner, spec=SPEC)
    assert controller.start() == [["nssm", "start", "im-gateway"]]
    assert controller.stop() == [["nssm", "stop", "im-gateway"]]
    assert controller.restart() == [["nssm", "restart", "im-gateway"]]


def test_lifecycle_dry_run_executes_nothing(fake_runner: FakeRunner) -> None:
    controller = service.ServiceController(platform="linux", runner=fake_runner, spec=SPEC)
    assert controller.start(dry_run=True) == [["systemctl", "--user", "start", "im-gateway"]]
    assert fake_runner.calls == []


def test_lifecycle_rejects_unknown_platform(fake_runner: FakeRunner) -> None:
    controller = service.ServiceController(platform="freebsd", runner=fake_runner, spec=SPEC)
    with pytest.raises(service.ServiceError, match="unsupported platform"):
        controller.start()


# ---------------------------------------------------------------------------
# ServiceController: failures become ServiceError
# ---------------------------------------------------------------------------


def test_called_process_error_becomes_service_error(tmp_path: Path) -> None:
    controller = service.ServiceController(platform="linux", runner=_failing_runner, spec=SPEC)
    with pytest.raises(service.ServiceError) as info:
        controller.install(target=tmp_path)
    message = str(info.value)
    assert "systemctl --user daemon-reload" in message
    assert "exit code 3" in message
    assert info.value.stderr == "Unit im-gateway.service not found."


def test_missing_executable_becomes_readable_service_error() -> None:
    controller = service.ServiceController(platform="linux", runner=_missing_runner, spec=SPEC)
    with pytest.raises(service.ServiceError) as info:
        controller.start()
    assert "cannot run `systemctl --user start im-gateway`" in str(info.value)
    assert "No such file or directory" in str(info.value)


# ---------------------------------------------------------------------------
# CLI: service commands (platform injected, no real side effects)
# ---------------------------------------------------------------------------


class _RecordingController:
    """Fake ``ServiceController`` used by the CLI tests."""

    def __init__(self, *, platform: str | None = None, spec: service.ServiceSpec | None = None) -> None:
        self.platform = platform
        self.spec = spec
        self.calls: list[str] = []

    def install(self, *, target: Path | None = None, dry_run: bool = False) -> list[list[str]]:
        self.calls.append(f"install:{dry_run}")
        return [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "--now", "im-gateway"],
        ]

    def uninstall(self, *, target: Path | None = None, dry_run: bool = False) -> list[list[str]]:
        self.calls.append(f"uninstall:{dry_run}")
        return [["systemctl", "--user", "disable", "--now", "im-gateway"]]

    def start(self, *, dry_run: bool = False) -> list[list[str]]:
        self.calls.append(f"start:{dry_run}")
        return [["systemctl", "--user", "start", "im-gateway"]]

    def stop(self, *, dry_run: bool = False) -> list[list[str]]:
        self.calls.append(f"stop:{dry_run}")
        return [["systemctl", "--user", "stop", "im-gateway"]]

    def restart(self, *, dry_run: bool = False) -> list[list[str]]:
        self.calls.append(f"restart:{dry_run}")
        return [["systemctl", "--user", "restart", "im-gateway"]]


def test_cli_start_dry_run_linux_prints_commands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_service_platform", lambda: "linux")
    result = runner.invoke(cli.app, ["start", "--dry-run"])
    assert result.exit_code == 0, result.stderr
    assert "$ systemctl --user start im-gateway" in result.stdout
    assert "dry run" in result.stdout.lower()


def test_cli_install_service_print_only_linux(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    target = tmp_path / "systemd"
    monkeypatch.setattr(cli, "_service_platform", lambda: "linux")
    monkeypatch.setattr(cli, "default_target", lambda *, platform, spec: target)
    result = runner.invoke(cli.app, ["install-service", "--print-only"])
    assert result.exit_code == 0, result.stderr
    assert "[Service]" in result.stdout
    assert "ExecStart=" in result.stdout
    assert "nssm" not in result.stdout
    assert not target.exists()


def test_cli_install_service_print_only_win32(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_service_platform", lambda: "win32")
    result = runner.invoke(cli.app, ["install-service", "--print-only"])
    assert result.exit_code == 0, result.stderr
    assert "nssm install im-gateway" in result.stdout
    assert "SERVICE_AUTO_START" in result.stdout


def test_cli_install_service_workdir_option(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(cli, "_service_platform", lambda: "linux")
    result = runner.invoke(cli.app, ["install-service", "--print-only", "--workdir", str(tmp_path)])
    assert result.exit_code == 0, result.stderr
    assert f"WorkingDirectory={tmp_path.resolve()}" in result.stdout


def test_cli_install_service_global_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_service_platform", lambda: "linux")
    result = runner.invoke(cli.app, ["--json", "install-service", "--dry-run"])
    assert result.exit_code == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["service_file"] is None
    assert payload["commands"][0] == ["systemctl", "--user", "daemon-reload"]


def test_cli_service_commands_exit_one_on_service_error(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FailingController(_RecordingController):
        def stop(self, *, dry_run: bool = False) -> list[list[str]]:
            raise service.ServiceError(
                "`systemctl --user stop im-gateway` failed with exit code 1: not loaded"
            )

    monkeypatch.setattr(cli, "ServiceController", _FailingController)
    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 1
    assert "error:" in result.stderr
    assert "not loaded" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_service_commands_exit_one_when_manager_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_service_platform", lambda: "linux")
    monkeypatch.setattr(service, "default_runner", _missing_runner)
    result = runner.invoke(cli.app, ["stop"])
    assert result.exit_code == 1
    assert "error:" in result.stderr
    assert "cannot run `systemctl --user stop im-gateway`" in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_install_service_dry_run_does_not_write(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    target = tmp_path / "systemd"
    monkeypatch.setattr(cli, "_service_platform", lambda: "linux")
    monkeypatch.setattr(cli, "default_target", lambda *, platform, spec: target)
    result = runner.invoke(cli.app, ["install-service", "--dry-run"])
    assert result.exit_code == 0, result.stderr
    assert "$ systemctl --user daemon-reload" in result.stdout
    assert not target.exists()


def test_cli_uninstall_service_dry_run(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "_service_platform", lambda: "linux")
    result = runner.invoke(cli.app, ["uninstall-service", "--dry-run"])
    assert result.exit_code == 0, result.stderr
    assert "$ systemctl --user disable --now im-gateway" in result.stdout
