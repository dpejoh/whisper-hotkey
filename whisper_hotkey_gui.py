#!/usr/bin/env python3
"""
Whisper Hotkey — GUI Edition
────────────────────────────
System tray app with floating recording indicator, transcription history, and settings.

Press your configured hotkey to start recording. Press it again to stop and transcribe.
The result is copied to clipboard and pasted into the active text field.

Requirements:
    pip install faster-whisper sounddevice scipy keyboard pyperclip pyautogui PyQt6
    pip install nvidia-cublas-cu12 nvidia-cudnn-cu12   (for CUDA)
"""

import os, sys, json, math, time, tempfile, threading, datetime
from pathlib import Path
from dataclasses import dataclass, asdict

# ── CUDA DLL pre-registration ─────────────────────────────────────────────────
# Must happen before ctranslate2/faster-whisper are imported, not inside a thread.
_DEFAULT_CUDA_PATHS = [
    r"C:\Users\khaled\AppData\Local\Programs\Python\Python311\Lib\site-packages\nvidia\cublas\bin",
    r"C:\Users\khaled\AppData\Local\Programs\Python\Python311\Lib\site-packages\nvidia\cudnn\bin",
]
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
import scipy.io.wavfile as wav
import sounddevice as sd
import keyboard
import pyperclip
import pyautogui

# ── Direct text insertion (no clipboard) ─────────────────────────────────────

def _type_text_direct(text: str) -> None:
    """
    Insert *text* into the currently focused field using Win32 SendInput
    with KEYEVENTF_UNICODE — no clipboard is touched at all.
    Works with every language / Unicode character Whisper can produce.
    """
    KEYEVENTF_UNICODE = 0x0004
    KEYEVENTF_KEYUP   = 0x0002
    INPUT_KEYBOARD    = 1

    PUL = ctypes.POINTER(ctypes.c_ulong)

    class _KeyBdInput(ctypes.Structure):
        _fields_ = [
            ("wVk",         ctypes.c_ushort),
            ("wScan",       ctypes.c_ushort),
            ("dwFlags",     ctypes.c_ulong),
            ("time",        ctypes.c_ulong),
            ("dwExtraInfo", PUL),
        ]

    # The union must be at least as wide as MOUSEINPUT (28 bytes on 64-bit)
    # so that sizeof(INPUT) matches Windows exactly.
    class _InputUnion(ctypes.Union):
        _fields_ = [
            ("ki",  _KeyBdInput),
            ("_pad", ctypes.c_ubyte * 28),
        ]

    class _Input(ctypes.Structure):
        _fields_ = [
            ("type", ctypes.c_ulong),
            ("ii",   _InputUnion),
        ]

    struct_size = ctypes.sizeof(_Input)
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
            for flags in (KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP):
                u = _InputUnion()
                u.ki = _KeyBdInput(wVk=0, wScan=sc, dwFlags=flags,
                                   time=0, dwExtraInfo=None)
                inp = _Input(type=INPUT_KEYBOARD, ii=u)
                inputs.append(inp)

    if not inputs:
        return
    arr = (_Input * len(inputs))(*inputs)
    ctypes.windll.user32.SendInput(len(inputs), arr, struct_size)

# ─────────────────────────────────────────────────────────────────────────────

from PyQt6.QtWidgets import (
    QApplication, QWidget, QDialog, QSystemTrayIcon, QMenu,
    QVBoxLayout, QHBoxLayout, QFormLayout, QLabel, QComboBox,
    QPushButton, QLineEdit, QScrollArea, QFrame, QSpinBox, QFileDialog,
)
from PyQt6.QtCore import (
    Qt, QThread, pyqtSignal, QTimer, QPropertyAnimation,
    QEasingCurve, QObject,
)
from PyQt6.QtGui import (
    QIcon, QPixmap, QPainter, QColor, QBrush, QPen, QFont, QPainterPath, QAction,
)

# ──────────────────────────────────────────────────────────────────────────────
# THEME
# ──────────────────────────────────────────────────────────────────────────────

