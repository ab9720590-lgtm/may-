"""
Arabic TTS - simple app for Sawtak Arabi text-to-speech (Kivy / Pydroid 3).

- Settings screen for the API key (show/hide, test connection)
- Voice list cached, last chosen voice remembered
- Long texts split automatically and merged into one WAV
- Open .txt / .srt / .vtt files (timings stripped)
- Paste, clear with confirmation, draft auto-saved
- Progress bar, cancel, retries, friendly errors
- Result saved to Music/ArabicTTS and playable in-app
- Finds an Arabic font automatically (or downloads one) -> no more empty squares
"""

import json
import math
import os
import re
import shutil
import threading
import time
import wave

import requests

from kivy.app import App
from kivy.clock import Clock
from kivy.core.audio import SoundLoader
from kivy.core.clipboard import Clipboard
from kivy.core.text import Label as CoreLabel
from kivy.core.window import Window
from kivy.metrics import dp, sp
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.filechooser import FileChooserListView
from kivy.uix.label import Label
from kivy.uix.popup import Popup
from kivy.uix.progressbar import ProgressBar
from kivy.uix.spinner import Spinner, SpinnerOption
from kivy.uix.textinput import TextInput
from kivy.utils import escape_markup, platform

try:
    import arabic_reshaper
    from bidi.algorithm import get_display
except Exception:
    arabic_reshaper = None
    get_display = None

# ==========================================
# Settings
# ==========================================

API_BASE = "https://api.sawtakarabi.ai/v1"
DEFAULT_VOICE = "277bf65f-a8f6-51ac-ae05-7ca11ba53c24"

# Built-in test key (can be changed from the Settings screen).
DEFAULT_API_KEY = "sk_ar_live_CXoQamio_D1VjudplDw3tRyS6U-9TfB1ayLb_GYlSpc"

MAX_CHUNK = 3000        # characters per request (service limit is 4000)
MAX_TOTAL = 50000       # maximum characters per conversion
MAX_FILE_BYTES = 2 * 1024 * 1024

# Fonts downloaded automatically only if no Arabic font is found on the phone.
FONT_URLS = [
    "https://github.com/google/fonts/raw/main/ofl/amiri/Amiri-Regular.ttf",
    "https://github.com/google/fonts/raw/main/ofl/notonaskharabic/NotoNaskhArabic%5Bwght%5D.ttf",
]

FONT_CANDIDATES = [
    "/system/fonts/NotoNaskhArabic-Regular.ttf",
    "/system/fonts/NotoNaskhArabicUI-Regular.ttf",
    "/system/fonts/NotoSansArabic-Regular.ttf",
    "/system/fonts/NotoSansArabicUI-Regular.ttf",
    "/system/fonts/DroidSansArabic.ttf",
    "C:/Windows/Fonts/arial.ttf",
    "/usr/share/fonts/truetype/noto/NotoNaskhArabic-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
]

BG = (0.07, 0.08, 0.11, 1)
NEUTRAL = (0.20, 0.22, 0.30, 1)
GREEN = (0.15, 0.60, 0.35, 1)
RED = (0.75, 0.25, 0.25, 1)
BLUE = (0.20, 0.45, 0.80, 1)

# ==========================================
# Arabic font + shaping
# ==========================================

FONT = "Roboto"           # replaced by init_font()
ARABIC_SHAPING = "auto"   # "auto" | "on" | "off"
_NEEDS_RESHAPE = None


def _glyph_signature(font_path, ch):
    lab = CoreLabel(text=ch, font_name=font_path, font_size=40)
    lab.refresh()
    tex = lab.texture
    if tex is None:
        return None
    return (tuple(tex.size), bytes(tex.pixels))


def font_supports_arabic(path):
    """A font without Arabic draws the same 'empty square' for every letter,
    so three different letters giving identical pictures means no support."""
    try:
        sigs = [_glyph_signature(path, c) for c in "\u0628\u0645\u0643"]
        return None not in sigs and len(set(sigs)) == 3
    except Exception:
        return False


def _font_paths(extra_dirs):
    seen = set()

    def emit(p):
        if p not in seen:
            seen.add(p)
            return True
        return False

    for d in extra_dirs:
        for n in ("arabic.ttf", "arabic.otf"):
            p = os.path.join(d, n)
            if emit(p):
                yield p
    for p in FONT_CANDIDATES:
        if emit(p):
            yield p
    for d in ("/system/fonts", "/system/font", "/product/fonts", "/system_ext/fonts"):
        try:
            names = sorted(os.listdir(d))
        except Exception:
            continue
        files = [n for n in names if n.lower().endswith((".ttf", ".otf"))]
        pref = [n for n in files if any(k in n.lower() for k in
                ("arab", "naskh", "kufi", "amiri", "cairo", "tajawal"))]
        rest = [n for n in files if n not in pref][:60]
        for n in pref + rest:
            p = os.path.join(d, n)
            if emit(p):
                yield p


