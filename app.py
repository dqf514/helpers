import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import ctypes
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QTextBrowser,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QDialog,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QStatusBar,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)


APP_DIR = Path(__file__).resolve().parent
DATA_DIR = APP_DIR / "data"
LOG_DIR = DATA_DIR / "logs"
CONFIG_PATH = DATA_DIR / "config.json"
ICON_RELATIVE_PATH = Path("image") / "openclaw_lobster.ico"
LANG_MODE = "bilingual"


def bi(zh: str, en: str) -> str:
    if LANG_MODE == "zh":
        return zh
    if LANG_MODE == "en":
        return en
    return f"{zh} / {en}"


def set_lang_mode(mode: str) -> None:
    global LANG_MODE
    if mode not in {"zh", "en", "bilingual"}:
        LANG_MODE = "bilingual"
        return
    LANG_MODE = mode


def is_windows() -> bool:
    return sys.platform.startswith("win")


def resolve_command_for_system(base_cmd: str) -> str:
    cmd = base_cmd.strip()
    if not cmd:
        return base_cmd
    candidates = [cmd]
    path_obj = Path(cmd)
    if is_windows() and not path_obj.suffix:
        candidates.extend(
            [f"{cmd}.cmd", f"{cmd}.exe", f"{cmd}.bat", f"{cmd}.ps1"]
        )
    seen = set()
    for item in candidates:
        if item in seen:
            continue
        seen.add(item)
        if Path(item).is_absolute() and Path(item).exists():
            return item
        hit = shutil.which(item)
        if hit:
            return hit

    # Windows 新装机场景：Node/npm 可能已安装，但当前进程 PATH 未刷新。
    if is_windows():
        lowered = cmd.lower()
        windows_fallbacks = {
            "node": ["node.exe"],
            "node.exe": ["node.exe"],
            "npm": ["npm.cmd", "npm.exe", "npm"],
            "npm.cmd": ["npm.cmd", "npm.exe", "npm"],
            "npx": ["npx.cmd", "npx.exe", "npx"],
            "npx.cmd": ["npx.cmd", "npx.exe", "npx"],
            "git": ["git.exe", "git.cmd", "git"],
            "git.exe": ["git.exe", "git.cmd", "git"],
        }
        tool_names = windows_fallbacks.get(lowered, [])
        if tool_names:
            search_roots = []
            for env_key in ["ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"]:
                root = os.environ.get(env_key, "").strip()
                if root:
                    search_roots.append(Path(root))
            candidate_dirs = []
            for root in search_roots:
                candidate_dirs.append(root / "nodejs")
                candidate_dirs.append(root / "Programs" / "nodejs")
                candidate_dirs.append(root / "Git" / "cmd")
                candidate_dirs.append(root / "Git" / "bin")
            for cdir in candidate_dirs:
                for tname in tool_names:
                    full = cdir / tname
                    if full.exists():
                        return str(full)
    return candidates[0]


def app_resource_path(relative_path: Path) -> Path:
    base_dir = Path(getattr(sys, "_MEIPASS", APP_DIR))
    return base_dir / relative_path


if is_windows():
    user32 = ctypes.windll.user32
    GWL_STYLE = -16
    WS_CAPTION = 0x00C00000
    WS_THICKFRAME = 0x00040000
    WS_MINIMIZE = 0x20000000
    WS_MAXIMIZE = 0x01000000
    WS_SYSMENU = 0x00080000
    WS_CHILD = 0x40000000
    WS_VISIBLE = 0x10000000
    SW_SHOW = 5
    SW_RESTORE = 9
    HWND_TOPMOST = -1
    HWND_NOTOPMOST = -2
    SWP_SHOWWINDOW = 0x0040


def find_console_window_by_pid(pid: int) -> int | None:
    if not is_windows():
        return None

    result: list[int] = []
    enum_windows_proc = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )

    def callback(hwnd: int, _lparam: int) -> bool:
        proc_id = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(proc_id))
        if proc_id.value != pid:
            return True
        class_buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, class_buf, 256)
        class_name = class_buf.value
        if class_name == "ConsoleWindowClass":
            result.append(hwnd)
            return False
        return True

    user32.EnumWindows(enum_windows_proc(callback), 0)
    if not result:
        return None
    return result[0]


def find_window_by_title_fragment(fragment: str) -> int | None:
    if not is_windows():
        return None
    result: list[int] = []
    enum_windows_proc = ctypes.WINFUNCTYPE(
        ctypes.c_bool,
        ctypes.c_void_p,
        ctypes.c_void_p,
    )

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        title_buf = ctypes.create_unicode_buffer(512)
        user32.GetWindowTextW(hwnd, title_buf, 512)
        title = title_buf.value
        if fragment in title:
            result.append(hwnd)
            return False
        return True

    user32.EnumWindows(enum_windows_proc(callback), 0)
    if not result:
        return None
    return result[0]


@dataclass
class AppConfig:
    openclaw_cmd: str = "openclaw"
    node_cmd: str = "node"
    npm_cmd: str = "npm"
    winget_cmd: str = "winget"
    npm_registry: str = "https://registry.npmmirror.com"
    language_mode: str = "bilingual"
    working_dir: str = ""
    thinking_level: str = "medium"
    timeout_seconds: int = 120
    extra_args: str = ""
    test_message: str = "你好，请回复“连接成功”。"


class SignalBus(QObject):
    log = Signal(str)
    chat_reply = Signal(str, str)
    task_done = Signal()
    check_result = Signal(bool, str)
    env_result = Signal(dict)
    startup_progress = Signal(int, str)
    startup_stage = Signal(str, str, str)
    install_progress = Signal(int, str)
    install_finished = Signal(bool, str)
    error = Signal(str)