def _windows_is_light() -> bool:
    """Read Windows AppsUseLightTheme registry key. Returns True for light."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        winreg.CloseKey(key)
        return bool(val)
    except Exception:
        return False  # default to dark if registry unavailable


def get_style(is_dark: bool) -> str:
    if is_dark:
        bg      = "#161616"; bg2     = "#1E1E1E"; bg3     = "#222222"
        border  = "#333333"; border2 = "#2D2D2D"; border3 = "#3A3A3A"
        hover_b = "#4A4A4A"; focus_b = "#666666"
        text    = "#E8E8E8"; muted   = "#555555"; muted2  = "#888888"
        btn_bg  = "#272727"; btn_hov = "#323232"; btn_pre = "#1A1A1A"
        pri_bg  = "#E8E8E8"; pri_txt = "#161616"; pri_hov = "#CCCCCC"
        scr_trk = "#161616"; scr_hdl = "#404040"
        menu_bg = "#1E1E1E"; menu_sel = "#2E2E2E"; menu_sep = "#2E2E2E"
        danger_border = "#3A1A1A"; danger_txt = "#CC4444"; danger_hov = "#1E1010"; danger_hbrd = "#553333"
        ghost_brd = "#383838"; ghost_hov_txt = "#E8E8E8"; ghost_hov_brd = "#555555"
        inp_bg  = "#222222"
        card_bg = "#1E1E1E"; card_brd = "#2D2D2D"; card_hov = "#3A3A3A"
        sep_col = "#2A2A2A"
        sec_col = "#555555"
        placeholder = "#555555"
    else:
        bg      = "#F2F2F2"; bg2     = "#E8E8E8"; bg3     = "#DEDEDE"
        border  = "#D0D0D0"; border2 = "#D8D8D8"; border3 = "#C8C8C8"
        hover_b = "#AAAAAA"; focus_b = "#888888"
        text    = "#1A1A1A"; muted   = "#888888"; muted2  = "#777777"
        btn_bg  = "#E0E0E0"; btn_hov = "#D4D4D4"; btn_pre = "#C8C8C8"
        pri_bg  = "#1A1A1A"; pri_txt = "#F2F2F2"; pri_hov = "#333333"
        scr_trk = "#F2F2F2"; scr_hdl = "#BBBBBB"
        menu_bg = "#F8F8F8"; menu_sel = "#E8E8E8"; menu_sep = "#DDDDDD"
        danger_border = "#F0CCCC"; danger_txt = "#CC3333"; danger_hov = "#FDF0F0"; danger_hbrd = "#E8AAAA"
        ghost_brd = "#CCCCCC"; ghost_hov_txt = "#1A1A1A"; ghost_hov_brd = "#999999"
        inp_bg  = "#FFFFFF"
        card_bg = "#FFFFFF"; card_brd = "#E0E0E0"; card_hov = "#CCCCCC"
        sep_col = "#DDDDDD"
        sec_col = "#999999"
        placeholder = "#AAAAAA"

    return f"""
QWidget {{
    background-color: {bg};
    color: {text};
    font-family: "Segoe UI", "SF Pro Text", sans-serif;
    font-size: 10pt;
}}
QDialog {{ background-color: {bg}; }}
QLabel  {{ background: transparent; }}

QFrame#card {{
    background-color: {card_bg};
    border: 1px solid {card_brd};
    border-radius: 6px;
}}
QFrame#card:hover {{ border-color: {card_hov}; }}
QFrame#separator {{
    background-color: {sep_col};
    max-height: 1px;
    border: none;
}}

QLabel#section_label {{
    color: {sec_col};
    font-size: 8pt;
    font-weight: 600;
    letter-spacing: 1.2px;
    margin-top: 4px;
}}
QLabel#muted      {{ color: {muted};  font-size: 8pt; font-family: "Consolas"; }}
QLabel#body_text  {{ color: {muted2}; font-size: 9.5pt; }}
QLabel#empty_hint {{ color: {muted};  font-size: 9.5pt; padding: 48px 0; }}

QComboBox {{
    background-color: {inp_bg};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 5px 10px;
    color: {text};
    min-height: 26px;
}}
QComboBox:hover  {{ border-color: {hover_b}; }}
QComboBox::drop-down {{ border: none; width: 24px; }}
QComboBox::down-arrow {{ image: none; }}
QComboBox QAbstractItemView {{
    background-color: {bg2};
    border: 1px solid {border3};
    color: {text};
    selection-background-color: {bg3};
    outline: none;
}}
QLineEdit {{
    background-color: {inp_bg};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 5px 10px;
    color: {text};
    min-height: 26px;
}}
QLineEdit:hover  {{ border-color: {hover_b}; }}
QLineEdit:focus  {{ border-color: {focus_b}; }}

