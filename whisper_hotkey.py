#!/usr/bin/env python3
"""
Whisper Hotkey — GUI Edition
────────────────────────────
System tray app with floating recording indicator, transcription history, and settings.

Press your configured hotkey to start recording. Press it again to stop and transcribe.
The result is copied to clipboard and pasted into the active text field.

Requirements:
    pip install faster-whisper sounddevice scipy keyboard pyperclip pyautogui PySide6
    pip install nvidia-cublas-cu12 nvidia-cudnn-cu12   (for CUDA)
"""

import os, sys, json, math, threading, datetime
from pathlib import Path
from dataclasses import dataclass, asdict

# ── CUDA DLL pre-registration ─────────────────────────────────────────────────
# Must happen before ctranslate2/faster-whisper are imported, not inside a thread.
import sysconfig as _sysconfig
_sp = _sysconfig.get_path("purelib")
_DEFAULT_CUDA_PATHS = [
    str(Path(_sp) / "nvidia" / "cublas" / "bin"),
    str(Path(_sp) / "nvidia" / "cudnn"  / "bin"),
] if _sp else []
_cfg_file = Path.home() / ".whisper_hotkey" / "config.json"
_cuda_paths = list(_DEFAULT_CUDA_PATHS)
if _cfg_file.exists():
    try:
        _d = json.loads(_cfg_file.read_text())
        _from_cfg = [p for p in (_d.get("cuda_cublas_path", ""), _d.get("cuda_cudnn_path", "")) if p]
        if _from_cfg:
            _cuda_paths = _from_cfg
    except Exception:
        pass
for _p in _cuda_paths:
    try:
        os.add_dll_directory(_p)
    except Exception:
        pass
    # ctranslate2 uses LoadLibrary from C++ which bypasses add_dll_directory.
    # Adding to PATH ensures it finds DLLs through the standard Windows search order.
    os.environ["PATH"] = _p + os.pathsep + os.environ.get("PATH", "")
# ─────────────────────────────────────────────────────────────────────────────

import ctypes
import numpy as np
import sounddevice as sd
import keyboard
import pyperclip

# ── Win32 SendInput structs (module-level — defined once, not per call) ───────
_PUL = ctypes.POINTER(ctypes.c_ulong)

class _KeyBdInput(ctypes.Structure):
    _fields_ = [
        ("wVk",         ctypes.c_ushort),
        ("wScan",       ctypes.c_ushort),
        ("dwFlags",     ctypes.c_ulong),
        ("time",        ctypes.c_ulong),
        ("dwExtraInfo", _PUL),
    ]

# The union must be at least as wide as MOUSEINPUT (28 bytes on 64-bit)
# so that sizeof(INPUT) matches Windows exactly.
class _InputUnion(ctypes.Union):
    _fields_ = [
        ("ki",   _KeyBdInput),
        ("_pad", ctypes.c_ubyte * 28),
    ]

class _Input(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_ulong),
        ("ii",   _InputUnion),
    ]

_INPUT_STRUCT_SIZE  = ctypes.sizeof(_Input)
_KEYEVENTF_UNICODE  = 0x0004
_KEYEVENTF_KEYUP    = 0x0002
_INPUT_KEYBOARD     = 1

# ── Direct text insertion (no clipboard) ─────────────────────────────────────

def _type_text_direct(text: str) -> None:
    """
    Insert *text* into the currently focused field using Win32 SendInput
    with KEYEVENTF_UNICODE — no clipboard is touched at all.
    Works with every language / Unicode character Whisper can produce.
    """
    inputs: list[_Input] = []

    for ch in text:
        code = ord(ch)
        # Characters outside the Basic Multilingual Plane need a surrogate pair
        if code > 0xFFFF:
            code -= 0x10000
            surrogates = [0xD800 | (code >> 10), 0xDC00 | (code & 0x3FF)]
        else:
            surrogates = [code]

        for sc in surrogates:
            for flags in (_KEYEVENTF_UNICODE, _KEYEVENTF_UNICODE | _KEYEVENTF_KEYUP):
                u = _InputUnion()
                u.ki = _KeyBdInput(wVk=0, wScan=sc, dwFlags=flags,
                                   time=0, dwExtraInfo=None)
                inputs.append(_Input(type=_INPUT_KEYBOARD, ii=u))

    if not inputs:
        return
    arr = (_Input * len(inputs))(*inputs)
    ctypes.windll.user32.SendInput(len(inputs), arr, _INPUT_STRUCT_SIZE)

# ─────────────────────────────────────────────────────────────────────────────

from PySide6.QtWidgets import (
    QApplication, QWidget, QDialog, QSystemTrayIcon, QMenu,
    QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QComboBox,
    QPushButton, QLineEdit, QScrollArea, QFrame, QSpinBox, QFileDialog,
)
from PySide6.QtCore import (
    Qt, QThread, Signal, QTimer, QPropertyAnimation,
    QEasingCurve, QObject,
)
from PySide6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QBrush, QPen, QFont, QPainterPath, QAction,
)

# ──────────────────────────────────────────────────────────────────────────────
# THEME
# ──────────────────────────────────────────────────────────────────────────────

# ── Windows 11 DWM Mica / Acrylic backdrop ────────────────────────────────────
# Applies the real system Mica blur to any top-level window on Win11 22H2+.
# Falls back silently on older OS versions.

_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_SYSTEMBACKDROP_TYPE     = 38   # Win11 22H2+
_BACKDROP_MICA                 = 2

def _apply_mica(widget, is_dark: bool = True) -> None:
    """Enable Windows 11 Mica backdrop on *widget* and set dark/light frame."""
    try:
        hwnd = int(widget.winId())
        dwm  = ctypes.windll.dwmapi
        dark = ctypes.c_int(1 if is_dark else 0)
        dwm.DwmSetWindowAttribute(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE,
                                  ctypes.byref(dark), ctypes.sizeof(dark))
        mica = ctypes.c_int(_BACKDROP_MICA)
        dwm.DwmSetWindowAttribute(hwnd, _DWMWA_SYSTEMBACKDROP_TYPE,
                                  ctypes.byref(mica), ctypes.sizeof(mica))
    except Exception:
        pass   # not on Windows 11 22H2+ — no-op


