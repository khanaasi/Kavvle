import os, sys, site, importlib, importlib.util, importlib.metadata, traceback
import time, asyncio, subprocess, json, gc, re, base64, requests, html, shutil, threading

# ----------------------------- EARLY PATH SETUP -----------------------------
def _ensure_user_site_path():
    user_site = site.getusersitepackages()
    if os.path.exists(user_site) and user_site not in sys.path:
        sys.path.insert(0, user_site)

_ensure_user_site_path()
# -----------------------------------------------------------------------------

WORK_DIR = "/kaggle/working" if os.path.exists("/kaggle") else "/tmp/kavvle_work"
os.makedirs(WORK_DIR, exist_ok=True)
os.chdir(WORK_DIR)

# Auto Install FFmpeg on Kaggle environment
if shutil.which("ffmpeg") is None:
    subprocess.run("apt-get update && apt-get install -y ffmpeg", shell=True)

# Globals
last_time = 0
start_time = 0
status_msg_id = None
app = None

CONFIG_B64 = ""

def report_critical_failure(error_msg):
    try:
        token = os.environ.get("BOT_TOKEN")
        chat_id = os.environ.get("CHAT_ID")
        msg_id = os.environ.get("TRIGGER_MSG_ID")
        if (not token or not chat_id) and CONFIG_B64:
            cfg = json.loads(base64.b64decode(CONFIG_B64).decode())
            token = cfg.get("bot_token")
            chat_id = cfg.get("chat_id")
            msg_id = cfg.get("trigger_msg_id")
        if token and chat_id:
            text = f"❌ <b>Kaggle Execution Error Traceback:</b>\n\n<pre><code class='language-python'>{html.escape(error_msg[:3500])}</code></pre>"
            payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
            if msg_id:
                payload["message_id"] = int(msg_id)
                requests.post(f"https://api.telegram.org/bot{token}/editMessageText", json=payload, timeout=10)
            else:
                requests.post(f"https://api.telegram.org/bot{token}/sendMessage", json=payload, timeout=10)
    except:
        pass

try:
    def load_config():
        if not CONFIG_B64:
            raise RuntimeError("CONFIG_B64 missing")
        return json.loads(base64.b64decode(CONFIG_B64).decode())

    CFG = load_config()
    API_ID = int(CFG["api_id"])
    API_HASH = CFG["api_hash"]
    BOT_TOKEN = CFG["bot_token"]
    TASK_TYPE = CFG["task_type"]
    CHAT_ID = int(CFG["chat_id"])
    USER_ID = int(CFG.get("user_id") or CFG["chat_id"])
    RESOLUTION = CFG.get("resolution", "none")
    WM_POS = CFG.get("wm_pos", "right")
    RENAME = CFG.get("rename", "none")
    TRIGGER_MSG_ID = CFG.get("trigger_msg_id")
    VIDEO_MSG_ID = CFG.get("video_msg_id", "none")
    SUB_MSG_ID = CFG.get("sub_msg_id", "none")
    WM_MSG_ID = CFG.get("wm_msg_id", "none")
    FONT_MSG_ID = CFG.get("font_msg_id", "none")
    DESK_CHANNEL_ID = -1003700822969
    HW_MODE = CFG.get("hardware_mode", "cpu")
    SESSION_STRING = CFG.get("session_string", None)
except Exception:
    tb = traceback.format_exc()
    report_critical_failure(tb)
    sys.exit(1)

# ----------------------------- DEPENDENCY SYSTEM -----------------------------
def ensure_deps():
    need = []
    for mod, pip_name in [("pyrogram", "pyrogram"), ("tgcrypto", "tgcrypto"),
                          ("fontTools", "fonttools")]:
        if importlib.util.find_spec(mod) is None:
            need.append(pip_name)
    if need:
        print(f"📦 Installing missing packages: {need}")
        cmd = [sys.executable, "-m", "pip", "install", "-q", "--user", "--no-cache-dir", *need]
        try:
            subprocess.run(cmd, check=True)
        except Exception:
            subprocess.run(cmd, check=False)
        _ensure_user_site_path()
        importlib.invalidate_caches()

def ensure_fonts():
    """Arial-compatible + Devanagari fonts, so subtitles never turn into boxes."""
    try:
        have = subprocess.run("fc-list", capture_output=True, text=True, timeout=30).stdout.lower()
        if "liberation" in have and "devanagari" in have:
            return
        subprocess.run("apt-get update -qq && apt-get install -y -qq --no-install-recommends "
                       "fontconfig fonts-liberation fonts-dejavu-core fonts-noto-core",
                       shell=True, timeout=300)
    except Exception as e:
        print(f"Font setup skipped: {e}")

ensure_deps()
ensure_fonts()

import pyrogram.utils
from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from fontTools.ttLib import TTFont