QPushButton {{
    background-color: {btn_bg};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 6px 16px;
    color: {text};
    min-height: 26px;
}}
QPushButton:hover   {{ background-color: {btn_hov}; border-color: {hover_b}; }}
QPushButton:pressed {{ background-color: {btn_pre}; }}
QPushButton#primary {{
    background-color: {pri_bg};
    border-color: {pri_bg};
    color: {pri_txt};
    font-weight: 600;
}}
QPushButton#primary:hover   {{ background-color: {pri_hov}; }}
QPushButton#primary:pressed {{ background-color: {btn_pre}; }}
QPushButton#ghost {{
    background: transparent;
    border: 1px solid {ghost_brd};
    color: {muted2};
    font-size: 9pt;
}}
QPushButton#ghost:hover {{ color: {ghost_hov_txt}; border-color: {ghost_hov_brd}; }}
QPushButton#danger {{
    background: transparent;
    border: 1px solid {danger_border};
    color: {danger_txt};
    font-size: 9pt;
}}
QPushButton#danger:hover {{ background-color: {danger_hov}; border-color: {danger_hbrd}; }}

QScrollArea              {{ background: transparent; border: none; }}
QScrollBar:vertical      {{ background: {scr_trk}; width: 5px; border-radius: 2px; }}
QScrollBar::handle:vertical {{ background: {scr_hdl}; border-radius: 2px; min-height: 30px; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: none; }}

QSpinBox {{
    background-color: {inp_bg};
    border: 1px solid {border};
    border-radius: 4px;
    padding: 5px 10px;
    color: {text};
    min-height: 26px;
}}
QSpinBox::up-button, QSpinBox::down-button {{ width: 0; }}

QMenu {{
    background-color: {menu_bg};
    border: 1px solid {border};
    color: {text};
    padding: 4px 0;
}}
QMenu::item          {{ padding: 7px 18px; font-size: 9.5pt; }}
QMenu::item:selected {{ background-color: {menu_sel}; }}
QMenu::item:disabled {{ color: {muted}; }}
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
        self._path.write_text(json.dumps(self.entries, indent=2))

    def clear(self):
        self.entries = []
        self._save()

# ──────────────────────────────────────────────────────────────────────────────
# WORKERS
# ──────────────────────────────────────────────────────────────────────────────

class ModelLoader(QThread):
    loaded = pyqtSignal(object)
    failed = pyqtSignal(str)
    status = pyqtSignal(str)

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
    finished = pyqtSignal(str)
    failed   = pyqtSignal(str)
    _SR = 16000

    def __init__(self, model, audio: np.ndarray, language: str):
        super().__init__()
        self.model    = model
        self.audio    = audio
        self.language = None if language == "auto" else language

    def run(self):
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
                tmp = f.name
            wav.write(tmp, self._SR, (self.audio * 32767).astype(np.int16))
            segs, _ = self.model.transcribe(tmp, language=self.language)
            text = " ".join(s.text for s in segs).strip()
            os.unlink(tmp)
            self.finished.emit(text)
        except Exception as e:
            self.failed.emit(str(e))

# ──────────────────────────────────────────────────────────────────────────────
# TRAY ICON
# ──────────────────────────────────────────────────────────────────────────────

def _make_icon(state: str) -> QIcon:
    """Programmatic mic icon. state: idle | recording | loading"""
    sz  = 64
    px  = QPixmap(sz, sz)
    px.fill(Qt.GlobalColor.transparent)
    p   = QPainter(px)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    mid = sz // 2

    bg = {"idle": QColor(90, 90, 90), "recording": QColor(210, 48, 48), "loading": QColor(55, 55, 55)}[state]
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QBrush(bg))
    p.drawEllipse(1, 1, sz - 2, sz - 2)

    # Mic capsule
    p.setBrush(QBrush(QColor(235, 235, 235)))
    p.drawRoundedRect(mid - 9, 10, 18, 27, 9, 9)

    # Stand
    pen = QPen(QColor(235, 235, 235), 3.5)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawArc(mid - 13, 29, 26, 16, 0, -180 * 16)
    p.drawLine(mid, 45, mid, 53)
    p.drawLine(mid - 8, 53, mid + 8, 53)
    p.end()
    return QIcon(px)

# ──────────────────────────────────────────────────────────────────────────────
# TOGGLE SWITCH
# ──────────────────────────────────────────────────────────────────────────────