def _windows_is_light() -> bool:
    """Read Windows AppsUseLightTheme registry key. Returns True for light."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as key:
            val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return bool(val)
    except Exception:
        return False  # default to dark if registry unavailable


def get_style(is_dark: bool) -> str:
    # ── Colours pixel-matched to the HTML Fluent reference ────────────────────
    # All rgba() values are Qt-style: rgba(r, g, b, 0-255).
    # Dark palette mirrors the HTML CSS variables exactly.
    if is_dark:
        # Window / dialog base — Mica dark (#202020 + wallpaper tint → ~#1C1C1C)
        bg       = "#1C1C1C"
        # Card rows: rgba(255,255,255,0.04) on #1C1C1C ≈ #252525
        card_bg  = "rgba(255, 255, 255, 10)"   # ~#252525
        card_brd = "rgba(255, 255, 255, 17)"   # ~#313131  border-subtle
        card_hov = "rgba(255, 255, 255, 26)"   # ~#393939  border-mid (hover)
        # Inputs: rgba(255,255,255,0.07)
        inp_bg   = "rgba(255, 255, 255, 18)"   # ~#2A2A2A
        inp_hov  = "rgba(255, 255, 255, 23)"   # ~#2E2E2E
        # Borders
        border   = "rgba(255, 255, 255, 17)"   # default stroke
        border2  = "rgba(255, 255, 255, 26)"   # hover stroke
        border3  = "rgba(255, 255, 255, 46)"   # strong stroke
        # Text — exactly from HTML --text-*
        text     = "#FFFFFF"
        muted2   = "#ABABAB"   # --text-secondary
        muted    = "#686868"   # --text-tertiary
        sec_col  = "#686868"   # section labels (tertiary)
        placeholder = "#686868"
        # Standard button: rgba(255,255,255,0.06)
        btn_bg   = "rgba(255, 255, 255, 15)"
        btn_hov  = "rgba(255, 255, 255, 23)"
        btn_pre  = "rgba(255, 255, 255, 8)"
        # Accent button — #0078D4
        pri_bg   = "#0078D4"; pri_txt = "#FFFFFF"
        pri_hov  = "#006CBE"; pri_pre = "#005BA6"
        # Focus ring
        focus_b  = "#0078D4"
        hover_b  = "rgba(255, 255, 255, 46)"
        # Ghost button
        ghost_brd     = "rgba(255, 255, 255, 26)"
        ghost_hov_txt = "#FFFFFF"
        ghost_hov_brd = "rgba(255, 255, 255, 46)"
        # Danger button — matches HTML .btn-danger
        danger_border = "rgba(196, 43, 28, 76)"   # ~30% opacity
        danger_txt    = "#F1707B"                  # --error
        danger_hov    = "rgba(196, 43, 28, 31)"
        danger_hbrd   = "rgba(196, 43, 28, 127)"
        # Separators
        sep_col  = "rgba(255, 255, 255, 18)"
        # Scrollbar
        scr_trk  = "transparent"
        scr_hdl  = "rgba(255, 255, 255, 38)"
        # Context menu (Acrylic-like dark surface)
        menu_bg  = "#2C2C2C"
        menu_sel = "#383838"
        menu_sep = "rgba(255, 255, 255, 20)"
    else:
        # Light palette — Mica light (#F3F3F3)
        bg       = "#F3F3F3"
        card_bg  = "rgba(255, 255, 255, 178)"  # rgba(255,255,255,0.70)
        card_brd = "rgba(0, 0, 0, 15)"
        card_hov = "rgba(0, 0, 0, 23)"
        inp_bg   = "#FFFFFF"
        inp_hov  = "#F6F6F6"
        border   = "rgba(0, 0, 0, 15)"
        border2  = "rgba(0, 0, 0, 23)"
        border3  = "rgba(0, 0, 0, 36)"
        text     = "#1A1A1A"
        muted2   = "#616161"
        muted    = "#9E9E9E"
        sec_col  = "#9E9E9E"
        placeholder = "#9E9E9E"
        btn_bg   = "rgba(255, 255, 255, 178)"
        btn_hov  = "rgba(0, 0, 0, 10)"
        btn_pre  = "rgba(0, 0, 0, 18)"
        pri_bg   = "#0078D4"; pri_txt = "#FFFFFF"
        pri_hov  = "#006CBE"; pri_pre = "#005BA6"
        focus_b  = "#0078D4"
        hover_b  = "rgba(0, 0, 0, 46)"
        ghost_brd     = "rgba(0, 0, 0, 31)"
        ghost_hov_txt = "#1A1A1A"
        ghost_hov_brd = "rgba(0, 0, 0, 56)"
        danger_border = "rgba(196, 43, 28, 64)"
        danger_txt    = "#C42B1C"
        danger_hov    = "rgba(196, 43, 28, 15)"
        danger_hbrd   = "rgba(196, 43, 28, 102)"
        sep_col  = "rgba(0, 0, 0, 20)"
        scr_trk  = "transparent"
        scr_hdl  = "rgba(0, 0, 0, 46)"
        menu_bg  = "#FFFFFF"
        menu_sel = "#F2F2F2"
        menu_sep = "rgba(0, 0, 0, 20)"

    return f"""
/* ── Mica window base ──────────────────────────────────────────────────────── */
QWidget {{
    background-color: {bg};
    color: {text};
    font-family: "Segoe UI Variable", "Segoe UI", sans-serif;
    font-size: 9.5pt;
    letter-spacing: 0.01em;
}}
QDialog  {{ background-color: {bg}; }}
QLabel   {{ background: transparent; }}

/* ── Card surface (matches HTML .card / .hist-card) ──────────────────────── */
QFrame#card {{
    background-color: {card_bg};
    border: 1px solid {card_brd};
    border-radius: 8px;
}}
QFrame#card:hover {{ border-color: {card_hov}; }}

/* ── Hairline separator ──────────────────────────────────────────────────── */
QFrame#separator {{
    background-color: {sep_col};
    max-height: 1px;
    border: none;
}}

/* ── Labels ──────────────────────────────────────────────────────────────── */
QLabel#section_label {{
    color: {sec_col};
    font-size: 7pt;
    font-weight: 600;
    letter-spacing: 0.08em;
    margin-top: 4px;
    margin-bottom: 1px;
}}
QLabel#muted      {{ color: {muted};  font-size: 8pt;   font-family: "Cascadia Code", "Consolas"; }}
QLabel#body_text  {{ color: {muted2}; font-size: 9.5pt; line-height: 1.5; }}
QLabel#empty_hint {{ color: {muted};  font-size: 9.5pt; padding: 48px 0; }}

/* ── ComboBox ─────────────────────────────────────────────────────────────── */
QComboBox {{
    background-color: {inp_bg};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 3px 28px 3px 9px;
    color: {text};
    min-height: 27px;
}}
QComboBox:hover {{
    background-color: {inp_hov};
    border-color: {border2};
}}
QComboBox:focus {{
    border-color: {border2};
    border-bottom-color: {focus_b};
    border-bottom-width: 2px;
}}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: none; width: 0; }}
QComboBox QAbstractItemView {{
    background-color: {menu_bg};
    border: 1px solid {card_brd};
    border-radius: 8px;
    color: {text};
    selection-background-color: {menu_sel};
    selection-color: {text};
    padding: 3px 0;
    outline: none;
}}
QComboBox QAbstractItemView::item {{
    padding: 5px 12px;
    border-radius: 4px;
    margin: 1px 4px;
    min-height: 22px;
}}


