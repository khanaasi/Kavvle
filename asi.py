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
os.makedirs(os.path.join(WORK_DIR, "fonts"), exist_ok=True)
os.chdir(WORK_DIR)

# Auto Install FFmpeg on Kaggle environment
if shutil.which("ffmpeg") is None:
    subprocess.run("apt-get update && apt-get install -y ffmpeg", shell=True)

# Globals
last_time = 0
start_time = 0
status_msg_id = None
app = None

# Render Bot Injects Base64 Config Here
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
            raise RuntimeError("CONFIG_B64 missing. Render didn't inject config.")
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
    SESSION_STRING = CFG.get("session_string", None)
except Exception:
    tb = traceback.format_exc()
    report_critical_failure(tb)
    sys.exit(1)

# ----------------------------- DEPENDENCY SYSTEM -----------------------------
def ensure_deps():
    need = []
    for mod, pip_name in [("pyrogram", "pyrogram"), ("tgcrypto", "tgcrypto"),
                          ("pysubs2", "pysubs2"), ("fontTools", "fonttools")]:
        if importlib.util.find_spec(mod) is None:
            need.append(pip_name)
    if need:
        cmd = [sys.executable, "-m", "pip", "install", "-q", "--user", "--no-cache-dir", *need]
        subprocess.run(cmd, check=False)
        _ensure_user_site_path()
        importlib.invalidate_caches()

ensure_deps()

import pyrogram.utils, pysubs2
from pyrogram import Client
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from fontTools.ttLib import TTFont

try:
    pyrogram.utils.get_peer_type = lambda p: "channel" if str(p).startswith("-100") else "chat" if str(p).startswith("-") else "user"
except:
    pass

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
    if not status_msg_id: return
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {"chat_id": CHAT_ID, "message_id": int(status_msg_id), "text": text, "parse_mode": "HTML"}
    try: requests.post(url, json=payload, timeout=5)
    except: pass

def fire_and_forget_http(text):
    threading.Thread(target=_sync_http_edit, args=(text,), daemon=True).start()

async def update_http_status(text):
    await asyncio.to_thread(_sync_http_edit, text)

# --- FLOODWAIT PROOF PROGRESS CALLBACK ---
async def prog(current, total, step_name):
    global last_time, start_time
    now = time.time()
    if start_time == 0:
        start_time = now; last_time = now
        return
    if now - last_time > 8 or current >= total:
        elapsed = now - start_time
        speed = current / elapsed if elapsed > 0 else 0
        speed_mb = (speed / 1024) / 1024
        percent = (current / total) * 100 if total > 0 else 0
        
        if "download" in step_name:
            text = f"📥 <b>Downloading Asset</b>\n<code>{get_download_bar(percent)}</code> [{percent:.1f}%]\n🚀 Speed: <b>{speed_mb:.2f} MB/s</b>\n📦 {current/1048576:.1f}MB / {total/1048576:.1f}MB"
        else:
            text = f"📤 <b>Sending Video</b>\n<code>{get_send_bar(percent)}</code> [{percent:.1f}%]\n🚀 Speed: <b>{speed_mb:.2f} MB/s</b>\n📦 {current/1048576:.1f}MB / {total/1048576:.1f}MB"
        
        asyncio.create_task(update_http_status(text))
        last_time = now

# ----------------------------- UTILITY FUNCTIONS -----------------------------
def get_video_info(video_path):
    cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration", "-of", "default=noprint_wrappers=1", video_path]
    width, height, duration = 1280, 720, 0.0
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        for line in res.stdout.strip().split("\n"):
            if "=" not in line: continue
            k, v = line.split("=", 1)
            if k == "width": width = int(v)
            elif k == "height": height = int(v)
            elif k == "duration": duration = float(v)
    except: pass
    return width, height, duration

def get_font_name(font_path):
    try:
        font = TTFont(font_path)
        for record in font['name'].names:
            if record.nameID == 4: return record.toUnicode()
    except: pass
    return "Arial"

