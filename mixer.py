#!/usr/bin/env python3
"""Compact tray panel to mute mic/sound for 4 Yandex Telemost instances."""

from __future__ import annotations

import json
import os
import re
import signal
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QSize
from PyQt6.QtGui import QAction, QCloseEvent, QIcon, QPainter, QColor, QPixmap
from PyQt6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizeGrip,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

APP_DIR = Path(__file__).resolve().parent
CHANNELS_FILE = APP_DIR / "channels.json"
PROFILES_ROOT = Path.home() / ".local" / "share" / "telemost-mixer"
WP_STREAM_PROPS = Path.home() / ".local" / "state" / "wireplumber" / "stream-properties"
BROWSER = "yandex-browser"
POLL_MS = 1000
LAUNCH_STAGGER_S = 2.5
# WirePlumber keys mute by application.name. Ordinary Yandex uses "Yandex input",
# so a mixer mute must not be saved under that name — otherwise the regular
# browser stays muted after reboot.
_PERSIST_MUTE_MARKERS = (
    "Yandex\\sinput",
    "Chromium\\sinput",
    "application.name:Yandex=",
    "TelemostMixer",
    "telemost-mixer",
)
_MUTE_TRUE_RE = re.compile(r'"mute"\s*:\s*true')

BTN = 28  # compact square control size

STYLE_ON = (
    f"QPushButton {{ background: #2e7d32; border: none; border-radius: 4px; "
    f"min-width: {BTN}px; max-width: {BTN}px; min-height: {BTN}px; max-height: {BTN}px; }}"
)
STYLE_OFF = (
    f"QPushButton {{ background: #c62828; border: none; border-radius: 4px; "
    f"min-width: {BTN}px; max-width: {BTN}px; min-height: {BTN}px; max-height: {BTN}px; }}"
)
STYLE_NA = (
    f"QPushButton {{ background: #555; border: none; border-radius: 4px; "
    f"min-width: {BTN}px; max-width: {BTN}px; min-height: {BTN}px; max-height: {BTN}px; }}"
)
STYLE_SOLO = (
    f"QPushButton {{ background: #1565c0; border: none; border-radius: 4px; "
    f"min-width: {BTN}px; max-width: {BTN}px; min-height: {BTN}px; max-height: {BTN}px; }}"
)
STYLE_SOLO_IDLE = (
    f"QPushButton {{ background: #333; border: none; border-radius: 4px; "
    f"min-width: {BTN}px; max-width: {BTN}px; min-height: {BTN}px; max-height: {BTN}px; }}"
)
STYLE_TOOL = (
    "QPushButton { background: #333; color: #eee; border: none; border-radius: 4px; "
    "padding: 4px 8px; min-height: 26px; }"
)


@dataclass
class ChannelConfig:
    id: str
    name: str
    url: str

    @property
    def profile_dir(self) -> Path:
        return PROFILES_ROOT / self.id

    @property
    def wm_class(self) -> str:
        return f"Telemost{self.id.capitalize()}"


@dataclass
class StreamState:
    index: int
    muted: bool


def load_channels() -> list[ChannelConfig]:
    if not CHANNELS_FILE.is_file():
        example = APP_DIR / "channels.example.json"
        if example.is_file():
            shutil.copy(example, CHANNELS_FILE)
        else:
            raise FileNotFoundError(
                f"Нет {CHANNELS_FILE} и нет channels.example.json"
            )
    data = json.loads(CHANNELS_FILE.read_text(encoding="utf-8"))
    return [
        ChannelConfig(id=c["id"], name=c["name"], url=c["url"])
        for c in data["channels"]
    ]


def save_channels(channels: list[ChannelConfig]) -> None:
    # Preserve unknown top-level keys (e.g. display_name) if present.
    try:
        data = json.loads(CHANNELS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data["channels"] = [
        {"id": c.id, "name": c.name, "url": c.url} for c in channels
    ]
    CHANNELS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    env["LANG"] = "C"
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
        env=env,
        **kwargs,
    )