class ToggleSwitch(QWidget):
    toggled = pyqtSignal(bool)

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

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h, r = self.width(), self.height(), self.height() / 2
        track = QColor("#E8E8E8") if self._on else QColor("#404040")
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(track))
        path = QPainterPath()
        path.addRoundedRect(0, 0, w, h, r, r)
        p.drawPath(path)
        m  = 2
        tx = w - h + m if self._on else m
        p.setBrush(QBrush(QColor("#161616") if self._on else QColor("#909090")))
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

        self._tick  = QTimer(interval=1000)
        self._tick.timeout.connect(self._on_tick)

        self._anim  = QTimer(interval=48)
        self._anim.timeout.connect(self._on_anim)

        self._hide  = QTimer(singleShot=True)
        self._hide.timeout.connect(self._fade_out)

        self._fade  = QPropertyAnimation(self, b"windowOpacity", duration=380)
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
        self.state = "transcribing"
        self.update()

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

        # Background pill — dark: near-black / light: near-white
        bg = QPainterPath()
        bg.addRoundedRect(0, 0, cW, cH, 11, 11)
        if self.is_dark:
            p.setBrush(QColor(20, 20, 20, 248))
            p.setPen(QPen(QColor(48, 48, 48, 200), 1))
        else:
            p.setBrush(QColor(250, 250, 250, 252))
            p.setPen(QPen(QColor(200, 200, 200, 220), 1))
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
        # Pulse ring
        ring = int(8 + 5 * pulse)
        p.setPen(QPen(QColor(220, 48, 48, int(70 * pulse)), 1.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(cx - ring, cy - ring, ring * 2, ring * 2)
        # Dot
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(220, 48, 48))
        p.drawEllipse(cx - 6, cy - 6, 12, 12)
        # Label
        p.setPen(QColor(30, 30, 30) if not self.is_dark else QColor(232, 232, 232))
        p.setFont(QFont("Segoe UI", 10, QFont.Weight.Medium))
        p.drawText(42, 0, 140, H, Qt.AlignmentFlag.AlignVCenter, "Recording")
        # Timer
        p.setPen(QColor(150, 150, 150) if not self.is_dark else QColor(110, 110, 110))
        p.setFont(QFont("Consolas", 9))
        m, s = self.elapsed // 60, self.elapsed % 60
        p.drawText(0, 0, W - 12, H, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, f"{m:02d}:{s:02d}")

    def _draw_transcribing(self, p, W, H, cy):
        cx = 22
        t  = time.time()
        for i in range(8):
            angle = i * (2 * math.pi / 8) - (t * 5 % (2 * math.pi))
            sx = cx + 8 * math.cos(angle)
            sy = cy + 8 * math.sin(angle)
            p.setPen(Qt.PenStyle.NoPen)
            if self.is_dark:
                p.setBrush(QColor(180, 180, 180, int(40 + 215 * (i / 8))))
            else:
                p.setBrush(QColor(80, 80, 80, int(40 + 215 * (i / 8))))
            p.drawEllipse(int(sx) - 2, int(sy) - 2, 4, 4)
        p.setPen(QColor(30, 30, 30) if not self.is_dark else QColor(200, 200, 200))
        p.setFont(QFont("Segoe UI", 10, QFont.Weight.Medium))
        p.drawText(42, 0, W - 50, H, Qt.AlignmentFlag.AlignVCenter, "Transcribing…")
        QTimer.singleShot(48, self.update)

    def _draw_done(self, p, W, H, cy):
        cx = 22
        p.setPen(Qt.PenStyle.NoPen)
        # Checkmark circle — inverts with theme
        circle_fill  = QColor(30, 30, 30)   if not self.is_dark else QColor(232, 232, 232)
        check_stroke = QColor(250, 250, 250) if not self.is_dark else QColor(20, 20, 20)
        p.setBrush(circle_fill)
        p.drawEllipse(cx - 8, cy - 8, 16, 16)
        pen = QPen(check_stroke, 2.5)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(cx - 4, cy + 1, cx - 1, cy + 4)
        p.drawLine(cx - 1, cy + 4, cx + 5, cy - 3)
        p.setPen(QColor(90, 90, 90) if not self.is_dark else QColor(170, 170, 170))
        p.setFont(QFont("Segoe UI", 9))
        p.drawText(42, 0, W - 50, H, Qt.AlignmentFlag.AlignVCenter, self.preview)

    def _draw_error(self, p, W, H, cy):
        cx = 22
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(180, 40, 40))
        p.drawEllipse(cx - 8, cy - 8, 16, 16)
        p.setPen(QColor(232, 232, 232))
        p.setFont(QFont("Segoe UI", 11, QFont.Weight.Bold))
        p.drawText(cx - 3, cy + 5, "!")
        p.setPen(QColor(90, 90, 90) if not self.is_dark else QColor(170, 170, 170))
        p.setFont(QFont("Segoe UI", 9))
        p.drawText(42, 0, W - 50, H, Qt.AlignmentFlag.AlignVCenter, self.preview)