def init_font(extra_dirs=()):
    """Pick the first font that really contains Arabic letters."""
    global FONT, _NEEDS_RESHAPE
    for path in _font_paths(extra_dirs):
        if os.path.exists(path) and font_supports_arabic(path):
            FONT = path
            _NEEDS_RESHAPE = None
            return True
    FONT = "Roboto"
    _NEEDS_RESHAPE = None
    return False


def needs_reshape():
    """Some Kivy builds already join Arabic letters; others do not.
    Reshaping twice breaks the text, so detect which case we are in."""
    global _NEEDS_RESHAPE
    if _NEEDS_RESHAPE is None:
        if ARABIC_SHAPING == "on":
            _NEEDS_RESHAPE = True
        elif ARABIC_SHAPING == "off":
            _NEEDS_RESHAPE = False
        else:
            try:
                lab = CoreLabel(font_name=FONT, font_size=40)
                single = lab.get_extents("\u0628")[0]
                triple = lab.get_extents("\u0628\u0628\u0628")[0]
                _NEEDS_RESHAPE = not (triple < single * 2.7)
            except Exception:
                _NEEDS_RESHAPE = True
    return _NEEDS_RESHAPE


def ar(text):
    if not arabic_reshaper or not get_display or not needs_reshape():
        return str(text)
    return "\n".join(
        get_display(arabic_reshaper.reshape(line)) for line in str(text).split("\n")
    )


ARABIC_RANGES = (
    (0x0600, 0x06FF), (0x0750, 0x077F), (0x08A0, 0x08FF),
    (0xFB50, 0xFDFF), (0xFE70, 0xFEFF),
)


def _is_arabic(ch):
    o = ord(ch)
    return any(a <= o <= b for a, b in ARABIC_RANGES)


def has_arabic(text):
    return any(_is_arabic(c) for c in str(text))


def short(text, limit=26):
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def rich(text):
    """Kivy markup: Arabic letters use the Arabic font, the rest uses Roboto."""
    shaped = ar(text)
    classes = []
    for ch in shaped:
        if _is_arabic(ch):
            classes.append("A")
        elif ch.isalnum():
            classes.append("L")
        else:
            classes.append("N")
    n = len(classes)
    resolved = list(classes)
    for i, c in enumerate(classes):
        if c != "N":
            continue
        prev = next((classes[j] for j in range(i - 1, -1, -1) if classes[j] != "N"), None)
        nxt = next((classes[j] for j in range(i + 1, n) if classes[j] != "N"), None)
        resolved[i] = "A" if (prev == "A" and nxt == "A") else "L"

    pieces, run, run_cls = [], "", None
    for ch, cls in zip(shaped, resolved):
        if cls != run_cls and run:
            font = FONT if run_cls == "A" else "Roboto"
            pieces.append(f"[font={font}]{escape_markup(run)}[/font]")
            run = ""
        run_cls = cls
        run += ch
    if run:
        font = FONT if run_cls == "A" else "Roboto"
        pieces.append(f"[font={font}]{escape_markup(run)}[/font]")
    return "".join(pieces)


# ==========================================
# Text helpers
# ==========================================

def clean_srt(text):
    """Remove subtitle numbers, timings, tags (SRT / VTT); keep spoken text."""
    text = text.replace("\ufeff", "").replace("\r\n", "\n").replace("\r", "\n")
    blocks = re.split(r"\n\s*\n", text)
    out = []
    for block in blocks:
        raw_lines = [l.strip() for l in block.split("\n")]
        if raw_lines and raw_lines[0].upper().startswith(("WEBVTT", "NOTE", "STYLE")):
            continue
        # drop everything up to and including the timing line (index / cue id)
        for idx, l in enumerate(raw_lines):
            if "-->" in l:
                raw_lines = raw_lines[idx + 1:]
                break
        lines = []
        for s in raw_lines:
            if not s:
                continue
            s = re.sub(r"<[^>]+>", "", s)
            s = re.sub(r"\{[^}]*\}", "", s)
            s = s.strip()
            if s:
                lines.append(s)
        if lines:
            out.append(" ".join(lines))
    return "\n".join(out)


