import os, sys, site, importlib, importlib.util, importlib.metadata, traceback
import time, asyncio, subprocess, json, gc, re, base64, requests, html, shutil, threading

# ----------------------------- EARLY PATH & FONT SETUP -----------------------------
def _ensure_user_site_path():
    user_site = site.getusersitepackages()
    if os.path.exists(user_site) and user_site not in sys.path:
        sys.path.insert(0, user_site)

_ensure_user_site_path()

WORK_DIR = "/kaggle/working" if os.path.exists("/kaggle") else "/tmp/kavvle_work"
FONTS_DIR = os.path.join(WORK_DIR, "fonts")
os.makedirs(WORK_DIR, exist_ok=True)
os.makedirs(FONTS_DIR, exist_ok=True)
os.chdir(WORK_DIR)

# Install essential Unicode & Symbol Fonts to prevent "Box" glyph errors in Watermarks
def setup_rich_fonts():
    try:
        # Download reliable unicode/emoji/symbol font into local fonts dir
        symbol_font_url = "https://github.com/googlefonts/noto-fonts/raw/main/hinted/ttf/NotoSansMath/NotoSansMath-Regular.ttf"
        r = requests.get(symbol_font_url, timeout=10)
        if r.status_code == 200:
            with open(os.path.join(FONTS_DIR, "NotoSansMath.ttf"), "wb") as f:
                f.write(r.content)
    except: pass

setup_rich_fonts()

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
            text = f"❌ <b>Processing Engine Error Traceback:</b>\n\n<pre><code class='language-python'>{html.escape(error_msg[:3500])}</code></pre>"
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

# ----------------------------- DEPENDENCIES -----------------------------
def ensure_deps():
    need = []
    for mod, pip_name in [("pyrogram", "pyrogram"), ("tgcrypto", "tgcrypto"),
                          ("pysubs2", "pysubs2"), ("fontTools", "fonttools")]:
        if importlib.util.find_spec(mod) is None:
            need.append(pip_name)
    if need:
        cmd = [sys.executable, "-m", "pip", "install", "-q", "--user", "--no-cache-dir", *need]
        try: subprocess.run(cmd, check=True)
        except: subprocess.run(cmd, check=False)
        _ensure_user_site_path()
        importlib.invalidate_caches()

ensure_deps()

import pyrogram.utils, pysubs2
from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from fontTools.ttLib import TTFont

pyrogram.utils.get_peer_type = lambda p: "channel" if str(p).startswith("-100") else "chat" if str(p).startswith("-") else "user"

# ----------------------------- PROGRESS UI -----------------------------
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
    try: requests.post(url, json=payload, timeout=5)
    except: pass

def fire_and_forget_http(text):
    threading.Thread(target=_sync_http_edit, args=(text,), daemon=True).start()

def prog(current, total, step_name):
    global last_time, start_time
    now = time.time()
    if start_time == 0:
        start_time = now
        last_time = now
        return
    if now - last_time > 8 or current >= total:
        elapsed = now - start_time
        speed = current / elapsed if elapsed > 0 else 0
        speed_mb = (speed / 1024) / 1024
        percent = (current / total) * 100 if total > 0 else 0
        if "download" in step_name:
            text = f"📥 <b>Downloading Video</b>\n<code>{get_download_bar(percent)}</code> [{percent:.1f}%]\n🚀 Speed: <b>{speed_mb:.2f} MB/s</b>\n📦 {current/1048576:.1f}MB / {total/1048576:.1f}MB"
        else:
            text = f"📤 <b>Sending Video</b>\n<code>{get_send_bar(percent)}</code> [{percent:.1f}%]\n🚀 Speed: <b>{speed_mb:.2f} MB/s</b>\n📦 {current/1048576:.1f}MB / {total/1048576:.1f}MB"
        fire_and_forget_http(text)
        last_time = now

