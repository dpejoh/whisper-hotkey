# whisper-hotkey

A small script that lets you dictate text anywhere on your computer. Press a hotkey, talk, press it again, and it types what you said into whatever field you have open.

That's it. No cloud, no API key, no subscription. It runs Whisper locally on your machine.

---

## Why this exists

I got tired of cloud speech-to-text tools that require an account or send your audio somewhere. This just runs the model on your own hardware and types the result directly. Works in any app, any text field.

---

## What you need

- Windows (uses Win32 for text input)
- Python 3.11
- A microphone
- A GPU helps a lot, but it works on CPU too

---

## Setup

Install the dependencies:

```
pip install faster-whisper sounddevice scipy keyboard pyperclip pyautogui PySide6
```

If you have an NVIDIA GPU and want faster transcription:

```
pip install nvidia-cublas-cu12 nvidia-cudnn-cu12
```

Then run the script:

```
python whisper_hotkey.py
```

A small icon shows up in your system tray. Right-click it to open Settings and configure your hotkey, model, and device.

---

## First time settings

Open Settings from the tray and set these:

- **Model** - use `turbo` if you have a GPU, `small` or `base` if you're on CPU
- **Device** - `cuda` for GPU, `cpu` otherwise
- **Compute type** - `float16` for GPU, `int8` for CPU
- **Hotkey** - whatever you want, default is `ctrl+shift+space`
- **Auto-paste** - leave this on, it's what types the text for you

If you installed the CUDA packages, also paste the paths to your `cublas\bin` and `cudnn\bin` folders in the CUDA settings section. They look something like:

```
C:\Users\you\AppData\Local\Programs\Python\Python311\Lib\site-packages\nvidia\cublas\bin
C:\Users\you\AppData\Local\Programs\Python\Python311\Lib\site-packages\nvidia\cudnn\bin
```

---

## How to use it

1. Click into any text field
2. Press your hotkey
3. Talk
4. Press the hotkey again
5. It transcribes and types the result

A small overlay appears in the corner of your screen to show when it's recording or transcribing.

---

## Running on startup

Press `Win+R`, type `shell:startup`, and drop a shortcut to the script or exe in that folder.

---

## Troubleshooting

**Hotkey not working** - try running as administrator, the keyboard hook sometimes needs it.

**CUDA errors** - double check your DLL paths in Settings, or just switch to CPU.

**Nothing gets typed** - make sure you click the target field before pressing the hotkey so it keeps focus.

**Slow on CPU** - use the `small` or `base` model with `int8` compute type.

---

## Models

Whisper has several model sizes. Bigger models are more accurate but slower.

| Model  | Speed | Accuracy |
|--------|-------|----------|
| tiny   | fastest | lowest |
| base   | fast    | decent |
| small  | good    | good |
| medium | slow    | better |
| turbo  | fast    | very good |
| large  | slowest | best |

For most people, `turbo` on GPU or `small` on CPU is the right pick.

---

## License

MIT