/* ── LineEdit — Fluent bottom-border focus ────────────────────────────────── */
QLineEdit {{
    background-color: {inp_bg};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 3px 9px;
    color: {text};
    min-height: 27px;
    selection-background-color: {pri_bg};
    selection-color: white;
}}
QLineEdit:hover {{
    background-color: {inp_hov};
    border-color: {border2};
}}
QLineEdit:focus {{
    border-color: {border2};
    border-bottom-color: {focus_b};
    border-bottom-width: 2px;
}}

/* ── Standard button ─────────────────────────────────────────────────────── */
QPushButton {{
    background-color: {btn_bg};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 4px 14px;
    color: {text};
    min-height: 27px;
    font-size: 9.5pt;
}}
QPushButton:hover   {{ background-color: {btn_hov}; border-color: {hover_b}; }}
QPushButton:pressed {{ background-color: {btn_pre}; }}

/* ── Accent / primary button — Windows blue ──────────────────────────────── */
QPushButton#primary {{
    background-color: {pri_bg};
    border: 1px solid rgba(0, 0, 0, 36);
    border-bottom-color: rgba(0, 0, 0, 64);
    color: {pri_txt};
    font-weight: 600;
}}
QPushButton#primary:hover   {{ background-color: {pri_hov}; }}
QPushButton#primary:pressed {{ background-color: {pri_pre}; }}

/* ── Ghost (subtle) button ───────────────────────────────────────────────── */
QPushButton#ghost {{
    background: transparent;
    border: 1px solid {ghost_brd};
    color: {muted2};
    font-size: 9pt;
    min-height: 27px;
    padding: 4px 12px;
}}
QPushButton#ghost:hover  {{ color: {ghost_hov_txt}; border-color: {ghost_hov_brd}; background: {btn_hov}; }}
QPushButton#ghost:pressed {{ background: {btn_pre}; }}

/* ── Danger button ───────────────────────────────────────────────────────── */
QPushButton#danger {{
    background: transparent;
    border: 1px solid {danger_border};
    color: {danger_txt};
    font-size: 9pt;
    min-height: 27px;
    padding: 4px 12px;
}}
QPushButton#danger:hover {{ background-color: {danger_hov}; border-color: {danger_hbrd}; }}

/* ── Scrollbar — 4 px thin like HTML ────────────────────────────────────── */
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{
    background: {scr_trk};
    width: 4px;
    border-radius: 2px;
    margin: 0;
}}
QScrollBar::handle:vertical {{
    background: {scr_hdl};
    border-radius: 2px;
    min-height: 32px;
}}
QScrollBar::handle:vertical:hover {{ background: {hover_b}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; border: none; }}
QScrollBar::add-page:vertical,  QScrollBar::sub-page:vertical {{ background: none; }}

/* ── SpinBox ─────────────────────────────────────────────────────────────── */
QSpinBox {{
    background-color: {inp_bg};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 3px 9px;
    color: {text};
    min-height: 27px;
}}
QSpinBox:hover {{ border-color: {border2}; }}
QSpinBox:focus {{ border-color: {border2}; border-bottom-color: {focus_b}; border-bottom-width: 2px; }}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; border: none; }}

