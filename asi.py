import os
import sys
import time
import asyncio
import re
import subprocess
import requests
import html
import shutil
import json
import gc
import traceback

# --- STEP 1: VERIFY/INSTALL RUNTIME DEPENDENCIES ONCE ---
import importlib.util
packages_to_install = []
for pkg in ["pyrogram", "pysubs2", "fontTools", "tgcrypto"]:
    if importlib.util.find_spec(pkg) is None:
        packages_to_install.append(pkg)
        
if packages_to_install:
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-cache-dir"] + packages_to_install)

import pyrogram.utils
import pysubs2
from pyrogram import Client
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.enums import ParseMode
from fontTools.ttLib import TTFont

pyrogram.utils.get_peer_type = lambda p: "channel" if str(p).startswith("-100") else "chat" if str(p).startswith("-") else "user"

# Globals injected dynamically at runtime
# API_ID, API_HASH, BOT_TOKEN, DESK_CHANNEL_ID are parsed from injected code headers.

os.makedirs("fonts", exist_ok=True)
semaphore = asyncio.Semaphore(3)  # Maximum 3 tasks processed concurrently
active_tasks = set()

# --- UTILITIES ---
last_time = 0
start_time = 0

def reset_prog():
    global last_time, start_time
    last_time = time.time()
    start_time = time.time()

def get_download_bar(percent):
    filled = int(percent / 100 * 20)
    return f"[{'█' * filled}{'░' * (20 - filled)}]"

def get_process_bar(percent):
    filled = int(percent / 100 * 20)
    return f"[{'▓' * filled}{'░' * (20 - filled)}]"

def get_send_bar(percent):
    filled = int(percent / 100 * 20)
    return f"[{'█' * filled}{'▒' * (20 - filled)}]"

async def edit_msg_safe(client, chat_id, msg_id, text):
    try:
        await client.edit_message_text(chat_id, msg_id, text, parse_mode=ParseMode.HTML)
    except Exception:
        pass

def prog(c, t, app_instance, step_name, chat_id, msg_id):
    global last_time, start_time
    now = time.time()
    if start_time == 0:
        start_time = now
        last_time = now
        return
        
    if now - last_time > 10 or c == t:
        elapsed = now - start_time
        speed = c / elapsed if elapsed > 0 else 0
        speed_mb = (speed / 1024) / 1024
        percent = (c / t) * 100 if t > 0 else 0
        
        if step_name in ["hardsub_download", "compress_download"]:
            text = f"📥 <b>Downloading Video</b>\n{get_download_bar(percent)} [{percent:.1f}%]\n🚀 Speed: <b>{speed_mb:.2f} MB/s</b>\n📦 {c/1048576:.1f}MB / {t/1048576:.1f}MB"
        else:
            text = f"📤 <b>Sending Video</b>\n{get_send_bar(percent)} [{percent:.1f}%]\n🚀 Speed: <b>{speed_mb:.2f} MB/s</b>\n📦 {c/1048576:.1f}MB / {t/1048576:.1f}MB"
        
        asyncio.create_task(edit_msg_safe(app_instance, chat_id, msg_id, text))
        last_time = now

def convert_to_clean_ass(input_sub, output_ass):
    try:
        subs = pysubs2.load(input_sub)
        subs.styles["Default"] = pysubs2.SSAStyle(fontname="Arial", fontsize=24, primarycolor=pysubs2.Color(255, 255, 255), outlinecolor=pysubs2.Color(0, 0, 0), outline=2, shadow=1, marginl=20, marginr=20, marginv=15)
        for line in subs:
            line.style = "Default"
            line.text = re.sub(r'<[^>]+>', '', re.sub(r'\{[^}]+\}', '', line.text)).replace('\r', '').replace('\n', '\\N').strip()
        subs.save(output_ass)
    except Exception:
        pass

def is_ass_format(path):
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            head = f.read(4000)
        return bool(re.search(r'\[Script Info\]|\[V4\+?\s*Styles\]|\[Events\]', head, re.IGNORECASE))
    except Exception:
        return False

def get_font_name(font_path):
    try:
        font = TTFont(font_path)
        for record in font['name'].names:
            if record.nameID == 4:
                return record.toUnicode()
    except:
        pass
    return "Arial"

