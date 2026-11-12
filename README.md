# ScreenBridge
Share your Windows screen to your Android phone over Wi-Fi / LAN.

- **Windows app** (`windows/`): Python + Tkinter, MJPEG screen server on port 8765, permission prompts, PIN + per-device approval, optional remote mouse/keyboard.
- **Android app** (`android/`): Kotlin viewer with tools: remote control, typing, Enter/Esc/Win keys, screenshot to gallery, quality presets, rotate, fullscreen, FPS counter.

## Use
1. Install `ScreenBridge-Setup.exe` on the PC, open it, accept the permissions, press **Start sharing**.
2. Install the APK on the phone (same Wi-Fi), enter the IP + PIN shown on the PC, tap **Connect**.
3. Approve the phone on the PC popup.

## Build
Push to GitHub: the workflow in `.github/workflows/build.yml` builds the Windows EXE + installer and the Android APK (see the Actions tab → Artifacts). Tag `v1.0.0` to publish a release.

Local: `pip install -r windows/requirements.txt && python windows/main.py`; Android: open `android/` in Android Studio.

## Phone → Windows
In the Android app tap **Share this phone to Windows** (accept the screen-capture prompt), then in the Windows app click **View phone screen on this PC** and enter the phone IP + PIN. Viewing only (no control of the phone).