/* ── Context / tray menu ─────────────────────────────────────────────────── */
QMenu {{
    background-color: {menu_bg};
    border: 1px solid {card_brd};
    border-radius: 8px;
    color: {text};
    padding: 4px 0;
}}
QMenu::item          {{ padding: 5px 16px 5px 12px; font-size: 9.5pt; border-radius: 4px; margin: 1px 4px; min-height: 24px; }}
QMenu::item:selected {{ background-color: {menu_sel}; }}
QMenu::item:disabled {{ color: {muted}; font-weight: 600; font-size: 8pt; padding-top: 7px; padding-bottom: 5px; }}
QMenu::separator     {{ height: 1px; background: {menu_sep}; margin: 3px 0; }}
"""

# ──────────────────────────────────────────────────────────────────────────────
# CONFIG
# ──────────────────────────────────────────────────────────────────────────────

_CFG_PATH = Path.home() / ".whisper_hotkey" / "config.json"

@dataclass
class Config:
    model:            str  = "turbo"
    language:         str  = "auto"
    hotkey:           str  = "ctrl+shift+space"
    device:           str  = "cuda"
    compute_type:     str  = "float16"
    auto_paste:       bool = True
    overlay_position: str  = "top-center"
    history_limit:    int  = 50
    cuda_cublas_path: str  = ""
    cuda_cudnn_path:  str  = ""
    theme:            str  = "auto"

    @classmethod
    def load(cls):
        if _CFG_PATH.exists():
            try:
                d = json.loads(_CFG_PATH.read_text())
                return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
            except Exception:
                pass
        return cls()

    def save(self):
        _CFG_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CFG_PATH.write_text(json.dumps(asdict(self), indent=2))

# ──────────────────────────────────────────────────────────────────────────────
# HISTORY
# ──────────────────────────────────────────────────────────────────────────────

class History:
    _path = Path.home() / ".whisper_hotkey" / "history.json"

    def __init__(self, limit: int = 50):
        self.limit   = limit
        self.entries: list[dict] = []
        self._load()

    def _load(self):
        if self._path.exists():
            try: self.entries = json.loads(self._path.read_text())
            except Exception: self.entries = []

    def add(self, text: str):
        self.entries.insert(0, {
            "text": text,
            "ts":   datetime.datetime.now().isoformat(timespec="seconds"),
        })
        self.entries = self.entries[:self.limit]
        self._save()

    def _save(self):
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.entries, indent=2))
        tmp.replace(self._path)   # atomic on all major OSes

    def clear(self):
        self.entries = []
        self._save()

# ──────────────────────────────────────────────────────────────────────────────
# WORKERS
# ──────────────────────────────────────────────────────────────────────────────

class ModelLoader(QThread):
    loaded = Signal(object)
    failed = Signal(str)
    status = Signal(str)

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg

    def run(self):
        try:
            for p in (self.cfg.cuda_cublas_path, self.cfg.cuda_cudnn_path):
                if p:
                    try: os.add_dll_directory(p)
                    except OSError: pass
            self.status.emit(f"Loading '{self.cfg.model}' on {self.cfg.device}…")
            from faster_whisper import WhisperModel
            m = WhisperModel(self.cfg.model, device=self.cfg.device, compute_type=self.cfg.compute_type)
            self.loaded.emit(m)
        except Exception as e:
            self.failed.emit(str(e))


class TranscribeWorker(QThread):
    finished = Signal(str)
    failed   = Signal(str)
    _SR = 16000

    def __init__(self, model, audio: np.ndarray, language: str):
        super().__init__()
        self.model    = model
        self.audio    = audio
        self.language = None if language == "auto" else language

    def run(self):
        try:
            segs, _ = self.model.transcribe(self.audio, language=self.language)
            text = " ".join(s.text for s in segs).strip()
            self.finished.emit(text)
        except Exception as e:
            self.failed.emit(str(e))

# ──────────────────────────────────────────────────────────────────────────────
# TRAY ICON
# ──────────────────────────────────────────────────────────────────────────────

# Icon files live in the /icons subfolder next to this script.
_ICON_DIR = Path(__file__).resolve().parent / "icons"

# state → preferred filename candidates (first found wins)
_ICON_FILES: dict[str, list[str]] = {
    "idle":      ["whisper-transparent.png", "whisper-transparent.svg"],
    "recording": ["whisper-transparent-red.png", "whisper-transparent-red.svg"],
    "loading":   ["whisper-transparent-blue.png", "whisper-transparent-blue.svg"],
}


def _make_icon(state: str) -> QIcon:
    """
    Load a tray icon from the asset files that live next to the script.
    Falls back to a minimal programmatic icon if no file is found.

    state: idle | recording | loading
    """
    for name in _ICON_FILES.get(state, []):
        path = _ICON_DIR / name
        if path.exists():
            return QIcon(str(path))

    # ── Fallback: simple coloured dot (no asset files found) ─────────────────
    _fallback_colours = {
        "idle":      QColor(50,  50,  50),
        "recording": QColor(196, 43,  28),
        "loading":   QColor(0,   120, 212),
    }
    sz  = 64
    px  = QPixmap(sz, sz)
    px.fill(Qt.GlobalColor.transparent)
    p   = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    mid = sz // 2
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(_fallback_colours.get(state, QColor(50, 50, 50))))
    p.drawEllipse(1, 1, sz - 2, sz - 2)
    p.setBrush(QBrush(QColor(235, 235, 235)))
    p.drawRoundedRect(mid - 9, 10, 18, 27, 9, 9)
    pen = QPen(QColor(235, 235, 235), 3.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(mid - 13, 29, 26, 16, 0, -180 * 16)
    p.drawLine(mid, 45, mid, 53)
    p.drawLine(mid - 8, 53, mid + 8, 53)
    p.end()
    return QIcon(px)


def _app_icon() -> QIcon:
    """Window / taskbar / shortcut icon — whisper.svg (or .png fallback)."""
    for name in ("whisper.svg", "whisper.png"):
        path = _ICON_DIR / name
        if path.exists():
            return QIcon(str(path))
    return _make_icon("idle")   # absolute last resort

# ──────────────────────────────────────────────────────────────────────────────
# TOGGLE SWITCH
# ──────────────────────────────────────────────────────────────────────────────

class ToggleSwitch(QWidget):
    toggled = Signal(bool)

    def __init__(self, checked: bool = False):
        super().__init__()
        self._on = checked
        self.setFixedSize(42, 22)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    @property
    def checked(self): return self._on

    @checked.setter
    def checked(self, v):
        self._on = bool(v)
        self.update()

    def mousePressEvent(self, _):
        self._on = not self._on
        self.toggled.emit(self._on)
        self.update()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return):
            self._on = not self._on
            self.toggled.emit(self._on)
            self.update()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h, r = self.width(), self.height(), self.height() / 2

        # Track: HTML toggle-track — accent #0078D4 ON, rgba(255,255,255,15%) OFF
        tpath = QPainterPath()
        tpath.addRoundedRect(0, 0, w, h, r, r)
        if self._on:
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(QColor(0, 120, 212)))         # #0078D4 AccentFill
        else:
            p.setPen(QPen(QColor(255, 255, 255, 90), 1.2))  # subtle border
            p.setBrush(QBrush(QColor(255, 255, 255, 38)))   # rgba(255,255,255,15%)
        p.drawPath(tpath)

        # Thumb: white ON, #B4B4B4 OFF — same as HTML
        p.setPen(Qt.PenStyle.NoPen)
        m  = 3
        tx = w - h + m if self._on else m
        p.setBrush(QBrush(QColor(255, 255, 255) if self._on else QColor(180, 180, 180)))
        p.drawEllipse(int(tx), m, h - m * 2, h - m * 2)
        p.end()

# ──────────────────────────────────────────────────────────────────────────────
# RECORDING OVERLAY
# ──────────────────────────────────────────────────────────────────────────────

class RecordingOverlay(QWidget):
    """Floating pill that shows recording/transcription state. Non-interactive."""

    def __init__(self, position: str = "top-center"):
        super().__init__()
        self.position = position
        self.state    = "idle"
        self.elapsed  = 0
        self.preview  = ""
        self._phase   = 0.0
        self.is_dark  = True   # updated by WhisperApp._apply_theme

        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        self.setFixedSize(320, 50)

        self._tick  = QTimer()
        self._tick.setInterval(1000)
        self._tick.timeout.connect(self._on_tick)

        self._anim  = QTimer()
        self._anim.setInterval(48)
        self._anim.timeout.connect(self._on_anim)

        self._hide  = QTimer()
        self._hide.setSingleShot(True)
        self._hide.timeout.connect(self._fade_out)

        self._fade  = QPropertyAnimation(self, b"windowOpacity")
        self._fade.setDuration(380)
        self._fade.setEasingCurve(QEasingCurve.Type.InCubic)
        self._fade.finished.connect(self.hide)

    def _on_tick(self):
        self.elapsed += 1
        self.update()

    def _on_anim(self):
        self._phase += 0.1
        self.update()

    def _place(self):
        g = QApplication.primaryScreen().availableGeometry()
        pos_map = {
            "top-center":    (g.center().x() - self.width() // 2, g.top() + 20),
            "top-right":     (g.right() - self.width() - 20,      g.top() + 20),
            "top-left":      (g.left() + 20,                       g.top() + 20),
            "bottom-center": (g.center().x() - self.width() // 2,  g.bottom() - self.height() - 58),
        }
        x, y = pos_map.get(self.position, pos_map["top-center"])
        self.move(x, y)

    # ── Public state transitions ─────────────────────────────────────────────

    def show_recording(self):
        self._hide.stop(); self._fade.stop(); self.setWindowOpacity(1.0)
        self.state, self.elapsed, self._phase = "recording", 0, 0.0
        self._tick.start(); self._anim.start()
        self._place(); self.show(); self.raise_(); self.update()

    def show_transcribing(self):
        self._tick.stop()
        self._anim.stop()
        self.state = "transcribing"
        self.update()
        self._anim.start()

    def show_done(self, text: str):
        self._anim.stop()
        self.state   = "done"
        self.preview = (text[:38] + "…") if len(text) > 38 else text
        self.update()
        self._hide.start(2600)

    def show_error(self, msg: str):
        self._tick.stop(); self._anim.stop()
        self.state   = "error"
        self.preview = msg[:42]
        self.update()
        self._hide.start(3500)

    def _fade_out(self):
        self._fade.setStartValue(1.0)
        self._fade.setEndValue(0.0)
        self._fade.start()

    # ── Painting ─────────────────────────────────────────────────────────────

    def paintEvent(self, _):
        p  = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        cW, cH = W - 2, H - 4   # content area (shadow offset)

        # Drop shadow
        sh = QPainterPath()
        sh.addRoundedRect(2, 3, cW, cH, 11, 11)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(0, 0, 0, 50 if self.is_dark else 20))
        p.drawPath(sh)

        # Background pill — matches HTML .pill: rgba(22,22,22,0.94) dark / rgba(243,243,243,0.98) light
        bg = QPainterPath()
        bg.addRoundedRect(0, 0, cW, cH, 12, 12)
        if self.is_dark:
            p.setBrush(QColor(22, 22, 22, 240))           # rgba(22,22,22,0.94)
            p.setPen(QPen(QColor(255, 255, 255, 26), 1))  # border-subtle
        else:
            p.setBrush(QColor(243, 243, 243, 250))
            p.setPen(QPen(QColor(0, 0, 0, 15), 1))
        p.drawPath(bg)

        cy = cH // 2
        if   self.state == "recording":    self._draw_recording(p, cW, cH, cy)
        elif self.state == "transcribing": self._draw_transcribing(p, cW, cH, cy)
        elif self.state == "done":         self._draw_done(p, cW, cH, cy)
        elif self.state == "error":        self._draw_error(p, cW, cH, cy)
        p.end()

    def _draw_recording(self, p, W, H, cy):
        cx = 22
        pulse = 0.5 + 0.5 * math.sin(self._phase)
        # Pulse ring — HTML: rgba(241,112,123,0.5) → QColor(241,112,123, alpha)
        ring = int(8 + 5 * pulse)
        p.setPen(QPen(QColor(241, 112, 123, int(127 * pulse)), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(cx - ring, cy - ring, ring * 2, ring * 2)
        # Dot — HTML: #F1707B (--error)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(241, 112, 123))
        p.drawEllipse(cx - 6, cy - 6, 12, 12)
        # "Recording" label — HTML: --text-primary #FFFFFF
        p.setPen(QColor(255, 255, 255) if self.is_dark else QColor(26, 26, 26))
        p.setFont(QFont("Segoe UI Variable", 10, QFont.Weight.Medium))
        p.drawText(42, 0, 140, H, Qt.AlignmentFlag.AlignVCenter, "Recording")
        # Timer — HTML: --text-tertiary #686868
        p.setPen(QColor(104, 104, 104))
        p.setFont(QFont("Cascadia Code", 9))
        m, s = self.elapsed // 60, self.elapsed % 60
        p.drawText(0, 0, W - 12, H, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, f"{m:02d}:{s:02d}")

    def _draw_transcribing(self, p, W, H, cy):
        cx = 22
        for i in range(8):
            angle = i * (2 * math.pi / 8) - self._phase
            sx = cx + 8 * math.cos(angle)
            sy = cy + 8 * math.sin(angle)
            p.setPen(Qt.PenStyle.NoPen)
            # HTML spin-dot: --text-secondary #ABABAB with opacity falloff
            alpha = int(40 + 215 * (i / 8))
            p.setBrush(QColor(171, 171, 171, alpha) if self.is_dark else QColor(80, 80, 80, alpha))
            p.drawEllipse(int(sx) - 2, int(sy) - 2, 4, 4)
        # "Transcribing…" — HTML: --text-secondary #ABABAB
        p.setPen(QColor(171, 171, 171) if self.is_dark else QColor(97, 97, 97))
        p.setFont(QFont("Segoe UI Variable", 10, QFont.Weight.Medium))
        p.drawText(42, 0, W - 50, H, Qt.AlignmentFlag.AlignVCenter, "Transcribing…")

    def _draw_done(self, p, W, H, cy):
        cx = 22
        p.setPen(Qt.PenStyle.NoPen)
        # HTML .check-circle: rgba(0,120,212,0.2) fill + #0078D4 border
        p.setBrush(QColor(0, 120, 212, 51))
        p.drawEllipse(cx - 9, cy - 9, 18, 18)
        pen_ring = QPen(QColor(0, 120, 212), 1.5)
        p.setPen(pen_ring); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(cx - 9, cy - 9, 18, 18)
        # White checkmark
        pen = QPen(QColor(0, 120, 212), 2.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen)
        p.drawLine(cx - 4, cy + 1, cx - 1, cy + 4)
        p.drawLine(cx - 1, cy + 4, cx + 5, cy - 3)
        # Preview text — HTML: --text-secondary #ABABAB
        p.setPen(QColor(171, 171, 171) if self.is_dark else QColor(97, 97, 97))
        p.setFont(QFont("Segoe UI Variable", 9))
        p.drawText(42, 0, W - 50, H, Qt.AlignmentFlag.AlignVCenter, self.preview)

    def _draw_error(self, p, W, H, cy):
        cx = 22
        p.setPen(Qt.PenStyle.NoPen)
        # HTML .error-circle: rgba(241,112,123,0.2) fill + #F1707B border
        p.setBrush(QColor(241, 112, 123, 51))
        p.drawEllipse(cx - 9, cy - 9, 18, 18)
        p.setPen(QPen(QColor(241, 112, 123), 1.5)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(cx - 9, cy - 9, 18, 18)
        # × mark in error red
        pen = QPen(QColor(241, 112, 123), 2)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        p.setPen(pen)
        p.drawLine(cx - 4, cy - 4, cx + 4, cy + 4)
        p.drawLine(cx + 4, cy - 4, cx - 4, cy + 4)
        # Error text — HTML: --text-secondary #ABABAB
        p.setPen(QColor(171, 171, 171) if self.is_dark else QColor(97, 97, 97))
        p.setFont(QFont("Segoe UI Variable", 9))
        p.drawText(42, 0, W - 50, H, Qt.AlignmentFlag.AlignVCenter, self.preview)

# ──────────────────────────────────────────────────────────────────────────────
# HISTORY WINDOW
# ──────────────────────────────────────────────────────────────────────────────

class HistoryWindow(QWidget):
    def __init__(self, history: History, is_dark: bool = True):
        super().__init__()
        self.history = history
        self._is_dark = is_dark
        self.setWindowTitle("Whisper — History")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowCloseButtonHint)
        self.setMinimumSize(480, 420)
        self.resize(480, 580)
        self._build()

    def showEvent(self, e):
        super().showEvent(e)
        self._refresh()
        _apply_mica(self, self._is_dark)

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 12)
        root.setSpacing(10)

        # Header row
        hdr = QHBoxLayout()
        title = QLabel("History")
        title.setFont(QFont("Segoe UI Variable", 13, QFont.Weight.Light))
        hdr.addWidget(title)
        hdr.addStretch()
        self._clear_btn = QPushButton("Clear all")
        self._clear_btn.setObjectName("danger")
        self._clear_btn.clicked.connect(self._clear)
        hdr.addWidget(self._clear_btn)
        root.addLayout(hdr)

        # Separator
        line = QFrame()
        line.setFrameShape(QFrame.Shape.HLine)
        line.setObjectName("separator")
        root.addWidget(line)

        # Scroll area
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._inner = QWidget()
        self._vbox  = QVBoxLayout(self._inner)
        self._vbox.setContentsMargins(0, 4, 0, 4)
        self._vbox.setSpacing(8)
        self._vbox.addStretch()
        self._scroll.setWidget(self._inner)
        root.addWidget(self._scroll)

        self._refresh()

    def _refresh(self):
        while self._vbox.count() > 1:
            item = self._vbox.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        if not self.history.entries:
            lbl = QLabel("No transcriptions yet.\nUse the hotkey to record something.")
            lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
            lbl.setObjectName("empty_hint")
            self._vbox.insertWidget(0, lbl)
            return

        for i, entry in enumerate(self.history.entries):
            self._vbox.insertWidget(i, self._card(entry))

    def _card(self, entry: dict) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        lay = QVBoxLayout(card)
        lay.setContentsMargins(12, 8, 12, 10)
        lay.setSpacing(5)

        top = QHBoxLayout()
        ts  = QLabel(self._fmt(entry["ts"]))
        ts.setObjectName("muted")
        top.addWidget(ts)
        top.addStretch()

        btn = QPushButton("Copy")
        btn.setObjectName("ghost")
        btn.setFixedSize(56, 22)
        text = entry["text"]

        def on_copy(_, b=btn, t=text):
            pyperclip.copy(t)
            b.setText("Copied")
            QTimer.singleShot(1400, lambda: b.setText("Copy"))

        btn.clicked.connect(on_copy)
        top.addWidget(btn)
        lay.addLayout(top)

        body = QLabel(text)
        body.setWordWrap(True)
        body.setObjectName("body_text")
        lay.addWidget(body)
        return card

    def _fmt(self, ts: str) -> str:
        try:
            dt  = datetime.datetime.fromisoformat(ts)
            now = datetime.datetime.now()
            if dt.date() == now.date():
                return "Today  " + dt.strftime("%H:%M:%S")
            if (now.date() - dt.date()).days == 1:
                return "Yesterday  " + dt.strftime("%H:%M")
            return dt.strftime("%b %d  %H:%M")
        except Exception:
            return ts

    def _clear(self):
        self.history.clear()
        self._refresh()

    def refresh(self):
        if self.isVisible():
            self._refresh()

# ──────────────────────────────────────────────────────────────────────────────
# SETTINGS DIALOG
# ──────────────────────────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    saved = Signal(object)   # emits Config

    _MODELS   = ["tiny", "base", "small", "medium", "large-v2", "large-v3", "turbo"]
    _DEVICES  = ["cuda", "cpu"]
    _CTYPES   = ["float16", "int8", "float32"]
    _POSITIONS = [
        ("Top center",    "top-center"),
        ("Top right",     "top-right"),
        ("Top left",      "top-left"),
        ("Bottom center", "bottom-center"),
    ]
    _LANGUAGES = [
        ("Auto-detect", "auto"), ("English",    "en"), ("Arabic",     "ar"),
        ("French",      "fr"),   ("Spanish",    "es"), ("German",     "de"),
        ("Chinese",     "zh"),   ("Japanese",   "ja"), ("Russian",    "ru"),
        ("Portuguese",  "pt"),   ("Italian",    "it"), ("Korean",     "ko"),
    ]

    def __init__(self, cfg: Config, history: History, parent=None, is_dark: bool = True):
        super().__init__(parent)
        self.cfg     = cfg
        self.history = history
        self._is_dark = is_dark
        self.setWindowTitle("Settings")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.WindowCloseButtonHint)
        self.setFixedWidth(440)
        self._build()

    def showEvent(self, e):
        super().showEvent(e)
        _apply_mica(self, self._is_dark)


    def _section(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setObjectName("section_label")
        return lbl

    def _card(self) -> QFrame:
        f = QFrame()
        f.setObjectName("card")
        return f

    def _form(self, card: QFrame) -> QFormLayout:
        fl = QFormLayout(card)
        fl.setContentsMargins(12, 8, 12, 10)
        fl.setSpacing(7)
        fl.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        fl.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        fl.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        return fl

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(6)

        title = QLabel("Settings")
        title.setFont(QFont("Segoe UI Variable", 13, QFont.Weight.Light))
        root.addWidget(title)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setObjectName("separator")
        root.addWidget(sep)

        # ── Model ──────────────────────────────────────────────────────────────
        root.addWidget(self._section("MODEL"))
        card = self._card(); fl = self._form(card)

        self._model_cb = QComboBox(); self._model_cb.addItems(self._MODELS)
        self._model_cb.setCurrentText(self.cfg.model)
        self._device_cb = QComboBox(); self._device_cb.addItems(self._DEVICES)
        self._device_cb.setCurrentText(self.cfg.device)
        self._ctype_cb = QComboBox(); self._ctype_cb.addItems(self._CTYPES)
        self._ctype_cb.setCurrentText(self.cfg.compute_type)

        fl.addRow("Whisper model",  self._model_cb)
        fl.addRow("Device",         self._device_cb)
        fl.addRow("Compute type",   self._ctype_cb)
        root.addWidget(card)

        # ── Recognition ────────────────────────────────────────────────────────
        root.addWidget(self._section("RECOGNITION"))
        card2 = self._card(); fl2 = self._form(card2)

        self._lang_cb = QComboBox()
        for label, code in self._LANGUAGES:
            self._lang_cb.addItem(label, code)
        idx = next((i for i, (_, c) in enumerate(self._LANGUAGES) if c == self.cfg.language), 0)
        self._lang_cb.setCurrentIndex(idx)
        fl2.addRow("Language", self._lang_cb)
        root.addWidget(card2)

        # ── Behavior ───────────────────────────────────────────────────────────
        root.addWidget(self._section("BEHAVIOR"))
        card3 = self._card(); fl3 = self._form(card3)

        self._hotkey_edit = QLineEdit(self.cfg.hotkey)
        self._hotkey_edit.setPlaceholderText("e.g. ctrl+shift+space")
        self._paste_toggle = ToggleSwitch(self.cfg.auto_paste)
        self._pos_cb = QComboBox()
        for label, data in self._POSITIONS:
            self._pos_cb.addItem(label, data)
        cur_pos = next((i for i, (_, d) in enumerate(self._POSITIONS) if d == self.cfg.overlay_position), 0)
        self._pos_cb.setCurrentIndex(cur_pos)

        self._theme_cb = QComboBox()
        for label, data in (("Dark", "dark"), ("Light", "light"), ("Auto (follow Windows)", "auto")):
            self._theme_cb.addItem(label, data)
        theme_idx = next((i for i, (_, d) in enumerate((("Dark","dark"),("Light","light"),("Auto (follow Windows)","auto"))) if d == self.cfg.theme), 2)
        self._theme_cb.setCurrentIndex(theme_idx)

        fl3.addRow("Hotkey",           self._hotkey_edit)
        fl3.addRow("Auto-paste",       self._paste_toggle)
        fl3.addRow("Overlay position", self._pos_cb)
        fl3.addRow("Theme",            self._theme_cb)
        root.addWidget(card3)

        # ── CUDA ───────────────────────────────────────────────────────────────
        root.addWidget(self._section("CUDA DLL PATHS  —  leave blank if not needed"))
        card4 = self._card(); fl4 = self._form(card4)

        self._cublas_edit = QLineEdit(self.cfg.cuda_cublas_path)
        self._cublas_edit.setPlaceholderText("…\\nvidia\\cublas\\bin")
        self._cudnn_edit  = QLineEdit(self.cfg.cuda_cudnn_path)
        self._cudnn_edit.setPlaceholderText("…\\nvidia\\cudnn\\bin")

        for edit, label in ((self._cublas_edit, "cuBLAS"), (self._cudnn_edit, "cuDNN")):
            row = QHBoxLayout(); row.setSpacing(6)
            row.addWidget(edit)
            btn = QPushButton("…"); btn.setFixedWidth(30)
            btn.clicked.connect(lambda _, e=edit: self._browse(e))
            row.addWidget(btn)
            fl4.addRow(label, row)
        root.addWidget(card4)

        # ── History ────────────────────────────────────────────────────────────
        root.addWidget(self._section("HISTORY"))
        card5 = self._card()
        hlay  = QHBoxLayout(card5)
        hlay.setContentsMargins(12, 8, 12, 8)
        hlay.setSpacing(10)

        hlay.addWidget(QLabel("Keep last"))
        self._hist_spin = QSpinBox(); self._hist_spin.setRange(10, 500)
        self._hist_spin.setValue(self.cfg.history_limit); self._hist_spin.setFixedWidth(72)
        hlay.addWidget(self._hist_spin)
        hlay.addWidget(QLabel("entries"))
        hlay.addStretch()
        clr = QPushButton("Clear history"); clr.setObjectName("danger")
        clr.clicked.connect(lambda: (self.history.clear(), clr.setText("Cleared")))
        hlay.addWidget(clr)
        root.addWidget(card5)

        root.addStretch()

        # ── Buttons ────────────────────────────────────────────────────────────
        btns = QHBoxLayout(); btns.setSpacing(8)
        btns.addStretch()
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        btns.addWidget(cancel)
        save = QPushButton("Save"); save.setObjectName("primary")
        save.clicked.connect(self._save)
        btns.addWidget(save)
        root.addLayout(btns)

    def _browse(self, edit: QLineEdit):
        d = QFileDialog.getExistingDirectory(self, "Select directory", edit.text())
        if d: edit.setText(d)

    def _save(self):
        self.cfg.model            = self._model_cb.currentText()
        self.cfg.device           = self._device_cb.currentText()
        self.cfg.compute_type     = self._ctype_cb.currentText()
        self.cfg.language         = self._lang_cb.currentData()
        self.cfg.hotkey           = self._hotkey_edit.text().strip() or "ctrl+shift+space"
        self.cfg.auto_paste       = self._paste_toggle.checked
        self.cfg.overlay_position = self._pos_cb.currentData()
        self.cfg.cuda_cublas_path = self._cublas_edit.text().strip()
        self.cfg.cuda_cudnn_path  = self._cudnn_edit.text().strip()
        self.cfg.history_limit    = self._hist_spin.value()
        self.cfg.theme            = self._theme_cb.currentData()
        self.cfg.save()
        self.saved.emit(self.cfg)
        self.accept()

# ──────────────────────────────────────────────────────────────────────────────
# MAIN APP CONTROLLER
# ──────────────────────────────────────────────────────────────────────────────

class WhisperApp(QObject):
    _toggle_sig        = Signal()
    _rec_started_sig   = Signal()
    _audio_ready_sig   = Signal(object)   # numpy array

    def __init__(self):
        super().__init__()
        self.cfg     = Config.load()
        self.history = History(self.cfg.history_limit)
        self.model   = None
        self._recording   = False
        self._chunks: list[np.ndarray] = []
        self._stream = None
        self._lock   = threading.Lock()
        self._worker = None   # keep reference alive

        self.overlay     = RecordingOverlay(self.cfg.overlay_position)
        self._hist_win: HistoryWindow | None = None
        self._hotkey_hook = None   # keyboard hook handle for targeted removal

        # Tray
        self.tray = QSystemTrayIcon()
        self.tray.setIcon(_make_icon("loading"))
        self.tray.setToolTip("Whisper Hotkey — loading…")
        self._build_tray_menu()
        self.tray.show()

        # Thread-safe signal bridges
        self._toggle_sig.connect(self._on_toggle)
        self._rec_started_sig.connect(self._on_rec_started)
        self._audio_ready_sig.connect(self._on_audio_ready)

        # Theme — apply on start, poll every 5s for auto mode
        self._last_dark: bool | None = None
        self._is_dark: bool = True
        self._apply_theme()
        self._theme_timer = QTimer()
        self._theme_timer.setInterval(5000)
        self._theme_timer.timeout.connect(self._apply_theme)
        self._theme_timer.start()

        # Load model
        self._start_loader()

    # ── Theme ────────────────────────────────────────────────────────────────

    def _apply_theme(self):
        t = self.cfg.theme
        if t == "dark":
            is_dark = True
        elif t == "light":
            is_dark = False
        else:
            is_dark = not _windows_is_light()
        if is_dark == self._last_dark:
            return
        self._last_dark = is_dark
        self._is_dark = is_dark
        style = get_style(is_dark)
        QApplication.instance().setStyleSheet(style)
        self.overlay.is_dark = is_dark
        self.overlay.update()
        if self._hist_win:
            self._hist_win.setStyleSheet(style)

    # ── Tray ─────────────────────────────────────────────────────────────────

    def _build_tray_menu(self):
        menu = QMenu()
        title = QAction("Whisper Hotkey", menu); title.setEnabled(False)
        menu.addAction(title)
        menu.addSeparator()
        hist_a = QAction("History…", menu); hist_a.triggered.connect(self._open_history)
        menu.addAction(hist_a)
        sett_a = QAction("Settings…", menu); sett_a.triggered.connect(self._open_settings)
        menu.addAction(sett_a)
        menu.addSeparator()
        quit_a = QAction("Quit", menu); quit_a.triggered.connect(self._quit)
        menu.addAction(quit_a)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(
            lambda r: self._open_history() if r == QSystemTrayIcon.ActivationReason.Trigger else None
        )

    # ── Model loading ─────────────────────────────────────────────────────────

    def _start_loader(self):
        self._loader = ModelLoader(self.cfg)
        self._loader.loaded.connect(self._on_loaded)
        self._loader.failed.connect(self._on_load_failed)
        self._loader.status.connect(lambda s: self.tray.setToolTip(f"Whisper Hotkey — {s}"))
        self._loader.start()

    def _on_loaded(self, model):
        self.model = model
        self.tray.setIcon(_make_icon("idle"))
        self.tray.setToolTip(f"Whisper Hotkey — {self.cfg.model}  ·  {self.cfg.hotkey}")
        self._register_hotkey()

    def _on_load_failed(self, err: str):
        self.tray.setIcon(_make_icon("idle"))
        self.tray.showMessage("Whisper Hotkey", f"Failed to load model:\n{err}", QSystemTrayIcon.MessageIcon.Critical, 6000)
        self.tray.setToolTip(f"Whisper Hotkey — load error")

    # ── Hotkey ───────────────────────────────────────────────────────────────

    def _register_hotkey(self):
        try:
            parts = self.cfg.hotkey.strip().split("+")
            last, mods = parts[-1], parts[:-1]
            if not last:
                raise ValueError("Empty key")
            self._hotkey_hook = keyboard.on_press_key(
                last,
                lambda _: self._toggle_sig.emit() if all(keyboard.is_pressed(k) for k in mods) else None
            )
        except Exception as e:
            self.tray.showMessage("Whisper Hotkey", f"Invalid hotkey: {e}", QSystemTrayIcon.MessageIcon.Warning, 4000)

    def _unregister_hotkey(self):
        if self._hotkey_hook is not None:
            try:
                keyboard.unhook(self._hotkey_hook)
            except Exception:
                pass
            self._hotkey_hook = None

    # ── Toggle (main thread) ─────────────────────────────────────────────────

    def _on_toggle(self):
        if self.model is None:
            self.tray.showMessage("Whisper Hotkey", "Model is still loading…", QSystemTrayIcon.MessageIcon.Information, 2000)
            return
        with self._lock:
            if not self._recording:
                self._recording = True
                threading.Thread(target=self._start_recording, daemon=True).start()
            else:
                self._recording = False
                self.overlay.show_transcribing()
                threading.Thread(target=self._stop_recording, daemon=True).start()

    # ── Recording threads ─────────────────────────────────────────────────────

    def _start_recording(self):
        self._chunks = []
        self._stream = sd.InputStream(
            samplerate=16000, channels=1, dtype="float32",
            callback=lambda data, *_: self._chunks.append(data.copy())
        )
        self._stream.start()
        self._rec_started_sig.emit()

    def _on_rec_started(self):
        self.tray.setIcon(_make_icon("recording"))
        self.overlay.show_recording()

    def _stop_recording(self):
        if self._stream:
            self._stream.stop(); self._stream.close(); self._stream = None
        if not self._chunks:
            self._audio_ready_sig.emit(np.array([]))
            return
        audio = np.concatenate(self._chunks, axis=0).flatten()
        self._audio_ready_sig.emit(audio)

    # ── Transcription (main thread) ───────────────────────────────────────────

    def _on_audio_ready(self, audio: np.ndarray):
        self.tray.setIcon(_make_icon("idle"))
        if audio.size == 0:
            self.overlay.show_error("No audio captured")
            return
        self._worker = TranscribeWorker(self.model, audio, self.cfg.language)
        self._worker.finished.connect(self._on_transcribed)
        self._worker.failed.connect(lambda e: self.overlay.show_error(e[:48]))
        self._worker.start()

    def _on_transcribed(self, text: str):
        if not text:
            self.overlay.show_error("Nothing recognized")
            return
        self.history.add(text)
        if self.cfg.auto_paste:
            QTimer.singleShot(80, lambda: _type_text_direct(text))
        self.overlay.show_done(text)
        if self._hist_win:
            self._hist_win.refresh()

    # ── Windows ──────────────────────────────────────────────────────────────

    def _open_history(self):
        if self._hist_win is None:
            self._hist_win = HistoryWindow(self.history, self._is_dark)
            self._hist_win.setStyleSheet(QApplication.instance().styleSheet())
        self._hist_win.show(); self._hist_win.raise_(); self._hist_win.activateWindow()

    def _open_settings(self):
        dlg = SettingsDialog(self.cfg, self.history, is_dark=self._is_dark)
        dlg.setStyleSheet(QApplication.instance().styleSheet())
        dlg.saved.connect(self._on_settings_saved)
        dlg.exec()

    def _on_settings_saved(self, new_cfg: Config):
        old = self.cfg
        self.cfg = new_cfg
        self.overlay.position = new_cfg.overlay_position
        self.history.limit    = new_cfg.history_limit

        self._apply_theme()

        model_changed = (
            new_cfg.model        != old.model or
            new_cfg.device       != old.device or
            new_cfg.compute_type != old.compute_type or
            new_cfg.cuda_cublas_path != old.cuda_cublas_path or
            new_cfg.cuda_cudnn_path  != old.cuda_cudnn_path
        )

        if model_changed:
            self._unregister_hotkey()
            self.model = None
            self.tray.setIcon(_make_icon("loading"))
            self.tray.setToolTip("Whisper Hotkey — reloading…")
            self._start_loader()
        elif new_cfg.hotkey != old.hotkey:
            self._unregister_hotkey()
            if self.model:
                self._register_hotkey()

    def _quit(self):
        self._unregister_hotkey()
        QApplication.quit()

# ──────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Whisper Hotkey")
    app.setWindowIcon(_app_icon())
    cfg = Config.load()
    _is_dark = (cfg.theme == "dark") or (cfg.theme == "auto" and not _windows_is_light())
    app.setStyleSheet(get_style(_is_dark))

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("ERROR: System tray is not available on this desktop.")
        sys.exit(1)

    _ = WhisperApp()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()