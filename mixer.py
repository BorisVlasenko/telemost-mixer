#!/usr/bin/env python3
"""Compact tray panel to mute mic/sound for Yandex Telemost instances."""

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
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizeGrip,
    QSlider,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)

APP_DIR = Path(__file__).resolve().parent
CHANNELS_FILE = APP_DIR / "channels.json"
STUDENTS_FILE = APP_DIR / "students.json"
PROFILES_ROOT = Path.home() / ".local" / "share" / "telemost-mixer"
WP_STREAM_PROPS = Path.home() / ".local" / "state" / "wireplumber" / "stream-properties"
BROWSER = "yandex-browser"
POLL_MS = 1000
DEFAULT_CHANNEL_COUNT = 2
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
VOLUME_MIN = 0
VOLUME_MAX = 150
VOLUME_DEFAULT = 100
VOLUME_SLIDER_W = 72
_VOLUME_PCT_RE = re.compile(r"(\d+)%")

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
STYLE_VOLUME = (
    "QSlider::groove:horizontal {"
    "  background: #444; height: 4px; border-radius: 2px;"
    "}"
    "QSlider::handle:horizontal {"
    "  background: #bbb; width: 10px; height: 14px; margin: -5px 0; border-radius: 2px;"
    "}"
    "QSlider::sub-page:horizontal {"
    "  background: #1565c0; height: 4px; border-radius: 2px;"
    "}"
)


def clamp_volume(value: int | float) -> int:
    return max(VOLUME_MIN, min(VOLUME_MAX, int(round(value))))


@dataclass
class StudentOption:
    id: int | str
    name: str
    url: str
    alias: str = ""


@dataclass
class ChannelConfig:
    id: str
    name: str
    url: str
    volume: int = VOLUME_DEFAULT
    student_id: int | str | None = None

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
    volume: int = VOLUME_DEFAULT


def _next_channel_id(existing: list[ChannelConfig]) -> str:
    used = {c.id for c in existing}
    n = 1
    while f"ch{n}" in used:
        n += 1
    return f"ch{n}"


def _blank_channels(count: int = DEFAULT_CHANNEL_COUNT) -> list[ChannelConfig]:
    return [
        ChannelConfig(id=f"ch{i}", name="", url="", volume=VOLUME_DEFAULT)
        for i in range(1, count + 1)
    ]


def load_channels() -> list[ChannelConfig]:
    if not CHANNELS_FILE.is_file():
        example = APP_DIR / "channels.example.json"
        if example.is_file():
            shutil.copy(example, CHANNELS_FILE)
        else:
            channels = _blank_channels()
            save_channels(channels)
            return channels
    data = json.loads(CHANNELS_FILE.read_text(encoding="utf-8"))
    raw = data.get("channels") or []
    if not raw:
        return _blank_channels()
    channels: list[ChannelConfig] = []
    for c in raw:
        volume = clamp_volume(c.get("volume", VOLUME_DEFAULT))
        channels.append(
            ChannelConfig(
                id=c["id"],
                name=c.get("name") or "",
                url=c.get("url") or "",
                volume=volume,
                student_id=c.get("student_id"),
            )
        )
    return channels