pyrogram.utils.get_peer_type = lambda p: "channel" if str(p).startswith("-100") else "chat" if str(p).startswith("-") else "user"

# ----------------------------- PROGRESS UI HELPERS -----------------------------
def reset_prog():
    global last_time, start_time
    last_time = time.time()
    start_time = time.time()

def get_download_bar(percent):
    filled = int(percent / 100 * 20)
    return f"[{'>' * filled}{'-' * (20 - filled)}]"

def get_process_bar(percent):
    filled = int(percent / 100 * 20)
    seq = ["•", "°", ":", "°", "•", ":"]
    bar = "".join(seq[i % len(seq)] for i in range(filled))
    return f"[{bar}{'-' * (20 - filled)}]"

def get_send_bar(percent):
    filled = int(percent / 100 * 20)
    return f"[{'▓' * filled}{'▒' * (20 - filled)}]"

def _sync_http_edit(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {"chat_id": CHAT_ID, "message_id": status_msg_id, "text": text, "parse_mode": "HTML",
               "reply_markup": {"inline_keyboard": [[{"text": "🛑 Cancel Task", "callback_data": "cancel_active_run"}]]}}
    try:
        requests.post(url, json=payload, timeout=5)
    except:
        pass

def fire_and_forget_http(text):
    threading.Thread(target=_sync_http_edit, args=(text,), daemon=True).start()

# --- FLOODWAIT PROOF PROGRESS CALLBACK ---
def prog(current, total, step_name):
    global last_time, start_time
    now = time.time()
    if start_time == 0:
        start_time = now
        last_time = now
        return
    if now - last_time >= 12 or current >= total:
        elapsed = now - start_time
        speed = current / elapsed if elapsed > 0 else 0
        speed_mb = (speed / 1024) / 1024
        percent = (current / total) * 100 if total > 0 else 0
        if "download" in step_name:
            text = f"📥 <b>Downloading Video</b>\n{get_download_bar(percent)} [{percent:.1f}%]\n🚀 Speed: <b>{speed_mb:.2f} MB/s</b>\n📦 {current/1048576:.1f}MB / {total/1048576:.1f}MB"
        else:
            text = f"📤 <b>Sending Video</b>\n{get_send_bar(percent)} [{percent:.1f}%]\n🚀 Speed: <b>{speed_mb:.2f} MB/s</b>\n📦 {current/1048576:.1f}MB / {total/1048576:.1f}MB"

        fire_and_forget_http(text)
        last_time = now

# ----------------------------- VIDEO PROBE -----------------------------
def probe_video(video_path):
    """duration (s), height, fps of the first video stream."""
    duration, height, fps = 0.0, 0, 0.0
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                            "-show_entries", "stream=height,avg_frame_rate,r_frame_rate:format=duration",
                            "-of", "json", video_path], capture_output=True, text=True, timeout=30)
        d = json.loads(r.stdout or "{}")
        st = (d.get("streams") or [{}])[0]
        height = int(st.get("height") or 0)
        for key in ("avg_frame_rate", "r_frame_rate"):
            num, _, den = str(st.get(key) or "0/1").partition("/")
            try:
                v = float(num) / float(den or 1)
            except (ValueError, ZeroDivisionError):
                v = 0.0
            if 1 < v < 240:
                fps = v
                break
        duration = float((d.get("format") or {}).get("duration") or 0)
    except Exception:
        pass
    return duration, height, fps

def get_duration(video_path):
    return probe_video(video_path)[0]

def get_font_name(font_path):
    try:
        font = TTFont(font_path, fontNumber=0)
        for record in font['name'].names:
            if record.nameID == 4:
                return record.toUnicode()
    except:
        pass
    return "Arial"

# ----------------------------- SUBTITLE HELPERS -----------------------------
# Dialogue ALWAYS uses the bot's own style (subtitle file ka style / tags ignore),
# same look as the Colab bot: Arial Bold, white, black outline, bottom centre,
# and never more than 2 lines (long lines get a slightly smaller font instead of a 3rd line).
PLAY_W, PLAY_H = 1920, 1080
DLG_FONT_SIZE = 90      # 8.3% of frame height (= old 24pt @ 288), measured against the Colab screenshot
DLG_OUTLINE = 4
DLG_SHADOW = 3
DLG_MARGIN_V = 70
DLG_MARGIN_LR = 75


def sec_to_ass_time(seconds):
    cs = int(round(max(0.0, float(seconds)) * 100))
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def read_text_any(path):
    raw = open(path, "rb").read()
    if raw[:3] == b"\xef\xbb\xbf":
        return raw[3:].decode("utf-8", "replace")
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16", "replace")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("cp1252", "replace")


def is_ass_text(text):
    return bool(re.search(r"\[Script Info\]|\[V4\+?\s*Styles\]|\[Events\]", text[:6000], re.I))