def prepare_text(raw):
    if "-->" in raw:
        raw = clean_srt(raw)
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")
    raw = re.sub(r"[ \t\u00a0]+", " ", raw)
    raw = re.sub(r"\n\s*\n+", "\n", raw)
    return raw.strip()


def _hard_split(sentence, limit):
    parts, cur = [], ""
    for word in sentence.split():
        while len(word) > limit:
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(word[:limit])
            word = word[limit:]
        if cur and len(cur) + 1 + len(word) > limit:
            parts.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}".strip()
    if cur:
        parts.append(cur)
    return parts


def split_text(text, limit=MAX_CHUNK):
    """Split into chunks <= limit, preferring paragraph/sentence boundaries."""
    pieces = []
    for para in text.split("\n"):
        para = para.strip()
        if not para:
            continue
        for sentence in re.split(r"(?<=[.!?\u061F\u2026])\s+", para):
            sentence = sentence.strip()
            if not sentence:
                continue
            if len(sentence) <= limit:
                pieces.append(sentence)
            else:
                pieces.extend(_hard_split(sentence, limit))

    chunks, cur = [], ""
    for piece in pieces:
        if cur and len(cur) + 1 + len(piece) > limit:
            chunks.append(cur)
            cur = piece
        else:
            cur = f"{cur} {piece}" if cur else piece
    if cur:
        chunks.append(cur)
    return chunks


def read_text_file(path):
    with open(path, "rb") as f:
        data = f.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise RuntimeError("File is too large (max 2 MB).")
    encodings = ["utf-8-sig"]
    if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
        encodings.insert(0, "utf-16")
    encodings += ["cp1256", "latin-1"]
    for enc in encodings:
        try:
            text = data.decode(enc)
            break
        except Exception:
            continue
    else:
        raise RuntimeError("Could not read the file encoding.")
    if path.lower().endswith((".srt", ".vtt")) or "-->" in text:
        text = clean_srt(text)
    return text.strip()


def merge_wavs(paths, out_path):
    """Join several WAV files (same format) into one."""
    if len(paths) == 1:
        shutil.copyfile(paths[0], out_path)
        return
    try:
        with wave.open(paths[0], "rb") as first:
            params = first.getparams()
        with wave.open(out_path, "wb") as out:
            out.setparams(params)
            for p in paths:
                with wave.open(p, "rb") as w:
                    if (w.getnchannels(), w.getsampwidth(), w.getframerate()) != (
                        params.nchannels, params.sampwidth, params.framerate,
                    ):
                        raise RuntimeError("Audio parts have different formats.")
                    while True:
                        frames = w.readframes(65536)
                        if not frames:
                            break
                        out.writeframes(frames)
    except wave.Error as exc:
        raise RuntimeError(f"Could not merge audio parts: {exc}")


# ==========================================
# API
# ==========================================

class ApiError(Exception):
    pass


class Cancelled(Exception):
    pass


def _error_detail(response):
    try:
        data = response.json()
    except ValueError:
        return response.text[:200].strip()
    if isinstance(data, dict):
        err = data.get("error", data.get("detail", data.get("message")))
        if isinstance(err, dict):
            err = err.get("message") or str(err)
        if err:
            return str(err)[:200]
    return str(data)[:200]


def friendly_error(response):
    code = response.status_code
    detail = _error_detail(response)
    if code in (401, 403):
        return ApiError("Your API key was rejected. Check it in Settings.")
    if code == 402:
        return ApiError("Not enough credit in your account.")
    if code == 429:
        return ApiError("Too many requests or quota reached. Try again in a minute.")
    if code >= 500:
        return ApiError("The service is having problems. Try again later.")
    return ApiError(f"Request failed ({code}): {detail}")


def api_request(method, path, cancel=None, retries=3, **kwargs):
    last = ApiError("Request failed.")
    for attempt in range(retries):
        if cancel is not None and cancel.is_set():
            raise Cancelled()
        try:
            response = requests.request(method, f"{API_BASE}{path}", **kwargs)
        except requests.exceptions.Timeout:
            last = ApiError("The service took too long to respond.")
        except requests.exceptions.ConnectionError:
            last = ApiError("No internet connection.")
        except requests.exceptions.RequestException as exc:
            raise ApiError(f"Network error: {str(exc)[:150]}")
        else:
            if response.ok:
                return response
            error = friendly_error(response)
            if response.status_code in (429, 500, 502, 503, 504):
                last = error
            else:
                raise error
        if attempt < retries - 1:
            # wait in small steps so Cancel reacts quickly
            for _ in range(2 * (attempt + 1) * 4):
                if cancel is not None and cancel.is_set():
                    raise Cancelled()
                time.sleep(0.25)
    raise last