def save_channels(channels: list[ChannelConfig]) -> None:
    # Preserve unknown top-level keys (e.g. display_name) if present.
    try:
        data = json.loads(CHANNELS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    data["channels"] = [
        {
            "id": c.id,
            "name": c.name,
            "url": c.url,
            "volume": clamp_volume(c.volume),
            **({"student_id": c.student_id} if c.student_id is not None else {}),
        }
        for c in channels
    ]
    CHANNELS_FILE.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_students(path: Path | None = None) -> list[StudentOption]:
    students_path = path or STUDENTS_FILE
    if not students_path.is_file():
        return []
    try:
        data = json.loads(students_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    raw = data.get("students") if isinstance(data, dict) else data
    if not isinstance(raw, list):
        return []
    out: list[StudentOption] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = (item.get("name") or item.get("alias") or "").strip()
        url = (item.get("url") or "").strip()
        sid = item.get("id", name or url)
        out.append(
            StudentOption(
                id=sid,
                name=name or str(sid),
                url=url,
                alias=(item.get("alias") or "").strip(),
            )
        )
    return out


def students_file_from_channels_config() -> Path:
    try:
        data = json.loads(CHANNELS_FILE.read_text(encoding="utf-8"))
        raw = (data.get("students_file") or "").strip()
        if raw:
            p = Path(raw).expanduser()
            return p if p.is_absolute() else (APP_DIR / p)
    except (OSError, json.JSONDecodeError):
        pass
    return STUDENTS_FILE


def set_students_file_in_config(path: Path) -> None:
    try:
        data = json.loads(CHANNELS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    try:
        rel = path.resolve().relative_to(APP_DIR.resolve())
        data["students_file"] = str(rel)
    except ValueError:
        data["students_file"] = str(path.resolve())
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


def _parse_pactl_streams(kind: str) -> list[tuple[int, int, bool, int]]:
    result = _run(["pactl", "list", kind])
    if result.returncode != 0:
        return []
    streams: list[tuple[int, int, bool, int]] = []
    index: int | None = None
    pid: int | None = None
    muted: bool | None = None
    volume: int = VOLUME_DEFAULT
    header = "Sink Input #" if kind == "sink-inputs" else "Source Output #"

    def flush() -> None:
        nonlocal index, pid, muted, volume
        if index is not None and pid is not None and muted is not None:
            streams.append((index, pid, muted, volume))
        index, pid, muted = None, None, None
        volume = VOLUME_DEFAULT

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
        elif stripped.startswith("Volume:"):
            match = _VOLUME_PCT_RE.search(stripped)
            if match:
                volume = clamp_volume(int(match.group(1)))
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
        StreamState(index=idx, muted=muted, volume=vol)
        for idx, pid, muted, vol in _parse_pactl_streams(kind)
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


def set_streams_volume(streams: list[StreamState], percent: int) -> None:
    """Set playback (sink-input) volume for streams. Mic streams are not supported."""
    pct = clamp_volume(percent)
    for stream in streams:
        _run(["pactl", "set-sink-input-volume", str(stream.index), f"{pct}%"])


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
    for index, pid, muted, _vol in _parse_pactl_streams("source-outputs"):
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
    del index  # kept for call-site compatibility; no stagger pause
    url = (channel.url or "").strip()
    if not url:
        return
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
        f"--app={url}",
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
        on_remove_row=None,
        students: list[StudentOption] | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.channel = channel
        self._on_solo = on_solo
        self._on_remove_row = on_remove_row
        self._students: list[StudentOption] = list(students or [])
        self._mic_streams: list[StreamState] = []
        self._sound_streams: list[StreamState] = []
        self._solo_active = False
        self._slider_dragging = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 1, 0, 1)
        layout.setSpacing(3)

        self.student_combo = QComboBox()
        self.student_combo.setMaximumHeight(BTN)
        self.student_combo.setMinimumWidth(130)
        self.student_combo.setEditable(False)
        self.student_combo.setStyleSheet(
            "QComboBox { background: #2a2a2a; color: #eee; border: 1px solid #555; "
            "border-radius: 4px; padding: 2px 6px; }"
            "QComboBox QAbstractItemView { background: #2a2a2a; color: #eee; }"
        )
        self._refill_students(select_current=True)
        self.student_combo.currentIndexChanged.connect(self._on_student_chosen)

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
        self.close_btn.setToolTip("Закрыть окно браузера этого канала")
        self.close_btn.setStyleSheet(STYLE_SOLO_IDLE)
        self.close_btn.clicked.connect(self.close_channel)

        self.remove_btn = QPushButton()
        self.remove_btn.setIcon(theme_icon(["list-remove", "edit-delete"], "−"))
        self.remove_btn.setIconSize(QSize(14, 14))
        self.remove_btn.setToolTip("Убрать строку канала из панели")
        self.remove_btn.setStyleSheet(STYLE_SOLO_IDLE)
        self.remove_btn.clicked.connect(self._remove_row)

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

        self.vol_slider = QSlider(Qt.Orientation.Horizontal)
        self.vol_slider.setRange(VOLUME_MIN, VOLUME_MAX)
        self.vol_slider.setSingleStep(5)
        self.vol_slider.setPageStep(10)
        self.vol_slider.setValue(clamp_volume(channel.volume))
        self.vol_slider.setFixedWidth(VOLUME_SLIDER_W)
        self.vol_slider.setMaximumHeight(BTN)
        self.vol_slider.setStyleSheet(STYLE_VOLUME)
        self.vol_slider.setToolTip(f"Громкость: {self.vol_slider.value()}%")
        self.vol_slider.sliderPressed.connect(self._on_slider_pressed)
        self.vol_slider.sliderReleased.connect(self._on_slider_released)
        self.vol_slider.valueChanged.connect(self._on_volume_changed)

        layout.addWidget(self.student_combo, stretch=1)
        layout.addWidget(self.solo_btn)
        layout.addWidget(self.copy_btn)
        layout.addWidget(self.close_btn)
        layout.addWidget(self.remove_btn)
        layout.addWidget(self.mic_btn)
        layout.addWidget(self.sound_btn)
        layout.addWidget(self.vol_slider)
        self._paint_buttons()

    def set_students(self, students: list[StudentOption]) -> None:
        self._students = list(students)
        self._refill_students(select_current=True)

    def _refill_students(self, *, select_current: bool) -> None:
        combo = self.student_combo
        combo.blockSignals(True)
        combo.clear()
        combo.addItem("— ученик —", None)
        for st in self._students:
            label = st.name
            if not (st.url or "").strip():
                label = f"{label} (нет ссылки)"
            combo.addItem(label, st)
        # Keep free-text name if not in list
        current_name = (self.channel.name or "").strip()
        matched = False
        if select_current:
            for i in range(1, combo.count()):
                st = combo.itemData(i)
                if st is None:
                    continue
                if self.channel.student_id is not None and str(st.id) == str(
                    self.channel.student_id
                ):
                    combo.setCurrentIndex(i)
                    matched = True
                    break
                if current_name and st.name == current_name:
                    combo.setCurrentIndex(i)
                    matched = True
                    break
            if not matched and current_name:
                # Show current assignment as custom entry
                custom = StudentOption(
                    id=self.channel.student_id or current_name,
                    name=current_name,
                    url=(self.channel.url or "").strip(),
                )
                combo.addItem(current_name, custom)
                combo.setCurrentIndex(combo.count() - 1)
                matched = True
            if not matched:
                combo.setCurrentIndex(0)
        combo.blockSignals(False)

    def _on_student_chosen(self, index: int) -> None:
        st = self.student_combo.itemData(index)
        if st is None:
            self.channel.name = ""
            self.channel.url = ""
            self.channel.student_id = None
            return
        self.channel.name = st.name
        self.channel.url = (st.url or "").strip()
        self.channel.student_id = st.id

    def _sync_channel_from_combo(self) -> None:
        self._on_student_chosen(self.student_combo.currentIndex())

    def _remove_row(self) -> None:
        if self._on_remove_row:
            self._on_remove_row(self)

    def _on_slider_pressed(self) -> None:
        self._slider_dragging = True

    def _on_slider_released(self) -> None:
        self._slider_dragging = False
        self._apply_volume(self.vol_slider.value())

    def _on_volume_changed(self, value: int) -> None:
        pct = clamp_volume(value)
        self.vol_slider.setToolTip(f"Громкость: {pct}%")
        self.channel.volume = pct
        if self._slider_dragging:
            # Live preview while dragging
            if self._sound_streams:
                set_streams_volume(self._sound_streams, pct)
            return
        self._apply_volume(pct)

    def _apply_volume(self, percent: int) -> None:
        pct = clamp_volume(percent)
        self.channel.volume = pct
        if self._sound_streams:
            set_streams_volume(self._sound_streams, pct)

    def _sync_volume_slider(self) -> None:
        desired = clamp_volume(self.channel.volume)
        if self.vol_slider.value() != desired:
            self.vol_slider.blockSignals(True)
            self.vol_slider.setValue(desired)
            self.vol_slider.blockSignals(False)
        self.vol_slider.setToolTip(f"Громкость: {desired}%")
        has_stream = bool(self._sound_streams)
        self.vol_slider.setEnabled(has_stream)
        if not has_stream:
            self.vol_slider.setToolTip("Громкость: нет потока")

    def _ensure_desired_volume(self) -> None:
        """Re-apply saved volume when stream appears or drifts (e.g. reconnect)."""
        if self._slider_dragging or not self._sound_streams:
            return
        desired = clamp_volume(self.channel.volume)
        # Average across streams belonging to this channel profile.
        current = round(
            sum(s.volume for s in self._sound_streams) / len(self._sound_streams)
        )
        if abs(current - desired) >= 2:
            set_streams_volume(self._sound_streams, desired)

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
        self._ensure_desired_volume()
        self._paint_buttons()
        if not self._slider_dragging:
            self._sync_volume_slider()

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
        self._students_path = students_file_from_channels_config()
        self.students = load_students(self._students_path)

        root = QWidget()
        root.setStyleSheet("background: #1e1e1e; color: #eee;")
        self.setCentralWidget(root)
        self._root_layout = QVBoxLayout(root)
        self._root_layout.setContentsMargins(6, 6, 6, 6)
        self._root_layout.setSpacing(3)

        self._rows_host = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_host)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(3)
        self._root_layout.addWidget(self._rows_host)

        for ch in self.channels:
            self._append_row(ch)

        self.btn_add = QPushButton("+ Добавить канал")
        self.btn_add.setStyleSheet(STYLE_TOOL)
        self.btn_add.setToolTip("Добавить ещё одну строку канала")
        self.btn_add.clicked.connect(self.add_channel)
        self._root_layout.addWidget(self.btn_add)

        tools = QHBoxLayout()
        tools.setSpacing(4)
        self.btn_launch = QPushButton("▶")
        self.btn_launch.setStyleSheet(STYLE_TOOL)
        self.btn_launch.setToolTip("Запустить окна (без пауз между ними)")
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
        self._root_layout.addLayout(tools)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(POLL_MS)

        self.setMinimumWidth(340)
        self.setMinimumHeight(120)
        self.resize(360, max(160, 40 + 34 * max(2, len(self.rows))))
        self.poll()
        self._setup_tray()
        unmute_non_mixer_browser_inputs(self.channels)

    def _append_row(self, channel: ChannelConfig) -> ChannelRow:
        row = ChannelRow(
            channel,
            on_solo=self.solo_channel,
            on_remove_row=self.remove_channel_row,
            students=self.students,
        )
        self.rows.append(row)
        self._rows_layout.addWidget(row)
        return row

    def add_channel(self) -> None:
        ch = ChannelConfig(
            id=_next_channel_id(self.channels),
            name="",
            url="",
            volume=VOLUME_DEFAULT,
        )
        self.channels.append(ch)
        self._append_row(ch)
        self.save_config()
        self.resize(
            max(self.width(), 360),
            max(self.height(), 40 + 34 * len(self.rows) + 40),
        )

    def remove_channel_row(self, row: ChannelRow) -> None:
        if len(self.rows) <= 1:
            QMessageBox.information(
                self, "Каналы", "Нужна хотя бы одна строка канала."
            )
            return
        if self._solo_row is row:
            self._solo_row = None
        release_mixer_mutes([row.channel])
        close_channels([row.channel], wait_s=3.0)
        self.rows.remove(row)
        if row.channel in self.channels:
            self.channels.remove(row.channel)
        self._rows_layout.removeWidget(row)
        row.deleteLater()
        self.save_config()

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
        act_edit = QAction("Редактировать каналы…", self)
        act_edit.triggered.connect(self.open_channels_file)
        act_reload = QAction("Перечитать каналы", self)
        act_reload.triggered.connect(self.reload_channels)
        act_students = QAction("Загрузить список учеников…", self)
        act_students.triggered.connect(self.load_students_dialog)
        act_quit = QAction("Выйти", self)
        act_quit.triggered.connect(self.quit_app)
        menu.addAction(act_show)
        menu.addSeparator()
        menu.addAction(act_launch)
        menu.addAction(act_close)
        menu.addSeparator()
        menu.addAction(act_edit)
        menu.addAction(act_reload)
        menu.addAction(act_students)
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
            row._sync_channel_from_combo()
        self.channels = [row.channel for row in self.rows]
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

    def load_students_dialog(self) -> None:
        start = str(self._students_path.parent if self._students_path else APP_DIR)
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Список учеников (JSON с сайта)",
            start,
            "JSON (*.json);;All (*)",
        )
        if not path:
            return
        self.apply_students_file(Path(path))

    def apply_students_file(self, path: Path) -> None:
        students = load_students(path)
        if not students:
            QMessageBox.warning(
                self,
                "Ученики",
                f"В файле нет учеников:\n{path}\n"
                "Ожидается JSON вида {\"students\": [{\"id\", \"name\", \"url\"}, …]}",
            )
            return
        self._students_path = path
        self.students = students
        set_students_file_in_config(path)
        # Also copy beside mixer for convenience if outside APP_DIR
        if path.resolve() != STUDENTS_FILE.resolve():
            try:
                STUDENTS_FILE.write_text(
                    path.read_text(encoding="utf-8"),
                    encoding="utf-8",
                )
            except OSError:
                pass
        for row in self.rows:
            row.set_students(students)
        if hasattr(self, "tray"):
            self.tray.showMessage(
                "Телемост микшер",
                f"Загружено учеников: {len(students)}",
                QSystemTrayIcon.MessageIcon.Information,
                2000,
            )

    def reload_channels(self) -> None:
        """Reload channel list from channels.json after manual edit."""
        try:
            fresh = load_channels()
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            QMessageBox.warning(
                self, "Конфиг", f"Не удалось прочитать channels.json:\n{exc}"
            )
            return
        self._students_path = students_file_from_channels_config()
        self.students = load_students(self._students_path)
        # Rebuild rows to match file (count may change)
        while self.rows:
            row = self.rows.pop()
            self._rows_layout.removeWidget(row)
            row.deleteLater()
        self.channels = fresh
        for ch in self.channels:
            self._append_row(ch)
        if hasattr(self, "tray"):
            self.tray.showMessage(
                "Телемост микшер",
                "Каналы перечитаны из channels.json",
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
        to_launch = [
            ch
            for ch in self.channels
            if (ch.url or "").strip() and not pids_for_profile(ch.profile_dir)
        ]
        if not to_launch:
            return

        def _run_launch() -> None:
            for i, ch in enumerate(to_launch):
                launch_channel(ch, index=i)

        threading.Thread(target=_run_launch, daemon=True).start()

    def close_all(self) -> None:
        release_mixer_mutes(self.channels)
        close_channels(self.channels)
        self._solo_row = None
        for row in self.rows:
            row.set_solo_active(False)

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