def get_video_dimensions_and_duration(video_path):
    cmd_dur = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", video_path]
    duration = 0.0
    try:
        res_dur = subprocess.run(cmd_dur, capture_output=True, text=True, timeout=10)
        if res_dur.stdout.strip():
            duration = float(res_dur.stdout.strip())
    except:
        pass
    return 1280, 720, duration

def is_gpu_available():
    try:
        res = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=5)
        return res.returncode == 0
    except:
        return False

async def download_tg_link(app_instance, link, output_path, step_name, chat_id, msg_id):
    if not link or link == "none":
        return None
    try:
        parts = link.split("/")
        chat_part = parts[-2]
        msg_id_target = int(parts[-1])
        
        target_chat = int(f"-100{chat_part}") if chat_part.isdigit() else chat_part
        msg = await app_instance.get_messages(target_chat, msg_id_target)
        
        if msg and (msg.document or msg.video or msg.photo or msg.animation):
            ext = ""
            if msg.document and msg.document.file_name:
                _, ext = os.path.splitext(msg.document.file_name)
            elif msg.video and msg.video.file_name:
                _, ext = os.path.splitext(msg.video.file_name)
            if ext and not output_path.endswith(ext.lower()):
                output_path = output_path + ext.lower()
            
            reset_prog()
            return await asyncio.wait_for(
                msg.download(file_name=output_path, progress=prog, progress_args=(app_instance, step_name, chat_id, msg_id)),
                timeout=1800
            )
    except Exception as e:
        print(f"Download Error: {e}")
    return None

async def deliver_video_asset(app_instance, chat_id, target_user, file_path, caption, progress_callback, status_msg_id):
    if not os.path.exists(file_path) or os.path.getsize(file_path) < 100:
        raise Exception("Processed output video file missing or empty!")
    
    thumb_path = "thumb.jpg"
    try:
        subprocess.run(["ffmpeg", "-y", "-i", file_path, "-ss", "00:00:01", "-vframes", "1", thumb_path], capture_output=True, timeout=15)
    except:
        pass
    if not os.path.exists(thumb_path):
        thumb_path = None

    pm_msg, file_id = None, None
    reset_prog()

    try:
        pm_msg = await asyncio.wait_for(
            app_instance.send_document(chat_id=target_user, document=file_path, caption=caption, thumb=thumb_path, progress=progress_callback, progress_args=(app_instance, "sending_video", chat_id, status_msg_id)),
            timeout=1800
        )
        if pm_msg and pm_msg.document:
            file_id = pm_msg.document.file_id
    except Exception:
        try:
            pm_msg = await asyncio.wait_for(
                app_instance.send_document(chat_id=chat_id, document=file_path, caption=f"⚠️ <a href='tg://user?id={target_user}'>User</a>, Video Ready:\n\n{caption}", thumb=thumb_path, progress=progress_callback, progress_args=(app_instance, "sending_video", chat_id, status_msg_id), parse_mode=ParseMode.HTML),
                timeout=1800
            )
            if pm_msg and pm_msg.document:
                file_id = pm_msg.document.file_id
        except:
            pass

    return pm_msg