def _find_measure_font(custom_path=None):
    """Font file used only to MEASURE text width (so we know when a line needs 2 lines / smaller size)."""
    if custom_path and os.path.exists(custom_path):
        return custom_path
    try:
        r = subprocess.run(["fc-match", "-f", "%{file}", "Arial:bold"], capture_output=True, text=True, timeout=10)
        p = r.stdout.strip()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    for p in ("/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
              "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"):
        if os.path.exists(p):
            return p
    return None


class TextMeter:
    def __init__(self, font_path):
        self.ok = False
        try:
            f = TTFont(font_path, fontNumber=0)
            self.cmap, self.hmtx = f.getBestCmap(), f["hmtx"]
            upem = f["head"].unitsPerEm
            os2, hh = f["OS/2"], f["hhea"]
            # ASS Fontsize = line cell height (win ascent + descent), so px per font unit = fs / cell
            self.cell = (os2.usWinAscent + os2.usWinDescent) or (hh.ascent - hh.descent) or upem
            self.missing = int(0.62 * upem)     # glyphs the font lacks (e.g. Devanagari) -> libass falls back
            self.ok = True
        except Exception:
            pass

    def width(self, text, fs):
        if not self.ok:
            return len(text) * 0.47 * fs
        total = 0
        for ch in text:
            g = self.cmap.get(ord(ch))
            total += self.hmtx[g][0] if g is not None else self.missing
        return total * fs / self.cell


def layout_dialogue(lines, meter):
    """lines -> ASS text with at most 2 lines. Keeps the author's own 1-2 line split when it fits,
    otherwise re-wraps into 2 balanced lines, and shrinks the font for that cue only if 2 lines are not enough."""
    lines = [l for l in lines if l.strip()]
    if not lines:
        return ""
    fs = DLG_FONT_SIZE
    limit = (PLAY_W - 2 * DLG_MARGIN_LR - 2 * DLG_OUTLINE) * 0.97

    if len(lines) <= 2 and all(meter.width(l, fs) <= limit for l in lines):
        return "\\N".join(lines)
    flat = " ".join(lines)
    if meter.width(flat, fs) <= limit:
        return flat

    words = flat.split(" ")
    best = None
    for i in range(1, len(words)):
        a, b = " ".join(words[:i]), " ".join(words[i:])
        m = max(meter.width(a, fs), meter.width(b, fs))
        if best is None or m < best[0]:
            best = (m, a + "\\N" + b)
    if best is None:                       # one single very long word
        best = (meter.width(flat, fs), flat)
    worst, text = best
    if worst <= limit:
        return text
    return "{\\fs%d}%s" % (max(30, int(fs * limit / worst)), text)


def _plain_lines(body):
    """Cue text -> list of plain lines (all tags / styling removed)."""
    body = re.sub(r"\{[^}]*\}", "", body)              # ass override tags
    body = re.sub(r"<\d{1,2}:\d{2}[^>]*>", "", body)   # vtt karaoke timestamps
    body = re.sub(r"</?[A-Za-z][^>]*>", "", body)      # <i> <b> <font ..> <c.x> <v ..>
    body = html.unescape(body).replace("\\N", "\n").replace("\\n", "\n").replace("\\h", " ")
    lines = [re.sub(r"\s+", " ", l).strip() for l in body.replace("\r", "").split("\n")]
    return [l for l in lines if l]


_TIME_RE = re.compile(
    r"(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})\s*-->\s*(?:(\d+):)?(\d{1,2}):(\d{2})[,.](\d{1,3})")


def _to_ms(h, m, s, ms):
    return ((int(h or 0) * 60 + int(m)) * 60 + int(s)) * 1000 + int(ms.ljust(3, "0"))