class CommandTask(QRunnable):
    def __init__(
        self,
        bus: SignalBus,
        config: AppConfig,
        user_message: str,
        command_type: str,
    ):
        super().__init__()
        self.bus = bus
        self.config = config
        self.user_message = user_message
        self.command_type = command_type

    def run(self) -> None:
        try:
            if self.command_type == "chat":
                self._run_chat()
            elif self.command_type == "check":
                self._run_chat_check()
            elif self.command_type == "env_check":
                self._run_environment_check()
            elif self.command_type == "install":
                self._run_install()
            elif self.command_type == "uninstall":
                self._run_uninstall()
            elif self.command_type == "repair_env":
                self._run_repair_env()
            elif self.command_type == "onboard_wizard":
                self._run_onboard_wizard()
            elif self.command_type == "package_exe":
                self._run_package_exe()
        except Exception as ex:
            self.bus.error.emit(bi(f"执行失败：{ex}", f"Execution failed: {ex}"))
        finally:
            self.bus.task_done.emit()

    @staticmethod
    def _is_windows() -> bool:
        return is_windows()

    def _command_candidates(self, base_cmd: str) -> list[str]:
        cmd = base_cmd.strip()
        if not cmd:
            return []
        path_obj = Path(cmd)
        has_suffix = bool(path_obj.suffix)
        candidates = [cmd]
        if self._is_windows() and not has_suffix:
            candidates.extend(
                [f"{cmd}.cmd", f"{cmd}.exe", f"{cmd}.bat", f"{cmd}.ps1"]
            )
        # 去重并保持顺序
        seen = set()
        unique = []
        for item in candidates:
            if item not in seen:
                seen.add(item)
                unique.append(item)
        return unique

    def _resolve_command(self, base_cmd: str) -> str:
        return resolve_command_for_system(base_cmd)

    def _run_subprocess(
        self, command: list[str], **kwargs
    ) -> subprocess.CompletedProcess:
        cmd = command.copy()
        cmd[0] = self._resolve_command(cmd[0])
        try:
            return subprocess.run(cmd, **kwargs)
        except FileNotFoundError as ex:
            hint = bi(
                "请检查 PATH 或在配置中填绝对路径",
                "check PATH or set absolute path in settings",
            )
            raise FileNotFoundError(
                f"{bi('命令不存在', 'Command not found')}: {command[0]} "
                f"({hint})"
            ) from ex

    def _popen_subprocess(
        self, command: list[str], **kwargs
    ) -> subprocess.Popen:
        cmd = command.copy()
        cmd[0] = self._resolve_command(cmd[0])
        try:
            return subprocess.Popen(cmd, **kwargs)
        except FileNotFoundError as ex:
            hint = bi(
                "请检查 PATH 或在配置中填绝对路径",
                "check PATH or set absolute path in settings",
            )
            raise FileNotFoundError(
                f"{bi('命令不存在', 'Command not found')}: {command[0]} "
                f"({hint})"
            ) from ex

    def _run_chat(self) -> None:
        command = [
            self.config.openclaw_cmd,
            "agent",
            "--agent",
            "main",
            "--message",
            self.user_message,
            "--thinking",
            self.config.thinking_level,
        ]
        if self.config.extra_args.strip():
            command.extend(self.config.extra_args.strip().split())

        self.bus.log.emit(f"[{self._now()}] 执行命令：{' '.join(command)}")
        result = self._run_subprocess(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=self.config.timeout_seconds,
            cwd=self.config.working_dir or None,
            shell=False,
        )
        if result.returncode != 0:
            message = (
                result.stderr.strip()
                or result.stdout.strip()
                or "openclaw 执行失败"
            )
            self.bus.error.emit(message)
            return

        reply = result.stdout.strip() or "(无返回内容)"
        self.bus.chat_reply.emit("助手", reply)
        self.bus.log.emit(f"[{self._now()}] 收到回复，长度 {len(reply)} 字符。")

    def _run_chat_check(self) -> None:
        version_cmd = [self.config.openclaw_cmd, "--version"]
        self.bus.log.emit(f"[{self._now()}] 检测命令：{' '.join(version_cmd)}")
        result = self._run_subprocess(
            version_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=min(self.config.timeout_seconds, 30),
            cwd=self.config.working_dir or None,
            shell=False,
        )
        if result.returncode != 0:
            message = (
                result.stderr.strip()
                or result.stdout.strip()
                or "无法执行 openclaw --version"
            )
            self.bus.check_result.emit(False, message)
            return

        version = result.stdout.strip() or "未知版本"
        self.bus.log.emit(f"[{self._now()}] openclaw 版本：{version}")

        test_cmd = [
            self.config.openclaw_cmd,
            "agent",
            "--agent",
            "main",
            "--message",
            self.config.test_message,
            "--thinking",
            "minimal",
        ]
        self.bus.log.emit(f"[{self._now()}] 连通性测试：{' '.join(test_cmd)}")
        test_result = self._run_subprocess(
            test_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=self.config.timeout_seconds,
            cwd=self.config.working_dir or None,
            shell=False,
        )
        if test_result.returncode != 0:
            message = (
                test_result.stderr.strip()
                or test_result.stdout.strip()
                or "连通性测试失败"
            )
            self.bus.check_result.emit(False, message)
            return

        preview = test_result.stdout.strip()
        if len(preview) > 120:
            preview = preview[:120] + "..."
        self.bus.check_result.emit(True, f"版本：{version}\n测试回复：{preview}")

    def _run_environment_check(self) -> None:
        self.bus.log.emit(
            f"[{self._now()}] {bi('开始环境检测。', 'Environment check started.')}"
        )
        self.bus.startup_stage.emit("env", "running", "")
        self.bus.startup_stage.emit("install", "pending", "")
        self.bus.startup_stage.emit("runtime", "pending", "")
        self.bus.startup_progress.emit(
            5, bi("启动检测：准备中", "Startup check: preparing")
        )
        details = self._collect_environment_details(
            self.bus.startup_progress.emit
        )
        env_ok = bool(
            details.get("node_ok")
            and details.get("npm_ok")
            and details.get("git_ok")
            and details.get("source_ok")
        )
        self.bus.startup_stage.emit(
            "env",
            "ok" if env_ok else "fail",
            bi(
                f"Node={details.get('node_text', '-')}; "
                f"npm={details.get('npm_text', '-')}; "
                f"git={details.get('git_text', '-')}; "
                f"安装源={details.get('source_text', '-')}",
                f"Node={details.get('node_text', '-')}; "
                f"npm={details.get('npm_text', '-')}; "
                f"git={details.get('git_text', '-')}; "
                f"registry={details.get('source_text', '-')}",
            ),
        )
        self.bus.startup_stage.emit(
            "install",
            "ok" if details.get("openclaw_ok") else "fail",
            details.get("openclaw_text", ""),
        )
        runtime_status = (
            "ok"
            if details.get("runtime_ok")
            else ("fail" if details.get("openclaw_ok") else "skip")
        )
        self.bus.startup_stage.emit(
            "runtime",
            runtime_status,
            details.get("runtime_text", ""),
        )
        self.bus.startup_progress.emit(
            100, bi("启动检测完成", "Startup check completed")
        )
        self.bus.log.emit(
            f"[{self._now()}] 环境检测完成："
            f"Node={details['node_ok']} "
            f"npm={details['npm_ok']} "
            f"git={details['git_ok']} "
            f"源={details['source_ok']}"
        )
        self.bus.env_result.emit(details)

    def _collect_environment_details(self, progress_cb=None) -> dict:
        details = {
            "node_ok": False,
            "node_text": bi("未检测", "Not checked"),
            "npm_ok": False,
            "npm_text": bi("未检测", "Not checked"),
            "git_ok": False,
            "git_text": bi("未检测", "Not checked"),
            "source_ok": False,
            "source_text": bi("未检测", "Not checked"),
            "openclaw_ok": False,
            "openclaw_text": bi("未检测", "Not checked"),
            "runtime_ok": False,
            "runtime_text": bi("未检测", "Not checked"),
            "install_ready": False,
        }
        if progress_cb is not None:
            progress_cb(
                12,
                bi(
                    "运行环境检测：Node/npm/git/安装源",
                    "Runtime env check: node/npm/git/registry",
                ),
            )

        # Node 检测（要求 >=22）
        try:
            node_result = self._run_subprocess(
                [self.config.node_cmd, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                shell=False,
            )
            if node_result.returncode == 0:
                node_version = node_result.stdout.strip()
                major = self._extract_major_version(node_version)
                if major >= 22:
                    details["node_ok"] = True
                    details["node_text"] = (
                        f"{node_version} "
                        f"{bi('（满足 >=22）', '(meets >=22)')}"
                    )
                else:
                    details["node_text"] = (
                        f"{node_version} "
                        f"{bi('（过低，需 >=22）', '(too low, requires >=22)')}"
                    )
            else:
                details["node_text"] = (
                    node_result.stderr.strip()
                    or bi("Node 不可用", "Node unavailable")
                )
        except Exception as ex:
            details["node_text"] = bi(
                f"Node 检测失败：{ex}",
                f"Node check failed: {ex}",
            )

        # npm 检测
        try:
            npm_result = self._run_subprocess(
                [self.config.npm_cmd, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                shell=False,
            )
            if npm_result.returncode == 0:
                details["npm_ok"] = True
                details["npm_text"] = f"npm {npm_result.stdout.strip()}"
            else:
                details["npm_text"] = (
                    npm_result.stderr.strip()
                    or bi("npm 不可用", "npm unavailable")
                )
        except Exception as ex:
            details["npm_text"] = bi(
                f"npm 检测失败：{ex}",
                f"npm check failed: {ex}",
            )

        # git 检测（npm 安装 openclaw 依赖）
        try:
            git_cmd = resolve_command_for_system("git")
            git_result = self._run_subprocess(
                [git_cmd, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                shell=False,
            )
            if git_result.returncode == 0:
                details["git_ok"] = True
                details["git_text"] = git_result.stdout.strip()
            else:
                details["git_text"] = (
                    git_result.stderr.strip()
                    or git_result.stdout.strip()
                    or bi("git 不可用", "git unavailable")
                )
        except Exception as ex:
            details["git_text"] = bi(
                f"git 检测失败：{ex}",
                f"git check failed: {ex}",
            )

        # 安装源连通性检测（中国大陆可用优先）
        try:
            source_url = (
                f"{self.config.npm_registry.rstrip('/')}/openclaw/latest"
            )
            req = urllib.request.Request(
                source_url,
                headers={"User-Agent": "openclaw-gui-installer/1.0"},
            )
            with urllib.request.urlopen(req, timeout=10) as response:
                code = response.getcode()
                if code == 200:
                    details["source_ok"] = True
                    details["source_text"] = (
                        bi("安装源可连接", "Registry reachable")
                        + f": {self.config.npm_registry}"
                    )
                else:
                    details["source_text"] = (
                        bi("安装源返回状态码", "Registry status code")
                        + f": {code}"
                    )
        except urllib.error.URLError as ex:
            details["source_text"] = bi(
                f"安装源不可达：{ex}",
                f"Registry unreachable: {ex}",
            )
        except Exception as ex:
            details["source_text"] = bi(
                f"安装源检测失败：{ex}",
                f"Registry check failed: {ex}",
            )

        # 已安装版本检测（非门禁项）
        if progress_cb is not None:
            progress_cb(55, bi("安装状态检测：OpenClaw", "Install status check"))
        try:
            openclaw_result = self._run_subprocess(
                [self.config.openclaw_cmd, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                shell=False,
            )
            if openclaw_result.returncode == 0:
                details["openclaw_ok"] = True
                details["openclaw_text"] = (
                    bi("已安装", "Installed")
                    + f": {openclaw_result.stdout.strip()}"
                )
            else:
                details["openclaw_text"] = bi(
                    "未安装或不可用",
                    "Not installed or unavailable",
                )
        except Exception:
            details["openclaw_text"] = bi(
                "未安装或不可用",
                "Not installed or unavailable",
            )

        # 运行态检测：会话活跃视为已初始化完成
        if details["openclaw_ok"]:
            if progress_cb is not None:
                progress_cb(
                    82,
                    bi(
                        "OpenClaw 运行状态检测：会话活跃性",
                        "OpenClaw runtime check: active session",
                    ),
                )
            try:
                status_result = self._run_subprocess(
                    [self.config.openclaw_cmd, "status"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=30,
                    shell=False,
                )
                if status_result.returncode == 0:
                    status_text = status_result.stdout.lower()
                    has_active_session = (
                        "default main active" in status_text
                        or "agent:main:main" in status_text
                    )
                    details["runtime_ok"] = has_active_session
                    details["runtime_text"] = (
                        bi("会话活跃，已在运行", "Session active, running")
                        if has_active_session
                        else bi(
                            "未检测到活跃会话",
                            "No active session detected",
                        )
                    )
                else:
                    details["runtime_text"] = bi(
                        "运行状态检测失败",
                        "Runtime status check failed",
                    )
            except Exception as ex:
                details["runtime_text"] = bi(
                    f"运行状态检测失败：{ex}",
                    f"Runtime status check failed: {ex}",
                )
        else:
            details["runtime_text"] = bi(
                "OpenClaw 未安装，跳过运行状态检测",
                "OpenClaw not installed; runtime check skipped",
            )

        details["install_ready"] = bool(
            details["node_ok"]
            and details["npm_ok"]
            and details["git_ok"]
            and details["source_ok"]
        )

        return details

    def _run_install(self) -> None:
        self.bus.install_progress.emit(
            5, bi("开始安装流程", "Starting installation flow")
        )
        self.bus.log.emit(
            f"[{self._now()}] "
            f"{bi('开始安装 OpenClaw。', 'OpenClaw installation started.')}"
        )

        # 步骤1：切换 npm 安装源到镜像
        self.bus.install_progress.emit(
            15, bi("配置 npm 镜像源", "Setting npm mirror")
        )
        registry_cmd = [
            self.config.npm_cmd,
            "config",
            "set",
            "registry",
            self.config.npm_registry,
        ]
        registry_result = self._run_subprocess(
            registry_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            shell=False,
        )
        if registry_result.returncode != 0:
            message = (
                registry_result.stderr.strip()
                or registry_result.stdout.strip()
                or bi("配置 npm 源失败", "Failed to set npm registry")
            )
            self.bus.install_finished.emit(False, message)
            return

        self.bus.log.emit(
            f"[{self._now()}] npm 源已设置为：{self.config.npm_registry}"
        )

        # 步骤1.5：确认 git 可用，否则 npm 可能报 ENOENT spawn git
        try:
            git_cmd = resolve_command_for_system("git")
            git_result = self._run_subprocess(
                [git_cmd, "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                shell=False,
            )
            if git_result.returncode != 0:
                self.bus.install_finished.emit(
                    False,
                    bi(
                        "检测到 git 不可用，无法继续安装。"
                        "请先执行“一键修复环境”安装 git。",
                        "git is unavailable, install cannot continue. "
                        "Run One-click Repair to install git first.",
                    ),
                )
                return
        except Exception:
            self.bus.install_finished.emit(
                False,
                bi(
                    "检测到 git 不可用，无法继续安装。"
                    "请先执行“一键修复环境”安装 git。",
                    "git is unavailable, install cannot continue. "
                    "Run One-click Repair to install git first.",
                ),
            )
            return

        # 步骤2：执行安装
        self.bus.install_progress.emit(
            25, bi("安装 openclaw@latest", "Installing openclaw@latest")
        )
        install_cmd = [
            self.config.npm_cmd,
            "install",
            "-g",
            "openclaw@latest",
            "--registry",
            self.config.npm_registry,
        ]
        self.bus.log.emit(f"[{self._now()}] 执行命令：{' '.join(install_cmd)}")

        try:
            process = self._popen_subprocess(
                install_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                shell=False,
            )
        except Exception as ex:
            self.bus.install_finished.emit(
                False,
                bi(f"启动安装失败：{ex}", f"Install start failed: {ex}"),
            )
            return

        progress = 25
        lines_seen = 0
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if line:
                self.bus.log.emit(f"[安装] {line}")
            lines_seen += 1
            if lines_seen % 2 == 0 and progress < 88:
                progress += 2
                self.bus.install_progress.emit(
                    progress,
                    bi("正在下载安装", "Downloading and installing"),
                )

        process.wait(timeout=1200)
        if process.returncode != 0:
            self.bus.install_finished.emit(
                False,
                bi("安装失败，请查看日志。", "Install failed, check logs."),
            )
            return

        # 步骤3：验证安装
        self.bus.install_progress.emit(
            92, bi("验证安装结果", "Verifying installation")
        )
        verify_cmd = [self.config.openclaw_cmd, "--version"]
        verify_result = self._run_subprocess(
            verify_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            shell=False,
        )
        if verify_result.returncode != 0:
            msg = (
                verify_result.stderr.strip()
                or verify_result.stdout.strip()
                or bi("安装后验证失败", "Post-install verification failed")
            )
            self.bus.install_finished.emit(False, msg)
            return

        version = verify_result.stdout.strip() or "未知版本"
        self.bus.install_progress.emit(100, bi("安装完成", "Install completed"))
        self.bus.install_finished.emit(
            True,
            bi(
                f"OpenClaw 安装成功，当前版本：{version}",
                f"OpenClaw installed successfully, version: {version}",
            ),
        )

    def _run_uninstall(self) -> None:
        self.bus.install_progress.emit(
            5, bi("开始卸载流程", "Starting uninstall flow")
        )
        self.bus.log.emit(
            f"[{self._now()}] "
            f"{bi('开始卸载 OpenClaw。', 'OpenClaw uninstall started.')}"
        )
        uninstall_cmd = [self.config.npm_cmd, "uninstall", "-g", "openclaw"]
        self.bus.install_progress.emit(
            30, bi("执行全局卸载", "Running global uninstall")
        )
        self.bus.log.emit(f"[{self._now()}] 执行命令：{' '.join(uninstall_cmd)}")
        uninstall_result = self._run_subprocess(
            uninstall_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
            shell=False,
        )
        if uninstall_result.returncode != 0:
            msg = (
                uninstall_result.stderr.strip()
                or uninstall_result.stdout.strip()
                or bi("卸载失败，请查看日志。", "Uninstall failed, check logs.")
            )
            self.bus.install_finished.emit(False, msg)
            return

        self.bus.install_progress.emit(
            80, bi("验证卸载结果", "Verifying uninstall")
        )
        verify_cmd = [self.config.openclaw_cmd, "--version"]
        verify_msg = bi(
            "OpenClaw 已从 npm 全局卸载。",
            "OpenClaw has been removed from global npm.",
        )
        try:
            verify_result = self._run_subprocess(
                verify_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=20,
                shell=False,
            )
            if verify_result.returncode == 0:
                still_version = verify_result.stdout.strip() or "unknown"
                verify_msg = bi(
                    "npm 卸载已完成，但 openclaw 命令仍可用，"
                    f"版本：{still_version}。可能来自其他安装方式。",
                    "npm uninstall completed, but openclaw "
                    "is still available, "
                    "version: "
                    f"{still_version}. It may come from another install.",
                )
        except FileNotFoundError:
            pass

        self.bus.install_progress.emit(100, bi("卸载完成", "Uninstall completed"))
        self.bus.install_finished.emit(True, verify_msg)

    def _run_repair_env(self) -> None:
        self.bus.install_progress.emit(5, bi("开始环境修复", "Starting repair"))
        self.bus.log.emit(
            f"[{self._now()}] {bi('开始环境修复。', 'Environment repair started.')}"
        )
        has_error = False
        repaired_steps = []

        details = self._collect_environment_details()
        node_ok = bool(details.get("node_ok"))
        if not node_ok:
            self.bus.install_progress.emit(
                20, bi("修复 Node 环境", "Repairing Node environment")
            )
            node_hint = bi(
                "Node 不可用或版本过低，尝试自动安装 Node LTS。",
                "Node is missing or too old;"
                " trying automatic Node LTS install.",
            )
            self.bus.log.emit(
                f"[{self._now()}] {node_hint}"
            )
            install_node_cmd = [
                self.config.winget_cmd,
                "install",
                "-e",
                "--id",
                "OpenJS.NodeJS.LTS",
                "--accept-package-agreements",
                "--accept-source-agreements",
                "--silent",
            ]
            node_result = self._run_subprocess(
                install_node_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=600,
                shell=False,
            )
            if node_result.returncode != 0:
                has_error = True
                err_text = (
                    node_result.stderr.strip() or node_result.stdout.strip()
                )
                self.bus.log.emit(f"[修复] Node 安装失败：{err_text}")
            else:
                repaired_steps.append("Node LTS 安装完成")
                self.bus.log.emit(
                    f"[{bi('修复', 'Repair')}] "
                    f"{bi('Node LTS 安装完成。', 'Node LTS installed.')}"
                )
                # 新装系统里 PATH 可能未即时刷新，主动重定位 node/npm。
                self.config.node_cmd = resolve_command_for_system("node")
                self.config.npm_cmd = resolve_command_for_system("npm")
                self.bus.log.emit(
                    f"[{bi('修复', 'Repair')}] "
                    f"{bi('已重定位命令路径', 'Command paths refreshed')}: "
                    f"node={self.config.node_cmd}, npm={self.config.npm_cmd}"
                )

        git_ok = bool(details.get("git_ok"))
        if not git_ok and is_windows():
            self.bus.install_progress.emit(
                40, bi("修复 Git 环境", "Repairing Git environment")
            )
            self.bus.log.emit(
                f"[{self._now()}] "
                f"{bi('Git 不可用，尝试自动安装 Git。', 'Git missing, trying auto install.')}"
            )
            install_git_cmd = [
                self.config.winget_cmd,
                "install",
                "-e",
                "--id",
                "Git.Git",
                "--accept-package-agreements",
                "--accept-source-agreements",
                "--silent",
            ]
            git_result = self._run_subprocess(
                install_git_cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=600,
                shell=False,
            )
            if git_result.returncode != 0:
                has_error = True
                err_text = (
                    git_result.stderr.strip() or git_result.stdout.strip()
                )
                self.bus.log.emit(f"[修复] Git 安装失败：{err_text}")
            else:
                repaired_steps.append(bi("Git 安装完成", "Git installed"))
                self.bus.log.emit(
                    f"[{bi('修复', 'Repair')}] "
                    f"{bi('Git 安装完成。', 'Git installed.')}"
                )

        self.bus.install_progress.emit(
            55, bi("修复 npm 镜像配置", "Repairing npm mirror settings")
        )
        self.config.node_cmd = resolve_command_for_system(self.config.node_cmd)
        self.config.npm_cmd = resolve_command_for_system(self.config.npm_cmd)
        set_registry_cmd = [
            self.config.npm_cmd,
            "config",
            "set",
            "registry",
            self.config.npm_registry,
        ]
        registry_result = self._run_subprocess(
            set_registry_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            shell=False,
        )
        if registry_result.returncode != 0:
            has_error = True
            err_text = (
                registry_result.stderr.strip()
                or registry_result.stdout.strip()
            )
            self.bus.log.emit(f"[修复] npm 源设置失败：{err_text}")
        else:
            repaired_steps.append(
                bi("npm registry 已修复", "npm registry repaired")
            )
            self.bus.log.emit(
                f"[修复] npm registry 已设置为：{self.config.npm_registry}"
            )

        self.bus.install_progress.emit(
            75, bi("验证 npm 缓存", "Verifying npm cache")
        )
        npm_cache_cmd = [self.config.npm_cmd, "cache", "verify"]
        cache_result = self._run_subprocess(
            npm_cache_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=180,
            shell=False,
        )
        if cache_result.returncode != 0:
            self.bus.log.emit(
                "[修复] npm cache verify 失败，继续执行："
                f"{cache_result.stderr.strip() or cache_result.stdout.strip()}"
            )
        else:
            repaired_steps.append(
                bi("npm cache 校验完成", "npm cache verified")
            )
            self.bus.log.emit(
                f"[{bi('修复', 'Repair')}] "
                f"{bi('npm cache 校验完成。', 'npm cache verify done.')}"
            )

        self.bus.install_progress.emit(
            90, bi("复检环境", "Re-checking environment")
        )
        new_details = self._collect_environment_details()
        self.bus.env_result.emit(new_details)

        if has_error or not bool(new_details.get("install_ready")):
            self.bus.install_progress.emit(
                100, bi("修复结束（部分失败）", "Repair completed (partial)")
            )
            self.bus.install_finished.emit(
                False,
                bi(
                    "环境修复执行完成，但仍存在问题。请查看日志后手动处理。",
                    "Repair completed but issues remain."
                    " Check logs and fix manually.",
                ),
            )
            return

        summary = (
            "；".join(repaired_steps)
            if repaired_steps
            else "无需修复"
        )
        self.bus.install_progress.emit(100, bi("修复完成", "Repair completed"))
        self.bus.install_finished.emit(
            True,
            f"环境修复完成：{summary}",
        )

    def _run_onboard_wizard(self) -> None:
        self.bus.install_progress.emit(
            10, bi("启动初始化向导", "Launching onboard wizard")
        )
        self.bus.log.emit(
            f"[{self._now()}] "
            f"{bi('启动 OpenClaw 初始化向导。', 'Starting OpenClaw onboard wizard.')}"
        )

        openclaw_exec = self._resolve_command(self.config.openclaw_cmd)
        self.bus.log.emit(
            f"[{self._now()}] "
            f"{bi('初始化命令解析为', 'Resolved onboarding command')}: "
            f"{openclaw_exec}"
        )

        if self._is_windows():
            onboard_line = (
                f"\"{openclaw_exec}\" onboard --install-daemon"
            )
            launch_cmd = [
                "cmd.exe",
                "/c",
                "start",
                "OpenClaw Onboard",
                "cmd.exe",
                "/k",
                onboard_line,
            ]
        else:
            launch_cmd = [
                openclaw_exec,
                "onboard",
                "--install-daemon",
            ]
        try:
            self._popen_subprocess(launch_cmd, shell=False)
        except Exception as ex:
            self.bus.install_finished.emit(
                False,
                bi(
                    f"启动初始化向导失败：{ex}",
                    f"Failed to launch onboard wizard: {ex}",
                ),
            )
            return

        self.bus.install_progress.emit(
            100, bi("初始化向导已打开", "Onboard wizard opened")
        )
        self.bus.install_finished.emit(
            True,
            bi(
                "已打开独立终端执行初始化向导，请按终端提示完成配置。",
                "Opened a terminal for onboarding. Follow prompts there.",
            ),
        )

    def _run_package_exe(self) -> None:
        self.bus.install_progress.emit(
            5, bi("准备打包环境", "Preparing packaging environment")
        )
        self.bus.log.emit(
            f"[{self._now()}] {bi('开始打包 EXE。', 'EXE packaging started.')}"
        )

        pip_install_cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-U",
            "pyinstaller",
            "-i",
            "https://mirrors.aliyun.com/pypi/simple/",
        ]
        self.bus.log.emit(
            f"[{self._now()}] 安装 PyInstaller：{' '.join(pip_install_cmd)}"
        )
        pip_result = self._run_subprocess(
            pip_install_cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=600,
            cwd=str(APP_DIR),
            shell=False,
        )
        if pip_result.returncode != 0:
            self.bus.install_finished.emit(
                False,
                pip_result.stderr.strip()
                or pip_result.stdout.strip()
                or bi("PyInstaller 安装失败", "PyInstaller install failed"),
            )
            return

        self.bus.install_progress.emit(
            35, bi("执行 PyInstaller 打包", "Running PyInstaller build")
        )
        pack_cmd = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onefile",
            "--windowed",
            "--name",
            "OpenClaw助手",
            "--icon",
            str(ICON_RELATIVE_PATH),
            "--add-data",
            f"{ICON_RELATIVE_PATH};image",
            "app.py",
        ]
        self.bus.log.emit(f"[{self._now()}] 打包命令：{' '.join(pack_cmd)}")
        try:
            process = self._popen_subprocess(
                pack_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                cwd=str(APP_DIR),
                shell=False,
            )
        except Exception as ex:
            self.bus.install_finished.emit(
                False,
                bi(f"启动打包失败：{ex}", f"Package start failed: {ex}"),
            )
            return

        progress = 35
        lines_seen = 0
        assert process.stdout is not None
        for raw_line in process.stdout:
            line = raw_line.strip()
            if line:
                self.bus.log.emit(f"[打包] {line}")
            lines_seen += 1
            if lines_seen % 3 == 0 and progress < 92:
                progress += 2
                self.bus.install_progress.emit(
                    progress, bi("正在打包 EXE", "Packaging EXE")
                )

        process.wait(timeout=1800)
        if process.returncode != 0:
            self.bus.install_finished.emit(
                False,
                bi("打包失败，请查看日志。", "Packaging failed, check logs."),
            )
            return

        exe_path = APP_DIR / "dist" / "OpenClaw助手.exe"
        self.bus.install_progress.emit(100, bi("打包完成", "Packaging completed"))
        self.bus.install_finished.emit(
            True,
            bi(f"打包成功：{exe_path}", f"Packaging succeeded: {exe_path}"),
        )

    @staticmethod
    def _extract_major_version(version_text: str) -> int:
        match = re.search(r"(\d+)", version_text)
        if not match:
            return 0
        return int(match.group(1))

    @staticmethod
    def _now() -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class OpenClawGui(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(
            bi("OpenClaw 本地助手控制台", "OpenClaw Local Assistant Console")
        )
        icon_path = app_resource_path(ICON_RELATIVE_PATH)
        if icon_path.exists():
            self.setWindowIcon(QIcon(str(icon_path)))
        self.resize(1280, 860)
        self.thread_pool = QThreadPool.globalInstance()
        self.bus = SignalBus()
        self.config = self._load_config()
        set_lang_mode(self.config.language_mode)
        self.running = False
        self.env_install_ready = False
        self.last_env_details: dict | None = None
        self.startup_check_pending = False
        self.startup_stage_state = {
            "env": {"status": "pending", "detail": ""},
            "install": {"status": "pending", "detail": ""},
            "runtime": {"status": "pending", "detail": ""},
        }
        self.field_labels: dict[str, QLabel] = {}
        self.field_editors: dict[str, QLineEdit] = {}
        self.action_buttons: list[QPushButton] = []

        self._build_ui()
        self._bind_signals()
        self._apply_config_to_ui()
        self._apply_theme()
        self._log(
            bi(
                "应用已启动。请先执行“环境检测”。",
                "App started. Please run environment check first.",
            )
        )
        QTimer.singleShot(600, self._auto_startup_check)

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(16, 16, 16, 16)
        root_layout.setSpacing(12)

        header_row = QHBoxLayout()
        header_row.setSpacing(12)
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        self.title_label = QLabel(
            bi("OpenClaw 桌面控制台", "OpenClaw Desktop Console")
        )
        self.title_label.setObjectName("titleLabel")
        self.subtitle_label = QLabel(
            bi(
                "集成环境检测、一键安装、部署配置与本地会话聊天。",
                "Environment checks, one-click install, setup and local chat.",
            )
        )
        self.subtitle_label.setObjectName("subTitleLabel")
        title_col.addWidget(self.title_label)
        title_col.addWidget(self.subtitle_label)
        header_row.addLayout(title_col, 1)

        lang_box = QHBoxLayout()
        lang_box.setSpacing(8)
        self.language_label = QLabel(
            bi("程序界面语言", "App UI Language")
        )
        self.language_label.setObjectName("fieldLabel")
        self.cmb_language = QComboBox()
        self.cmb_language.addItem("中文", "zh")
        self.cmb_language.addItem("English", "en")
        self.cmb_language.addItem("中文 / English", "bilingual")
        lang_box.addWidget(self.language_label)
        lang_box.addWidget(self.cmb_language)
        header_row.addLayout(lang_box)
        root_layout.addLayout(header_row)

        self.tabs = QTabWidget()
        self.setup_tab = self._create_setup_tab()
        self.chat_tab = self._create_chat_tab()
        self.log_tab = self._create_log_tab()
        self.tabs.addTab(self.setup_tab, bi("引导与安装", "Setup & Install"))
        self.tabs.addTab(self.chat_tab, bi("会话聊天", "Chat"))
        self.tabs.addTab(self.log_tab, bi("运行日志", "Logs"))

        root_layout.addWidget(self.tabs, 1)
        self.setCentralWidget(root)
        self.setStatusBar(QStatusBar())
        self.statusBar().showMessage(bi("就绪", "Ready"))

    def _create_setup_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        self.wizard_header = QLabel(bi("快速引导", "Quick Wizard"))
        self.wizard_header.setObjectName("sectionHeader")
        layout.addWidget(self.wizard_header)

        wizard_card = QFrame()
        wizard_card.setObjectName("subCard")
        wizard_layout = QVBoxLayout(wizard_card)
        wizard_layout.setContentsMargins(10, 10, 10, 10)
        wizard_layout.setSpacing(6)
        self.wizard_steps_label = QLabel(
            bi(
                "1) 保存配置  2) 环境检测  3) 一键修复(可选)  "
                "4) 一键安装  5) 一键初始化向导  6) 去聊天页开始对话",
                "1) Save  2) Env Check  3) Repair(optional)  "
                "4) Install  5) Onboard  6) Go to Chat tab",
            )
        )
        self.wizard_steps_label.setWordWrap(True)
        wizard_layout.addWidget(self.wizard_steps_label)
        layout.addWidget(wizard_card)

        self.btn_save = QPushButton(bi("保存配置", "Save"))
        self.btn_env_check = QPushButton(bi("环境检测", "Environment Check"))
        self.btn_repair = QPushButton(bi("一键修复环境", "One-click Repair"))
        self.btn_install = QPushButton(
            bi("一键安装 OpenClaw", "One-click Install OpenClaw")
        )
        self.btn_install.setEnabled(False)
        self.btn_uninstall = QPushButton(
            bi("一键卸载 OpenClaw", "One-click Uninstall OpenClaw")
        )
        self.btn_onboard = QPushButton(
            bi("设置向导", "Setup Wizard")
        )

        self.action_buttons = [
            self.btn_save,
            self.btn_env_check,
            self.btn_repair,
            self.btn_install,
            self.btn_uninstall,
            self.btn_onboard,
        ]

        op_card = QFrame()
        op_card.setObjectName("subCard")
        op_layout = QVBoxLayout(op_card)
        op_layout.setContentsMargins(10, 10, 10, 10)
        op_layout.setSpacing(8)
        self.op_header = QLabel(bi("操作步骤", "Action Steps"))
        self.op_header.setObjectName("sectionHeader")
        op_layout.addWidget(self.op_header)
        op_row = QHBoxLayout()
        op_row.setSpacing(8)
        for btn in self.action_buttons:
            op_row.addWidget(btn)
        op_layout.addLayout(op_row)
        self.next_hint_label = QLabel(
            bi(
                "建议下一步：请等待自动检测完成。",
                "Next suggestion: wait for auto check to finish.",
            )
        )
        self.next_hint_label.setObjectName("tipsLabel")
        self.next_hint_label.setWordWrap(True)
        op_layout.addWidget(self.next_hint_label)
        layout.addWidget(op_card)

        content_row = QHBoxLayout()
        content_row.setSpacing(12)
        left_col = QVBoxLayout()
        left_col.setSpacing(12)
        right_col = QVBoxLayout()
        right_col.setSpacing(12)

        config_card = QFrame()
        config_card.setObjectName("card")
        config_layout = QVBoxLayout(config_card)
        config_layout.setContentsMargins(12, 12, 12, 12)
        config_layout.setSpacing(10)

        self.config_header = QLabel(bi("部署配置", "Deployment Settings"))
        self.config_header.setObjectName("sectionHeader")
        config_layout.addWidget(self.config_header)

        form = QGridLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(10)
        form.setColumnStretch(1, 1)

        self.input_cmd = QLineEdit()
        self.input_node_cmd = QLineEdit()
        self.input_npm_cmd = QLineEdit()
        self.input_winget_cmd = QLineEdit()
        self.input_registry = QLineEdit()
        self.input_workdir = QLineEdit()
        self.input_thinking = QLineEdit()
        self.input_timeout = QLineEdit()
        self.input_extra = QLineEdit()
        self.input_test_message = QLineEdit()

        self.field_editors = {
            "openclaw_cmd": self.input_cmd,
            "node_cmd": self.input_node_cmd,
            "npm_cmd": self.input_npm_cmd,
            "winget_cmd": self.input_winget_cmd,
            "npm_registry": self.input_registry,
            "working_dir": self.input_workdir,
            "thinking_level": self.input_thinking,
            "timeout_seconds": self.input_timeout,
            "extra_args": self.input_extra,
            "test_message": self.input_test_message,
        }
        field_meta = [
            (
                "openclaw_cmd",
                "OpenClaw 命令",
                "OpenClaw Command",
                "例如：openclaw",
                "Example: openclaw",
            ),
            (
                "node_cmd",
                "Node 命令",
                "Node Command",
                "例如：node",
                "Example: node",
            ),
            (
                "npm_cmd",
                "npm 命令",
                "npm Command",
                "例如：npm",
                "Example: npm",
            ),
            (
                "winget_cmd",
                "winget 命令",
                "winget Command",
                "例如：winget",
                "Example: winget",
            ),
            (
                "npm_registry",
                "npm 安装源",
                "npm Registry",
                "默认：https://registry.npmmirror.com",
                "Default: https://registry.npmmirror.com",
            ),
            (
                "working_dir",
                "工作目录",
                "Working Directory",
                "可留空，默认当前目录",
                "Optional, defaults to current dir",
            ),
            (
                "thinking_level",
                "思考等级",
                "Thinking Level",
                "off|minimal|low|medium|high",
                "off|minimal|low|medium|high",
            ),
            (
                "timeout_seconds",
                "超时秒数",
                "Timeout Seconds",
                "例如：120",
                "Example: 120",
            ),
            (
                "extra_args",
                "附加参数",
                "Extra Args",
                "例如：--verbose",
                "Example: --verbose",
            ),
            (
                "test_message",
                "检测消息",
                "Check Message",
                "用于连接测试",
                "Used for connectivity test",
            ),
        ]
        for row, meta in enumerate(field_meta):
            key, zh_label, en_label, zh_ph, en_ph = meta
            label = QLabel(bi(zh_label, en_label))
            label.setObjectName("fieldLabel")
            editor = self.field_editors[key]
            editor.setPlaceholderText(bi(zh_ph, en_ph))
            self.field_labels[key] = label
            form.addWidget(label, row, 0)
            form.addWidget(editor, row, 1)
        config_layout.addLayout(form)

        left_col.addWidget(config_card)

        env_card = QFrame()
        env_card.setObjectName("card")
        env_layout = QVBoxLayout(env_card)
        env_layout.setContentsMargins(12, 12, 12, 12)
        env_layout.setSpacing(8)
        self.env_header = QLabel(bi("环境检测结果", "Environment Status"))
        self.env_header.setObjectName("sectionHeader")
        env_layout.addWidget(self.env_header)
        env_grid = QGridLayout()
        env_grid.setHorizontalSpacing(10)
        env_grid.setVerticalSpacing(8)

        self.lbl_node = QLabel(bi("Node：未检测", "Node: Not checked"))
        self.lbl_npm = QLabel(bi("npm：未检测", "npm: Not checked"))
        self.lbl_git = QLabel(bi("git：未检测", "git: Not checked"))
        self.lbl_source = QLabel(
            bi("安装源：未检测", "Registry source: Not checked")
        )
        self.lbl_openclaw = QLabel(
            bi("OpenClaw：未检测", "OpenClaw: Not checked")
        )
        self.lbl_runtime = QLabel(
            bi("运行状态：未检测", "Runtime status: Not checked")
        )
        self.lbl_summary = QLabel(
            bi("综合：请先点击环境检测", "Summary: run environment check first")
        )
        self.lbl_summary.setObjectName("summaryWarn")
        env_grid.addWidget(self.lbl_node, 0, 0)
        env_grid.addWidget(self.lbl_npm, 1, 0)
        env_grid.addWidget(self.lbl_git, 2, 0)
        env_grid.addWidget(self.lbl_source, 3, 0)
        env_grid.addWidget(self.lbl_openclaw, 4, 0)
        env_grid.addWidget(self.lbl_runtime, 5, 0)
        env_grid.addWidget(self.lbl_summary, 6, 0)
        env_layout.addLayout(env_grid)
        right_col.addWidget(env_card)

        action_card = QFrame()
        action_card.setObjectName("card")
        action_layout = QVBoxLayout(action_card)
        action_layout.setContentsMargins(12, 12, 12, 12)
        action_layout.setSpacing(8)
        self.action_header = QLabel(bi("安装与维护", "Install & Maintenance"))
        self.action_header.setObjectName("sectionHeader")
        action_layout.addWidget(self.action_header)

        self.lbl_install_hint = QLabel(
            bi(
                "仅在检测全部通过后可点击安装",
                "Install enabled only when all checks pass",
            )
        )
        self.lbl_install_hint.setObjectName("summaryWarn")
        action_layout.addWidget(self.lbl_install_hint)

        self.btn_package = QPushButton(
            bi("一键打包 EXE", "One-click Package EXE")
        )
        self.btn_package.setVisible(False)

        self.install_progress = QProgressBar()
        self.install_progress.setRange(0, 100)
        self.install_progress.setValue(0)
        self.install_progress.setFormat("%p%")
        self.install_progress.setVisible(False)
        action_layout.addWidget(self.install_progress)

        self.install_status = QLabel(
            bi("任务状态：待开始", "Task status: Pending")
        )
        self.install_status.setObjectName("fieldLabel")
        self.install_status.setVisible(True)
        action_layout.addWidget(self.install_status)
        self.startup_checklist_label = QLabel()
        self.startup_checklist_label.setObjectName("tipsLabel")
        self.startup_checklist_label.setWordWrap(True)
        action_layout.addWidget(self.startup_checklist_label)

        self.tips_label = QLabel(
            bi(
                "推荐流程：保存配置 -> 环境检测 -> 一键修复（如需） -> "
                "一键安装 -> 设置向导 -> 连接检测 -> 开始聊天。",
                "Recommended: Save -> Env Check -> Repair(if needed) -> "
                "Install -> Setup Wizard -> Connection Test -> Chat.",
            )
        )
        self.tips_label.setObjectName("tipsLabel")
        self.tips_label.setWordWrap(True)
        self.tips_label.setVisible(False)
        action_layout.addWidget(self.tips_label)
        right_col.addWidget(action_card)
        right_col.addStretch(1)

        left_box = QWidget()
        left_box.setLayout(left_col)
        right_box = QWidget()
        right_box.setLayout(right_col)
        content_row.addWidget(left_box, 6)
        content_row.addWidget(right_box, 5)
        layout.addLayout(content_row, 1)
        layout.addStretch(1)
        return page

    def _create_chat_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        self.chat_header = QLabel(bi("会话聊天", "Chat Session"))
        self.chat_header.setObjectName("sectionHeader")
        layout.addWidget(self.chat_header)
        self.chat_note_label = QLabel(
            bi(
                "本会话窗口仅作调试用途，请使用命令行 openclaw tui "
                "或部署飞书等频道（Channel）进行会话。",
                "This chat window is for debugging only. "
                "Use `openclaw tui` or channels such as Feishu for real chats.",
            )
        )
        self.chat_note_label.setObjectName("tipsLabel")
        self.chat_note_label.setWordWrap(True)
        layout.addWidget(self.chat_note_label)

        self.chat_list = QListWidget()
        self.chat_list.setAlternatingRowColors(False)
        self.chat_list.setStyleSheet(
            "QListWidget { background: #ffffff; color: #000000; }"
        )
        layout.addWidget(self.chat_list, 1)

        self.input_message = QPlainTextEdit()
        self.input_message.setPlaceholderText(
            bi(
                "请输入你要发送给 OpenClaw 的消息...",
                "Type your message to OpenClaw...",
            )
        )
        self.input_message.setStyleSheet(
            "QPlainTextEdit { background: #ffffff; color: #000000; }"
        )
        self.input_message.setFixedHeight(120)
        layout.addWidget(self.input_message)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.btn_chat_check = QPushButton(
            bi("检测连接", "Connection Test")
        )
        self.btn_clear = QPushButton(bi("清空会话", "Clear"))
        self.btn_send = QPushButton(bi("发送消息", "Send"))
        row.addWidget(self.btn_chat_check)
        row.addWidget(self.btn_clear)
        row.addStretch(1)
        row.addWidget(self.btn_send)
        layout.addLayout(row)
        return page

    def _create_log_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)
        self.log_header = QLabel(bi("运行日志", "Runtime Logs"))
        self.log_header.setObjectName("sectionHeader")
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMinimumHeight(220)
        layout.addWidget(self.log_header)
        layout.addWidget(self.log_view, 1)
        return page

    def _bind_signals(self) -> None:
        self.btn_save.clicked.connect(self._save_from_ui)
        self.btn_env_check.clicked.connect(self._environment_check)
        self.btn_install.clicked.connect(self._install_openclaw)
        self.btn_uninstall.clicked.connect(self._uninstall_openclaw)
        self.btn_repair.clicked.connect(self._repair_environment)
        self.btn_onboard.clicked.connect(self._start_onboard_wizard)
        self.btn_package.clicked.connect(self._package_exe)
        self.btn_send.clicked.connect(self._send_message)
        self.btn_chat_check.clicked.connect(self._check_connection)
        self.btn_clear.clicked.connect(self.chat_list.clear)
        self.cmb_language.currentIndexChanged.connect(
            self._on_language_changed
        )

        self.bus.log.connect(self._log)
        self.bus.chat_reply.connect(self._append_chat)
        self.bus.task_done.connect(self._on_task_done)
        self.bus.check_result.connect(self._on_check_result)
        self.bus.env_result.connect(self._on_env_result)
        self.bus.startup_progress.connect(self._on_startup_progress)
        self.bus.startup_stage.connect(self._on_startup_stage)
        self.bus.install_progress.connect(self._on_install_progress)
        self.bus.install_finished.connect(self._on_install_finished)
        self.bus.error.connect(self._on_error)

    def _on_language_changed(self) -> None:
        mode = self.cmb_language.currentData()
        if mode is None:
            return
        set_lang_mode(str(mode))
        self._refresh_language_texts()
        self._save_from_ui(silent=True)
        if not self.running:
            self._environment_check()

    def _auto_startup_check(self) -> None:
        if self.running:
            return
        self.startup_check_pending = True
        self.startup_stage_state = {
            "env": {"status": "pending", "detail": ""},
            "install": {"status": "pending", "detail": ""},
            "runtime": {"status": "pending", "detail": ""},
        }
        self._refresh_startup_checklist()
        self.tabs.setEnabled(False)
        self.install_progress.setVisible(True)
        self.install_status.setVisible(True)
        self.install_progress.setValue(0)
        self.install_status.setText(
            bi("启动检测：等待开始", "Startup check: waiting to start")
        )
        self._set_suggested_button(self.btn_env_check)
        self.next_hint_label.setText(
            bi(
                "建议下一步：正在自动检测环境，请稍候。",
                "Next suggestion: auto-checking environment, please wait.",
            )
        )
        self._environment_check()

    def _set_suggested_button(self, target: QPushButton | None) -> None:
        for btn in self.action_buttons:
            btn.setProperty("suggested", btn is target)
            btn.style().unpolish(btn)
            btn.style().polish(btn)

    def _update_next_action_hint(self, details: dict) -> None:
        if details.get("runtime_ok"):
            self._set_suggested_button(None)
            self.next_hint_label.setText(
                bi(
                    "检测到会话已在运行，已初始化完成。"
                    "可直接进入会话页进行调试。",
                    "Detected active session; initialization is complete. "
                    "You can go to Chat tab directly.",
                )
            )
            return
        if (
            not details.get("node_ok")
            or not details.get("npm_ok")
            or not details.get("git_ok")
        ):
            self._set_suggested_button(self.btn_repair)
            self.next_hint_label.setText(
                bi(
                    "建议下一步：先点“一键修复环境”，"
                    "修复 Node/npm/git 后再安装。",
                    "Next suggestion: click One-click Repair first, "
                    "then install after node/npm/git are fixed.",
                )
            )
            return
        if not details.get("source_ok"):
            self._set_suggested_button(self.btn_env_check)
            self.next_hint_label.setText(
                bi(
                    "建议下一步：检查网络或镜像源配置，再重新环境检测。",
                    "Next suggestion: check network/registry settings "
                    "and run environment check again.",
                )
            )
            return
        if not details.get("openclaw_ok"):
            self._set_suggested_button(self.btn_install)
            self.next_hint_label.setText(
                bi(
                    "建议下一步：环境已满足，点击“一键安装 OpenClaw”。",
                    "Next suggestion: environment is ready, "
                    "click One-click Install OpenClaw.",
                )
            )
            return

        self._set_suggested_button(self.btn_onboard)
        self.next_hint_label.setText(
            bi(
                "建议下一步：OpenClaw 已安装，先完成设置向导，"
                "再做连接检测。",
                "Next suggestion: OpenClaw is installed. "
                "Run setup wizard first, then connection test.",
            )
        )

    def _update_stage_panel(self, details: dict) -> None:
        _ = details

    def _status_tag(self, status: str) -> str:
        mapping = {
            "pending": bi("未开始", "Pending"),
            "running": bi("进行中", "Running"),
            "ok": bi("通过", "Passed"),
            "fail": bi("失败", "Failed"),
            "skip": bi("跳过", "Skipped"),
        }
        return mapping.get(status, bi("未知", "Unknown"))

    def _refresh_startup_checklist(self) -> None:
        env = self.startup_stage_state["env"]
        install = self.startup_stage_state["install"]
        runtime = self.startup_stage_state["runtime"]
        text = bi(
            "启动检测明细<br>"
            f"1) 运行环境检测：{self._status_tag(env['status'])}<br>"
            f"{env['detail']}<br>"
            f"2) 安装状态检测：{self._status_tag(install['status'])}<br>"
            f"{install['detail']}<br>"
            f"3) OpenClaw 运行状态检测：{self._status_tag(runtime['status'])}<br>"
            f"{runtime['detail']}",
            "Startup diagnostic details<br>"
            f"1) Runtime environment: {self._status_tag(env['status'])}<br>"
            f"{env['detail']}<br>"
            f"2) Install status: {self._status_tag(install['status'])}<br>"
            f"{install['detail']}<br>"
            f"3) OpenClaw runtime state: {self._status_tag(runtime['status'])}<br>"
            f"{runtime['detail']}",
        )
        self.startup_checklist_label.setText(text)

    def _refresh_language_texts(self) -> None:
        self.setWindowTitle(
            bi("OpenClaw 本地助手控制台", "OpenClaw Local Assistant Console")
        )
        self.title_label.setText(
            bi("OpenClaw 桌面控制台", "OpenClaw Desktop Console")
        )
        self.subtitle_label.setText(
            bi(
                "集成环境检测、一键安装、部署配置与本地会话聊天。",
                "Environment checks, one-click install, setup and local chat.",
            )
        )
        self.config_header.setText(bi("部署配置", "Deployment Settings"))
        self.wizard_header.setText(bi("快速引导", "Quick Wizard"))
        self.op_header.setText(bi("操作步骤", "Action Steps"))
        self.env_header.setText(bi("环境检测结果", "Environment Status"))
        self.action_header.setText(bi("安装与维护", "Install & Maintenance"))
        self.log_header.setText(bi("运行日志", "Runtime Logs"))
        self.chat_header.setText(bi("会话聊天", "Chat Session"))
        self.chat_note_label.setText(
            bi(
                "本会话窗口仅作调试用途，请使用命令行 openclaw tui "
                "或部署飞书等频道（Channel）进行会话。",
                "This chat window is for debugging only. "
                "Use `openclaw tui` or channels such as Feishu for real chats.",
            )
        )
        self.wizard_steps_label.setText(
            bi(
                "1) 保存配置  2) 环境检测  3) 一键修复(可选)  "
                "4) 一键安装  5) 一键初始化向导  6) 去聊天页开始对话",
                "1) Save  2) Env Check  3) Repair(optional)  "
                "4) Install  5) Onboard  6) Go to Chat tab",
            )
        )
        self.language_label.setText(
            bi("程序界面语言", "App UI Language")
        )
        self.btn_save.setText(bi("保存配置", "Save"))
        self.btn_env_check.setText(bi("环境检测", "Environment Check"))
        self.btn_install.setText(
            bi("一键安装 OpenClaw", "One-click Install OpenClaw")
        )
        self.btn_uninstall.setText(
            bi("一键卸载 OpenClaw", "One-click Uninstall OpenClaw")
        )
        self.btn_repair.setText(bi("一键修复环境", "One-click Repair"))
        self.btn_onboard.setText(
            bi("设置向导", "Setup Wizard")
        )
        self.btn_package.setText(bi("一键打包 EXE", "One-click Package EXE"))
        self.btn_chat_check.setText(bi("检测连接", "Connection Test"))
        self.btn_clear.setText(bi("清空会话", "Clear"))
        self.btn_send.setText(bi("发送消息", "Send"))
        self.tabs.setTabText(0, bi("引导与安装", "Setup & Install"))
        self.tabs.setTabText(1, bi("会话聊天", "Chat"))
        self.tabs.setTabText(2, bi("运行日志", "Logs"))
        self.lbl_install_hint.setText(
            bi(
                "仅在检测全部通过后可点击安装",
                "Install enabled only when all checks pass",
            )
        )
        self.install_status.setText(
            bi("启动检测：待执行", "Startup check: pending")
        )
        self._refresh_startup_checklist()
        self.tips_label.setText(
            bi(
                "推荐流程：保存配置 -> 环境检测 -> 一键修复（如需） -> "
                "一键安装 -> 设置向导 -> 连接检测 -> 开始聊天。",
                "Recommended: Save -> Env Check -> Repair(if needed) -> "
                "Install -> Setup Wizard -> Connection Test -> Chat.",
            )
        )
        if self.last_env_details is not None:
            self._update_next_action_hint(self.last_env_details)
        else:
            self.next_hint_label.setText(
                bi(
                    "建议下一步：请等待自动检测完成。",
                    "Next suggestion: wait for auto check to finish.",
                )
            )

        placeholders = {
            "openclaw_cmd": ("例如：openclaw", "Example: openclaw"),
            "node_cmd": ("例如：node", "Example: node"),
            "npm_cmd": ("例如：npm", "Example: npm"),
            "winget_cmd": ("例如：winget", "Example: winget"),
            "npm_registry": (
                "默认：https://registry.npmmirror.com",
                "Default: https://registry.npmmirror.com",
            ),
            "working_dir": (
                "可留空，默认当前目录",
                "Optional, defaults to current dir",
            ),
            "thinking_level": (
                "off|minimal|low|medium|high",
                "off|minimal|low|medium|high",
            ),
            "timeout_seconds": ("例如：120", "Example: 120"),
            "extra_args": ("例如：--verbose", "Example: --verbose"),
            "test_message": ("用于连接测试", "Used for connectivity test"),
        }
        labels = {
            "openclaw_cmd": ("OpenClaw 命令", "OpenClaw Command"),
            "node_cmd": ("Node 命令", "Node Command"),
            "npm_cmd": ("npm 命令", "npm Command"),
            "winget_cmd": ("winget 命令", "winget Command"),
            "npm_registry": ("npm 安装源", "npm Registry"),
            "working_dir": ("工作目录", "Working Directory"),
            "thinking_level": ("思考等级", "Thinking Level"),
            "timeout_seconds": ("超时秒数", "Timeout Seconds"),
            "extra_args": ("附加参数", "Extra Args"),
            "test_message": ("检测消息", "Check Message"),
        }
        for key, label in self.field_labels.items():
            zh_text, en_text = labels[key]
            label.setText(bi(zh_text, en_text))
            ph_zh, ph_en = placeholders[key]
            self.field_editors[key].setPlaceholderText(bi(ph_zh, ph_en))

        self.input_message.setPlaceholderText(
            bi(
                "请输入你要发送给 OpenClaw 的消息...",
                "Type your message to OpenClaw...",
            )
        )

    def _send_message(self) -> None:
        if self.running:
            QMessageBox.information(
                self,
                bi("提示", "Notice"),
                bi("有任务正在执行，请稍候。", "A task is running, please wait."),
            )
            return
        message = self.input_message.toPlainText().strip()
        if not message:
            QMessageBox.warning(
                self,
                bi("提示", "Notice"),
                bi("请输入消息内容。", "Please enter a message."),
            )
            return
        self._save_from_ui(silent=True)
        self._append_chat("我", message)
        self.input_message.clear()
        self._run_task(message, "chat")

    def _check_connection(self) -> None:
        if self.running:
            QMessageBox.information(
                self,
                bi("提示", "Notice"),
                bi("有任务正在执行，请稍候。", "A task is running, please wait."),
            )
            return
        self._save_from_ui(silent=True)
        self._run_task("", "check")

    def _environment_check(self) -> None:
        if self.running:
            QMessageBox.information(
                self,
                bi("提示", "Notice"),
                bi("有任务正在执行，请稍候。", "A task is running, please wait."),
            )
            return
        self._save_from_ui(silent=True)
        self._run_task("", "env_check")

    def _install_openclaw(self) -> None:
        if self.running:
            QMessageBox.information(
                self,
                bi("提示", "Notice"),
                bi("有任务正在执行，请稍候。", "A task is running, please wait."),
            )
            return
        if not self.env_install_ready:
            QMessageBox.warning(
                self,
                bi("提示", "Notice"),
                bi(
                    "环境检测未全部通过，无法安装。请先执行“环境检测”。",
                    "Not all checks passed. Run environment check first.",
                ),
            )
            return
        self._save_from_ui(silent=True)
        self._run_task("", "install")

    def _uninstall_openclaw(self) -> None:
        if self.running:
            QMessageBox.information(
                self,
                bi("提示", "Notice"),
                bi("有任务正在执行，请稍候。", "A task is running, please wait."),
            )
            return
        answer = QMessageBox.question(
            self,
            bi("确认卸载", "Confirm uninstall"),
            bi(
                "将仅卸载 npm 全局安装的 OpenClaw，"
                "不会卸载 Node/npm 等环境。确认继续？",
                "This only uninstalls globally installed OpenClaw via npm. "
                "Node/npm environment remains. Continue?",
            ),
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._save_from_ui(silent=True)
        self._run_task("", "uninstall")

    def _repair_environment(self) -> None:
        if self.running:
            QMessageBox.information(
                self,
                bi("提示", "Notice"),
                bi("有任务正在执行，请稍候。", "A task is running, please wait."),
            )
            return
        self._save_from_ui(silent=True)
        self._run_task("", "repair_env")

    def _start_onboard_wizard(self) -> None:
        if self.running:
            QMessageBox.information(
                self,
                bi("提示", "Notice"),
                bi("有任务正在执行，请稍候。", "A task is running, please wait."),
            )
            return
        self._save_from_ui(silent=True)
        dialog = OnboardWizardDialog(self.config, self)
        dialog.exec()

    def _package_exe(self) -> None:
        if self.running:
            QMessageBox.information(
                self,
                bi("提示", "Notice"),
                bi("有任务正在执行，请稍候。", "A task is running, please wait."),
            )
            return
        self._save_from_ui(silent=True)
        self._run_task("", "package_exe")

    def _run_task(self, message: str, command_type: str) -> None:
        self.running = True
        self._set_controls_enabled(False)
        self.statusBar().showMessage(bi("执行中...", "Running..."))
        task = CommandTask(self.bus, self.config, message, command_type)
        self.thread_pool.start(task)

    def _on_task_done(self) -> None:
        QTimer.singleShot(0, self._unlock_ui)

    def _unlock_ui(self) -> None:
        self.running = False
        self._set_controls_enabled(True)
        if not self.env_install_ready:
            self.btn_install.setEnabled(False)
        self.statusBar().showMessage(bi("就绪", "Ready"))

    def _set_controls_enabled(self, enabled: bool) -> None:
        self.btn_save.setEnabled(enabled)
        self.btn_env_check.setEnabled(enabled)
        self.btn_chat_check.setEnabled(enabled)
        self.btn_send.setEnabled(enabled)
        self.btn_clear.setEnabled(enabled)
        self.btn_repair.setEnabled(enabled)
        self.btn_uninstall.setEnabled(enabled)
        self.btn_onboard.setEnabled(enabled)
        self.btn_package.setEnabled(enabled)
        self.input_message.setEnabled(enabled)
        if enabled:
            self.btn_install.setEnabled(self.env_install_ready)
        else:
            self.btn_install.setEnabled(False)

    def _on_error(self, message: str) -> None:
        self._log(f"[错误] {message}")
        if self.startup_check_pending:
            self.startup_check_pending = False
            self.tabs.setEnabled(True)
        QMessageBox.critical(self, bi("执行失败", "Execution failed"), message)

    def _on_check_result(self, ok: bool, message: str) -> None:
        self._log(message)
        if ok:
            QMessageBox.information(
                self, bi("连接成功", "Connection success"), message
            )
        else:
            QMessageBox.warning(
                self, bi("连接失败", "Connection failed"), message
            )

    def _on_env_result(self, details: dict) -> None:
        self.last_env_details = details
        unknown_text = bi("未知", "Unknown")
        node_text = details.get("node_text", unknown_text)
        npm_text = details.get("npm_text", unknown_text)
        git_text = details.get("git_text", unknown_text)
        self.lbl_node.setText(
            f"{bi('Node', 'Node')}: {node_text}"
        )
        self.lbl_npm.setText(
            f"{bi('npm', 'npm')}: {npm_text}"
        )
        self.lbl_git.setText(
            f"{bi('git', 'git')}: {git_text}"
        )
        self.lbl_source.setText(
            f"{bi('安装源', 'Registry source')}: "
            f"{details.get('source_text', bi('未知', 'Unknown'))}"
        )
        self.lbl_openclaw.setText(
            f"{bi('OpenClaw', 'OpenClaw')}: "
            f"{details.get('openclaw_text', bi('未知', 'Unknown'))}"
        )
        self.lbl_runtime.setText(
            f"{bi('运行状态', 'Runtime status')}: "
            f"{details.get('runtime_text', bi('未知', 'Unknown'))}"
        )

        self.env_install_ready = bool(details.get("install_ready", False))
        if self.env_install_ready:
            self.lbl_summary.setText(
                bi(
                    "综合：通过，可进行一键安装",
                    "Summary: passed, one-click install enabled",
                )
            )
            self.lbl_summary.setObjectName("summaryOk")
            self.lbl_install_hint.setText(
                bi(
                    "环境通过，可点击一键安装",
                    "Checks passed, you can click one-click install",
                )
            )
        else:
            self.lbl_summary.setText(
                bi(
                    "综合：未通过，请按提示修复后重试",
                    "Summary: failed, fix issues and retry",
                )
            )
            self.lbl_summary.setObjectName("summaryWarn")
            self.lbl_install_hint.setText(
                bi(
                    "仅在检测全部通过后可点击安装",
                    "Install enabled only when all checks pass",
                )
            )
        if details.get("runtime_ok"):
            self.lbl_summary.setText(
                bi(
                    "综合：已初始化并运行中，可直接进入会话页调试",
                    "Summary: initialized and running, go to Chat directly",
                )
            )
            self.lbl_summary.setObjectName("summaryOk")
            self.lbl_install_hint.setText(
                bi(
                    "检测到活跃会话，可直接在会话页调试。",
                    "Active session detected. Debug directly in Chat tab.",
                )
            )
        self.lbl_summary.style().unpolish(self.lbl_summary)
        self.lbl_summary.style().polish(self.lbl_summary)
        self.btn_install.setEnabled(
            self.env_install_ready and not self.running
        )
        self._update_next_action_hint(details)
        if self.startup_check_pending:
            self.startup_check_pending = False
            self.tabs.setEnabled(True)
            self.tabs.setCurrentIndex(0)

    def _on_startup_progress(self, value: int, text: str) -> None:
        self.install_progress.setValue(value)
        self.install_status.setText(
            f"{bi('启动检测', 'Startup check')}: {text}"
        )

    def _on_startup_stage(self, stage: str, status: str, detail: str) -> None:
        if stage not in self.startup_stage_state:
            return
        self.startup_stage_state[stage]["status"] = status
        self.startup_stage_state[stage]["detail"] = detail
        self._refresh_startup_checklist()

    def _on_install_progress(self, value: int, text: str) -> None:
        _ = (value, text)

    def _on_install_finished(self, ok: bool, message: str) -> None:
        self._log(message)
        if ok:
            QMessageBox.information(self, bi("执行成功", "Success"), message)
            # 任务完成后自动刷新环境状态
            QTimer.singleShot(200, self._environment_check)
        else:
            QMessageBox.warning(
                self, bi("执行失败", "Failed"), message
            )

    def _append_chat(self, role: str, content: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        item = QListWidgetItem(f"[{ts}] {role}：\n{content}")
        item.setForeground(QColor("#000000"))
        self.chat_list.addItem(item)
        self.chat_list.scrollToBottom()

    def _save_from_ui(self, silent: bool = False) -> None:
        timeout_text = self.input_timeout.text().strip()
        timeout_value = 120
        if timeout_text:
            try:
                timeout_value = int(timeout_text)
                if timeout_value < 10:
                    timeout_value = 10
            except ValueError:
                timeout_value = 120

        self.config = AppConfig(
            openclaw_cmd=self.input_cmd.text().strip() or "openclaw",
            node_cmd=self.input_node_cmd.text().strip() or "node",
            npm_cmd=self.input_npm_cmd.text().strip() or "npm",
            winget_cmd=self.input_winget_cmd.text().strip() or "winget",
            npm_registry=(
                self.input_registry.text().strip()
                or "https://registry.npmmirror.com"
            ),
            language_mode=self.cmb_language.currentData() or "bilingual",
            working_dir=self.input_workdir.text().strip(),
            thinking_level=self.input_thinking.text().strip() or "medium",
            timeout_seconds=timeout_value,
            extra_args=self.input_extra.text().strip(),
            test_message=(
                self.input_test_message.text().strip()
                or "你好，请回复“连接成功”。"
            ),
        )
        self._save_config(self.config)
        if not silent:
            QMessageBox.information(
                self, bi("提示", "Notice"), bi("配置已保存。", "Saved.")
            )
        self._log(bi("配置已更新。", "Configuration updated."))

    def _apply_config_to_ui(self) -> None:
        self.input_cmd.setText(self.config.openclaw_cmd)
        self.input_node_cmd.setText(self.config.node_cmd)
        self.input_npm_cmd.setText(self.config.npm_cmd)
        self.input_winget_cmd.setText(self.config.winget_cmd)
        self.input_registry.setText(self.config.npm_registry)
        language_index = self.cmb_language.findData(self.config.language_mode)
        if language_index < 0:
            language_index = self.cmb_language.findData("bilingual")
        self.cmb_language.blockSignals(True)
        self.cmb_language.setCurrentIndex(language_index)
        self.cmb_language.blockSignals(False)
        self.input_workdir.setText(self.config.working_dir)
        self.input_thinking.setText(self.config.thinking_level)
        self.input_timeout.setText(str(self.config.timeout_seconds))
        self.input_extra.setText(self.config.extra_args)
        self.input_test_message.setText(self.config.test_message)

    def _log(self, text: str) -> None:
        line = text
        if not text.startswith("["):
            line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {text}"
        self.log_view.appendPlainText(line)
        self._persist_log_line(line)

    def _persist_log_line(self, line: str) -> None:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        file_path = LOG_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.log"
        with file_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    @staticmethod
    def _load_config() -> AppConfig:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not CONFIG_PATH.exists():
            cfg = AppConfig()
            OpenClawGui._save_config(cfg)
            return cfg
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return AppConfig(
                openclaw_cmd=data.get("openclaw_cmd", "openclaw"),
                node_cmd=data.get("node_cmd", "node"),
                npm_cmd=data.get("npm_cmd", "npm"),
                winget_cmd=data.get("winget_cmd", "winget"),
                npm_registry=data.get(
                    "npm_registry",
                    "https://registry.npmmirror.com",
                ),
                language_mode=data.get("language_mode", "bilingual"),
                working_dir=data.get("working_dir", ""),
                thinking_level=data.get("thinking_level", "medium"),
                timeout_seconds=int(data.get("timeout_seconds", 120)),
                extra_args=data.get("extra_args", ""),
                test_message=data.get(
                    "test_message",
                    "你好，请回复“连接成功”。",
                ),
            )
        except Exception:
            cfg = AppConfig()
            OpenClawGui._save_config(cfg)
            return cfg

    @staticmethod
    def _save_config(config: AppConfig) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with CONFIG_PATH.open("w", encoding="utf-8") as f:
            json.dump(asdict(config), f, ensure_ascii=False, indent=2)

    def _apply_theme(self) -> None:
        font = QFont("Segoe UI", 10)
        self.setFont(font)
        self.setStyleSheet(
            """
            QWidget {
                background: #f5f7fb;
                color: #1f2a37;
            }
            QFrame#card {
                border: 1px solid #d7deea;
                border-radius: 14px;
                background: #ffffff;
            }
            QFrame#subCard {
                border: 1px solid #d7deea;
                border-radius: 10px;
                background: #fbfcff;
                padding: 6px;
            }
            QTabWidget::pane {
                border: 1px solid #d7deea;
                border-radius: 10px;
                background: #ffffff;
                top: -1px;
            }
            QTabBar::tab {
                background: #eef3fc;
                color: #425466;
                border: 1px solid #d7deea;
                border-bottom: none;
                padding: 8px 16px;
                min-width: 120px;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                margin-right: 4px;
            }
            QTabBar::tab:selected {
                background: #ffffff;
                color: #1d4ed8;
            }
            QTabBar::tab:hover {
                background: #e4ecfb;
                color: #2a3f56;
            }
            QLabel#titleLabel {
                font-size: 24px;
                font-weight: 700;
                color: #0f172a;
            }
            QLabel#subTitleLabel {
                font-size: 13px;
                color: #5a6b7d;
                margin-bottom: 4px;
            }
            QLabel#sectionHeader {
                font-size: 16px;
                font-weight: 600;
                color: #1e293b;
            }
            QLabel#fieldLabel {
                color: #3c4a5a;
            }
            QLabel#summaryWarn {
                color: #b45309;
            }
            QLabel#summaryOk {
                color: #047857;
                font-weight: 600;
            }
            QLabel#tipsLabel {
                color: #4f5d70;
                background: #f3f7ff;
                border: 1px solid #d6e1f5;
                border-radius: 8px;
                padding: 8px;
            }
            QLineEdit, QPlainTextEdit, QListWidget {
                background: #ffffff;
                border: 1px solid #cfd9e8;
                border-radius: 8px;
                padding: 8px;
                selection-background-color: #bfdbfe;
                selection-color: #0f172a;
            }
            QProgressBar {
                background: #eef2f9;
                border: 1px solid #cfd9e8;
                border-radius: 6px;
                text-align: center;
                color: #334155;
                min-height: 18px;
            }
            QProgressBar::chunk {
                background: #3b82f6;
                border-radius: 5px;
            }
            QPushButton {
                background: #2563eb;
                border: none;
                border-radius: 8px;
                padding: 8px 12px;
                color: #ffffff;
                font-weight: 600;
            }
            QPushButton:hover {
                background: #1d4ed8;
            }
            QPushButton[suggested="true"] {
                background: #f59e0b;
                color: #111827;
                border: 2px solid #d97706;
            }
            QPushButton[suggested="true"]:hover {
                background: #fbbf24;
            }
            QPushButton:disabled {
                background: #c7d3e6;
                color: #6b7c93;
            }
            QStatusBar {
                background: #f5f7fb;
                color: #516173;
            }
            """
        )


class OnboardWizardDialog(QDialog):
    def __init__(self, config: AppConfig, parent: QWidget | None = None):
        super().__init__(parent)
        self.config = config
        self.process: subprocess.Popen | None = None
        self.cmd_hwnd: int | None = None
        self.console_title_token = ""
        self.setWindowTitle(
            bi("OpenClaw 图形化初始化向导", "OpenClaw Onboarding Guide")
        )
        self.resize(1200, 760)
        self.setMinimumSize(640, 480)

        root = QVBoxLayout(self)
        root.setContentsMargins(12, 12, 12, 12)
        root.setSpacing(10)

        title = QLabel(
            bi(
                "初始化将通过弹出终端完成，请按提示逐步设置",
                "Onboarding runs in popup terminal, "
                "follow prompts step by step",
            )
        )
        title.setObjectName("sectionHeader")
        root.addWidget(title)

        self.status_label = QLabel(bi("状态：未启动", "Status: Not started"))
        self.status_label.setObjectName("fieldLabel")
        root.addWidget(self.status_label)

        self.guide_browser = QTextBrowser()
        self.guide_browser.setObjectName("subCard")
        self.guide_browser.setOpenExternalLinks(True)
        self.guide_browser.setMaximumHeight(420)
        self.guide_browser.setHtml(self._build_guide_html())
        root.addWidget(self.guide_browser, 1)

        tip = QLabel(
            bi(
                "点击“开始初始化”后会弹出原生 CMD。"
                "首次建议最小化设置，只完成必要项（见下方步骤）。",
                "Click Start to open native CMD. "
                "For first run, keep settings minimal "
                "and complete required items.",
            )
        )
        tip.setObjectName("tipsLabel")
        tip.setWordWrap(True)
        root.addWidget(tip)

        button_row = QHBoxLayout()
        self.btn_start = QPushButton(bi("开始初始化", "Start Onboarding"))
        self.btn_restart = QPushButton(bi("重来", "Restart"))
        self.btn_stop = QPushButton(bi("停止", "Stop"))
        self.btn_close = QPushButton(bi("关闭", "Close"))
        self.btn_stop.setEnabled(False)
        self.btn_restart.setEnabled(False)
        button_row.addWidget(self.btn_start)
        button_row.addWidget(self.btn_restart)
        button_row.addWidget(self.btn_stop)
        button_row.addStretch(1)
        button_row.addWidget(self.btn_close)
        root.addLayout(button_row)

        self.btn_start.clicked.connect(self._start_onboard)
        self.btn_restart.clicked.connect(self._restart_onboard)
        self.btn_stop.clicked.connect(self._stop_onboard)
        self.btn_close.clicked.connect(self.close)
        self.move_timer = QTimer(self)
        self.move_timer.setInterval(400)
        self.move_timer.timeout.connect(self._maintain_cmd_window)

    def _build_guide_html(self) -> str:
        title = bi("初始化推荐步骤", "Recommended Onboarding Steps")
        must_title = bi("首次必做项（最小化配置）", "First-run required items")
        must_1 = bi(
            "1) 继续确认：危险提示选择 Yes。",
            "1) Continue confirmation: choose Yes on safety warning.",
        )
        must_2 = bi(
            "2) 选择模式：Onboarding mode 选 QuickStart。",
            "2) Mode selection: choose QuickStart in onboarding mode.",
        )
        must_3 = bi(
            "3) 完成必要配置：至少选 1 个模型提供商并填好 API Key。",
            "3) Required setup: pick at least one model provider "
            "and fill API key.",
        )
        first_run = bi(
            "首次建议：先最小化配置，仅完成必要设置，"
            "确认可用后再补充高级项。",
            "First run: keep configuration minimal, "
            "finish required items first, "
            "then refine advanced options later.",
        )
        warning = bi(
            "1) 危险警告：建议选择 Yes（同意继续）。",
            "1) Safety warning: choose Yes to continue.",
        )
        quickstart = bi(
            "2) Onboarding mode：建议选 QuickStart（快速部署）。",
            "2) Onboarding mode: choose QuickStart.",
        )
        model = bi(
            "3) 模型提供商建议（国内优先，可先选其一）：",
            "3) Model provider suggestions (CN-friendly):",
        )
        model_opts = bi(
            "MiniMax、Moonshot(Kimi)、Qwen（阿里云百炼）",
            "MiniMax, Moonshot (Kimi), Qwen (Alibaba Bailian)",
        )
        api_path = bi(
            "API 获取入口：",
            "API key portals:",
        )
        skip = bi(
            "4) 后续高级项（高级路由/扩展配置）建议先跳过，"
            "优先跑通主链路。",
            "4) Skip advanced options first; refine later.",
        )
        channel = bi(
            "5) Channel 建议优先飞书（企业内部落地更方便）：",
            "5) Channel recommendation: Feishu first.",
        )
        feishu_steps = bi(
            "在飞书开放平台创建企业自建应用 -> 开启机器人能力 -> "
            "拿到 app_id / app_secret / verification token / encrypt key -> "
            "按向导填写并完成事件订阅。",
            "Create app in Feishu Open Platform -> enable bot -> "
            "get app_id / app_secret / verification token / encrypt key -> "
            "fill in wizard and complete event subscription.",
        )
        finish = bi(
            "6) 完成后回到主程序点击“检测连接”，确认本地聊天可用。",
            "6) After onboarding, click Connection Test in the main app.",
        )
        return (
            f"<div style='font-family:Segoe UI,Microsoft YaHei;"
            "font-size:13px;line-height:1.55;'>"
            f"<b>{title}</b><br>"
            f"{first_run}<br>"
            "<div style='margin:8px 0 10px 0;padding:8px 10px;"
            "border:1px solid #f59e0b;border-radius:8px;"
            "background:#fffbeb;'>"
            f"<b>{must_title}</b><br>"
            f"{must_1}<br>"
            f"{must_2}<br>"
            f"{must_3}"
            "</div>"
            f"{warning}<br>"
            f"{quickstart}<br>"
            f"{model}<br>"
            f"<b>{model_opts}</b><br>"
            f"{api_path}"
            "<a href='https://platform.minimaxi.com/'>MiniMax</a> | "
            "<a href='https://platform.moonshot.cn/'>Moonshot</a> | "
            "<a href='https://bailian.console.aliyun.com/'>"
            "Qwen(Bailian)</a><br>"
            f"{skip}<br>"
            f"{channel}<br>"
            f"{feishu_steps}<br>"
            f"{finish}"
            "</div>"
        )

    def _set_status(self, text: str) -> None:
        self.status_label.setText(text)

    def _start_onboard(self) -> None:
        if self.process is not None:
            return
        if not is_windows():
            QMessageBox.warning(
                self,
                bi("不支持", "Unsupported"),
                bi(
                    "当前仅在 Windows 支持原生 CMD 初始化。",
                    "Native CMD onboarding supports Windows only.",
                ),
            )
            return

        self._arrange_windows_left_right()

        executable = resolve_command_for_system(self.config.openclaw_cmd)
        cmd_exec = subprocess.list2cmdline(
            [executable, "onboard", "--install-daemon"]
        )
        cmd_line = f"{cmd_exec}"
        try:
            self.process = subprocess.Popen(
                ["cmd.exe", "/k", cmd_line],
                cwd=self.config.working_dir or None,
                shell=False,
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
        except Exception as ex:
            self._set_status(
                bi(
                    f"状态：启动失败（{ex}）",
                    f"Status: Failed to start ({ex})",
                )
            )
            self.process = None
            return

        self._set_status(
            bi(
                "状态：已启动原生 CMD，请在该窗口完成初始化。",
                "Status: Native CMD started; complete onboarding there.",
            )
        )
        self.cmd_hwnd = None
        self.move_timer.stop()
        self._set_status(
            bi(
                "状态：运行中（CMD 为原生窗口，不做任何控制）",
                "Status: Running (CMD stays native, unmanaged)",
            )
        )
        self.btn_restart.setEnabled(True)
        self.btn_stop.setEnabled(True)

    def _stop_onboard(self) -> None:
        if self.process is None:
            return
        pid = self.process.pid
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=10,
                shell=False,
            )
        except Exception:
            pass
        self.process = None
        self.cmd_hwnd = None
        self.console_title_token = ""
        self.move_timer.stop()
        self._set_status(bi("状态：已停止", "Status: Stopped"))
        self.btn_restart.setEnabled(False)
        self.btn_stop.setEnabled(False)

    def _restart_onboard(self) -> None:
        self._set_status(bi("状态：重启中...", "Status: Restarting..."))
        self._stop_onboard()
        self._start_onboard()

    def _arrange_windows_left_right(self) -> None:
        if not is_windows():
            return
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        geom = screen.availableGeometry()
        # Keep guide window size unchanged; only place it on the left.
        self.move(geom.left(), geom.top())

    def _move_cmd_to_right(
        self,
        hwnd: int,
        force_restore: bool = False,
        bring_to_front: bool = False,
    ) -> None:
        if not is_windows():
            return
        if not user32.IsWindow(hwnd):
            return
        # If user maximized CMD manually, do not override their choice.
        if user32.IsZoomed(hwnd):
            return
        screen = self.screen() or QApplication.primaryScreen()
        if screen is None:
            return
        geom = screen.availableGeometry()
        half_w = max(640, geom.width() // 2)
        x = geom.left() + half_w
        y = geom.top()
        width = geom.width() - half_w
        height = geom.height()

        # Restore only during initial placement; not on each timer tick.
        if force_restore:
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.MoveWindow(
            hwnd,
            x,
            y,
            width,
            height,
            True,
        )
        user32.ShowWindow(hwnd, SW_SHOW)
        if bring_to_front:
            # Bring to front reliably on Windows (topmost flip trick).
            user32.SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                x,
                y,
                width,
                height,
                SWP_SHOWWINDOW,
            )
            user32.SetWindowPos(
                hwnd,
                HWND_NOTOPMOST,
                x,
                y,
                width,
                height,
                SWP_SHOWWINDOW,
            )
            user32.SetForegroundWindow(hwnd)

    def _maintain_cmd_window(self) -> None:
        if self.process is None:
            return
        hwnd = self.cmd_hwnd
        if hwnd is None:
            hwnd = find_window_by_title_fragment(self.console_title_token)
            if hwnd is not None:
                self.cmd_hwnd = hwnd
        if hwnd is None:
            return
        self._move_cmd_to_right(hwnd)

    def closeEvent(self, event) -> None:
        self._stop_onboard()
        super().closeEvent(event)


def main() -> None:
    app = QApplication(sys.argv)
    win = OpenClawGui()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