def get_voices(api_key):
    response = api_request(
        "GET", "/voices", retries=2,
        headers={"Authorization": f"Bearer {api_key}"}, timeout=(15, 30),
    )
    try:
        data = response.json()
    except ValueError:
        raise ApiError("Unexpected answer from the voices service.")
    if isinstance(data, dict):
        data = data.get("data", data.get("voices", []))
    return data if isinstance(data, list) else []


def generate_audio(api_key, text, voice_id, cancel):
    for _ in range(2):  # one extra try if the service returns an empty body
        response = api_request(
            "POST", "/audio/speech", cancel=cancel,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={"input": text, "voice": voice_id, "response_format": "wav"},
            timeout=(15, 180),
        )
        content = response.content
        if not content:
            continue
        if content[:4] == b"RIFF":
            return content
        ctype = response.headers.get("Content-Type", "")
        if "json" in ctype or content[:1] in (b"{", b"["):
            raise ApiError(f"Unexpected answer from the service: {_error_detail(response)}")
        raise ApiError("The service did not return a WAV file.")
    raise ApiError("The service returned an empty audio file.")


# ==========================================
# UI helpers
# ==========================================

class ArOption(SpinnerOption):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.markup = True
        self.font_size = sp(16)
        self.size_hint_y = None
        self.height = dp(46)
        self.background_normal = ""
        self.background_color = NEUTRAL
        self.halign = "center"
        self.valign = "middle"
        self.bind(size=lambda w, s: setattr(w, "text_size", (s[0] - dp(12), None)))


def make_button(text, callback, color=NEUTRAL, height=48):
    btn = Button(
        text=text,
        size_hint_y=None,
        height=dp(height),
        font_size=sp(16),
        background_normal="",
        background_down="",
        background_disabled_normal="",
        background_color=color,
        disabled_color=(1, 1, 1, 0.35),
    )
    btn.bind(on_release=callback)
    return btn


def make_label(text, height=28, size=14, color=(1, 1, 1, 1), halign="left"):
    lbl = Label(
        text=text,
        size_hint_y=None,
        height=dp(height),
        font_size=sp(size),
        color=color,
        halign=halign,
        valign="middle",
    )
    lbl.bind(size=lambda w, s: setattr(w, "text_size", (s[0], s[1])))
    return lbl


def set_label_text(lbl, text):
    """Use Arabic-aware markup only when the text contains Arabic."""
    text = str(text)
    lbl.markup = True
    lbl.text = rich(text) if has_arabic(text) else escape_markup(text)


# ==========================================
# App
# ==========================================

