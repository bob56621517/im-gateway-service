"""System service definitions and lifecycle commands (HANDOVER §13.7).

Only ``imgw run`` starts the gateway process; ``imgw install-service`` merely
writes the platform service definition (systemd unit, launchd plist or an NSSM
command script), and ``start`` / ``stop`` / ``restart`` delegate to the system
service manager.

Design notes:

* the three generators (:func:`systemd_unit`, :func:`launchd_plist`,
  :func:`nssm_commands`) are **pure**: they return text/commands and have no
  side effects, so they are trivially unit-testable;
* :func:`write_service_file` is the only function that touches the filesystem;
* :class:`ServiceController` executes commands through an injected ``runner``
  (default: :func:`subprocess.run`), which lets the tests assert the exact
  command sequence without a real systemd / launchd / NSSM installation;
* every ``subprocess`` failure is converted into :class:`ServiceError`, a
  ``RuntimeError`` the CLI prints as a one-line error and exits 1 for.

Scope is derived from :attr:`ServiceSpec.user`: when it is ``None`` the service
is **user-scoped** (``systemctl --user``, a launchd agent, ``WantedBy=default.target``);
when it is set the service is **system-scoped** and runs as that OS user
(``User=`` in the unit, a launchd daemon, ``WantedBy=multi-user.target``).
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from im_gateway.cli.i18n import t
from im_gateway.core import paths

__all__ = [
    "LAUNCHD_FILENAME",
    "LAUNCHD_LABEL",
    "NSSM_FILENAME",
    "SYSTEMD_FILENAME",
    "ServiceController",
    "ServiceError",
    "ServiceSpec",
    "default_exec_path",
    "default_runner",
    "default_spec",
    "default_target",
    "launchd_plist",
    "nssm_commands",
    "nssm_script",
    "render_command",
    "render_service_file",
    "service_file_path",
    "systemd_unit",
    "write_service_file",
]

#: systemd unit / NSSM service name.
SERVICE_NAME = "im-gateway"
#: launchd label (reverse-DNS, ``com.imgw.gateway``).
LAUNCHD_LABEL = "com.imgw.gateway"
SYSTEMD_FILENAME = "im-gateway.service"
LAUNCHD_FILENAME = "com.imgw.gateway.plist"
NSSM_FILENAME = "im-gateway-nssm.txt"

#: A runner takes one argv list and raises ``CalledProcessError`` / ``OSError`` on failure.
Runner = Callable[[list[str]], subprocess.CompletedProcess[str]]


class ServiceError(RuntimeError):
    """A user-facing service-management failure (the CLI prints it and exits 1)."""

    def __init__(
        self,
        message: str,
        *,
        command: list[str] | None = None,
        stderr: str = "",
    ) -> None:
        super().__init__(message)
        self.command: list[str] | None = command
        self.stderr: str = stderr

    @classmethod
    def from_called_process_error(cls, exc: subprocess.CalledProcessError) -> ServiceError:
        """Render a failed system command (including its stderr) for the CLI."""
        command = _command_argv(exc.cmd)
        stderr = _decode_stream(exc.stderr).strip()
        message = t(
            "`{command}` failed with exit code {code}",
            command=render_command(command),
            code=exc.returncode,
        )
        if stderr:
            message = t("{message}: {stderr}", message=message, stderr=stderr)
        return cls(message, command=command, stderr=stderr)


def _command_argv(cmd: object) -> list[str]:
    if isinstance(cmd, str):
        return [cmd]
    if isinstance(cmd, list | tuple):
        return [str(part) for part in cmd]
    if isinstance(cmd, bytes):
        return [cmd.decode("utf-8", "replace")]
    return [str(cmd)]


def _decode_stream(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


# ---------------------------------------------------------------------------
# Spec and defaults
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ServiceSpec:
    """Everything a platform service definition needs to start ``imgw run``.

    ``user`` doubles as the scope switch: ``None`` means a user-scoped service
    (``systemctl --user`` / launchd agent); a value means a system-scoped
    service that runs as that user.
    """

    exec_path: str
    workdir: str
    name: str = SERVICE_NAME
    user: str | None = None
    env_file: str | None = None
    log_level: str = "info"
    restart_seconds: int = 5


def default_exec_path() -> str:
    """Absolute path of the ``imgw`` console script (``shutil.which`` or venv fallback)."""
    found = shutil.which("imgw")
    if found:
        return found
    suffix = ".exe" if sys.platform == "win32" else ""
    return str(Path(sys.executable).with_name(f"imgw{suffix}"))


def default_spec(*, workdir: str | None = None, user: str | None = None) -> ServiceSpec:
    """Spec used by :class:`ServiceController` when the caller does not pass one."""
    return ServiceSpec(
        exec_path=default_exec_path(),
        workdir=workdir or str(Path.cwd()),
        user=user,
    )


def default_target(*, platform: str, spec: ServiceSpec) -> Path:
    """Directory where the platform service definition is installed by default."""
    if platform == "linux":
        if spec.user is None:
            return Path.home() / ".config" / "systemd" / "user"
        return Path("/etc/systemd/system")
    if platform == "darwin":
        if spec.user is None:
            return Path.home() / "Library" / "LaunchAgents"
        return Path("/Library/LaunchDaemons")
    if platform == "win32":
        return paths.data_dir()
    raise _unsupported_platform(platform)


# ---------------------------------------------------------------------------
# Pure generators (no side effects)
# ---------------------------------------------------------------------------


def render_command(command: list[str]) -> str:
    """Render an argv list for display / copy-paste (argv parts with spaces quoted)."""
    return " ".join(_quote_argument(part) for part in command)


def _quote_argument(argument: str) -> str:
    if not argument:
        return '""'
    if any(char.isspace() for char in argument):
        return '"' + argument.replace('"', '\\"') + '"'
    return argument


def systemd_unit(spec: ServiceSpec) -> str:
    """Complete systemd ``[Unit]/[Service]/[Install]`` unit for ``imgw run``."""
    lines = [
        "[Unit]",
        f"Description={spec.name} — IM gateway daemon",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "Type=simple",
        f"ExecStart={render_command([spec.exec_path, 'run'])}",
        f"WorkingDirectory={spec.workdir}",
        f"Environment=IMGW_LOG_LEVEL={spec.log_level}",
    ]
    if spec.env_file:
        lines.append(f"EnvironmentFile={spec.env_file}")
    if spec.user:
        lines.append(f"User={spec.user}")
    lines.extend(
        [
            "Restart=on-failure",
            f"RestartSec={spec.restart_seconds}",
            "",
            "[Install]",
            f"WantedBy={'default.target' if spec.user is None else 'multi-user.target'}",
            "",
        ]
    )
    return "\n".join(lines)


def launchd_plist(spec: ServiceSpec, *, log_dir: str | None = None) -> str:
    """Valid launchd XML plist (``plistlib`` can read it) for ``imgw run``.

    When ``log_dir`` is given, stdout/stderr are redirected into that directory
    (the plist's own target directory in the normal install flow).
    """
    payload: dict[str, object] = {
        "Label": LAUNCHD_LABEL,
        "ProgramArguments": [spec.exec_path, "run"],
        "WorkingDirectory": spec.workdir,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "EnvironmentVariables": {"IMGW_LOG_LEVEL": spec.log_level},
    }
    if spec.user:
        payload["UserName"] = spec.user
    if log_dir is not None:
        directory = Path(log_dir)
        payload["StandardOutPath"] = str(directory / f"{spec.name}.out.log")
        payload["StandardErrorPath"] = str(directory / f"{spec.name}.err.log")
    return plistlib.dumps(payload, fmt=plistlib.FMT_XML, sort_keys=False).decode("utf-8")


def nssm_commands(spec: ServiceSpec) -> list[list[str]]:
    """Ordered NSSM argv lists that install and configure the Windows service."""
    return [
        ["nssm", "install", spec.name, spec.exec_path, "run"],
        ["nssm", "set", spec.name, "AppDirectory", spec.workdir],
        ["nssm", "set", spec.name, "Start", "SERVICE_AUTO_START"],
        ["nssm", "set", spec.name, "AppEnvironmentExtra", f"IMGW_LOG_LEVEL={spec.log_level}"],
        ["nssm", "set", spec.name, "AppExit", "Default", "Restart"],
        ["nssm", "set", spec.name, "AppRestartDelay", str(spec.restart_seconds * 1000)],
    ]


def nssm_script(spec: ServiceSpec) -> str:
    """The ``im-gateway-nssm.txt`` record of the NSSM commands (one per line)."""
    return "\n".join(render_command(command) for command in nssm_commands(spec)) + "\n"


# ---------------------------------------------------------------------------
# Writing the definition
# ---------------------------------------------------------------------------


def service_file_path(*, platform: str, target: Path) -> Path:
    """Path of the definition file written into ``target`` for ``platform``."""
    if platform == "linux":
        return target / SYSTEMD_FILENAME
    if platform == "darwin":
        return target / LAUNCHD_FILENAME
    if platform == "win32":
        return target / NSSM_FILENAME
    raise _unsupported_platform(platform)


def render_service_file(spec: ServiceSpec, *, platform: str, target: Path | None = None) -> str:
    """Render the definition text without touching the filesystem."""
    if platform == "linux":
        return systemd_unit(spec)
    if platform == "darwin":
        return launchd_plist(spec, log_dir=str(target) if target is not None else None)
    if platform == "win32":
        return nssm_script(spec)
    raise _unsupported_platform(platform)


def write_service_file(spec: ServiceSpec, *, platform: str, target: Path) -> Path:
    """Write the platform definition into ``target`` (created if missing)."""
    content = render_service_file(spec, platform=platform, target=target)
    path = service_file_path(platform=platform, target=target)
    target.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------


class ServiceController:
    """Generate definitions and drive the platform service manager.

    ``runner`` is injected by the tests: no real systemd / launchd / NSSM is
    required. A failing runner command becomes :class:`ServiceError`.
    """

    def __init__(
        self,
        *,
        platform: str | None = None,
        runner: Runner | None = None,
        spec: ServiceSpec | None = None,
    ) -> None:
        self.platform: str = platform or sys.platform
        self.spec: ServiceSpec = spec or default_spec()
        self._runner: Runner = runner if runner is not None else default_runner

    @property
    def target(self) -> Path:
        """Default directory for this controller's platform and scope."""
        return default_target(platform=self.platform, spec=self.spec)

    # -- install / uninstall -------------------------------------------------

    def install(self, *, target: Path | None = None, dry_run: bool = False) -> list[list[str]]:
        """Write the definition and install/enable the service."""
        resolved = target if target is not None else self.target
        commands = self._install_commands(resolved)
        if dry_run:
            return commands
        try:
            write_service_file(self.spec, platform=self.platform, target=resolved)
        except OSError as exc:
            raise ServiceError(
                t(
                    "cannot write the service definition under {target}: {error}",
                    target=resolved,
                    error=exc,
                )
            ) from exc
        self._execute(commands)
        return commands

    def uninstall(self, *, target: Path | None = None, dry_run: bool = False) -> list[list[str]]:
        """Stop/disable the service and remove its definition file."""
        resolved = target if target is not None else self.target
        definition = service_file_path(platform=self.platform, target=resolved)

        if self.platform == "linux":
            systemctl = self._systemctl()
            commands = [
                [*systemctl, "disable", "--now", self.spec.name],
                [*systemctl, "daemon-reload"],
            ]
            if dry_run:
                return commands
            self._execute(commands[:1])
            _remove(definition)
            self._execute(commands[1:])
            return commands

        if self.platform == "darwin":
            commands = [["launchctl", "unload", str(definition)]]
            if dry_run:
                return commands
            self._execute(commands)
            _remove(definition)
            return commands

        if self.platform == "win32":
            commands = [
                ["nssm", "stop", self.spec.name],
                ["nssm", "remove", self.spec.name, "confirm"],
            ]
            if dry_run:
                return commands
            self._execute(commands)
            _remove(definition)
            return commands

        # Unreachable today: ``service_file_path`` above already rejects unknown
        # platforms.  Kept as defence in depth for future platform branches.
        raise _unsupported_platform(self.platform)  # pragma: no cover

    # -- lifecycle -----------------------------------------------------------

    def start(self, *, dry_run: bool = False) -> list[list[str]]:
        """Start the installed service."""
        return self._lifecycle("start", dry_run=dry_run)

    def stop(self, *, dry_run: bool = False) -> list[list[str]]:
        """Stop the installed service."""
        return self._lifecycle("stop", dry_run=dry_run)

    def restart(self, *, dry_run: bool = False) -> list[list[str]]:
        """Restart the installed service."""
        return self._lifecycle("restart", dry_run=dry_run)

    def status(self, *, dry_run: bool = False) -> list[list[str]]:
        """Ask the platform service manager for the service status."""
        return self._lifecycle("status", dry_run=dry_run)

    def _lifecycle(self, action: str, *, dry_run: bool) -> list[list[str]]:
        commands = self._lifecycle_commands(action)
        if not dry_run:
            self._execute(commands)
        return commands

    # -- command builders ----------------------------------------------------

    def _systemctl(self) -> list[str]:
        # Empty user => user-scoped manager; a user => system manager + User=.
        return ["systemctl"] if self.spec.user is not None else ["systemctl", "--user"]

    def _install_commands(self, target: Path) -> list[list[str]]:
        if self.platform == "linux":
            systemctl = self._systemctl()
            return [
                [*systemctl, "daemon-reload"],
                [*systemctl, "enable", "--now", self.spec.name],
            ]
        if self.platform == "darwin":
            return [["launchctl", "load", str(service_file_path(platform=self.platform, target=target))]]
        if self.platform == "win32":
            return nssm_commands(self.spec)
        raise _unsupported_platform(self.platform)

    def _lifecycle_commands(self, action: str) -> list[list[str]]:
        if self.platform == "linux":
            return [[*self._systemctl(), action, self.spec.name]]
        if self.platform == "darwin":
            if action == "restart":
                return [
                    ["launchctl", "stop", LAUNCHD_LABEL],
                    ["launchctl", "start", LAUNCHD_LABEL],
                ]
            if action == "status":
                return [["launchctl", "list", LAUNCHD_LABEL]]
            return [["launchctl", action, LAUNCHD_LABEL]]
        if self.platform == "win32":
            return [["nssm", action, self.spec.name]]
        raise _unsupported_platform(self.platform)

    def _execute(self, commands: list[list[str]]) -> None:
        for command in commands:
            try:
                self._runner(command)
            except subprocess.CalledProcessError as exc:
                raise ServiceError.from_called_process_error(exc) from exc
            except OSError as exc:
                raise ServiceError(
                    t(
                        "cannot run `{command}`: {error}",
                        command=render_command(command),
                        error=exc,
                    ),
                    command=command,
                ) from exc


# ---------------------------------------------------------------------------
# Default runner / helpers
# ---------------------------------------------------------------------------


def default_runner(command: list[str]) -> subprocess.CompletedProcess[str]:
    """Run one system command, raising on a non-zero exit (the production runner)."""
    return subprocess.run(command, check=True, capture_output=True, text=True)


def _remove(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise ServiceError(t("cannot remove {path}: {error}", path=path, error=exc)) from exc


def _unsupported_platform(platform: str) -> ServiceError:
    return ServiceError(
        t(
            "unsupported platform {platform!r}: service management supports linux, "
            "darwin and win32",
            platform=platform,
        )
    )