def pids_for_profile(profile_dir: Path) -> set[int]:
    needle = str(profile_dir)
    found: set[int] = set()
    try:
        entries = list(Path("/proc").iterdir())
    except OSError:
        return found
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except (OSError, PermissionError):
            continue
        if not raw:
            continue
        cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="ignore")
        if needle in cmdline:
            found.add(int(entry.name))
    return found


def close_channels(channels: list[ChannelConfig], *, wait_s: float = 8.0) -> int:
    all_pids: set[int] = set()
    for ch in channels:
        all_pids |= pids_for_profile(ch.profile_dir)
    if not all_pids:
        return 0
    for pid in sorted(all_pids):
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    deadline = time.time() + wait_s
    while time.time() < deadline:
        left: set[int] = set()
        for ch in channels:
            left |= pids_for_profile(ch.profile_dir)
        if not left:
            time.sleep(0.3)
            return len(all_pids)
        time.sleep(0.25)
    left = set()
    for ch in channels:
        left |= pids_for_profile(ch.profile_dir)
    for pid in left:
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(0.2)
    return len(all_pids)


def _parse_pactl_streams(kind: str) -> list[tuple[int, int, bool]]:
    result = _run(["pactl", "list", kind])
    if result.returncode != 0:
        return []
    streams: list[tuple[int, int, bool]] = []
    index: int | None = None
    pid: int | None = None
    muted: bool | None = None
    header = "Sink Input #" if kind == "sink-inputs" else "Source Output #"

    def flush() -> None:
        nonlocal index, pid, muted
        if index is not None and pid is not None and muted is not None:
            streams.append((index, pid, muted))
        index, pid, muted = None, None, None

    for line in result.stdout.splitlines():
        if line.startswith(header):
            flush()
            try:
                index = int(line.split("#", 1)[1].strip())
            except ValueError:
                index = None
            continue
        stripped = line.strip()
        if stripped.startswith("Mute:"):
            muted = stripped.split(":", 1)[1].strip().lower() == "yes"
        elif "application.process.id" in stripped:
            value = stripped.split("=", 1)[-1].strip().strip('"')
            try:
                pid = int(value)
            except ValueError:
                pid = None
    flush()
    return streams


def streams_for_pids(kind: str, pids: set[int]) -> list[StreamState]:
    if not pids:
        return []
    return [
        StreamState(index=idx, muted=muted)
        for idx, pid, muted in _parse_pactl_streams(kind)
        if pid in pids
    ]


def set_streams_mute(kind: str, streams: list[StreamState], mute: bool) -> None:
    verb = (
        "set-sink-input-mute"
        if kind == "sink-inputs"
        else "set-source-output-mute"
    )
    flag = "1" if mute else "0"
    for stream in streams:
        _run(["pactl", verb, str(stream.index), flag])
    # Live mute may still apply; do not let WirePlumber remember it for Yandex.
    clear_persisted_browser_mute()


def _is_browser_pid(pid: int) -> bool:
    try:
        comm = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return comm in {"yandex_browser", "chrome", "chromium"}


def rewrite_stream_properties(text: str) -> str:
    """Force saved mute=false for browser/mixer stream identities."""
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        if any(marker in line for marker in _PERSIST_MUTE_MARKERS):
            line = _MUTE_TRUE_RE.sub('"mute":false', line)
        out.append(line)
    return "".join(out)


def clear_persisted_browser_mute() -> None:
    path = WP_STREAM_PROPS
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    updated = rewrite_stream_properties(text)
    if updated == text:
        return
    try:
        path.write_text(updated, encoding="utf-8")
    except OSError:
        pass


def pids_for_channels(channels: list[ChannelConfig]) -> set[int]:
    pids: set[int] = set()
    for channel in channels:
        pids |= pids_for_profile(channel.profile_dir)
    return pids


