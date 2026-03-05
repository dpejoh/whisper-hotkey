# Whisper Hotkey

A system tray app that lets you dictate text into any field by pressing a hotkey, it records your voice, transcribes it with Whisper AI, and types the result instantly.

## Preview

![Preview](preview.gif)

---
## Whisper Hotkey — Windows Setup Guide

### 1 · Install Python 3.11

Download the **Windows installer (64-bit)** from https://www.python.org/downloads/release/python-3119/

During installation **check both boxes**:
- ✅ Add Python to PATH
- ✅ Install for all users  (optional but recommended)

Verify in a new terminal:
```
python --version
```
Expected: `Python 3.11.x`

---

### 2 · Install required packages

Open **Command Prompt** (or PowerShell) and run:

```bat
pip install faster-whisper sounddevice scipy keyboard pyperclip pyautogui PyQt6
```

---

### 3 · Choose your device: GPU (CUDA) or CPU

#### Option A — NVIDIA GPU (faster, recommended if you have one)

Install the CUDA-specific packages:
```bat
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

Find the DLL folders after install (replace `khaled` with **your Windows username**):
```
C:\Users\<YOUR_USER>\AppData\Local\Programs\Python\Python311\Lib\site-packages\nvidia\cublas\bin
C:\Users\<YOUR_USER>\AppData\Local\Programs\Python\Python311\Lib\site-packages\nvidia\cudnn\bin
```

You will paste these two paths into **Settings → CUDA DLL Paths** the first time you run the app.

> **Note:** If Python is installed system-wide (not per-user), the path starts with  
> `C:\Program Files\Python311\Lib\site-packages\nvidia\...`

#### Option B — CPU only (no GPU required)

No extra packages needed.  
In **Settings** set:
- Device → `cpu`
- Compute type → `int8`
- Model → `small` or `base` (large models are slow on CPU)

---

### 4 · Run the script

```bat
python whisper_hotkey_gui.py
```

A microphone icon will appear in the **system tray** (bottom-right, near the clock).  
The first launch downloads the Whisper model — this can take a minute.

---

### 5 · First-time settings

Right-click the tray icon → **Settings** and configure:

| Setting | Recommended |
|---|---|
| Whisper model | `turbo` (GPU) or `small` (CPU) |
| Device | `cuda` or `cpu` |
| Compute type | `float16` (GPU) or `int8` (CPU) |
| Hotkey | e.g. `ctrl+shift+space` |
| Auto-paste | ON |
| CUDA cuBLAS path | *(GPU only — paste path from step 3)* |
| CUDA cuDNN path | *(GPU only — paste path from step 3)* |

Click **Save**. The model reloads automatically.

---

### 6 · How to use

1. Click into any text field (browser, Word, Notepad, chat app, etc.)
2. Press your hotkey → the floating overlay shows **Recording**
3. Speak
4. Press the hotkey again → overlay shows **Transcribing…** then the result
5. The transcribed text is typed directly into the focused field — **no clipboard is used**

---

### 7 · Run on startup (optional)

Press `Win + R`, type `shell:startup`, press Enter.  
Create a shortcut to `whisper_hotkey_gui.py` (or a `.bat` file) in that folder:

```bat
@echo off
pythonw "C:\path\to\whisper_hotkey_gui.py"
```

(`pythonw` runs without a console window.)

---

### 8 · Troubleshooting

| Problem | Fix |
|---|---|
| `ModuleNotFoundError` | Run the `pip install` command from step 2 again |
| CUDA error on startup | Check the DLL paths in Settings, or switch to CPU |
| Hotkey not triggering | Try running the script **as Administrator** (keyboard hook may need elevation) |
| No sound captured | Check Windows microphone privacy settings and default input device |
| Text not inserted | Click the target text field *before* pressing the hotkey so it keeps focus |
| Slow on CPU | Use `small` or `base` model with `int8` compute type |