class TTSApp(App):
    title = "Arabic TTS"

    # ---------- build ----------

    def build(self):
        Window.clearcolor = BG
        Window.softinput_mode = "below_target"

        init_font(self._font_dirs())

        self.api_key_value = DEFAULT_API_KEY
        self.voices = {}
        self._name_to_display = {}
        self._display_to_name = {}
        self.current_voice_id = DEFAULT_VOICE
        self.current_voice_name = ""
        self._silent = False
        self.busy = False
        self.cancel_event = threading.Event()
        self.last_audio_path = None
        self.sound = None
        self._draft_event = None

        root = BoxLayout(orientation="vertical", padding=dp(12), spacing=dp(8))

        top = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        top.add_widget(make_label("Arabic Text to Speech", height=44, size=20))
        self.settings_btn = make_button("Settings", self.show_settings, height=44)
        self.settings_btn.size_hint_x = None
        self.settings_btn.width = dp(100)
        top.add_widget(self.settings_btn)
        root.add_widget(top)

        voice_row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        lbl = make_label("Voice:", height=48)
        lbl.size_hint_x = None
        lbl.width = dp(60)
        voice_row.add_widget(lbl)
        self.voice_spinner = Spinner(
            text="Default voice",
            values=[],
            markup=True,
            font_size=sp(16),
            option_cls=ArOption,
            background_normal="",
            background_color=NEUTRAL,
        )
        try:   # keep the dropdown list from covering the whole screen
            self.voice_spinner._dropdown.max_height = Window.height * 0.5
        except Exception:
            pass
        self.voice_spinner.bind(text=self.on_voice_selected)
        voice_row.add_widget(self.voice_spinner)
        root.add_widget(voice_row)

        root.add_widget(make_label("Your text (Arabic):", height=24))
        self.text_input = TextInput(
            font_name=FONT,
            font_size=sp(18),
            background_color=(0.12, 0.13, 0.18, 1),
            foreground_color=(1, 1, 1, 1),
            cursor_color=(1, 1, 1, 1),
        )
        root.add_widget(self.text_input)

        tools = BoxLayout(size_hint_y=None, height=dp(44), spacing=dp(8))
        self.paste_btn = make_button("Paste", self.paste_text, height=44)
        self.open_btn = make_button("Open file", self.open_file_dialog, height=44)
        self.clear_btn = make_button("Clear", self.clear_text, height=44)
        for b in (self.paste_btn, self.open_btn, self.clear_btn):
            tools.add_widget(b)
        root.add_widget(tools)

        self.counter = make_label("", height=22, size=13, color=(0.7, 0.7, 0.75, 1))
        root.add_widget(self.counter)

        self.progress = ProgressBar(max=100, value=0, size_hint_y=None, height=dp(8))
        root.add_widget(self.progress)
        self.status = make_label("Ready.", height=64, size=14)
        self.status.valign = "top"
        root.add_widget(self.status)

        self.convert_btn = make_button("Convert to speech", self.on_convert, GREEN, 56)
        root.add_widget(self.convert_btn)
        self.play_btn = make_button("Play result", self.toggle_play, BLUE, 48)
        self.play_btn.disabled = True
        root.add_widget(self.play_btn)

        self.load_settings()
        self.text_input.bind(text=self.on_text_changed)
        self.update_counter()
        return root

    def on_start(self):
        if platform == "android":
            try:
                from android.permissions import Permission, request_permissions
                request_permissions([
                    Permission.WRITE_EXTERNAL_STORAGE,
                    Permission.READ_EXTERNAL_STORAGE,
                ])
            except Exception:
                pass
        if FONT == "Roboto":
            self.download_font()
        if not self.api_key_value:
            Clock.schedule_once(lambda dt: self.show_settings(first_run=True), 0.5)
        else:
            self.fetch_voices(self.api_key_value, silent=bool(self.voices))

    def on_pause(self):
        self.save_draft()
        self.save_config()
        return True

    def on_stop(self):
        self.save_draft()
        self.save_config()
        if self.sound:
            self.sound.stop()

    # ---------- font ----------

    def _font_dirs(self):
        dirs = [self.user_data_dir]
        try:
            dirs.append(os.path.dirname(os.path.abspath(__file__)))
        except Exception:
            pass
        dirs += ["/storage/emulated/0", "/storage/emulated/0/Download",
                 "/storage/emulated/0/Fonts"]
        return dirs

    def download_font(self):
        def work():
            self.ui(self.set_status, "No Arabic font found. Downloading one (once)...")
            os.makedirs(self.user_data_dir, exist_ok=True)
            dest = os.path.join(self.user_data_dir, "arabic.ttf")
            for url in FONT_URLS:
                try:
                    r = requests.get(url, timeout=(15, 60))
                    if r.ok and len(r.content) > 50000:
                        with open(dest, "wb") as f:
                            f.write(r.content)
                        self.ui(self.on_font_downloaded, dest)
                        return
                except Exception:
                    continue
            self.ui(self.set_status,
                    "Arabic font missing. Put a file named arabic.ttf in your phone's "
                    "main storage (or Download) folder and restart the app.",
                    (1, 0.8, 0.4, 1))

        threading.Thread(target=work, daemon=True).start()

    def on_font_downloaded(self, path):
        if init_font([os.path.dirname(path)]):
            self.text_input.font_name = FONT
            if self.voices:
                self.apply_voices(self.voices, announce=False)
            self.set_status("Arabic font ready.", (0.5, 1, 0.6, 1))
        else:
            self.set_status("The downloaded font could not be used. Restart the app.",
                            (1, 0.8, 0.4, 1))

    # ---------- small utilities ----------

    def ui(self, func, *args):
        Clock.schedule_once(lambda dt: func(*args), 0)

    def set_status(self, text, color=(1, 1, 1, 1)):
        set_label_text(self.status, str(text)[:280])
        self.status.color = color

    def set_progress(self, value, text=None):
        self.progress.value = value
        if text:
            self.set_status(text)

    def popup(self, title, content, height=0.4):
        p = Popup(title=title, content=content, size_hint=(0.92, height))
        p.open()
        return p

    def confirm(self, title, message, on_yes):
        box = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(10))
        box.add_widget(make_label(message, height=60, size=15))
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        p = self.popup(title, box, 0.3)

        def yes(*_):
            p.dismiss()
            on_yes()

        row.add_widget(make_button("Yes", yes, RED))
        row.add_widget(make_button("No", lambda *_: p.dismiss()))
        box.add_widget(row)

    # ---------- config & draft ----------
    # NOTE: never name a method load_config / open_settings: Kivy's App already
    # has methods with those names and calls them itself.

    @property
    def config_path(self):
        return os.path.join(self.user_data_dir, "config.json")

    @property
    def draft_path(self):
        return os.path.join(self.user_data_dir, "draft.txt")

    def load_settings(self):
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            self.api_key_value = cfg.get("api_key") or DEFAULT_API_KEY
            self.current_voice_id = cfg.get("voice_id", DEFAULT_VOICE)
            self.current_voice_name = cfg.get("voice_name", "")
            self.voices = cfg.get("voices", {}) or {}
        except Exception:
            pass
        if self.voices:
            self.apply_voices(self.voices, announce=False)
        try:
            with open(self.draft_path, "r", encoding="utf-8") as f:
                self.text_input.text = f.read()
        except Exception:
            pass

    def save_config(self):
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            with open(self.config_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        # only store a key the user typed; the built-in one
                        # always comes from the code (easy to replace later)
                        "api_key": ("" if self.api_key_value == DEFAULT_API_KEY
                                    else self.api_key_value),
                        "voice_id": self.current_voice_id,
                        "voice_name": self.current_voice_name,
                        "voices": self.voices,
                    },
                    f,
                    ensure_ascii=False,
                )
        except Exception:
            pass

    def save_draft(self, *_):
        try:
            os.makedirs(self.user_data_dir, exist_ok=True)
            with open(self.draft_path, "w", encoding="utf-8") as f:
                f.write(self.text_input.text)
        except Exception:
            pass

    def on_text_changed(self, *_):
        self.update_counter()
        if self._draft_event:
            self._draft_event.cancel()
        self._draft_event = Clock.schedule_once(self.save_draft, 2)

    def update_counter(self, *_):
        n = len(self.text_input.text.strip())
        if n > MAX_TOTAL:
            self.counter.text = f"{n:,} characters - too long (max {MAX_TOTAL:,})"
            self.counter.color = (1, 0.45, 0.45, 1)
            return
        parts = math.ceil(n / MAX_CHUNK) if n else 0
        msg = f"{n:,} characters"
        if parts > 1:
            msg += f"  |  will be converted in about {parts} parts"
        self.counter.text = msg
        self.counter.color = (0.7, 0.7, 0.75, 1)

    # ---------- text tools ----------

    def paste_text(self, *_):
        try:
            clip = Clipboard.paste()
        except Exception:
            clip = ""
        if not clip:
            self.set_status("The clipboard is empty.")
            return
        self.text_input.insert_text(clip)
        self.set_status("Text pasted.")

    def clear_text(self, *_):
        if len(self.text_input.text) > 100:
            self.confirm(
                "Clear text",
                "Delete all the text?",
                lambda: setattr(self.text_input, "text", ""),
            )
        else:
            self.text_input.text = ""

    def open_file_dialog(self, *_):
        start = os.path.expanduser("~")
        if platform == "android":
            try:
                from android.storage import primary_external_storage_path
                start = primary_external_storage_path()
            except Exception:
                start = "/storage/emulated/0"
        if not os.path.isdir(start):
            start = os.getcwd()

        box = BoxLayout(orientation="vertical", spacing=dp(8))
        chooser = FileChooserListView(
            path=start,
            filters=["*.txt", "*.srt", "*.vtt", "*.TXT", "*.SRT", "*.VTT"],
        )
        box.add_widget(chooser)
        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        p = self.popup("Choose a .txt / .srt / .vtt file", box, 0.9)

        def load(*_):
            if not chooser.selection:
                return
            try:
                text = read_text_file(chooser.selection[0])
            except Exception as exc:
                p.dismiss()
                self.set_status(f"Could not open the file: {exc}", (1, 0.5, 0.5, 1))
                return
            p.dismiss()
            self.text_input.text = text
            self.set_status("File loaded.")

        row.add_widget(make_button("Open", load, GREEN))
        row.add_widget(make_button("Cancel", lambda *_: p.dismiss()))
        box.add_widget(row)

    # ---------- settings ----------

    def show_settings(self, *_, first_run=False):
        box = BoxLayout(orientation="vertical", padding=dp(10), spacing=dp(10))
        if first_run:
            box.add_widget(make_label(
                "Welcome! Enter your Sawtak Arabi API key to get started.",
                height=44, size=15))
        box.add_widget(make_label("API key:", height=24))

        row = BoxLayout(size_hint_y=None, height=dp(48), spacing=dp(8))
        key_in = TextInput(
            text=self.api_key_value, multiline=False, password=True,
            write_tab=False, font_size=sp(16),
        )
        row.add_widget(key_in)

        def toggle(btn):
            key_in.password = not key_in.password
            btn.text = "Show" if key_in.password else "Hide"

        show_btn = make_button("Show", toggle, height=48)
        show_btn.size_hint_x = None
        show_btn.width = dp(80)
        row.add_widget(show_btn)
        box.add_widget(row)

        msg = make_label("", height=60, size=14)
        msg.valign = "top"
        box.add_widget(msg)

        def say(text, color=(1, 1, 1, 1)):
            def apply():
                set_label_text(msg, text)
                msg.color = color
            self.ui(apply)

        def connect(*_):
            key = key_in.text.strip().strip("\"'").strip()
            if not key:
                say("Please enter your API key.", (1, 0.5, 0.5, 1))
                return
            self.api_key_value = key
            self.save_config()
            say("Connecting...")
            self.fetch_voices(key, silent=False, say=say)

        def use_default(*_):
            key_in.text = DEFAULT_API_KEY

        box.add_widget(make_button("Save and connect", connect, GREEN))
        box.add_widget(make_button("Use built-in key", use_default, height=44))
        p = self.popup("Settings", box, 0.75)
        box.add_widget(make_button("Close", lambda *_: p.dismiss()))

    def fetch_voices(self, key, silent=True, say=None):
        def work():
            try:
                voices = get_voices(key)
                mapping = {}
                for i, v in enumerate(voices):
                    if not isinstance(v, dict):
                        continue
                    vid = v.get("id") or v.get("voice_id")
                    if not vid:
                        continue
                    name = str(v.get("name") or vid)
                    if name in mapping:
                        name = f"{name} ({i})"
                    mapping[name] = vid
                if not mapping:
                    if say:
                        say("Connected, but no voices were returned.", (1, 0.8, 0.4, 1))
                    elif not silent:
                        self.ui(self.set_status, "No voices were returned.",
                                (1, 0.8, 0.4, 1))
                    return
                self.ui(self.apply_voices, mapping, True)
                if say:
                    say(f"Connected successfully. {len(mapping)} voices available.",
                        (0.5, 1, 0.6, 1))
            except Exception as exc:
                if say:
                    say(str(exc), (1, 0.5, 0.5, 1))
                elif not silent:
                    self.ui(self.set_status, str(exc), (1, 0.5, 0.5, 1))

        threading.Thread(target=work, daemon=True).start()

    # ---------- voices ----------

    def apply_voices(self, mapping, announce=True):
        self.voices = mapping
        self._name_to_display = {}
        self._display_to_name = {}
        for name in mapping:
            display = rich(short(name))
            k = 1
            while display in self._display_to_name:
                k += 1
                display = rich(f"{short(name, 22)} #{k}")
            self._name_to_display[name] = display
            self._display_to_name[display] = name
        self.voice_spinner.values = list(self._display_to_name.keys())

        selected = None
        for name, vid in mapping.items():
            if vid == self.current_voice_id:
                selected = name
                break
        if selected is None:
            selected = next(iter(mapping))
            self.current_voice_id = mapping[selected]
        self.current_voice_name = selected
        self._silent = True
        self.voice_spinner.text = self._name_to_display[selected]
        self._silent = False
        self.save_config()

    def on_voice_selected(self, spinner, text):
        if self._silent:
            return
        name = self._display_to_name.get(text)
        if name and name in self.voices:
            self.current_voice_id = self.voices[name]
            self.current_voice_name = name
            self.save_config()

    # ---------- conversion ----------

    def set_busy(self, busy):
        self.busy = busy
        for w in (self.paste_btn, self.open_btn, self.clear_btn,
                  self.settings_btn, self.voice_spinner):
            w.disabled = busy
        self.text_input.readonly = busy
        if busy:
            self.convert_btn.text = "Cancel"
            self.convert_btn.background_color = RED
            self.play_btn.disabled = True
        else:
            self.convert_btn.text = "Convert to speech"
            self.convert_btn.background_color = GREEN
            self.play_btn.disabled = not self.last_audio_path

    def on_convert(self, *_):
        if self.busy:
            self.cancel_event.set()
            self.set_status("Cancelling after the current part...")
            return

        if not self.api_key_value:
            self.show_settings(first_run=True)
            return

        text = prepare_text(self.text_input.text)
        if not text:
            self.set_status("Type, paste or open some text first.", (1, 0.8, 0.4, 1))
            return
        if len(text) > MAX_TOTAL:
            self.set_status(
                f"The text is too long ({len(text):,}). Maximum is {MAX_TOTAL:,} characters.",
                (1, 0.5, 0.5, 1))
            return

        if self.sound:
            self.sound.stop()
        self.cancel_event.clear()
        self.set_busy(True)
        self.set_progress(0, "Starting...")

        key = self.api_key_value
        voice = self.current_voice_id
        threading.Thread(target=self.convert_worker, args=(key, text, voice),
                         daemon=True).start()

    def convert_worker(self, key, text, voice):
        tmp_dir = os.path.join(self.user_data_dir, "tmp")
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            os.makedirs(tmp_dir, exist_ok=True)

            chunks = split_text(text)
            total = len(chunks)
            parts = []
            for i, chunk in enumerate(chunks, 1):
                if self.cancel_event.is_set():
                    raise Cancelled()
                label = f"Converting part {i} of {total}..." if total > 1 else "Converting..."
                self.ui(self.set_progress, (i - 1) / total * 100, label)
                audio = generate_audio(key, chunk, voice, self.cancel_event)
                part_path = os.path.join(tmp_dir, f"part_{i:03d}.wav")
                with open(part_path, "wb") as f:
                    f.write(audio)
                parts.append(part_path)

            self.ui(self.set_progress, 98, "Saving audio...")
            name = time.strftime("voice_%Y%m%d_%H%M%S.wav")
            work_path = os.path.join(self.user_data_dir, name)
            merge_wavs(parts, work_path)
            saved = self.export_file(work_path, name)
            self.ui(self.on_done, work_path, saved)
        except Cancelled:
            self.ui(self.on_cancelled)
        except (ApiError, RuntimeError) as exc:
            self.ui(self.on_error, str(exc))
        except Exception as exc:
            self.ui(self.on_error, f"Unexpected error: {exc}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    def export_file(self, path, name):
        """Copy the result to a folder the user can reach. Returns path or None."""
        folders = []
        if platform == "android":
            try:
                from android.storage import primary_external_storage_path
                base = primary_external_storage_path()
            except Exception:
                base = "/storage/emulated/0"
            folders.append(os.path.join(base, "Music", "ArabicTTS"))
            folders.append(os.path.join(base, "Download", "ArabicTTS"))
        else:
            folders.append(os.path.join(os.path.expanduser("~"), "ArabicTTS"))
        for folder in folders:
            try:
                os.makedirs(folder, exist_ok=True)
                dest = os.path.join(folder, name)
                shutil.copyfile(path, dest)
                return dest
            except Exception:
                continue
        return None

    def on_done(self, work_path, saved):
        self.progress.value = 100
        old = self.last_audio_path
        self.last_audio_path = work_path
        if saved and old and old != work_path and os.path.exists(old):
            try:
                os.remove(old)
            except Exception:
                pass
        self.set_busy(False)
        if saved:
            self.set_status(f"Done! Saved to:\n{saved}", (0.5, 1, 0.6, 1))
        else:
            self.set_status(
                "Done, but the file could not be copied to your storage. "
                "You can still play it here. Check the storage permission.",
                (1, 0.8, 0.4, 1))

    def on_cancelled(self):
        self.progress.value = 0
        self.set_busy(False)
        self.set_status("Cancelled.", (1, 0.8, 0.4, 1))

    def on_error(self, message):
        self.progress.value = 0
        self.set_busy(False)
        self.set_status(message, (1, 0.5, 0.5, 1))

    # ---------- playback ----------

    def toggle_play(self, *_):
        if self.sound and self.sound.state == "play":
            self.sound.stop()
            self.play_btn.text = "Play result"
            return
        if not self.last_audio_path or not os.path.exists(self.last_audio_path):
            self.set_status("No audio yet.")
            return
        if self.sound:
            self.sound.unload()
        self.sound = SoundLoader.load(self.last_audio_path)
        if not self.sound:
            self.set_status("Could not play the file here. Open it from your Music folder.",
                            (1, 0.8, 0.4, 1))
            return
        self.sound.bind(on_stop=lambda *_: self.ui(setattr, self.play_btn, "text", "Play result"))
        self.sound.play()
        self.play_btn.text = "Stop"


if __name__ == "__main__":
    TTSApp().run()