def _srt_vtt_cues(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    cues = []
    for block in re.split(r"\n\s*\n", text):
        lines = block.strip("\n").split("\n")
        if lines and lines[0].strip().upper().startswith(("NOTE", "STYLE", "REGION", "WEBVTT")) and \
                not any("-->" in l for l in lines):
            continue
        ti = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if ti is None:
            continue
        m = _TIME_RE.search(lines[ti])
        if not m:
            continue
        g = m.groups()
        plain = _plain_lines("\n".join(lines[ti + 1:]))
        if plain:
            cues.append((sec_to_ass_time(_to_ms(*g[0:4]) / 1000.0), sec_to_ass_time(_to_ms(*g[4:8]) / 1000.0), plain))
    return cues


def _ass_cues(text):
    """Only the dialogue text + timing is taken from an .ass; its styles / positions / effects are ignored.
    Vector drawings and watermark/logo/credit styled lines are dropped (they are not dialogue)."""
    cues, sec, fmt = [], None, None
    for raw in text.split("\n"):
        s = raw.strip()
        low = s.lower()
        if s.startswith("[") and s.endswith("]"):
            sec, fmt = low, None
        elif sec == "[events]":
            if low.startswith("format:"):
                fmt = [x.strip().lower() for x in s[7:].split(",")]
            elif low.startswith("dialogue:") and fmt:
                parts = s.split(":", 1)[1].lstrip().split(",", len(fmt) - 1)
                if len(parts) < len(fmt):
                    continue
                d = dict(zip(fmt, parts))
                txt = d.get("text", "")
                if re.search(r"(watermark|logo|credit)", d.get("style", ""), re.I):
                    continue
                if re.search(r"\{[^}]*\\p[1-9]", txt):
                    continue
                plain = _plain_lines(txt)
                if plain:
                    cues.append((d.get("start", "0:00:00.00").strip(), d.get("end", "0:00:00.00").strip(), plain))
    return cues


def build_dialogue_ass(cues, font_name, bold, meter):
    font_name = (font_name or "Arial").replace(",", " ")
    head = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {PLAY_W}\nPlayResY: {PLAY_H}\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\nYCbCr Matrix: None\n\n"      # WrapStyle 2 = only OUR line breaks
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{DLG_FONT_SIZE},&H00FFFFFF,&H000000FF,&H00000000,&H00000000,"
        f"{-1 if bold else 0},0,0,0,100,100,0,0,1,{DLG_OUTLINE},{DLG_SHADOW},2,"
        f"{DLG_MARGIN_LR},{DLG_MARGIN_LR},{DLG_MARGIN_V},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for start, end, lines in cues:
        t = layout_dialogue(lines, meter)
        if t:
            events.append(f"Dialogue: 0,{start},{end},Default,,0,0,0,,{t}")
    if not events:
        raise Exception("Subtitle file me koi valid dialogue nahi mila.")
    return head + "\n".join(events) + "\n"


def prepare_subtitle(sub_file, font_name, custom_font, out_path, font_path=None):
    """Writes ready_sub.ass in the bot's own style. Returns False (the watermark is the PNG overlay)."""
    text = read_text_any(sub_file).replace("\r\n", "\n").replace("\r", "\n")
    if sub_file.lower().endswith((".ass", ".ssa")) or is_ass_text(text):
        cues = _ass_cues(text)
    else:
        cues = _srt_vtt_cues(text)
    meter = TextMeter(_find_measure_font(font_path if custom_font else None))
    ass = build_dialogue_ass(cues, font_name, bold=not custom_font, meter=meter)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(ass)
    return False


# ----------------------------- KAGGLE NOTEBOOK CLEANUP -----------------------------
async def kill_all_other_notebooks():
    username = os.environ.get("KAGGLE_USERNAME", "").strip()
    api_key = os.environ.get("KAGGLE_KEY", "").strip()
    current_kernel = os.environ.get("KAGGLE_KERNEL_NAME", "").strip()

    if not username or not api_key:
        print("⚠️ Kaggle credentials not found. Skipping notebook cleanup.")
        return

    os.environ["KAGGLE_USERNAME"] = username
    os.environ["KAGGLE_KEY"] = api_key

    proc = await asyncio.create_subprocess_exec(
        "kaggle", "kernels", "list", "--user", username, "--csv",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        print(f"⚠️ Failed to list kernels: {stderr.decode()}")
        return

    lines = stdout.decode().strip().split("\n")
    killed = []
    for line in lines[1:]:
        parts = line.split(",")
        if not parts:
            continue
        ref = parts[0].strip()
        if current_kernel and ref == current_kernel:
            continue
        del_proc = await asyncio.create_subprocess_exec(
            "kaggle", "kernels", "delete", "-k", ref,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        await del_proc.communicate()
        killed.append(ref)

    if killed:
        print(f"🧹 Killed {len(killed)} old notebook(s): {', '.join(killed)}")
    else:
        print("✨ No stale notebooks to kill.")

# ----------------------------- DOWNLOAD ENGINE -----------------------------
async def download_message_asset(app_instance, msg_id_str, output_path, step_name, show_progress=True):
    if not msg_id_str or msg_id_str == "none":
        return None
    try:
        msg_id = int(msg_id_str)
        msg = await app_instance.get_messages(DESK_CHANNEL_ID, msg_id)
        if not msg:
            raise Exception(f"Mirrored asset {msg_id} was removed from logging channel.")
        media = msg.document or msg.video or msg.audio or msg.photo or msg.animation
        if not media:
            raise Exception("No valid downloadable stream in secured message.")
        kw = {}
        if show_progress:
            reset_prog()
            kw = dict(progress=prog, progress_args=(step_name,))
        result = await asyncio.wait_for(
            app_instance.download_media(msg, file_name=output_path, **kw),
            timeout=1800
        )
        if not result or not os.path.exists(result):
            raise Exception("Mirrored file failed to write successfully.")
        return result
    except Exception as e:
        raise Exception(f"Download Error on secured step '{step_name}': {type(e).__name__}: {e}")

async def download_by_file_id(app_instance, file_id, output_path, step_name, show_progress=True):
    if not file_id or file_id == "none":
        return None
    try:
        kw = {}
        if show_progress:
            reset_prog()
            kw = dict(progress=prog, progress_args=(step_name,))
        result = await asyncio.wait_for(
            app_instance.download_media(file_id, file_name=output_path, **kw),
            timeout=1800
        )
        if not result or not os.path.exists(result):
            raise Exception("File path failed to register on fallback.")
        return result
    except Exception as e:
        raise Exception(f"Fallback download failed on '{step_name}': {type(e).__name__}: {e}")

async def download_asset_robust(app_instance, val, output_path, step_name, show_progress=True):
    if not val or val == "none":
        return None
    if str(val).isdigit():
        return await download_message_asset(app_instance, val, output_path, step_name, show_progress)
    return await download_by_file_id(app_instance, val, output_path, step_name, show_progress)

# ----------------------------- EMBEDDED SUBTITLE EXTRACTION -----------------------------
TEXT_SUB_CODECS = {"ass", "ssa", "subrip", "srt", "webvtt", "mov_text", "text"}

def extract_embedded_subs_sync(video_file, base_name):
    """One ffmpeg pass for all text subtitle tracks. Runs in a thread while the encode is going on."""
    try:
        res = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries",
                              "stream=index,codec_name", "-of", "json", video_file],
                             capture_output=True, text=True, timeout=60)
        streams = json.loads(res.stdout or "{}").get("streams", [])
        cmd = ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", video_file]
        outs = []
        for st in streams:
            if st.get("codec_name") in TEXT_SUB_CODECS:
                out = os.path.join(WORK_DIR, f"{base_name}_track_{len(outs) + 1}.ass")
                cmd += ["-map", f"0:{st['index']}", "-c:s", "ass", out]
                outs.append(out)
        if not outs:
            return []
        subprocess.run(cmd, capture_output=True, timeout=900)
        return [o for o in outs if os.path.exists(o) and os.path.getsize(o) > 0]
    except Exception as e:
        print(f"Subtitle extraction failed: {e}")
        return []

# ----------------------------- ENCODING ENGINE -----------------------------
_PROGRESS_KEYS = re.compile(r"^(frame|fps|stream_\d+_\d+_q|bitrate|total_size|out_time_us|out_time_ms|out_time|"
                            r"dup_frames|drop_frames|speed|progress)=")

def run_ffmpeg_sync(cmd, duration, process_title):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    last_edit = time.time()
    log_tail = []
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        if not _PROGRESS_KEYS.match(line):
            log_tail.append(line)
            if len(log_tail) > 20:
                log_tail.pop(0)
        if line.startswith("out_time_us=") and duration > 0:
            now = time.time()
            if now - last_edit >= 12:
                try:
                    us = int(line.split("=")[1])
                    percent = min((us / 1_000_000.0 / duration) * 100, 100.0)
                    print(f"[encode] {percent:.1f}%", flush=True)
                    fire_and_forget_http(f"⚙️ {process_title}\n{get_process_bar(percent)} [{percent:.1f}%]")
                except:
                    pass
                last_edit = now
    proc.wait()
    return proc.returncode, log_tail

def build_ffmpeg_cmds(video_file, out_name, crf, max_rate, buf_size, gop, vf=None, complex_f=None, wm_file=None):
    """Returns (gpu_cmd, cpu_cmd). Both: keyframe every 2s (instant seeking), faststart, yuv420p, stereo aac."""
    head = ["ffmpeg", "-y", "-hide_banner", "-nostats", "-loglevel", "error", "-progress", "pipe:1", "-i", video_file]
    if complex_f:
        head += ["-i", wm_file, "-filter_complex", complex_f, "-map", "[vout]"]
    else:
        head += ["-vf", vf, "-map", "0:v:0"]
    head += ["-map", "0:a?", "-sn", "-dn"]
    tail = ["-pix_fmt", "yuv420p",
            "-g", str(gop), "-force_key_frames", "expr:gte(t,n_forced*2)",
            "-c:a", "aac", "-b:a", "128k", "-ac", "2",
            "-max_muxing_queue_size", "1024", "-movflags", "+faststart", out_name]
    cpu = head + ["-c:v", "libx264", "-preset", "ultrafast", "-crf", crf, "-maxrate", max_rate,
                  "-bufsize", buf_size, "-threads", "0", "-forced-idr", "1"] + tail
    gpu = head + ["-c:v", "h264_nvenc", "-preset", "p4", "-rc", "vbr", "-cq", crf, "-b:v", "0",
                  "-maxrate", max_rate, "-bufsize", buf_size, "-profile:v", "high", "-forced-idr", "1"] + tail
    return gpu, cpu

# ----------------------------- OUTPUT VERIFICATION -----------------------------
def _faststart_ok(path):
    """moov atom must come BEFORE mdat, otherwise Telegram has to download everything before playing."""
    size_total = os.path.getsize(path)
    with open(path, "rb") as f:
        pos = 0
        while pos + 8 <= size_total:
            f.seek(pos)
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            sz, typ = int.from_bytes(hdr[:4], "big"), hdr[4:8]
            if typ == b"moov":
                return True
            if typ == b"mdat":
                return False
            if sz == 1:
                sz = int.from_bytes(f.read(8), "big")
            elif sz == 0:
                break
            pos += sz
    return False

def verify_output(path, src_duration, full_decode=False):
    """Returns (ok, reason). A file that fails here is NEVER sent."""
    try:
        if not os.path.exists(path) or os.path.getsize(path) < 1000:
            return False, "output file missing/empty"
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration:stream=codec_type,codec_name,pix_fmt", "-of", "json", path],
                           capture_output=True, text=True, timeout=60)
        info = json.loads(r.stdout or "{}")
        vs = [s for s in info.get("streams", []) if s.get("codec_type") == "video"]
        if not vs:
            return False, "no video stream"
        if vs[0].get("codec_name") != "h264" or vs[0].get("pix_fmt") != "yuv420p":
            return False, f"unexpected codec/pix_fmt {vs[0].get('codec_name')}/{vs[0].get('pix_fmt')}"
        dur = float((info.get("format") or {}).get("duration") or 0)
        if dur <= 0:
            return False, "no duration"
        if src_duration > 0 and abs(dur - src_duration) > max(2.0, src_duration * 0.02):
            return False, f"duration mismatch (src {src_duration:.1f}s / out {dur:.1f}s)"
        if not _faststart_ok(path):
            return False, "faststart missing"

        kp = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "packet=pts_time,flags", "-of", "csv=p=0", path],
                            capture_output=True, text=True, timeout=300)
        keys = []
        for line in kp.stdout.splitlines():
            p = line.split(",")
            if len(p) >= 2 and "K" in p[1]:
                try:
                    keys.append(float(p[0]))
                except ValueError:
                    pass
        if not keys:
            return False, "no keyframes"
        keys.sort()
        gaps = [b - a for a, b in zip(keys, keys[1:])] + [dur - keys[-1]]
        if max(gaps) > 3.5:
            return False, f"keyframe gap {max(gaps):.1f}s (seeking would lag)"

        if full_decode:
            d = subprocess.run(["ffmpeg", "-v", "error", "-threads", "0", "-i", path, "-map", "0:v:0", "-an",
                                "-f", "null", "-"], capture_output=True, text=True, timeout=3600)
            if d.returncode != 0 or d.stderr.strip():
                return False, f"decode errors: {d.stderr.strip()[:150]}"
        else:
            for t in sorted({0.0, max(0.0, dur / 2 - 7), max(0.0, dur - 15)}):
                d = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-t", "15", "-i", path,
                                    "-map", "0:v:0", "-an", "-f", "null", "-"],
                                   capture_output=True, text=True, timeout=300)
                if d.returncode != 0 or d.stderr.strip():
                    return False, f"decode errors near {t:.0f}s: {d.stderr.strip()[:150]}"
        return True, "ok"
    except Exception as e:
        return False, f"verification crashed: {e}"

