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
            text = f"❌ **Kaggle Execution Error Traceback:**\n\n<pre><code class='language-python'>{html.escape(error_msg[:3500])}</code></pre>"
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
    SESSION_STRING = CFG.get("session_string", None)  # New: session string for no-authorization login
except Exception:
    tb = traceback.format_exc()
    report_critical_failure(tb)
    sys.exit(1)

# ----------------------------- DEPENDENCY SYSTEM (with path fix) -----------------------------
def ensure_deps():
    need = []
    for mod, pip_name in [("pyrogram", "pyrogram"), ("tgcrypto", "tgcrypto"),
                          ("pysubs2", "pysubs2"), ("fontTools", "fonttools")]:
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

ensure_deps()

import pyrogram.utils, pysubs2
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

async def prog(current, total, step_name):
    global last_time, start_time, app
    now = time.time()
    if start_time == 0:
        start_time = now; last_time = now; return
    if now - last_time > 12 or current >= total:
        elapsed = now - start_time
        speed = current / elapsed if elapsed > 0 else 0
        speed_mb = (speed / 1024) / 1024
        percent = (current / total) * 100 if total > 0 else 0
        if "download" in step_name:
            text = f"📥 **Downloading Video**\n{get_download_bar(percent)} [{percent:.1f}%]\n🚀 Speed: **{speed_mb:.2f} MB/s**\n📦 {current/1048576:.1f}MB / {total/1048576:.1f}MB"
        else:
            text = f"📤 **Sending Video**\n{get_send_bar(percent)} [{percent:.1f}%]\n🚀 Speed: **{speed_mb:.2f} MB/s**\n📦 {current/1048576:.1f}MB / {total/1048576:.1f}MB"
        try:
            if app and status_msg_id:
                await app.edit_message_text(CHAT_ID, status_msg_id, text,
                                            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Cancel Task", callback_data="cancel_active_run")]]))
        except:
            pass
        last_time = now

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

# ----------------------------- UTILITY FUNCTIONS -----------------------------
def get_duration(video_path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
                            capture_output=True, text=True, timeout=10)
        return float(r.stdout.strip()) if r.stdout.strip() else 0.0
    except:
        return 0

def get_font_name(font_path):
    try:
        font = TTFont(font_path)
        for record in font['name'].names:
            if record.nameID == 4:
                return record.toUnicode()
    except:
        pass
    return "Arial"

def is_ass_format(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(4000)
        return bool(re.search(r'\[Script Info\]|\[V4\+?\s*Styles\]|\[Events\]', head, re.IGNORECASE))
    except:
        return False

def convert_to_clean_ass(input_sub, output_ass):
    try:
        subs = pysubs2.load(input_sub)
        subs.styles["Default"] = pysubs2.SSAStyle(fontname="Arial", fontsize=24,
            primarycolor=pysubs2.Color(255, 255, 255), outlinecolor=pysubs2.Color(0, 0, 0),
            outline=2, shadow=1, marginl=20, marginr=20, marginv=15)
        for line in subs:
            line.style = "Default"
            line.text = re.sub(r'<[^>]+>', '', re.sub(r'\{[^}]+\}', '', line.text)).replace('\r', '').replace('\n', '\\N').strip()
        subs.save(output_ass)
    except:
        pass

# ----------------------------- KAGGLE NOTEBOOK CLEANUP -----------------------------
async def kill_all_other_notebooks():
    """Kill all active Kaggle notebooks for the current account except this one."""
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
async def download_message_asset(app_instance, msg_id_str, output_path, step_name):
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
        reset_prog()
        result = await asyncio.wait_for(
            app_instance.download_media(msg, file_name=output_path, progress=prog, progress_args=(step_name,)),
            timeout=1800
        )
        if not result or not os.path.exists(result):
            raise Exception("Mirrored file failed to write successfully.")
        return result
    except Exception as e:
        raise Exception(f"Download Error on secured step '{step_name}': {type(e).__name__}: {e}")

async def download_by_file_id(app_instance, file_id, output_path, step_name):
    if not file_id or file_id == "none":
        return None
    try:
        reset_prog()
        result = await asyncio.wait_for(
            app_instance.download_media(file_id, file_name=output_path, progress=prog, progress_args=(step_name,)),
            timeout=1800
        )
        if not result or not os.path.exists(result):
            raise Exception("File path failed to register on fallback.")
        return result
    except Exception as e:
        raise Exception(f"Fallback download failed on '{step_name}': {type(e).__name__}: {e}")

async def download_asset_robust(app_instance, val, output_path, step_name):
    if not val or val == "none":
        return None
    if str(val).isdigit():
        return await download_message_asset(app_instance, val, output_path, step_name)
    return await download_by_file_id(app_instance, val, output_path, step_name)

# ----------------------------- ENCODING ENGINE -----------------------------
def run_ffmpeg_sync(cmd, duration, process_title):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    last_edit = time.time()
    log_tail = []
    for line in proc.stdout:
        line = line.strip()
        if not line:
            continue
        if "out_time_us=" not in line and "frame=" not in line:
            log_tail.append(line)
            if len(log_tail) > 20:
                log_tail.pop(0)
        if "out_time_us=" in line and duration > 0:
            now = time.time()
            if now - last_edit > 10:
                try:
                    us = int(line.split("=")[1])
                    percent = min((us / 1_000_000.0 / duration) * 100, 100.0)
                    fire_and_forget_http(f"⚙️ {process_title}\n{get_process_bar(percent)} [{percent:.1f}%]")
                except:
                    pass
                last_edit = now
    proc.wait()
    return proc.returncode, log_tail

def encode_with_fallback(base_cmd_gpu, base_cmd_cpu, duration, title):
    if HW_MODE == "gpu" and base_cmd_gpu:
        rc, log = run_ffmpeg_sync(base_cmd_gpu, duration, title + " (GPU)")
        if rc == 0:
            return
        fire_and_forget_http(f"⚠️ GPU fallback activated. Switching to CPU encoding...")
    rc, log = run_ffmpeg_sync(base_cmd_cpu, duration, title + " (CPU)")
    if rc != 0:
        raise Exception("FFmpeg command crashed on execution.\n" + "\n".join(log[-8:]))

# ----------------------------- UPLOAD ENGINE -----------------------------
async def deliver_video_asset(app_instance, chat_id, target_user, file_path, caption):
    if not os.path.exists(file_path) or os.path.getsize(file_path) < 100:
        raise Exception("Processed output file was empty or missing.")
    thumb_path = "thumb.jpg"
    try:
        subprocess.run(["ffmpeg", "-y", "-i", file_path, "-ss", "00:00:01", "-vframes", "1", thumb_path],
                       capture_output=True, timeout=15)
    except:
        pass
    if not os.path.exists(thumb_path):
        thumb_path = None

    reset_prog()
    pm_msg, file_id = None, None
    try:
        pm_msg = await asyncio.wait_for(
            app_instance.send_document(chat_id=target_user, document=file_path, caption=caption,
                                       thumb=thumb_path, progress=prog, progress_args=("sending_video",)),
            timeout=1800
        )
        if pm_msg and pm_msg.document:
            file_id = pm_msg.document.file_id
    except:
        try:
            pm_msg = await asyncio.wait_for(
                app_instance.send_document(chat_id=chat_id, document=file_path,
                                           caption=f"⚠️ <a href='tg://user?id={target_user}'>User</a>, Video Ready:\n\n{caption}",
                                           thumb=thumb_path, progress=prog, progress_args=("sending_video",),
                                           parse_mode=ParseMode.HTML),
                timeout=1800
            )
            if pm_msg and pm_msg.document:
                file_id = pm_msg.document.file_id
        except:
            pass

    if file_id:
        try:
            await app_instance.send_document(chat_id=DESK_CHANNEL_ID, document=file_id,
                                             caption=f"🎬 Logs: {caption}\nUser: `{target_user}`")
        except:
            pass
    return pm_msg

# ----------------------------- MAIN DRIVER -----------------------------
async def main_driver():
    global status_msg_id, app

    # --- FIRST SESSION (DOWNLOAD) USING SESSION STRING ---
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

    # Kill other notebooks (except self)
    await kill_all_other_notebooks()

    step_dl = "hardsub_download" if TASK_TYPE == "hardsub" else "compress_download"
    video_file = await download_asset_robust(app, VIDEO_MSG_ID, os.path.join(WORK_DIR, "video.mkv"), step_dl)
    if not video_file:
        raise Exception("Telegram video download failed.")

    duration = get_duration(video_file)

    base_name = "output"
    if RENAME and RENAME != "none":
        base_name = RENAME.rsplit('.', 1)[0]
    out_name = os.path.join(WORK_DIR, f"{base_name}.mp4")

    font_name = "Arial"
    if FONT_MSG_ID and FONT_MSG_ID != "none":
        fonts_dir = os.path.join(WORK_DIR, "fonts")
        os.makedirs(fonts_dir, exist_ok=True)
        font_path = await download_asset_robust(app, FONT_MSG_ID, os.path.join(fonts_dir, "custom_font.ttf"), step_dl)
        if font_path and os.path.exists(font_path):
            font_name = get_font_name(font_path)

    sub_file, wm_file, has_watermark = None, None, False
    extracted_subs = []

    if TASK_TYPE == "hardsub":
        if SUB_MSG_ID and SUB_MSG_ID != "none":
            sub_file = await download_asset_robust(app, SUB_MSG_ID, os.path.join(WORK_DIR, "sub_raw"), "hardsub_download")
        if not sub_file or not os.path.exists(sub_file):
            raise Exception("Subtitles download failed.")
        ready_sub_path = os.path.join(WORK_DIR, "ready_sub.ass")
        if sub_file.lower().endswith('.ass') or is_ass_format(sub_file):
            try:
                with open(sub_file, 'r', encoding='utf-8', errors='ignore') as f:
                    ass_content = f.read()
            except:
                with open(sub_file, 'r', encoding='latin-1', errors='ignore') as f:
                    ass_content = f.read()
            if any(word in ass_content.lower() for word in ["logo", "watermark", "cr", "credit"]):
                has_watermark = True
            if FONT_MSG_ID and FONT_MSG_ID != "none":
                lines = ass_content.splitlines()
                new_lines = []
                for line in lines:
                    if line.strip().startswith("Style:"):
                        parts = line.split(",", 2)
                        if len(parts) >= 3:
                            line = f"{parts[0]},{font_name},{parts[2]}"
                    new_lines.append(line)
                with open(ready_sub_path, "w", encoding='utf-8') as f:
                    f.write("\n".join(new_lines))
            else:
                shutil.copy(sub_file, ready_sub_path)
        else:
            try:
                subs = pysubs2.load(sub_file, encoding='utf-8')
            except:
                subs = pysubs2.load(sub_file, encoding='latin-1')
            new_subs = pysubs2.SSAFile()
            new_subs.styles["Default"] = pysubs2.SSAStyle(fontname=font_name, fontsize=24,
                primarycolor=pysubs2.Color(255, 255, 255), outlinecolor=pysubs2.Color(0, 0, 0),
                outline=2, shadow=1, marginl=20, marginr=20, marginv=15)
            for line in subs:
                clean_text = re.sub(r'<[^>]+>', '', re.sub(r'\{[^}]+\}', '', line.text)).replace('\r', '').replace('\n', '\\N').strip()
                if clean_text:
                    new_subs.append(pysubs2.SSAEvent(start=line.start, end=line.end, text=clean_text, style="Default"))
            new_subs.save(ready_sub_path)

        if WM_MSG_ID and WM_MSG_ID != "none" and not has_watermark:
            wm_file = await download_asset_robust(app, WM_MSG_ID, os.path.join(WORK_DIR, "watermark.png"), "hardsub_download")

    await app.stop()

    process_title = "Compressing" if TASK_TYPE == "compress" else "Encoding Hardsub"

    if TASK_TYPE == "compress":
        await update_http_status("⚙️ Checking and extracting subtitles from container...")
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
        scale_filter = f"scale=-2:{reso_clean}" if reso_clean and reso_clean.lower() != "none" else "scale='trunc(iw/2)*2:trunc(ih/2)*2'"

        await update_http_status(f"⚙️ {process_title}\n{get_process_bar(0)} [0.0%]")

        # CRF 34 compression
        cmd_cpu = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-vf", scale_filter,
                   "-map", "0:v", "-map", "0:a?", "-c:v", "libx264", "-preset", "ultrafast",
                   "-crf", "34", "-pix_fmt", "yuv420p", "-threads", "0", "-c:a", "aac", "-b:a", "128k",
                   "-movflags", "+faststart", out_name]
        cmd_gpu = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-vf", scale_filter,
                   "-map", "0:v", "-map", "0:a?", "-c:v", "h264_nvenc", "-preset", "p4",
                   "-cq", "34", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
                   "-movflags", "+faststart", out_name]

        await asyncio.to_thread(encode_with_fallback, cmd_gpu, cmd_cpu, duration, process_title)

    elif TASK_TYPE == "hardsub":
        vf_filter = "subtitles='ready_sub.ass':charenc=UTF-8"
        if FONT_MSG_ID and FONT_MSG_ID != "none":
            vf_filter += ":fontsdir=fonts"
        v_filter = f"scale='trunc(iw/2)*2:trunc(ih/2)*2',{vf_filter}"
        overlay_coord = "W-w-15:15" if WM_POS == "right" else "15:15"

        await update_http_status(f"⚙️ {process_title}\n{get_process_bar(0)} [0.0%]")

        if wm_file and os.path.exists(wm_file):
            complex_f = f"[0:v]{v_filter}[vsub];[1:v]scale=200:-1[wm];[vsub][wm]overlay={overlay_coord}"
            cmd_cpu = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-i", wm_file,
                       "-filter_complex", complex_f, "-c:v", "libx264", "-preset", "ultrafast",
                       "-crf", "26", "-pix_fmt", "yuv420p", "-threads", "0", "-c:a", "aac",
                       "-movflags", "+faststart", out_name]
            cmd_gpu = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-i", wm_file,
                       "-filter_complex", complex_f, "-c:v", "h264_nvenc", "-preset", "p4",
                       "-cq", "26", "-pix_fmt", "yuv420p", "-c:a", "aac",
                       "-movflags", "+faststart", out_name]
        else:
            cmd_cpu = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-vf", v_filter,
                       "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p",
                       "-threads", "0", "-c:a", "aac", "-movflags", "+faststart", out_name]
            cmd_gpu = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-vf", v_filter,
                       "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "26", "-pix_fmt", "yuv420p",
                       "-c:a", "aac", "-movflags", "+faststart", out_name]

        await asyncio.to_thread(encode_with_fallback, cmd_gpu, cmd_cpu, duration, process_title)

    # --- SECOND SESSION (UPLOAD) USING SESSION STRING ---
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

    await update_http_status(f"📤 Sending Video\n{get_send_bar(0)} [0.0%]")
    caption = os.path.basename(out_name)  # FIX 2
    await deliver_video_asset(app, CHAT_ID, USER_ID, out_name, caption)

    if TASK_TYPE == "compress" and extracted_subs:
        for sub_f in extracted_subs:
            try:
                await app.send_document(chat_id=USER_ID, document=sub_f, caption="📄 Extracted Clean Subtitles (.ass)")
            except:
                try:
                    await app.send_document(chat_id=CHAT_ID, document=sub_f, caption="📄 Extracted Clean Subtitles (.ass)")
                except:
                    pass

    try:
        await app.delete_messages(CHAT_ID, status_msg_id)
    except:
        pass
    await app.stop()
    sys.exit(0)  # FIX 3

async def update_http_status(text):
    fire_and_forget_http(text)

if __name__ == "__main__":
    try:
        asyncio.run(main_driver())
    except Exception as outer_err:
        tb_data = traceback.format_exc()
        report_critical_failure(tb_data)
        sys.exit(1)