# ----------------------------- UTILITIES -----------------------------
def get_duration(video_path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
                            capture_output=True, text=True, timeout=10)
        return float(r.stdout.strip()) if r.stdout.strip() else 0.0
    except: return 0.0

def get_font_name(font_path):
    try:
        font = TTFont(font_path)
        for record in font['name'].names:
            if record.nameID == 4: return record.toUnicode()
    except: pass
    return "Arial"

def is_ass_format(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(4000)
        return bool(re.search(r'\[Script Info\]|\[V4\+?\s*Styles\]|\[Events\]', head, re.IGNORECASE))
    except: return False

def convert_to_clean_ass(input_sub, output_ass):
    try:
        subs = pysubs2.load(input_sub)
        subs.styles["Default"] = pysubs2.SSAStyle(fontname="Arial", fontsize=24,
            primarycolor=pysubs2.Color(255, 255, 255), outlinecolor=pysubs2.Color(0, 0, 0),
            outline=2, shadow=1, marginl=20, marginr=20, marginv=15)
        for line in subs:
            line.style = "Default"
            line.text = re.sub(r'<[^>]+>', '', line.text).replace('\r', '').replace('\n', '\\N').strip()
        subs.save(output_ass)
    except: pass

# ----------------------------- DOWNLOADS -----------------------------
async def download_asset_robust(app_instance, val, output_path, step_name):
    if not val or val == "none": return None
    try:
        reset_prog()
        if str(val).isdigit():
            msg = await app_instance.get_messages(DESK_CHANNEL_ID, int(val))
            res = await asyncio.wait_for(
                app_instance.download_media(msg, file_name=output_path, progress=prog, progress_args=(step_name,)),
                timeout=2400
            )
        else:
            res = await asyncio.wait_for(
                app_instance.download_media(val, file_name=output_path, progress=prog, progress_args=(step_name,)),
                timeout=2400
            )
        if res and os.path.exists(res) and os.path.getsize(res) > 500:
            return res
        raise Exception("Downloaded file is empty.")
    except Exception as e:
        raise Exception(f"Download Error on '{step_name}': {e}")

# ----------------------------- HARDWARE-ACCELERATED ENCODER -----------------------------
def run_ffmpeg_sync(cmd, duration, process_title):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    last_edit = time.time()
    log_tail = []
    for line in proc.stdout:
        line = line.strip()
        if not line: continue
        if "out_time_us=" not in line and "frame=" not in line:
            log_tail.append(line)
            if len(log_tail) > 15: log_tail.pop(0)
        if "out_time_us=" in line and duration > 0:
            now = time.time()
            if now - last_edit > 8:
                try:
                    us = int(line.split("=")[1])
                    percent = min((us / 1_000_000.0 / duration) * 100, 100.0)
                    fire_and_forget_http(f"⚙️ <b>{process_title}</b>\n<code>{get_process_bar(percent)}</code> [{percent:.1f}%]")
                except: pass
                last_edit = now
    proc.wait()
    return proc.returncode, log_tail

def encode_with_fallback(base_cmd_gpu, base_cmd_cpu, duration, title):
    if HW_MODE == "gpu" and base_cmd_gpu:
        rc, log = run_ffmpeg_sync(base_cmd_gpu, duration, title + " [GPU-NVENC]")
        if rc == 0: return
        fire_and_forget_http("⚠️ GPU fallback triggered, switching to Multi-Threaded CPU...")
    rc, log = run_ffmpeg_sync(base_cmd_cpu, duration, title + " [CPU Fast]")
    if rc != 0:
        raise Exception("FFmpeg processing failure:\n" + "\n".join(log[-8:]))

# ----------------------------- UPLOAD ENGINE -----------------------------
async def deliver_video_asset(app_instance, chat_id, target_user, file_path, caption):
    if not os.path.exists(file_path) or os.path.getsize(file_path) < 1000:
        raise Exception("Processed output video file missing or empty!")
    thumb_path = "thumb.jpg"
    try:
        subprocess.run(["ffmpeg", "-y", "-i", file_path, "-ss", "00:00:01", "-vframes", "1", thumb_path], capture_output=True, timeout=10)
    except: pass
    if not os.path.exists(thumb_path): thumb_path = None

    reset_prog()
    pm_msg = None
    try:
        pm_msg = await asyncio.wait_for(
            app_instance.send_video(chat_id=target_user, video=file_path, supports_streaming=True, caption=caption, thumb=thumb_path, progress=prog, progress_args=("sending_video",)),
            timeout=2400
        )
    except:
        pm_msg = await asyncio.wait_for(
            app_instance.send_video(chat_id=chat_id, video=file_path, supports_streaming=True, caption=f"⚠️ <a href='tg://user?id={target_user}'>User</a>, Video Ready:\n\n{caption}", thumb=thumb_path, progress=prog, progress_args=("sending_video",), parse_mode=ParseMode.HTML),
            timeout=2400
        )

    if pm_msg and pm_msg.video:
        try:
            await app_instance.send_video(chat_id=DESK_CHANNEL_ID, video=pm_msg.video.file_id, caption=f"🎬 Logs: {caption}\nUser: `{target_user}`")
        except: pass
    return pm_msg

# ----------------------------- MAIN DRIVER -----------------------------
async def main_driver():
    global status_msg_id, app

    client_params = {
        "name": "worker_unified", "api_id": API_ID, "api_hash": API_HASH,
        "workers": 24, "max_concurrent_transmissions": 12, "no_updates": True, "in_memory": True
    }
    if SESSION_STRING: client_params["session_string"] = SESSION_STRING
    else: client_params["bot_token"] = BOT_TOKEN

    app = Client(**client_params)
    await app.start()
    try: await app.get_chat(CHAT_ID)
    except: pass

    status_msg_id = int(TRIGGER_MSG_ID) if TRIGGER_MSG_ID else None
    if not status_msg_id:
        init_msg = await app.send_message(CHAT_ID, "⚙️ Processing worker initialized...")
        status_msg_id = init_msg.id

    step_dl = "hardsub_download" if TASK_TYPE == "hardsub" else "compress_download"
    video_file = await download_asset_robust(app, VIDEO_MSG_ID, os.path.join(WORK_DIR, "video.mkv"), step_dl)
    if not video_file: raise Exception("Telegram video download failed.")

    duration = get_duration(video_file)
    base_name = RENAME.rsplit('.', 1)[0] if RENAME and RENAME != "none" else "output"
    out_name = os.path.join(WORK_DIR, f"{base_name}.mp4")

    font_name = "Arial"
    if FONT_MSG_ID and FONT_MSG_ID != "none":
        font_path = await download_asset_robust(app, FONT_MSG_ID, os.path.join(FONTS_DIR, "custom_font.ttf"), step_dl)
        if font_path and os.path.exists(font_path):
            font_name = get_font_name(font_path)

    sub_file, wm_file, has_watermark = None, None, False
    extracted_subs = []

    if TASK_TYPE == "hardsub":
        if SUB_MSG_ID and SUB_MSG_ID != "none":
            sub_file = await download_asset_robust(app, SUB_MSG_ID, os.path.join(WORK_DIR, "sub_raw"), "hardsub_download")
        if not sub_file or not os.path.exists(sub_file):
            raise Exception("Subtitles download failed or missing.")
            
        ready_sub_path = os.path.join(WORK_DIR, "ready_sub.ass")
        
        # PRESERVE RAW FORMAT & STYLES (Supports Watermark Unicode & colors like 𝙰𝚂𝙸☠)
        if sub_file.lower().endswith('.ass') or is_ass_format(sub_file):
            try:
                with open(sub_file, 'r', encoding='utf-8', errors='ignore') as f:
                    ass_content = f.read()
            except:
                with open(sub_file, 'r', encoding='latin-1', errors='ignore') as f:
                    ass_content = f.read()
            
            if any(word in ass_content.lower() for word in ["logo", "watermark", "cr", "credit"]):
                has_watermark = True
                
            # If custom font provided, map Default styles cleanly without destroying watermark styles
            if FONT_MSG_ID and FONT_MSG_ID != "none":
                lines = ass_content.splitlines()
                new_lines = []
                for line in lines:
                    if line.strip().startswith("Style:") and "Default" in line:
                        parts = line.split(",", 2)
                        if len(parts) >= 3:
                            line = f"{parts[0]},{font_name},{parts[2]}"
                    new_lines.append(line)
                with open(ready_sub_path, "w", encoding='utf-8') as f:
                    f.write("\n".join(new_lines))
            else:
                shutil.copy(sub_file, ready_sub_path)
        else:
            try: subs = pysubs2.load(sub_file, encoding='utf-8')
            except: subs = pysubs2.load(sub_file, encoding='latin-1')
            new_subs = pysubs2.SSAFile()
            new_subs.styles["Default"] = pysubs2.SSAStyle(fontname=font_name, fontsize=24,
                primarycolor=pysubs2.Color(255, 255, 255), outlinecolor=pysubs2.Color(0, 0, 0),
                outline=2, shadow=1, marginl=20, marginr=20, marginv=15)
            for line in subs:
                clean_text = line.text.replace('\r', '').replace('\n', '\\N').strip()
                if clean_text:
                    new_subs.append(pysubs2.SSAEvent(start=line.start, end=line.end, text=clean_text, style="Default"))
            new_subs.save(ready_sub_path)

        if WM_MSG_ID and WM_MSG_ID != "none" and not has_watermark:
            wm_file = await download_asset_robust(app, WM_MSG_ID, os.path.join(WORK_DIR, "watermark.png"), "hardsub_download")

    process_title = "Compressing Video" if TASK_TYPE == "compress" else "Encoding Hardsub"

    if TASK_TYPE == "compress":
        await update_http_status("⚙️ <b>Scanning internal subtitles...</b>")
        cmd_probe = ["ffprobe", "-v", "error", "-select_streams", "s",
                     "-show_entries", "stream=index,codec_name", "-of", "csv=p=0", video_file]
        res_probe = subprocess.run(cmd_probe, capture_output=True, text=True)
        if res_probe.stdout.strip():
            streams = res_probe.stdout.strip().split('\n')
            for i, st in enumerate(streams):
                if not st: continue
                parts = st.split(',')
                s_idx = parts[0]
                s_codec = parts[1].strip()
                if s_codec in ['ass', 'ssa']:
                    ass_out = os.path.join(WORK_DIR, f"{base_name}_track_{i+1}.ass")
                    subprocess.run(["ffmpeg", "-y", "-i", video_file, "-map", f"0:{s_idx}", ass_out])
                    if os.path.exists(ass_out) and os.path.getsize(ass_out) > 0:
                        extracted_subs.append(ass_out)
                elif s_codec in ['subrip', 'srt', 'webvtt']:
                    temp_ext = ".srt" if s_codec == 'subrip' else ".vtt"
                    temp_sub = os.path.join(WORK_DIR, f"temp_{i+1}{temp_ext}")
                    subprocess.run(["ffmpeg", "-y", "-i", video_file, "-map", f"0:{s_idx}", temp_sub])
                    if os.path.exists(temp_sub) and os.path.getsize(temp_sub) > 0:
                        ass_out = os.path.join(WORK_DIR, f"{base_name}_track_{i+1}.ass")
                        convert_to_clean_ass(temp_sub, ass_out)
                        if os.path.exists(ass_out):
                            extracted_subs.append(ass_out)

        reso_clean = str(RESOLUTION).replace("p", "").replace("P", "").strip() if RESOLUTION else ""
        scale_filter = f"scale=-2:min({reso_clean}\\,ih)" if reso_clean and reso_clean.lower() != "none" else "scale='trunc(iw/2)*2:trunc(ih/2)*2'"

        await update_http_status(f"⚙️ <b>{process_title}</b>\n<code>{get_process_bar(0)}</code> [0.0%]")

        cmd_cpu = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-vf", scale_filter,
                   "-map", "0:v", "-map", "0:a?", "-c:v", "libx264", "-preset", "ultrafast",
                   "-crf", "24", "-pix_fmt", "yuv420p", "-threads", "0", "-c:a", "aac", "-b:a", "128k",
                   "-movflags", "+faststart", out_name]
        
        # High-Speed Optimized NVENC Command (Fixed rate-control)
        cmd_gpu = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-vf", scale_filter,
                   "-map", "0:v", "-map", "0:a?", "-c:v", "h264_nvenc", "-preset", "p1",
                   "-tune", "ull", "-rc:v", "vbr", "-cq", "25", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                   "-movflags", "+faststart", out_name]

        await asyncio.to_thread(encode_with_fallback, cmd_gpu, cmd_cpu, duration, process_title)

    elif TASK_TYPE == "hardsub":
        # Point libass to fonts directory to parse special Unicode characters & glyphs
        vf_filter = f"subtitles='ready_sub.ass':fontsdir='{FONTS_DIR}':charenc=UTF-8"
        v_filter = f"scale='trunc(iw/2)*2:trunc(ih/2)*2',{vf_filter}"
        overlay_coord = "W-w-15:15" if WM_POS == "right" else "15:15"

        await update_http_status(f"⚙️ <b>{process_title}</b>\n<code>{get_process_bar(0)}</code> [0.0%]")

        if wm_file and os.path.exists(wm_file):
            complex_f = f"[0:v]{v_filter}[vsub];[1:v]scale=200:-1[wm];[vsub][wm]overlay={overlay_coord}"
            cmd_cpu = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-i", wm_file,
                       "-filter_complex", complex_f, "-c:v", "libx264", "-preset", "ultrafast",
                       "-crf", "22", "-pix_fmt", "yuv420p", "-threads", "0", "-c:a", "aac",
                       "-movflags", "+faststart", out_name]
            cmd_gpu = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-i", wm_file,
                       "-filter_complex", complex_f, "-c:v", "h264_nvenc", "-preset", "p1",
                       "-tune", "ull", "-rc:v", "vbr", "-cq", "23", "-pix_fmt", "yuv420p", "-c:a", "aac",
                       "-movflags", "+faststart", out_name]
        else:
            cmd_cpu = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-vf", v_filter,
                       "-c:v", "libx264", "-preset", "ultrafast", "-crf", "22", "-pix_fmt", "yuv420p",
                       "-threads", "0", "-c:a", "aac", "-movflags", "+faststart", out_name]
            cmd_gpu = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-vf", v_filter,
                       "-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ull", "-rc:v", "vbr",
                       "-cq", "23", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", out_name]

        await asyncio.to_thread(encode_with_fallback, cmd_gpu, cmd_cpu, duration, process_title)

    await update_http_status(f"📤 <b>Sending Video</b>\n<code>{get_send_bar(0)}</code> [0.0%]")
    caption = f"✅ <b>Process Completed!</b>\n<code>{os.path.basename(out_name)}</code>"
    await deliver_video_asset(app, CHAT_ID, USER_ID, out_name, caption)

    if TASK_TYPE == "compress" and extracted_subs:
        for sub_f in extracted_subs:
            try: await app.send_document(chat_id=USER_ID, document=sub_f, caption="📄 Extracted Clean Subtitles (.ass)")
            except:
                try: await app.send_document(chat_id=CHAT_ID, document=sub_f, caption="📄 Extracted Clean Subtitles (.ass)")
                except: pass

    try: await app.delete_messages(CHAT_ID, status_msg_id)
    except: pass
    await app.stop()
    sys.exit(0)

if __name__ == "__main__":
    try:
        asyncio.run(main_driver())
    except Exception as outer_err:
        tb_data = traceback.format_exc()
        report_critical_failure(tb_data)
        sys.exit(1)