# --- CORE CONCURRENT TASK HANDLER ---
async def process_task(app, payload):
    task_id = payload["task_id"]
    task_type = payload["task_type"]
    video_id = payload["video_id"]
    sub_id = payload["sub_id"]
    chat_id = payload["chat_id"]
    user_id = payload["user_id"]
    resolution = payload["resolution"]
    wm_id = payload["wm_id"]
    wm_pos = payload["wm_pos"]
    rename = payload["rename"]
    font_link = payload["font_link"]
    trigger_msg_id = payload["trigger_msg_id"]

    try:
        await edit_msg_safe(app, chat_id, trigger_msg_id, "⚙️ <b>Worker received task. Downloading source files...</b>")
        
        step_dl = "hardsub_download" if task_type == "hardsub" else "compress_download"
        video_file = await download_tg_link(app, video_id, f"video_{task_id}", step_dl, chat_id, trigger_msg_id)
        if not video_file:
            raise Exception("Telegram video download failed.")

        _, _, duration = get_video_dimensions_and_duration(video_file)

        base_name = "output"
        if rename and rename != "none":
            base_name = rename.rsplit('.', 1)[0]
        out_name = f"{base_name}.mp4"

        font_name = "Arial"
        fonts_dir = f"fonts_{task_id}"
        os.makedirs(fonts_dir, exist_ok=True)
        
        if font_link and font_link != "none":
            r = requests.get(font_link)
            if r.status_code == 200:
                custom_font_path = f"{fonts_dir}/custom_font.ttf"
                with open(custom_font_path, "wb") as f:
                    f.write(r.content)
                font_name = get_font_name(custom_font_path)

        sub_file, wm_file, has_watermark = None, None, False
        extracted_subs = []

        if task_type == "hardsub":
            if sub_id and sub_id != "none":
                sub_file = await download_tg_link(app, sub_id, f"sub_raw_{task_id}", "hardsub_download", chat_id, trigger_msg_id)
            if not sub_file or not os.path.exists(sub_file):
                raise Exception("Subtitle download failed or missing.")

            ready_sub_path = f"ready_sub_{task_id}.ass"
            if sub_file.lower().endswith('.ass') or is_ass_format(sub_file):
                try:
                    with open(sub_file, 'r', encoding='utf-8', errors='ignore') as f:
                        ass_content = f.read()
                except Exception:
                    with open(sub_file, 'r', encoding='latin-1', errors='ignore') as f:
                        ass_content = f.read()

                if any(word in ass_content.lower() for word in ["logo", "watermark", "cr", "credit"]):
                    has_watermark = True

                if font_link and font_link != "none":
                    lines = ass_content.splitlines()
                    new_lines = []
                    for line in lines:
                        if line.strip().startswith("Style:"):
                            parts = line.split(",", 2)
                            if len(parts) >= 3:
                                line = f"{parts[0]},{font_name},{parts[2]}"
                        new_lines.append(line)
                    with open(ready_sub_path, "w", encoding="utf-8") as f:
                        f.write("\n".join(new_lines))
                else:
                    shutil.copy(sub_file, ready_sub_path)
            else:
                try:
                    subs = pysubs2.load(sub_file, encoding="utf-8")
                except:
                    subs = pysubs2.load(sub_file, encoding="latin-1")
                new_subs = pysubs2.SSAFile()
                new_subs.styles["Default"] = pysubs2.SSAStyle(fontname=font_name, fontsize=24, primarycolor=pysubs2.Color(255, 255, 255), outlinecolor=pysubs2.Color(0, 0, 0), outline=2, shadow=1, marginl=20, marginr=20, marginv=15)
                for line in subs:
                    clean_text = re.sub(r'<[^>]+>', '', re.sub(r'\{[^}]+\}', '', line.text)).replace('\r', '').replace('\n', '\\N').strip()
                    if clean_text:
                        new_subs.append(pysubs2.SSAEvent(start=line.start, end=line.end, text=clean_text, style="Default"))
                new_subs.save(ready_sub_path)

            if wm_id and wm_id != "none" and not has_watermark:
                wm_file = await download_tg_link(app, wm_id, f"watermark_{task_id}.png", "hardsub_download", chat_id, trigger_msg_id)

        # ---------------- ENCODE PHASE ----------------
        process_title = "Compressing" if task_type == "compress" else "Encoding Hardsub"
        gpu_active = is_gpu_available()

        if task_type == "compress":
            await edit_msg_safe(app, chat_id, trigger_msg_id, "⚙️ Checking and Extracting Subtitles...")
            cmd_probe = ["ffprobe", "-v", "error", "-select_streams", "s", "-show_entries", "stream=index,codec_name", "-of", "csv=p=0", video_file]
            res_probe = subprocess.run(cmd_probe, capture_output=True, text=True)
            if res_probe.stdout.strip():
                streams = res_probe.stdout.strip().split('\n')
                for i, st in enumerate(streams):
                    if not st: continue
                    parts = st.split(',')
                    s_idx = parts[0]
                    s_codec = parts[1].strip()
                    if s_codec in ['ass', 'ssa']:
                        ass_out = f"{base_name}_track_{i+1}.ass"
                        subprocess.run(["ffmpeg", "-y", "-i", video_file, "-map", f"0:{s_idx}", ass_out])
                        if os.path.exists(ass_out) and os.path.getsize(ass_out) > 0:
                            extracted_subs.append(ass_out)
                    elif s_codec in ['subrip', 'srt', 'webvtt']:
                        temp_ext = ".srt" if s_codec == 'subrip' else ".vtt"
                        temp_sub = f"temp_{i+1}_{task_id}{temp_ext}"
                        subprocess.run(["ffmpeg", "-y", "-i", video_file, "-map", f"0:{s_idx}", temp_sub])
                        if os.path.exists(temp_sub) and os.path.getsize(temp_sub) > 0:
                            ass_out = f"{base_name}_track_{i+1}.ass"
                            convert_to_clean_ass(temp_sub, ass_out)
                            if os.path.exists(ass_out):
                                extracted_subs.append(ass_out)
                            try: os.remove(temp_sub)
                            except: pass

            reso_clean = str(resolution).replace("p", "").replace("P", "").strip() if resolution else ""
            scale_filter = f"scale=-2:{reso_clean}" if (reso_clean and reso_clean.lower() != "none") else "scale='trunc(iw/2)*2:trunc(ih/2)*2'"

            await edit_msg_safe(app, chat_id, trigger_msg_id, f"⚙️ {process_title}\n{get_process_bar(0)} [0.0%]")
            
            # Choose GPU (NVENC) or CPU (libx264)
            if gpu_active:
                cmd = [
                    "ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-vf", scale_filter, 
                    "-map", "0:v", "-map", "0:a?",
                    "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "26", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out_name
                ]
            else:
                cmd = [
                    "ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-vf", scale_filter, 
                    "-map", "0:v", "-map", "0:a?",
                    "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p", "-threads", "0", 
                    "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", out_name
                ]
            
            # Execution & Real-time Progress Tracking
            process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            last_edit = time.time()
            log_tail = []
            
            while True:
                line = await process.stdout.readline()
                if not line: break
                line_str = line.decode('utf-8', errors='ignore').strip()
                if line_str and "out_time_us=" not in line_str and "frame=" not in line_str:
                    log_tail.append(line_str)
                    if len(log_tail) > 20: log_tail.pop(0)
                if "out_time_us=" in line_str:
                    now = time.time()
                    if now - last_edit > 12:
                        try:
                            percent = min((int(line_str.split("=")[1]) / 1000000.0 / duration) * 100, 100.0)
                            asyncio.create_task(edit_msg_safe(app, chat_id, trigger_msg_id, f"⚙️ {process_title}\n{get_process_bar(percent)} [{percent:.1f}%]"))
                        except: pass
                        last_edit = now
            await process.wait()
            if process.returncode != 0:
                raise Exception("FFmpeg compression failed.\n" + "\n".join(log_tail[-8:]))

        elif task_type == "hardsub":
            vf_filter = f"subtitles='{ready_sub_path}':charenc=UTF-8"
            if font_link and font_link != "none":
                vf_filter += f":fontsdir={fonts_dir}"
            v_filter = f"scale='trunc(iw/2)*2:trunc(ih/2)*2',{vf_filter}"
            overlay_coord = "W-w-15:15" if wm_pos == "right" else "15:15"

            await edit_msg_safe(app, chat_id, trigger_msg_id, f"⚙️ {process_title}\n{get_process_bar(0)} [0.0%]")

            if gpu_active:
                if wm_file and os.path.exists(wm_file):
                    cmd = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-i", wm_file, "-filter_complex", f"[0:v]{v_filter}[vsub];[1:v]scale=200:-1[wm];[vsub][wm]overlay={overlay_coord}", "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "26", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", out_name]
                else:
                    cmd = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-vf", v_filter, "-c:v", "h264_nvenc", "-preset", "p4", "-cq", "26", "-pix_fmt", "yuv420p", "-c:a", "aac", "-movflags", "+faststart", out_name]
            else:
                if wm_file and os.path.exists(wm_file):
                    cmd = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-i", wm_file, "-filter_complex", f"[0:v]{v_filter}[vsub];[1:v]scale=200:-1[wm];[vsub][wm]overlay={overlay_coord}", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p", "-threads", "0", "-c:a", "aac", "-movflags", "+faststart", out_name]
                else:
                    cmd = ["ffmpeg", "-y", "-progress", "pipe:1", "-i", video_file, "-vf", v_filter, "-c:v", "libx264", "-preset", "ultrafast", "-crf", "26", "-pix_fmt", "yuv420p", "-threads", "0", "-c:a", "aac", "-movflags", "+faststart", out_name]

            process = await asyncio.create_subprocess_exec(*cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            last_edit = time.time()
            log_tail = []
            
            while True:
                line = await process.stdout.readline()
                if not line: break
                line_str = line.decode('utf-8', errors='ignore').strip()
                if line_str and "out_time_us=" not in line_str and "frame=" not in line_str:
                    log_tail.append(line_str)
                    if len(log_tail) > 20: log_tail.pop(0)
                if "out_time_us=" in line_str:
                    now = time.time()
                    if now - last_edit > 12:
                        try:
                            percent = min((int(line_str.split("=")[1]) / 1000000.0 / duration) * 100, 100.0)
                            asyncio.create_task(edit_msg_safe(app, chat_id, trigger_msg_id, f"⚙️ {process_title}\n{get_process_bar(percent)} [{percent:.1f}%]"))
                        except: pass
                        last_edit = now
            await process.wait()
            if process.returncode != 0:
                raise Exception("FFmpeg encoding failed.\n" + "\n".join(log_tail[-8:]))

        # ---------------- UPLOAD PHASE ----------------
        await edit_msg_safe(app, chat_id, trigger_msg_id, f"📤 Sending Video\n{get_send_bar(0)} [0.0%]")
        
        # Caption formatting strictly according to requirements:
        caption = f"✅ Successful\n`{out_name}`"
        await deliver_video_asset(app, chat_id, user_id, out_name, caption, prog, trigger_msg_id)

        # Send extracted tracks if present
        if task_type == "compress" and extracted_subs:
            for sub_f in extracted_subs:
                try: await app.send_document(chat_id=user_id, document=sub_f, caption="📄 Extracted Clean Subtitles (.ass)")
                except:
                    try: await app.send_document(chat_id=chat_id, document=sub_f, caption="📄 Extracted Clean Subtitles (.ass)")
                    except: pass

        # Delete status message on success
        try: await app.delete_messages(chat_id, trigger_msg_id)
        except: pass

    except Exception as e:
        err_msg = f"❌ <b>Workflow Error:</b>\n<code>{html.escape(str(e))}</code>\n\n🛠️ <b>Traceback:</b>\n<code>{html.escape(traceback.format_exc()[-2500:])}</code>"
        await edit_msg_safe(app, chat_id, trigger_msg_id, err_msg)
        
    finally:
        # Cleanup temporary files
        for path in [f"video_{task_id}", f"sub_raw_{task_id}", f"watermark_{task_id}.png", f"ready_sub_{task_id}.ass", out_name]:
            if path and os.path.exists(path):
                try: os.remove(path)
                except: pass
        if os.path.exists(fonts_dir):
            try: shutil.rmtree(fonts_dir)
            except: pass
        for f in os.listdir("."):
            if f.startswith(base_name) and f != out_name:
                try: os.remove(f)
                except: pass
        gc.collect()

async def worker_task_wrapper(app, payload, queue_msg):
    async with semaphore:
        try:
            await process_task(app, payload)
        finally:
            active_tasks.discard(payload["task_id"])
            try: await queue_msg.delete()
            except: pass

# --- POLLING TASK QUEUE LOOP ---
async def persistent_queue_polling_loop(app):
    print("🔋 Persistent queue polling active...")
    while True:
        try:
            # Fetch last 20 messages from queue
            async for message in app.get_chat_history(DESK_CHANNEL_ID, limit=20):
                if message.text and message.text.startswith("[TASK_QUEUE]"):
                    try:
                        payload_str = message.text[len("[TASK_QUEUE]"):].strip()
                        payload = json.loads(payload_str)
                        task_id = payload["task_id"]

                        if task_id in active_tasks:
                            continue

                        # Atomically lock task on Telegram side by editing prefix to [TASK_PROCESSING]
                        await message.edit_text(f"[TASK_PROCESSING] {payload_str}")
                        
                        active_tasks.add(task_id)
                        # Process tasks asynchronously up to concurrency limits
                        asyncio.create_task(worker_task_wrapper(app, payload, message))
                    except Exception as parse_ex:
                        print(f"Failed to process task envelope: {parse_ex}")
        except Exception as poll_ex:
            print(f"Polling loop connection glitch: {poll_ex}")
        
        await asyncio.sleep(5) # Poll queue channel every 5 seconds

async def main():
    # Login as worker with updates disabled to prevent multiple client conflicts
    app = Client(
        "KagglePersistentWorker", 
        api_id=API_ID, 
        api_hash=API_HASH, 
        bot_token=BOT_TOKEN, 
        workers=32, 
        max_concurrent_transmissions=16, 
        no_updates=True
    )
    await app.start()
    print("✅ Persistent Worker Connected to Telegram.")
    
    # Start task polling
    await persistent_queue_polling_loop(app)

if __name__ == "__main__":
    asyncio.run(main())