def encode_with_fallback(base_cmd_gpu, base_cmd_cpu, duration, title, out_name):
    if HW_MODE == "gpu" and base_cmd_gpu:
        rc, log = run_ffmpeg_sync(base_cmd_gpu, duration, title + " (GPU)")
        why = "ffmpeg failed"
        if rc == 0:
            ok, why = verify_output(out_name, duration, full_decode=True)   # GPU path: full decode check
            if ok:
                return
        print(f"GPU output rejected: {why}")
        fire_and_forget_http("⚠️ GPU fallback activated. Switching to CPU encoding...")
    rc, log = run_ffmpeg_sync(base_cmd_cpu, duration, title + " (CPU)")
    if rc != 0:
        raise Exception("FFmpeg command crashed on execution.\n" + "\n".join(log[-8:]))
    ok, why = verify_output(out_name, duration)
    if not ok:
        raise Exception(f"Output check failed, file not sent: {why}")

# ----------------------------- UPLOAD ENGINE -----------------------------
def make_thumb(file_path, duration):
    thumb = os.path.join(WORK_DIR, "thumb.jpg")
    try:
        if os.path.exists(thumb):
            os.remove(thumb)
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-ss", "1" if duration > 2 else "0",
                        "-i", file_path, "-frames:v", "1", "-vf", "scale=320:-2", "-q:v", "6", thumb],
                       capture_output=True, timeout=30)
    except:
        pass
    return thumb if os.path.exists(thumb) and os.path.getsize(thumb) > 0 else None