def unmute_non_mixer_browser_inputs(channels: list[ChannelConfig]) -> None:
    """Unmute ordinary Yandex/Chrome if a previous mixer mute leaked to them."""
    mixer_pids = pids_for_channels(channels)
    for index, pid, muted in _parse_pactl_streams("source-outputs"):
        if not muted or pid in mixer_pids or not _is_browser_pid(pid):
            continue
        _run(["pactl", "set-source-output-mute", str(index), "0"])
    clear_persisted_browser_mute()


def release_mixer_mutes(channels: list[ChannelConfig]) -> None:
    """Undo mixer mutes so they do not stick after exit or closing windows."""
    pids = pids_for_channels(channels)
    set_streams_mute("source-outputs", streams_for_pids("source-outputs", pids), False)
    set_streams_mute("sink-inputs", streams_for_pids("sink-inputs", pids), False)
    unmute_non_mixer_browser_inputs(channels)


def launch_channel(channel: ChannelConfig, *, index: int = 0) -> None:
    profile = channel.profile_dir
    profile.mkdir(parents=True, exist_ok=True)
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        lock = profile / name
        if lock.exists() or lock.is_symlink():
            try:
                lock.unlink()
            except OSError:
                pass
    cmd = [
        BROWSER,
        f"--user-data-dir={profile}",
        f"--class={channel.wm_class}",
        f"--app={channel.url}",
        "--disable-video-capture",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    env = os.environ.copy()
    # Isolate restore identity so solo/mute is not saved as "Yandex input".
    env["PULSE_PROP_OVERRIDE"] = (
        f"application.name=TelemostMixer-{channel.id} "
        f"application.id=telemost-mixer.{channel.id} "
        f"module-stream-restore.id=telemost-mixer:{channel.id}"
    )
    if index > 0:
        time.sleep(LAUNCH_STAGGER_S)
    subprocess.Popen(
        cmd,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def theme_icon(names: list[str], fallback_glyph: str, color: str = "#eee") -> QIcon:
    for name in names:
        icon = QIcon.fromTheme(name)
        if not icon.isNull():
            return icon
    # Fallback: paint a glyph
    pm = QPixmap(BTN, BTN)
    pm.fill(QColor(0, 0, 0, 0))
    p = QPainter(pm)
    p.setPen(QColor(color))
    font = p.font()
    font.setPixelSize(16)
    font.setBold(True)
    p.setFont(font)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, fallback_glyph)
    p.end()
    return QIcon(pm)


def app_tray_icon() -> QIcon:
    icon = QIcon.fromTheme("audio-headphones")
    if not icon.isNull():
        return icon
    icon = QIcon.fromTheme("audio-volume-high")
    if not icon.isNull():
        return icon
    pm = QPixmap(64, 64)
    pm.fill(QColor("#1565c0"))
    p = QPainter(pm)
    p.setPen(QColor("#fff"))
    font = p.font()
    font.setPixelSize(36)
    font.setBold(True)
    p.setFont(font)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, "T")
    p.end()
    return QIcon(pm)


