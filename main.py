"""
Arabic TTS - simple app for Sawtak Arabi text-to-speech (Flet).

- Settings screen for the API key (show/hide, test connection)
- Voice list cached, last chosen voice remembered
- Long texts split automatically and merged into one WAV
- Open .txt / .srt / .vtt files (timings stripped)
- Paste, clear with confirmation, draft auto-saved
- Progress bar, cancel, retries, friendly errors
- Result saved to Music/ArabicTTS and playable in-app

Tested against Flet 0.28.x API:  pip install flet==0.28.3
(Flutter shapes Arabic text natively, so no font detection / reshaping needed.)
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

import flet as ft

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

BG = "#121419"
INPUT_BG = "#1F212E"
NEUTRAL = "#333847"
GREEN = "#26994D"
RED = "#BF4040"
BLUE = "#3373CC"

WHITE = "#FFFFFF"
GREY = "#B3B3BF"
C_WARN = "#FFCC66"
C_OK = "#80FF99"
C_ERR = "#FF8080"


def short(text, limit=26):
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "\u2026"


def data_dir():
    """Private app folder (Flet sets FLET_APP_STORAGE_DATA in built apps)."""
    base = os.environ.get("FLET_APP_STORAGE_DATA")
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".arabic_tts")
    os.makedirs(base, exist_ok=True)
    return base


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

def make_button(text, callback, color=NEUTRAL, height=48):
    return ft.ElevatedButton(
        text=text,
        height=height,
        bgcolor=color,
        color=WHITE,
        on_click=callback,
        style=ft.ButtonStyle(
            shape=ft.RoundedRectangleBorder(radius=4),
            text_style=ft.TextStyle(size=16),
        ),
    )


def make_label(text, size=14, color=WHITE):
    return ft.Text(text, size=size, color=color)


# ==========================================
# App
# ==========================================

class TTSApp:
    title = "Arabic TTS"

    def __init__(self, page: ft.Page):
        self.page = page
        self.user_data_dir = data_dir()

        self.api_key_value = DEFAULT_API_KEY
        self.voices = {}
        self.current_voice_id = DEFAULT_VOICE
        self.current_voice_name = ""
        self.busy = False
        self.cancel_event = threading.Event()
        self.last_audio_path = None
        self.audio = None
        self.playing = False
        self._draft_timer = None

        self.build()

    # ---------- build ----------

    def build(self):
        page = self.page
        page.title = self.title
        page.bgcolor = BG
        page.theme_mode = ft.ThemeMode.DARK
        page.padding = 12
        page.on_disconnect = lambda e: self.on_stop()
        try:
            page.on_app_lifecycle_state_change = self.on_lifecycle
        except Exception:
            pass

        # file picker (Open file)
        self.file_picker = ft.FilePicker(on_result=self.on_file_picked)
        page.overlay.append(self.file_picker)

        # top bar
        self.settings_btn = make_button("Settings", self.show_settings, height=44)
        top = ft.Row(
            [ft.Container(make_label("Arabic Text to Speech", 20), expand=True),
             self.settings_btn],
            alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
        )

        # voice row
        self.voice_spinner = ft.Dropdown(
            options=[],
            value=None,
            hint_text="Default voice",
            text_size=16,
            bgcolor=NEUTRAL,
            expand=True,
            on_change=self.on_voice_selected,
        )
        voice_row = ft.Row([make_label("Voice:", 14), self.voice_spinner])

        # text
        self.text_input = ft.TextField(
            multiline=True,
            expand=True,
            text_size=18,
            bgcolor=INPUT_BG,
            color=WHITE,
            cursor_color=WHITE,
            border_color=NEUTRAL,
            on_change=self.on_text_changed,
        )

        # tools
        self.paste_btn = make_button("Paste", self.paste_text, height=44)
        self.open_btn = make_button("Open file", self.open_file_dialog, height=44)
        self.clear_btn = make_button("Clear", self.clear_text, height=44)
        tools = ft.Row(
            [ft.Container(b, expand=True) for b in
             (self.paste_btn, self.open_btn, self.clear_btn)],
        )
        for b in (self.paste_btn, self.open_btn, self.clear_btn):
            b.expand = True

        self.counter = make_label("", 13, GREY)
        self.progress = ft.ProgressBar(value=0, bgcolor=NEUTRAL, color=GREEN)
        self.status = ft.Text("Ready.", size=14, color=WHITE)
        self.status_box = ft.Container(self.status, height=64, alignment=ft.alignment.top_left)

        self.convert_btn = make_button("Convert to speech", self.on_convert, GREEN, 56)
        self.convert_btn.expand = True
        self.play_btn = make_button("Play result", self.toggle_play, BLUE, 48)
        self.play_btn.expand = True
        self.play_btn.disabled = True

        root = ft.Column(
            [
                top,
                voice_row,
                make_label("Your text (Arabic):", 14),
                self.text_input,
                tools,
                self.counter,
                self.progress,
                self.status_box,
                ft.Row([self.convert_btn]),
                ft.Row([self.play_btn]),
            ],
            expand=True,
            spacing=8,
        )
        page.add(ft.SafeArea(root, expand=True))

        self.load_settings()
        self.update_counter()
        page.update()
        self.on_start()

    def on_start(self):
        if not self.api_key_value:
            threading.Timer(0.5, lambda: self.show_settings(first_run=True)).start()
        else:
            self.fetch_voices(self.api_key_value, silent=bool(self.voices))

    def on_lifecycle(self, e):
        state = str(getattr(e, "data", "")).lower()
        if "pause" in state or "inactive" in state or "detach" in state or "hidden" in state:
            self.save_draft()
            self.save_config()

    def on_stop(self):
        self.save_draft()
        self.save_config()
        self.stop_sound()

    # ---------- small utilities ----------

    def ui(self, func, *args):
        func(*args)
        self.page.update()

    def set_status(self, text, color=WHITE):
        self.status.value = str(text)[:280]
        self.status.color = color
        self.page.update()

    def set_progress(self, value, text=None):
        self.progress.value = value / 100
        if text:
            self.set_status(text)
        else:
            self.page.update()

    def popup(self, title, content, height=None):
        dlg = ft.AlertDialog(
            modal=True,
            title=ft.Text(title),
            content=content,
            bgcolor=INPUT_BG,
        )
        self.page.open(dlg)
        return dlg

    def close_popup(self, dlg):
        try:
            self.page.close(dlg)
        except Exception:
            dlg.open = False
            self.page.update()

    def confirm(self, title, message, on_yes):
        box = ft.Column([make_label(message, 15)], tight=True)
        p = self.popup(title, box)

        def yes(*_):
            self.close_popup(p)
            on_yes()

        row = ft.Row([
            ft.Container(make_button("Yes", yes, RED), expand=True),
            ft.Container(make_button("No", lambda *_: self.close_popup(p)), expand=True),
        ])
        box.controls.append(row)
        self.page.update()

    # ---------- config & draft ----------

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
                self.text_input.value = f.read()
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
                f.write(self.text_input.value or "")
        except Exception:
            pass

    def on_text_changed(self, *_):
        self.update_counter()
        self.page.update()
        if self._draft_timer:
            self._draft_timer.cancel()
        self._draft_timer = threading.Timer(2, self.save_draft)
        self._draft_timer.daemon = True
        self._draft_timer.start()

    def update_counter(self, *_):
        n = len((self.text_input.value or "").strip())
        if n > MAX_TOTAL:
            self.counter.value = f"{n:,} characters - too long (max {MAX_TOTAL:,})"
            self.counter.color = C_ERR
            return
        parts = math.ceil(n / MAX_CHUNK) if n else 0
        msg = f"{n:,} characters"
        if parts > 1:
            msg += f"  |  will be converted in about {parts} parts"
        self.counter.value = msg
        self.counter.color = GREY

    # ---------- text tools ----------

    def paste_text(self, *_):
        try:
            clip = self.page.get_clipboard()
        except Exception:
            clip = ""
        if not clip:
            self.set_status("The clipboard is empty.")
            return
        self.text_input.value = (self.text_input.value or "") + clip
        self.update_counter()
        self.set_status("Text pasted.")
        self.save_draft()

    def clear_text(self, *_):
        def do_clear():
            self.text_input.value = ""
            self.update_counter()
            self.page.update()
            self.save_draft()

        if len(self.text_input.value or "") > 100:
            self.confirm("Clear text", "Delete all the text?", do_clear)
        else:
            do_clear()

    def open_file_dialog(self, *_):
        self.file_picker.pick_files(
            dialog_title="Choose a .txt / .srt / .vtt file",
            allow_multiple=False,
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=["txt", "srt", "vtt"],
        )

    def on_file_picked(self, e):
        if not e.files:
            return
        path = e.files[0].path
        try:
            if not path:
                raise RuntimeError("The file path is not available.")
            text = read_text_file(path)
        except Exception as exc:
            self.set_status(f"Could not open the file: {exc}", C_ERR)
            return
        self.text_input.value = text
        self.update_counter()
        self.set_status("File loaded.")
        self.save_draft()

    # ---------- settings ----------

    def show_settings(self, *_, first_run=False):
        box = ft.Column(tight=True, spacing=10, width=360)
        if first_run:
            box.controls.append(make_label(
                "Welcome! Enter your Sawtak Arabi API key to get started.", 15))
        box.controls.append(make_label("API key:", 14))

        key_in = ft.TextField(
            value=self.api_key_value, password=True, can_reveal_password=False,
            text_size=16, expand=True,
        )

        def toggle(e):
            key_in.password = not key_in.password
            show_btn.text = "Show" if key_in.password else "Hide"
            self.page.update()

        show_btn = make_button("Show", toggle, height=48)
        box.controls.append(ft.Row([key_in, show_btn]))

        msg = ft.Text("", size=14, color=WHITE)
        box.controls.append(ft.Container(msg, height=60, alignment=ft.alignment.top_left))

        def say(text, color=WHITE):
            def apply():
                msg.value = str(text)
                msg.color = color
            self.ui(apply)

        def connect(*_):
            key = key_in.value.strip().strip("\"'").strip()
            if not key:
                say("Please enter your API key.", C_ERR)
                return
            self.api_key_value = key
            self.save_config()
            say("Connecting...")
            self.fetch_voices(key, silent=False, say=say)

        def use_default(*_):
            key_in.value = DEFAULT_API_KEY
            self.page.update()

        box.controls.append(make_button("Save and connect", connect, GREEN))
        box.controls.append(make_button("Use built-in key", use_default, height=44))
        p = self.popup("Settings", box)
        box.controls.append(make_button("Close", lambda *_: self.close_popup(p)))
        self.page.update()

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
                        say("Connected, but no voices were returned.", C_WARN)
                    elif not silent:
                        self.ui(self.set_status, "No voices were returned.", C_WARN)
                    return
                self.ui(self.apply_voices, mapping, True)
                if say:
                    say(f"Connected successfully. {len(mapping)} voices available.",
                        C_OK)
            except Exception as exc:
                if say:
                    say(str(exc), C_ERR)
                elif not silent:
                    self.ui(self.set_status, str(exc), C_ERR)

        threading.Thread(target=work, daemon=True).start()

    # ---------- voices ----------

    def apply_voices(self, mapping, announce=True):
        self.voices = mapping
        options = []
        used = set()
        for name in mapping:
            display = short(name)
            k = 1
            while display in used:
                k += 1
                display = f"{short(name, 22)} #{k}"
            used.add(display)
            options.append(ft.dropdown.Option(key=name, text=display))
        self.voice_spinner.options = options

        selected = None
        for name, vid in mapping.items():
            if vid == self.current_voice_id:
                selected = name
                break
        if selected is None:
            selected = next(iter(mapping))
            self.current_voice_id = mapping[selected]
        self.current_voice_name = selected
        self.voice_spinner.value = selected
        self.save_config()
        self.page.update()

    def on_voice_selected(self, e):
        name = self.voice_spinner.value
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
        self.text_input.read_only = busy
        if busy:
            self.convert_btn.text = "Cancel"
            self.convert_btn.bgcolor = RED
            self.play_btn.disabled = True
        else:
            self.convert_btn.text = "Convert to speech"
            self.convert_btn.bgcolor = GREEN
            self.play_btn.disabled = not self.last_audio_path
        self.page.update()

    def on_convert(self, *_):
        if self.busy:
            self.cancel_event.set()
            self.set_status("Cancelling after the current part...")
            return

        if not self.api_key_value:
            self.show_settings(first_run=True)
            return

        text = prepare_text(self.text_input.value or "")
        if not text:
            self.set_status("Type, paste or open some text first.", C_WARN)
            return
        if len(text) > MAX_TOTAL:
            self.set_status(
                f"The text is too long ({len(text):,}). Maximum is {MAX_TOTAL:,} characters.",
                C_ERR)
            return

        self.stop_sound()
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

    def is_android(self):
        try:
            return "android" in str(self.page.platform).lower()
        except Exception:
            return False

    def export_file(self, path, name):
        """Copy the result to a folder the user can reach. Returns path or None."""
        folders = []
        if self.is_android():
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
        self.progress.value = 1
        old = self.last_audio_path
        self.last_audio_path = work_path
        if saved and old and old != work_path and os.path.exists(old):
            try:
                os.remove(old)
            except Exception:
                pass
        self.set_busy(False)
        if saved:
            self.set_status(f"Done! Saved to:\n{saved}", C_OK)
        else:
            self.set_status(
                "Done, but the file could not be copied to your storage. "
                "You can still play it here. Check the storage permission.",
                C_WARN)

    def on_cancelled(self):
        self.progress.value = 0
        self.set_busy(False)
        self.set_status("Cancelled.", C_WARN)

    def on_error(self, message):
        self.progress.value = 0
        self.set_busy(False)
        self.set_status(message, C_ERR)

    # ---------- playback ----------

    def stop_sound(self):
        if self.audio is not None:
            try:
                self.audio.pause()
                self.audio.release()
            except Exception:
                pass
            try:
                self.page.overlay.remove(self.audio)
            except Exception:
                pass
            self.audio = None
        self.playing = False
        self.play_btn.text = "Play result"
        try:
            self.page.update()
        except Exception:
            pass

    def on_audio_state(self, e):
        if str(e.data).lower() in ("completed", "stopped"):
            self.playing = False
            self.play_btn.text = "Play result"
            self.page.update()

    def toggle_play(self, *_):
        if self.playing:
            self.stop_sound()
            return
        if not self.last_audio_path or not os.path.exists(self.last_audio_path):
            self.set_status("No audio yet.")
            return
        self.stop_sound()
        try:
            self.audio = ft.Audio(
                src=self.last_audio_path,
                autoplay=True,
                on_state_changed=self.on_audio_state,
            )
            self.page.overlay.append(self.audio)
            self.playing = True
            self.play_btn.text = "Stop"
            self.page.update()
        except Exception:
            self.audio = None
            self.playing = False
            self.set_status("Could not play the file here. Open it from your Music folder.",
                            C_WARN)


def main(page: ft.Page):
    TTSApp(page)


if __name__ == "__main__":
    ft.app(target=main)