# ──────────────────────────────────────────────────────────────────────────────
# HISTORY WINDOW
# ──────────────────────────────────────────────────────────────────────────────

class HistoryWindow(QWidget):
    def __init__(self, history: History):
        super().__init__()
        self.history = history
        self.setWindowTitle("Whisper — History")
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.WindowCloseButtonHint)
        self.setMinimumSize(480, 420)
        self.resize(480, 580)
        self._build()

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 16)
        root.setSpacing(14)

        # Header row
        hdr = QHBoxLayout()
        title = QLabel("History")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Light))
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
        lay.setContentsMargins(14, 10, 14, 12)
        lay.setSpacing(7)

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

    def showEvent(self, e):
        self._refresh()
        super().showEvent(e)

# ──────────────────────────────────────────────────────────────────────────────
# SETTINGS DIALOG
# ──────────────────────────────────────────────────────────────────────────────

class SettingsDialog(QDialog):
    saved = pyqtSignal(object)   # emits Config

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

    def __init__(self, cfg: Config, history: History, parent=None):
        super().__init__(parent)
        self.cfg     = cfg
        self.history = history
        self.setWindowTitle("Settings")
        self.setWindowFlags(Qt.WindowType.Dialog | Qt.WindowType.WindowCloseButtonHint)
        self.setFixedWidth(500)
        self._build()

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
        fl.setContentsMargins(14, 12, 14, 14)
        fl.setSpacing(11)
        fl.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        fl.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)
        fl.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        return fl

    def _build(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(20, 20, 20, 20)
        root.setSpacing(10)

        title = QLabel("Settings")
        title.setFont(QFont("Segoe UI", 16, QFont.Weight.Light))
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
        hlay.setContentsMargins(14, 12, 14, 12)
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
    _toggle_sig        = pyqtSignal()
    _rec_started_sig   = pyqtSignal()
    _audio_ready_sig   = pyqtSignal(object)   # numpy array

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
        self._apply_theme()
        self._theme_timer = QTimer(interval=5000)
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
        parts = self.cfg.hotkey.split("+")
        last, mods = parts[-1], parts[:-1]
        keyboard.on_press_key(last, lambda _: self._toggle_sig.emit() if all(keyboard.is_pressed(k) for k in mods) else None)

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
            time.sleep(0.08)          # let the hotkey release be processed first
            _type_text_direct(text)   # direct SendInput — clipboard untouched
        self.overlay.show_done(text)
        if self._hist_win:
            self._hist_win.refresh()

    # ── Windows ──────────────────────────────────────────────────────────────

    def _open_history(self):
        if self._hist_win is None:
            self._hist_win = HistoryWindow(self.history)
            self._hist_win.setStyleSheet(QApplication.instance().styleSheet())
        self._hist_win.show(); self._hist_win.raise_(); self._hist_win.activateWindow()

    def _open_settings(self):
        dlg = SettingsDialog(self.cfg, self.history)
        dlg.setStyleSheet(QApplication.instance().styleSheet())
        dlg.saved.connect(self._on_settings_saved)
        dlg.exec()

    def _on_settings_saved(self, new_cfg: Config):
        old = self.cfg
        self.cfg = new_cfg
        self.overlay.position = new_cfg.overlay_position
        self.history.limit    = new_cfg.history_limit

        self._apply_theme()

        if new_cfg.hotkey != old.hotkey:
            keyboard.unhook_all()
            if self.model: self._register_hotkey()

        model_changed = (
            new_cfg.model        != old.model or
            new_cfg.device       != old.device or
            new_cfg.compute_type != old.compute_type or
            new_cfg.cuda_cublas_path != old.cuda_cublas_path or
            new_cfg.cuda_cudnn_path  != old.cuda_cudnn_path
        )
        if model_changed:
            self.model = None
            keyboard.unhook_all()
            self.tray.setIcon(_make_icon("loading"))
            self.tray.setToolTip("Whisper Hotkey — reloading…")
            self._start_loader()

    def _quit(self):
        keyboard.unhook_all()
        QApplication.quit()

# ──────────────────────────────────────────────────────────────────────────────
# ENTRY POINT
# ──────────────────────────────────────────────────────────────────────────────

def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName("Whisper Hotkey")
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