def is_ass_format(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f: head = f.read(4000)
        return bool(re.search(r'\[Script Info\]|\[V4\+?\s*Styles\]|\[Events\]', head, re.IGNORECASE))
    except: return False

def convert_to_clean_ass(input_sub, output_ass):
    try:
        subs = pysubs2.load(input_sub)
        subs.styles["Default"] = pysubs2.SSAStyle(fontname="Arial", fontsize=24, primarycolor=pysubs2.Color(255, 255, 255), outlinecolor=pysubs2.Color(0, 0, 0), outline=2, shadow=1, marginl=20, marginr=20, marginv=15)
        for line in subs:
            line.style = "Default"
            line.text = re.sub(r'<[^>]+>', '', re.sub(r'\{[^}]+\}', '', line.text)).replace('\r', '').replace('\n', '\\N').strip()
        subs.save(output_ass)
    except: pass

# ----------------------------- KAGGLE NOTEBOOK CLEANUP -----------------------------
async def kill_all_other_notebooks():
    username = os.environ.get("KAGGLE_USERNAME", "").strip()
    api_key = os.environ.get("KAGGLE_KEY", "").strip()
    current_kernel = os.environ.get("KAGGLE_KERNEL_NAME", "").strip()

    if not username or not api_key: return

    os.environ["KAGGLE_USERNAME"] = username
    os.environ["KAGGLE_KEY"] = api_key

    try:
        proc = await asyncio.create_subprocess_exec("kaggle", "kernels", "list", "--user", username, "--csv", stdout=asyncio.subprocess.PIPE)
        stdout, _ = await proc.communicate()
        if proc.returncode == 0:
            lines = stdout.decode().strip().split("\n")
            for line in lines[1:]:
                parts = line.split(",")
                if parts:
                    ref = parts[0].strip()
                    if current_kernel and ref != current_kernel:
                        del_proc = await asyncio.create_subprocess_exec("kaggle", "kernels", "delete", "-k", ref)
                        await del_proc.communicate()
    except: pass

# ----------------------------- DOWNLOAD ENGINE -----------------------------
async def download_asset_robust(app_instance, val, output_path, step_name):
    if not val or val == "none": return None
    try:
        media = None
        if str(val).isdigit():
            msg = await app_instance.get_messages(DESK_CHANNEL_ID, int(val))
            media = msg.document or msg.video or msg.audio or msg.photo
        else:
            media = val
        if not media: raise Exception("No valid downloadable media.")
        
        reset_prog()
        result = await app_instance.download_media(media, file_name=output_path, progress=prog, progress_args=(step_name,))
        if result and os.path.exists(result): return result
    except: pass
    return None

# ----------------------------- ENCODING ENGINE -----------------------------
async def run_ffmpeg_async(cmd, duration, process_title):
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    last_edit = time.time()
    log_tail = []
    
    while True:
        line = await proc.stdout.readline()
        if not line: break
        line_str = line.decode('utf-8', errors='ignore').strip()
        
        if line_str and "out_time_us=" not in line_str and "frame=" not in line_str:
            log_tail.append(line_str)
            if len(log_tail) > 15: log_tail.pop(0)
            
        if "out_time_us=" in line_str and duration > 0:
            now = time.time()
            if now - last_edit > 8:
                try:
                    percent = min((int(line_str.split("=")[1]) / 1000000.0 / duration) * 100, 100.0)
                    asyncio.create_task(update_http_status(f"⚙️ <b>{process_title}</b>\n<code>{get_process_bar(percent)}</code> [{percent:.1f}%]"))
                except: pass
                last_edit = now

    await proc.wait()
    return proc.returncode, log_tail

async def encode_with_fallback(cmd_gpu, cmd_cpu, duration, title):
    await update_http_status(f"⚙️ <b>{title} (GPU)</b>\nStarting hardware acceleration...")
    rc, log = await run_ffmpeg_async(cmd_gpu, duration, f"{title} (GPU)")
    if rc == 0: return
    
    await update_http_status("⚠️ <b>GPU Falied. Switching to CPU Fallback...</b>")
    rc, log = await run_ffmpeg_async(cmd_cpu, duration, f"{title} (CPU)")
    if rc != 0: raise Exception("FFmpeg crashed on both GPU and CPU.\n" + "\n".join(log[-8:]))

# ----------------------------- MAIN DRIVER -----------------------------
async def main_driver():
    global status_msg_id, app

    if SESSION_STRING:
        app = Client("worker_down", api_id=API_ID, api_hash=API_HASH, session_string=SESSION_STRING, workers=32, max_concurrent_transmissions=20, no_updates=True, in_memory=True)
    else:
        app = Client("worker_down", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workers=32, max_concurrent_transmissions=20, no_updates=True, in_memory=True)
    
    await app.start()
    
    status_msg_id = int(TRIGGER_MSG_ID) if TRIGGER_MSG_ID else None
    if not status_msg_id:
        init_msg = await app.send_message(CHAT_ID, "⚙️ Worker Booting...")
        status_msg_id = init_msg.id

    await kill_all_other_notebooks()

    step_dl = "hardsub_download" if TASK_TYPE == "hardsub" else "compress_download"
    video_file = await download_asset_robust(app, VIDEO_MSG_ID, os.path.join(WORK_DIR, "video.mkv"), step_dl)
    if not video_file: raise Exception("Telegram video download failed.")

    width, height, duration = get_video_info(video_file)
    if duration <= 0: duration = 1.0

    base_name = "output"
    if RENAME and RENAME != "none": base_name = RENAME.rsplit('.', 1)[0]
    out_name = os.path.join(WORK_DIR, f"{base_name}.mp4")

    font_name = "Arial"
    if FONT_MSG_ID and FONT_MSG_ID != "none":
        font_path = await download_asset_robust(app, FONT_MSG_ID, os.path.join(WORK_DIR, "fonts", "custom_font.ttf"), step_dl)
        if font_path and os.path.exists(font_path): font_name = get_font_name(font_path)

    sub_file, wm_file, has_watermark = None, None, False
    extracted_subs = []

    if TASK_TYPE == "hardsub":
        if SUB_MSG_ID and SUB_MSG_ID != "none":
            sub_file = await download_asset_robust(app, SUB_MSG_ID, os.path.join(WORK_DIR, "sub_raw"), "hardsub_download")
        if not sub_file or not os.path.exists(sub_file): raise Exception("Subtitles download failed.")
        
        ready_sub_path = os.path.join(WORK_DIR, "ready_sub.ass")
        if sub_file.lower().endswith('.ass') or is_ass_format(sub_file):
            try:
                with open(sub_file, 'r', encoding='utf-8', errors='ignore') as f: ass_content = f.read()
            except:
                with open(sub_file, 'r', encoding='latin-1', errors='ignore') as f: ass_content = f.read()
            
            if any(word in ass_content.lower() for word in ["logo", "watermark", "cr", "credit"]): has_watermark = True
            
            if FONT_MSG_ID and FONT_MSG_ID != "none":
                lines = ass_content.splitlines()
                new_lines = []
                for line in lines:
                    if line.strip().startswith("Style:"):
                        parts = line.split(",", 2)
                        if len(parts) >= 3: line = f"{parts[0]},{font_name},{parts[2]}"
                    new_lines.append(line)
                with open(ready_sub_path, "w", encoding='utf-8') as f: f.write("\n".join(new_lines))
            else:
                shutil.copy(sub_file, ready_sub_path)
        else:
            try: subs = pysubs2.load(sub_file, encoding='utf-8')
            except: subs = pysubs2.load(sub_file, encoding='latin-1')
            new_subs = pysubs2.SSAFile()
            new_subs.styles["Default"] = pysubs2.SSAStyle(fontname=font_name, fontsize=24, primarycolor=pysubs2.Color(255, 255, 255), outlinecolor=pysubs2.Color(0, 0, 0), outline=2, shadow=1, marginl=20, marginr=20, marginv=15)
            for line in subs:
                clean_text = re.sub(r'<[^>]+>', '', re.sub(r'\{[^}]+\}', '', line.text)).replace('\r', '').replace('\n', '\\N').strip()
                if clean_text: new_subs.append(pysubs2.SSAEvent(start=line.start, end=line.end, text=clean_text, style="Default"))
            new_subs.save(ready_sub_path)

        if WM_MSG_ID and WM_MSG_ID != "none" and not has_watermark:
            wm_file = await download_asset_robust(app, WM_MSG_ID, os.path.join(WORK_DIR, "watermark.png"), "hardsub_download")

    process_title = "Compressing" if TASK_TYPE == "compress" else "Encoding Hardsub"

    # Rate logic from studio.py
    reso_clean = str(RESOLUTION).replace("p", "").replace("P", "").strip() if RESOLUTION else ""
    if reso_clean == "1080": max_rate, buf_size = "1400k", "2000k"
    elif reso_clean == "720": max_rate, buf_size = "850k", "1300k"
    elif reso_clean == "480": max_rate, buf_size = "500k", "800k"
    else: max_rate, buf_size = "1200k", "1800k"

    if TASK_TYPE == "compress":
        await update_http_status("⚙️ <b>Extracting Subtitles...</b>")
        cmd_probe = ["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries", "stream=index,codec_name", "-of", "csv=p=0", video_file]
        res_probe = subprocess.run(cmd_probe, capture_output=True, text=True)
        if res_probe.stdout.strip():
            for i, st in enumerate(res_probe.stdout.strip().split('\n')):
                if not st: continue
                s_idx, s_codec = st.split(',')[0], st.split(',')[1].strip()
                if s_codec in ['ass', 'ssa', 'subrip', 'srt', 'webvtt']:
                    temp_ext = ".srt" if s_codec == 'subrip' else ".vtt" if s_codec == 'webvtt' else ".ass"
                    temp_sub = os.path.join(WORK_DIR, f"temp_{i}{temp_ext}")
                    subprocess.run(["ffmpeg", "-y", "-i", video_file, "-map", f"0:{s_idx}", temp_sub])
                    if os.path.exists(temp_sub) and os.path.getsize(temp_sub) > 0:
                        ass_out = os.path.join(WORK_DIR, f"{base_name}_track_{i+1}.ass")
                        convert_to_clean_ass(temp_sub, ass_out) if not temp_sub.endswith('.ass') else shutil.copy(temp_sub, ass_out)
                        if os.path.exists(ass_out): extracted_subs.append(ass_out)

        scale_filter = f"scale=-2:min({reso_clean}\\,ih)" if reso_clean and reso_clean != "none" else "scale='trunc(iw/2)*2:trunc(ih/2)*2'"
        
        cmd_gpu = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-vf", scale_filter, "-map", "0:v", "-map", "0:a?", "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "28", "-maxrate", max_rate, "-bufsize", buf_size, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out_name]
        cmd_cpu = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-vf", scale_filter, "-map", "0:v", "-map", "0:a?", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-maxrate", max_rate, "-bufsize", buf_size, "-pix_fmt", "yuv420p", "-threads", "0", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out_name]

        await encode_with_fallback(cmd_gpu, cmd_cpu, duration, process_title)

    elif TASK_TYPE == "hardsub":
        vf_filter = "subtitles='ready_sub.ass':charenc=UTF-8"
        if FONT_MSG_ID and FONT_MSG_ID != "none": vf_filter += ":fontsdir=fonts"
        scale_filter = f"scale=-2:min({reso_clean}\\,ih)" if reso_clean and reso_clean != "none" else "scale='trunc(iw/2)*2:trunc(ih/2)*2'"
        v_filter = f"{scale_filter},{vf_filter}"
        overlay_coord = "W-w-15:15" if WM_POS == "right" else "15:15"

        if wm_file and os.path.exists(wm_file):
            complex_f = f"[0:v]{v_filter}[vsub];[1:v]scale=-1:min(ih*0.08\\,80)[wm];[vsub][wm]overlay={overlay_coord}:format=yuv420p"
            cmd_gpu = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-i", wm_file, "-filter_complex", complex_f, "-map", "0:a?", "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "28", "-maxrate", max_rate, "-bufsize", buf_size, "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out_name]
            cmd_cpu = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-i", wm_file, "-filter_complex", complex_f, "-map", "0:a?", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-maxrate", max_rate, "-bufsize", buf_size, "-threads", "0", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out_name]
        else:
            cmd_gpu = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-vf", v_filter, "-map", "0:a?", "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "28", "-maxrate", max_rate, "-bufsize", buf_size, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out_name]
            cmd_cpu = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-vf", v_filter, "-map", "0:a?", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "28", "-maxrate", max_rate, "-bufsize", buf_size, "-pix_fmt", "yuv420p", "-threads", "0", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", out_name]

        await encode_with_fallback(cmd_gpu, cmd_cpu, duration, process_title)

    if not os.path.exists(out_name) or os.path.getsize(out_name) < 1000:
        raise Exception("Output video is missing or invalid.")

    # Upload section with thumbnail
    thumb = "thumb.jpg"
    subprocess.run(["ffmpeg", "-y", "-i", out_name, "-ss", "00:00:01", "-vframes", "1", thumb], capture_output=True)
    thumb = thumb if os.path.exists(thumb) else None
    
    reset_prog()
    caption = f"✅ <b>Process Completed!</b>\n<code>{os.path.basename(out_name)}</code>"
    try:
        await app.send_video(chat_id=USER_ID, video=out_name, width=width, height=height, duration=int(duration), caption=caption, thumb=thumb, supports_streaming=True, progress=prog, progress_args=("upload",))
    except:
        await app.send_video(chat_id=CHAT_ID, video=out_name, width=width, height=height, duration=int(duration), caption=caption, thumb=thumb, supports_streaming=True, progress=prog, progress_args=("upload",))

    if TASK_TYPE == "compress" and extracted_subs:
        for sub_f in extracted_subs:
            try: await app.send_document(chat_id=USER_ID, document=sub_f, caption="📄 Extracted Subtitles")
            except: 
                try: await app.send_document(chat_id=CHAT_ID, document=sub_f, caption="📄 Extracted Subtitles")
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