async def deliver_video_asset(app_instance, chat_id, target_user, file_path, caption):
    """Sends the result as a DOCUMENT."""
    if not os.path.exists(file_path) or os.path.getsize(file_path) < 100:
        raise Exception("Processed output file was empty or missing.")
    thumb_path = make_thumb(file_path, get_duration(file_path))
    safe_cap = html.escape(caption)

    reset_prog()
    pm_msg, file_id = None, None
    try:
        pm_msg = await asyncio.wait_for(
            app_instance.send_document(chat_id=target_user, document=file_path, caption=safe_cap,
                                       parse_mode=ParseMode.HTML, thumb=thumb_path,
                                       progress=prog, progress_args=("sending_video",)),
            timeout=1800
        )
    except Exception as e:
        print(f"PM delivery failed ({e}); sending in chat instead")
        reset_prog()
        pm_msg = await asyncio.wait_for(
            app_instance.send_document(chat_id=chat_id, document=file_path,
                                       caption=f"⚠️ <a href='tg://user?id={target_user}'>User</a>, Video Ready:\n\n{safe_cap}",
                                       thumb=thumb_path, progress=prog, progress_args=("sending_video",),
                                       parse_mode=ParseMode.HTML),
            timeout=1800
        )   # if this fails too, the error is reported instead of silently "finishing"

    if pm_msg and pm_msg.document:
        file_id = pm_msg.document.file_id
    if file_id:
        try:
            await app_instance.send_document(chat_id=DESK_CHANNEL_ID, document=file_id,
                                             caption=f"🎬 Logs: {safe_cap}\nUser: <code>{target_user}</code>",
                                             parse_mode=ParseMode.HTML)
        except:
            pass
    return pm_msg