class ChannelRow(QWidget):
    def __init__(
        self,
        channel: ChannelConfig,
        on_solo,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.channel = channel
        self._on_solo = on_solo
        self._mic_streams: list[StreamState] = []
        self._sound_streams: list[StreamState] = []
        self._solo_active = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 1, 0, 1)
        layout.setSpacing(3)

        self.name_edit = QLineEdit(channel.name)
        self.name_edit.setPlaceholderText("ученик")
        self.name_edit.setMaximumHeight(BTN)
        # Fits ~«Артемий Савин» without feeling cramped.
        self.name_edit.setMinimumWidth(110)
        self.name_edit.editingFinished.connect(self._on_name_changed)

        self.solo_btn = QPushButton()
        self.solo_btn.setIcon(
            theme_icon(["emblem-favorite", "starred", "go-next"], "★")
        )
        self.solo_btn.setIconSize(QSize(16, 16))
        self.solo_btn.setToolTip(
            "Говорить сюда: включить мик этого канала, выключить мик у остальных "
            "(звук всех каналов не трогаем)"
        )
        self.solo_btn.setStyleSheet(STYLE_SOLO_IDLE)
        self.solo_btn.clicked.connect(lambda: self._on_solo(self))

        self.copy_btn = QPushButton()
        self.copy_btn.setIcon(theme_icon(["edit-copy", "document-copy"], "⧉"))
        self.copy_btn.setIconSize(QSize(14, 14))
        self.copy_btn.setToolTip("Скопировать ссылку на встречу")
        self.copy_btn.setStyleSheet(STYLE_SOLO_IDLE)
        self.copy_btn.clicked.connect(self.copy_link)

        self.close_btn = QPushButton()
        self.close_btn.setIcon(theme_icon(["window-close", "process-stop"], "×"))
        self.close_btn.setIconSize(QSize(14, 14))
        self.close_btn.setToolTip("Закрыть только этот канал")
        self.close_btn.setStyleSheet(STYLE_SOLO_IDLE)
        self.close_btn.clicked.connect(self.close_channel)

        self.mic_btn = QPushButton()
        self.mic_btn.setIcon(
            theme_icon(
                ["audio-input-microphone", "mic-on", "microphone-sensitivity-high"],
                "M",
            )
        )
        self.mic_btn.setIconSize(QSize(16, 16))
        self.mic_btn.setToolTip("Микрофон")
        self.mic_btn.clicked.connect(self.toggle_mic)

        self.sound_btn = QPushButton()
        self.sound_btn.setIcon(
            theme_icon(["audio-volume-high", "audio-speakers"], "S")
        )
        self.sound_btn.setIconSize(QSize(16, 16))
        self.sound_btn.setToolTip("Звук")
        self.sound_btn.clicked.connect(self.toggle_sound)

        layout.addWidget(self.name_edit, stretch=1)
        layout.addWidget(self.solo_btn)
        layout.addWidget(self.copy_btn)
        layout.addWidget(self.close_btn)
        layout.addWidget(self.mic_btn)
        layout.addWidget(self.sound_btn)
        self._paint_buttons()

    def _on_name_changed(self) -> None:
        self.channel.name = self.name_edit.text().strip() or self.channel.id

    def copy_link(self) -> None:
        url = (self.channel.url or "").strip()
        if not url:
            return
        QApplication.clipboard().setText(url)
        self.copy_btn.setToolTip("Скопировано!")
        QTimer.singleShot(
            1500,
            lambda: self.copy_btn.setToolTip("Скопировать ссылку на встречу"),
        )

    def close_channel(self) -> None:
        release_mixer_mutes([self.channel])
        close_channels([self.channel], wait_s=5.0)
        self.set_solo_active(False)
        self.refresh()

    def set_solo_active(self, active: bool) -> None:
        self._solo_active = active
        self.solo_btn.setStyleSheet(STYLE_SOLO if active else STYLE_SOLO_IDLE)

    def refresh(self) -> None:
        pids = pids_for_profile(self.channel.profile_dir)
        self._mic_streams = streams_for_pids("source-outputs", pids)
        self._sound_streams = streams_for_pids("sink-inputs", pids)
        self._paint_buttons()

    def _paint_buttons(self) -> None:
        self._paint_one(self.mic_btn, self._mic_streams, "Микрофон")
        self._paint_one(self.sound_btn, self._sound_streams, "Звук")

    @staticmethod
    def _paint_one(btn: QPushButton, streams: list[StreamState], kind: str) -> None:
        if not streams:
            btn.setStyleSheet(STYLE_NA)
            btn.setEnabled(False)
            btn.setToolTip(f"{kind}: нет потока")
            return
        btn.setEnabled(True)
        muted = all(s.muted for s in streams)
        if muted:
            btn.setStyleSheet(STYLE_OFF)
            btn.setToolTip(f"{kind}: выкл")
        else:
            btn.setStyleSheet(STYLE_ON)
            btn.setToolTip(f"{kind}: вкл")

    def toggle_mic(self) -> None:
        if not self._mic_streams:
            return
        mute = not all(s.muted for s in self._mic_streams)
        set_streams_mute("source-outputs", self._mic_streams, mute)
        self.refresh()

    def toggle_sound(self) -> None:
        if not self._sound_streams:
            return
        mute = not all(s.muted for s in self._sound_streams)
        set_streams_mute("sink-inputs", self._sound_streams, mute)
        self.refresh()

    def set_mic_muted(self, mute: bool) -> None:
        if self._mic_streams:
            set_streams_mute("source-outputs", self._mic_streams, mute)
        self.refresh()


class MixerWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Телемост")
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.FramelessWindowHint
        )
        self.channels = load_channels()
        self.rows: list[ChannelRow] = []
        self._solo_row: ChannelRow | None = None

        root = QWidget()
        root.setStyleSheet("background: #1e1e1e; color: #eee;")
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(3)

        for ch in self.channels:
            row = ChannelRow(ch, on_solo=self.solo_channel)
            self.rows.append(row)
            layout.addWidget(row)

        tools = QHBoxLayout()
        tools.setSpacing(4)
        self.btn_launch = QPushButton("▶")
        self.btn_launch.setStyleSheet(STYLE_TOOL)
        self.btn_launch.setToolTip("Запустить окна")
        self.btn_launch.setFixedWidth(36)
        self.btn_launch.clicked.connect(self.launch_all)

        self.btn_close = QPushButton("■")
        self.btn_close.setStyleSheet(STYLE_TOOL)
        self.btn_close.setToolTip("Закрыть окна Телемоста")
        self.btn_close.setFixedWidth(36)
        self.btn_close.clicked.connect(self.close_all)

        self.btn_hide = QPushButton("−")
        self.btn_hide.setStyleSheet(STYLE_TOOL)
        self.btn_hide.setToolTip("Свернуть в трей")
        self.btn_hide.setFixedWidth(36)
        self.btn_hide.clicked.connect(self.hide_to_tray)

        self.btn_quit = QPushButton("×")
        self.btn_quit.setStyleSheet(STYLE_TOOL)
        self.btn_quit.setToolTip("Выйти из микшера")
        self.btn_quit.setFixedWidth(36)
        self.btn_quit.clicked.connect(self.quit_app)

        tools.addWidget(self.btn_launch)
        tools.addWidget(self.btn_close)
        tools.addStretch(1)
        tools.addWidget(self.btn_hide)
        tools.addWidget(self.btn_quit)
        grip = QSizeGrip(root)
        tools.addWidget(
            grip, 0, Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignRight
        )
        layout.addLayout(tools)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(POLL_MS)

        # Resizable; min width fits «Артемий Савин» + icon buttons.
        self.setMinimumWidth(220)
        self.setMinimumHeight(150)
        self.resize(240, 170)
        self.poll()
        self._setup_tray()
        unmute_non_mixer_browser_inputs(self.channels)

    def _setup_tray(self) -> None:
        if not QSystemTrayIcon.isSystemTrayAvailable():
            self.setWindowFlags(
                Qt.WindowType.Tool | Qt.WindowType.WindowStaysOnTopHint
            )
            return
        self.tray = QSystemTrayIcon(app_tray_icon(), self)
        self.tray.setToolTip("Телемост микшер")
        menu = QMenu()
        act_show = QAction("Показать", self)
        act_show.triggered.connect(self.show_from_tray)
        act_launch = QAction("Запустить окна", self)
        act_launch.triggered.connect(self.launch_all)
        act_close = QAction("Закрыть окна", self)
        act_close.triggered.connect(self.close_all)
        act_edit = QAction("Редактировать ссылки…", self)
        act_edit.triggered.connect(self.open_channels_file)
        act_reload = QAction("Перечитать ссылки", self)
        act_reload.triggered.connect(self.reload_channels)
        act_quit = QAction("Выйти", self)
        act_quit.triggered.connect(self.quit_app)
        menu.addAction(act_show)
        menu.addSeparator()
        menu.addAction(act_launch)
        menu.addAction(act_close)
        menu.addSeparator()
        menu.addAction(act_edit)
        menu.addAction(act_reload)
        menu.addSeparator()
        menu.addAction(act_quit)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            if self.isVisible():
                self.hide_to_tray()
            else:
                self.show_from_tray()

    def show_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def hide_to_tray(self) -> None:
        self.save_config()
        self.hide()

    def quit_app(self) -> None:
        self.save_config()
        release_mixer_mutes(self.channels)
        QApplication.instance().quit()

    def poll(self) -> None:
        for row in self.rows:
            row.refresh()
        clear_persisted_browser_mute()

    def save_config(self) -> None:
        for row in self.rows:
            row._on_name_changed()
        save_channels(self.channels)

    def solo_channel(self, row: ChannelRow) -> None:
        """Enable this channel's mic; mute other mics. Sound of all channels stays as is."""
        for other in self.rows:
            other.refresh()
            if other is row:
                other.set_mic_muted(False)
                other.set_solo_active(True)
            else:
                other.set_mic_muted(True)
                other.set_solo_active(False)
        self._solo_row = row

    def open_channels_file(self) -> None:
        self.save_config()
        path = str(CHANNELS_FILE)
        try:
            subprocess.Popen(
                ["xdg-open", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            QMessageBox.warning(self, "Файл", f"Не удалось открыть:\n{path}\n{exc}")

    def reload_channels(self) -> None:
        """Reload URLs/names from channels.json after manual edit."""
        try:
            fresh = load_channels()
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            QMessageBox.warning(
                self, "Конфиг", f"Не удалось прочитать channels.json:\n{exc}"
            )
            return
        by_id = {c.id: c for c in fresh}
        for row in self.rows:
            updated = by_id.get(row.channel.id)
            if not updated:
                continue
            row.channel.name = updated.name
            row.channel.url = updated.url
            row.name_edit.setText(updated.name)
        self.channels = [row.channel for row in self.rows]
        if hasattr(self, "tray"):
            self.tray.showMessage(
                "Телемост микшер",
                "Ссылки перечитаны из channels.json",
                QSystemTrayIcon.MessageIcon.Information,
                2000,
            )

    def launch_all(self) -> None:
        self.save_config()
        if not shutil.which(BROWSER):
            QMessageBox.critical(
                self, "Браузер", f"«{BROWSER}» не найден в PATH."
            )
            return

        def _run_launch() -> None:
            i = 0
            for ch in self.channels:
                if pids_for_profile(ch.profile_dir):
                    continue
                launch_channel(ch, index=i)
                i += 1

        threading.Thread(target=_run_launch, daemon=True).start()

    def close_all(self) -> None:
        release_mixer_mutes(self.channels)
        n = close_channels(self.channels)
        self._solo_row = None
        for row in self.rows:
            row.set_solo_active(False)
        if n == 0:
            # silent is fine in compact UI
            pass

    def closeEvent(self, event: QCloseEvent) -> None:
        # Closing the window hides to tray instead of quitting.
        event.ignore()
        self.hide_to_tray()

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_pos = (
                event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            )
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if (
            event.buttons() & Qt.MouseButton.LeftButton
            and hasattr(self, "_drag_pos")
        ):
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()


def main() -> int:
    PROFILES_ROOT.mkdir(parents=True, exist_ok=True)
    app = QApplication(sys.argv)
    app.setApplicationName("telemost-mixer")
    app.setQuitOnLastWindowClosed(False)  # keep running in tray
    win = MixerWindow()
    app.aboutToQuit.connect(lambda: release_mixer_mutes(win.channels))
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