# ----------------------------- MAIN DRIVER -----------------------------
async def main_driver():
    global status_msg_id, app

    if SESSION_STRING:
        app = Client("worker_down", api_id=API_ID, api_hash=API_HASH,
                     session_string=SESSION_STRING,
                     workers=32, max_concurrent_transmissions=16, no_updates=True, in_memory=True)
    else:
        app = Client("worker_down", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
                     workers=32, max_concurrent_transmissions=16, no_updates=True, in_memory=True)
    await app.start()
    try:
        await app.get_chat(CHAT_ID)
    except:
        pass

    status_msg_id = int(TRIGGER_MSG_ID) if TRIGGER_MSG_ID else None
    if not status_msg_id:
        init_msg = await app.send_message(CHAT_ID, "⚙️ Worker running...")
        status_msg_id = init_msg.id

    await kill_all_other_notebooks()

    step_dl = "hardsub_download" if TASK_TYPE == "hardsub" else "compress_download"

    # video download starts immediately; the tiny subtitle/font files are fetched alongside
    video_task = asyncio.create_task(
        download_asset_robust(app, VIDEO_MSG_ID, os.path.join(WORK_DIR, "video.mkv"), step_dl))
    sub_file = font_path = None
    try:
        if TASK_TYPE == "hardsub":
            if SUB_MSG_ID and SUB_MSG_ID != "none":
                sub_file = await download_asset_robust(app, SUB_MSG_ID, os.path.join(WORK_DIR, "sub_raw"),
                                                       "sub", show_progress=False)
            if not sub_file or not os.path.exists(sub_file):
                raise Exception("Subtitles download failed.")
        if FONT_MSG_ID and FONT_MSG_ID != "none":
            fonts_dir = os.path.join(WORK_DIR, "fonts")
            os.makedirs(fonts_dir, exist_ok=True)
            font_path = await download_asset_robust(app, FONT_MSG_ID, os.path.join(fonts_dir, "custom_font.ttf"),
                                                    "font", show_progress=False)
    except Exception:
        video_task.cancel()
        raise
    video_file = await video_task
    if not video_file:
        raise Exception("Telegram video download failed.")

    duration, vid_height, fps = probe_video(video_file)
    gop = int(round(fps * 2)) if fps else 48          # 2 seconds worth of frames
    gop = max(12, min(gop, 250))

    base_name = "output"
    if RENAME and RENAME != "none":
        base_name = RENAME.rsplit('.', 1)[0] if '.' in RENAME else RENAME
    base_name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", base_name).strip()[:120] or "output"
    out_name = os.path.join(WORK_DIR, f"{base_name}.mp4")

    custom_font = bool(font_path and os.path.exists(font_path))
    font_name = get_font_name(font_path) if custom_font else "Arial"

    wm_file, has_watermark = None, False
    extracted_subs = []

    if TASK_TYPE == "hardsub":
        has_watermark = prepare_subtitle(sub_file, font_name, custom_font, os.path.join(WORK_DIR, "ready_sub.ass"),
                                        font_path=font_path)
        if WM_MSG_ID and WM_MSG_ID != "none" and not has_watermark:
            wm_file = await download_asset_robust(app, WM_MSG_ID, os.path.join(WORK_DIR, "watermark.png"),
                                                  "wm", show_progress=False)

    await app.stop()

    process_title = "Compressing" if TASK_TYPE == "compress" else "Encoding Hardsub"

    reso_clean = str(RESOLUTION).replace("p", "").replace("P", "").strip() if RESOLUTION else ""
    has_reso = reso_clean.isdigit()

    # CRF / bitrate caps: unchanged from your tuned values
    if TASK_TYPE == "hardsub":
        crf_val = "23"
        if reso_clean == "1080": max_rate, buf_size = "3000k", "4000k"
        elif reso_clean == "720": max_rate, buf_size = "1500k", "2000k"
        elif reso_clean == "480": max_rate, buf_size = "800k", "1200k"
        else: max_rate, buf_size = "2500k", "3500k"
    else:
        crf_val = "28"
        if reso_clean == "1080": max_rate, buf_size = "1400k", "2000k"
        elif reso_clean == "720": max_rate, buf_size = "850k", "1300k"
        elif reso_clean == "480": max_rate, buf_size = "500k", "800k"
        else: max_rate, buf_size = "1200k", "1800k"

    # -2 keeps width even; min() never upscales; trunc keeps height even (odd heights used to crash x264)
    scale_filter = (f"scale=-2:'min({reso_clean},trunc(ih/2)*2)'" if has_reso
                    else "scale=trunc(iw/2)*2:trunc(ih/2)*2")

    if TASK_TYPE == "compress":
        fire_and_forget_http(f"⚙️ <b>{process_title}</b>\n{get_process_bar(0)} [0.0%]")
        cmd_gpu, cmd_cpu = build_ffmpeg_cmds(video_file, out_name, crf_val, max_rate, buf_size, gop, vf=scale_filter)
        extract_task = asyncio.create_task(asyncio.to_thread(extract_embedded_subs_sync, video_file, base_name))
        await asyncio.to_thread(encode_with_fallback, cmd_gpu, cmd_cpu, duration, process_title, out_name)
        extracted_subs = await extract_task

    elif TASK_TYPE == "hardsub":
        vf_filter = "subtitles='ready_sub.ass':charenc=UTF-8"
        if custom_font:
            vf_filter += ":fontsdir=fonts"
        v_filter = f"{scale_filter},{vf_filter}"
        overlay_coord = "W-w-15:15" if WM_POS == "right" else "15:15"

        fire_and_forget_http(f"⚙️ <b>{process_title}</b>\n{get_process_bar(0)} [0.0%]")

        if wm_file and os.path.exists(wm_file):
            complex_f = f"[0:v]{v_filter}[vsub];[1:v]scale=-1:min(ih*0.08\\,80)[wm];[vsub][wm]overlay={overlay_coord},format=yuv420p[vout]"
            cmd_gpu, cmd_cpu = build_ffmpeg_cmds(video_file, out_name, crf_val, max_rate, buf_size, gop,
                                                 complex_f=complex_f, wm_file=wm_file)
        else:
            cmd_gpu, cmd_cpu = build_ffmpeg_cmds(video_file, out_name, crf_val, max_rate, buf_size, gop, vf=v_filter)

        await asyncio.to_thread(encode_with_fallback, cmd_gpu, cmd_cpu, duration, process_title, out_name)

    if SESSION_STRING:
        app = Client("worker_up", api_id=API_ID, api_hash=API_HASH,
                     session_string=SESSION_STRING,
                     workers=32, max_concurrent_transmissions=16, no_updates=True, in_memory=True)
    else:
        app = Client("worker_up", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN,
                     workers=32, max_concurrent_transmissions=16, no_updates=True, in_memory=True)
    await app.start()
    try:
        await app.get_chat(CHAT_ID)
    except:
        pass

    fire_and_forget_http(f"📤 <b>Sending Video</b>\n{get_send_bar(0)} [0.0%]")
    caption = os.path.basename(out_name)
    await deliver_video_asset(app, CHAT_ID, USER_ID, out_name, caption)

    if TASK_TYPE == "compress" and extracted_subs:
        for sub_f in extracted_subs:
            try:
                await app.send_document(chat_id=USER_ID, document=sub_f, caption="📄 Extracted Subtitles (.ass)")
            except:
                try:
                    await app.send_document(chat_id=CHAT_ID, document=sub_f, caption="📄 Extracted Subtitles (.ass)")
                except:
                    pass

    try:
        await app.delete_messages(CHAT_ID, status_msg_id)
    except:
        pass
    await app.stop()
    sys.exit(0)

if __name__ == "__main__":
    try:
        asyncio.run(main_driver())
    except Exception as outer_err:
        tb_data = traceback.format_exc()
        report_critical_failure(tb_data)
        sys.exit(1)
