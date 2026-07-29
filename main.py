import os
import re
import time
import json
import base64
import tempfile
import asyncio
import threading
import shutil
import pytz
import psutil
import requests
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
import pyrogram.utils
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums import ChatType

# Route peer types correctly
pyrogram.utils.get_peer_type = lambda p: "channel" if str(p).startswith("-100") else "chat" if str(p).startswith("-") else "user"

# --- CONFIGURATIONS & ENVIRONMENT VARIABLES ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", 8080))

OWNER_ID = 5344078567
ALLOWED_USER = 5351848105
GROUP_ID = -1003899919015
DESK_CHANNEL_ID = -1003700822969

app = Client("HarsubBotFrontend", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workers=32)

users_data = {}
wm_positions = {}
current_running_hw = None
current_running_acc = None

# --- BASIC AUTHORIZATION CHECK ---
def is_authorized(m: Message):
    if not m.from_user:
        return False
    u_id = m.from_user.id
    if u_id in [OWNER_ID, ALLOWED_USER]:
        return True
    if m.chat and m.chat.id == GROUP_ID:
        return True
    return False

async def check_command_privacy(c, m: Message):
    is_pm = m.chat.type == ChatType.PRIVATE
    if is_pm and m.from_user.id in [OWNER_ID, ALLOWED_USER]:
        return True
    if is_pm:
        try:
            chat_info = await c.get_chat(GROUP_ID)
            invite_link = chat_info.invite_link or "https://t.me/Mangajii"
        except:
            invite_link = "https://t.me/Mangajii"
        await m.reply(f"❌ **Aap is Bot ko Private mein use nahi kar sakte!**\n\n👉 Humara [Official Group]({invite_link}) join karein.", disable_web_page_preview=True)
        return False
    return is_authorized(m)

# --- KAGGER CREDENTIAL MANAGER ---
def setup_kaggle_credentials(username, api_key):
    for k_dir in [os.path.expanduser("~/.kaggle"), os.path.expanduser("~/.config/kaggle")]:
        os.makedirs(k_dir, exist_ok=True)
        path = os.path.join(k_dir, "kaggle.json")
        with open(path, "w") as f:
            json.dump({"username": username, "key": api_key}, f)
        os.chmod(path, 0o600)

def get_current_kaggle_config():
    accounts = [(os.getenv(f"KAG_USER{i}", "").strip(), os.getenv(f"KAG_KEY{i}", "").strip()) for i in range(1, 5) if os.getenv(f"KAG_USER{i}", "")]
    if not accounts:
        return None, None, False, 0
    tz = pytz.timezone('Asia/Kolkata')
    now = datetime.now(tz)
    active_idx = (now - datetime(2026, 1, 1, tzinfo=tz)).days % len(accounts)
    return accounts[active_idx][0], accounts[active_idx][1], True, active_idx + 1

def get_current_hardware_mode():
    """Returns 'cpu' or 'gpu' based on India Time (IST) schedule."""
    tz = pytz.timezone('Asia/Kolkata')
    now = datetime.now(tz)
    if 0 <= now.hour < 12:
        return "cpu"
    else:
        return "gpu"

# --- KAGGER KERNEL API METHODS ---
async def trigger_kaggle_run_async(username, api_key, slug, code_content, hw_mode):
    setup_kaggle_credentials(username, api_key)
    os.environ.update({"KAGGLE_USERNAME": username, "KAGGLE_KEY": api_key})
    enable_gpu = True if hw_mode == "gpu" else False
    
    tmpdir = tempfile.mkdtemp()
    try:
        with open(os.path.join(tmpdir, "main.py"), "w", encoding="utf-8") as f:
            f.write(code_content)
        with open(os.path.join(tmpdir, "kernel-metadata.json"), "w") as f:
            json.dump({
                "id": f"{username}/{slug}",
                "title": slug,
                "code_file": "main.py",
                "language": "python",
                "kernel_type": "script",
                "is_private": True,
                "enable_gpu": enable_gpu,
                "enable_internet": True,
                "dataset_sources": []
            }, f)
        proc = await asyncio.create_subprocess_exec(
            "kaggle", "kernels", "push", "-p", tmpdir,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=45)
            return proc.returncode == 0, stdout.decode(), stderr.decode()
        except asyncio.TimeoutError:
            try: proc.kill()
            except: pass
            return False, "", "Timeout pushing to Kaggle"
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

async def check_kaggle_status_async(username, api_key, slug):
    os.environ.update({"KAGGLE_USERNAME": username, "KAGGLE_KEY": api_key})
    proc = await asyncio.create_subprocess_exec(
        "kaggle", "kernels", "status", f"{username}/{slug}",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
        return stdout.decode().lower()
    except asyncio.TimeoutError:
        try: proc.kill()
        except: pass
        return "running"

async def kill_all_kaggle_kernels_internal():
    """Kills and purges persistent worker instances on all accounts to prevent duplicate responses."""
    for i in range(1, 5):
        username = os.getenv(f"KAG_USER{i}", "").strip()
        api_key = os.getenv(f"KAG_KEY{i}", "").strip()
        if not username or not api_key:
            continue
        try:
            setup_kaggle_credentials(username, api_key)
            os.environ.update({"KAGGLE_USERNAME": username, "KAGGLE_KEY": api_key})
            ref = f"{username}/da-persistent-worker"
            proc = await asyncio.create_subprocess_exec(
                "kaggle", "kernels", "delete", "-k", ref,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            await proc.communicate()
        except Exception as e:
            print(f"Error purging account {username}: {e}")

# --- KAGGER BACKGROUND KEEP-ALIVE LOOP ---
def get_kaggle_worker_code():
    """Generates the dynamic worker script to push to Kaggle."""
    with open("asi.py", "r", encoding="utf-8") as f:
        core_worker_code = f.read()
    
    config_header = f"""# Dynamically generated config
API_ID = {API_ID}
API_HASH = "{API_HASH}"
BOT_TOKEN = "{BOT_TOKEN}"
DESK_CHANNEL_ID = {DESK_CHANNEL_ID}
"""
    return config_header + "\n" + core_worker_code

async def kaggle_keep_alive_scheduler():
    global current_running_hw, current_running_acc
    await asyncio.sleep(10) # Post-boot grace period
    while True:
        try:
            username, api_key, available, acc_num = get_current_kaggle_config()
            if not username:
                await asyncio.sleep(60)
                continue
            
            desired_hw = get_current_hardware_mode()
            slug = "da-persistent-worker"
            
            # If time shift or daily account rotation occurred, stop old worker sessions
            if current_running_acc != acc_num or current_running_hw != desired_hw:
                print(f"🔄 Shift detected! Rotating Kaggle instances...")
                await kill_all_kaggle_kernels_internal()
                current_running_acc = acc_num
                current_running_hw = desired_hw
                
            status = await check_kaggle_status_async(username, api_key, slug)
            if "running" not in status:
                print(f"🚀 Deploying Persistent Kaggle Worker: Account #{acc_num} ({username}), Mode: {desired_hw.upper()}")
                worker_code = get_kaggle_worker_code()
                succ, out, err = await trigger_kaggle_run_async(username, api_key, slug, worker_code, desired_hw)
                if succ:
                    print(f"✅ Persistent worker initialized successfully.")
                    current_running_acc = acc_num
                    current_running_hw = desired_hw
                else:
                    print(f"❌ Worker deploy failed: {err}")
        except Exception as e:
            print(f"Keep-alive exception: {e}")
        await asyncio.sleep(180) # Check status every 3 minutes

# --- GENERAL TELEGRAM COMMANDS ---
async def get_pinned_file_link(chat_id, target_name):
    try:
        chat = await app.get_chat(chat_id)
        if chat.pinned_message and chat.pinned_message.text and f"Name – {target_name}" in chat.pinned_message.text:
            match = re.search(r"Link – (https://\S+)", chat.pinned_message.text)
            if match: return match.group(1)
        async for msg in app.get_chat_history(chat_id, limit=50):
            if msg.text and f"Name – {target_name}" in msg.text:
                match = re.search(r"Link – (https://\S+)", msg.text)
                if match: return match.group(1)
    except:
        pass
    return "none"

@app.on_message(filters.command(["start", "stats", "addposition", "admark", "deletmark", "addfont", "removefont"]))
async def general_cmds(c, m: Message):
    cmd = m.command[0]
    if cmd == "start" and m.chat.type == ChatType.PRIVATE:
        if m.from_user.id in [OWNER_ID, ALLOWED_USER]:
            return await m.reply("🙋‍♂️ Welcome Owner!")
        return await check_command_privacy(c, m)
    if not await check_command_privacy(c, m): return

    if cmd == "stats":
        ram = psutil.virtual_memory()
        cpu = psutil.cpu_percent()
        await m.reply(f"📊 **Bot Diagnostics:**\n🖥️ CPU: `{cpu}%`\n💾 RAM: `{ram.percent}%`\n🔄 Current Node: Account #{current_running_acc or 'None'} ({current_running_hw or 'None'})")
    elif cmd == "addposition":
        if len(m.command) < 2 or m.command[1].lower() not in ["left", "right"]:
            return await m.reply("❌ Usage: /addposition left|right")
        wm_positions[m.chat.id] = m.command[1].lower()
        await m.reply(f"✅ Watermark position updated: **{m.command[1].upper()}**")
    elif cmd in ["admark", "addfont"]:
        if not m.reply_to_message or not (m.reply_to_message.photo or m.reply_to_message.document):
            return await m.reply("❌ Reply to a file.")
        msg_link = f"https://t.me/c/{str(m.chat.id)[4:]}/{m.reply_to_message.id}"
        t_name = "watermark" if cmd == "admark" else "file"
        pinned = await m.reply(f"ID – {m.from_user.id}\nLink – {msg_link}\nName – {t_name}")
        await pinned.pin()
        await m.reply(f"✅ Configuration saved.")
    elif cmd in ["deletmark", "removefont"]:
        chat = await c.get_chat(m.chat.id)
        t_name = "watermark" if cmd == "deletmark" else "file"
        if chat.pinned_message and f"Name – {t_name}" in chat.pinned_message.text:
            await chat.pinned_message.unpin()
            await m.reply("🗑️ Registry removed.")
        else:
            await m.reply("❌ Registry not found.")

@app.on_message(filters.command("clean"))
async def clean_cmd(c, m: Message):
    if not await check_command_privacy(c, m): return
    uid = m.from_user.id
    if uid in users_data:
        users_data.pop(uid)
        await m.reply("🧹 **Your active configuration session has been refreshed and cleared!**")
    else:
        await m.reply("❌ **You do not have any active configuration session.**")

@app.on_message(filters.command("kill"))
async def kill_cmd(c, m: Message):
    if not is_authorized(m): return
    status_msg = await m.reply("🗑️ **Querying and terminating active running worker scripts across all accounts...**")
    await kill_all_kaggle_kernels_internal()
    await status_msg.edit("✅ **All persistent Kaggle worker notebooks terminated and purged.**")

# --- CONSOLE COMPRESSION COMMANDS ---
RES_CMD_MAP = {"1080g": "1080p", "720g": "720p", "480g": "480p"}

@app.on_message(filters.command(["1080g", "720g", "480g"]))
async def compress_cmd(c, m: Message):
    if not await check_command_privacy(c, m): return
    media = m.reply_to_message.video or m.reply_to_message.document or m.reply_to_message.animation if m.reply_to_message else None
    if not media:
        return await m.reply("❌ Compression task ke liye kisi valid video/document par reply karein.")
    
    cmd = RES_CMD_MAP[m.command[0].lower()]
    orig_name = getattr(media, "file_name", "output.mp4")
    
    st = await m.reply(f"⏳ **Task Dispatched to Kaggle!**\nProcessing will begin instantly on Account #{current_running_acc or '1'}...")
    font_link = await get_pinned_file_link(m.chat.id, "file")

    task_payload = {
        "task_id": f"task_{int(time.time())}_{m.from_user.id}",
        "task_type": "compress",
        "video_id": f"https://t.me/c/{str(m.chat.id)[4:]}/{m.reply_to_message.id}",
        "sub_id": "none",
        "chat_id": m.chat.id,
        "user_id": m.from_user.id,
        "resolution": cmd,
        "wm_id": "none",
        "wm_pos": "none",
        "rename": orig_name,
        "font_link": font_link,
        "trigger_msg_id": st.id
    }
    # Enqueue task to DESK_CHANNEL_ID
    await app.send_message(DESK_CHANNEL_ID, f"[TASK_QUEUE] {json.dumps(task_payload)}")

# --- CONSOLE HARDSUB COMMANDS ---
@app.on_message(filters.command("sub"))
async def hsub_cmd(c, m: Message):
    if not await check_command_privacy(c, m): return
    media = m.reply_to_message.video or m.reply_to_message.document or m.reply_to_message.animation if m.reply_to_message else None
    if not media:
        return await m.reply("❌ Hardsub ke liye kisi forwarded video par reply karein.")

    orig_name = getattr(media, "file_name", "output.mp4")
    await m.reply("Send subtitle file (vtt/srt/ass) or type `S` to skip.")
    users_data[m.from_user.id] = {
        "video_msg_id": m.reply_to_message.id,
        "chat_id": m.chat.id,
        "state": "WAIT_SUB",
        "rename": "none",
        "orig_name": orig_name
    }

async def prompt_watermark_or_execute(c, m, user_id, session):
    wm_link = await get_pinned_file_link(session["chat_id"], "watermark")
    if wm_link != "none":
        session["state"] = "WAIT_WM_CHOICE"
        await m.reply("Add watermark? Type `A` for Add or `S` to skip.")
    else:
        session["watermark"] = "no"
        await execute_dispatch_hardsub(user_id, m)

@app.on_message(filters.text | filters.document)
async def replies_controller(c, m: Message):
    if not m.from_user or (m.text and m.text.startswith("/")): return
    user_id = m.from_user.id
    if user_id not in users_data: return
    session = users_data[user_id]
    if session["chat_id"] != m.chat.id: return
    
    state, text = session.get("state"), m.text.strip().upper() if m.text else ""
    
    if state == "WAIT_SUB":
        if m.document and m.document.file_name and m.document.file_name.lower().endswith(('.srt', '.ass', '.vtt', '.txt')):
            session["sub_msg_link"] = f"https://t.me/c/{str(m.chat.id)[4:]}/{m.id}"
            session["state"] = "WAIT_RENAME_CHOICE"
            await m.reply("Rename type `R` / Same name type `S`")
        elif text == "S":
            session["sub_msg_link"] = "none"
            session["state"] = "WAIT_RENAME_CHOICE"
            await m.reply("Rename type `R` / Same name type `S`")
        else:
            await m.reply("❌ Invalid format! Please send a valid subtitle file (.srt, .ass, .vtt) or type `S` to skip.")
        return

    if state == "WAIT_RENAME_CHOICE":
        if text == "R":
            session["state"] = "WAIT_RENAME_VALUE"
            await m.reply("Send new file name:")
        elif text == "S":
            session["rename"] = session["orig_name"]
            await prompt_watermark_or_execute(c, m, user_id, session)
        else:
            await m.reply("❌ Invalid! Type `R` to rename or `S` to skip.")
        return
            
    elif state == "WAIT_RENAME_VALUE":
        if not m.text:
            return await m.reply("❌ Please send a valid text name.")
        
        # Kept exactly as entered by user, stripping path characters for security
        clean_name = m.text.strip().replace("/", "").replace("\\", "")
        if not clean_name.lower().endswith(".mp4"):
            clean_name += ".mp4"
            
        session["rename"] = clean_name
        await prompt_watermark_or_execute(c, m, user_id, session)
        return
        
    elif state == "WAIT_WM_CHOICE":
        if text == "A":
            session["watermark"] = "yes"
        elif text == "S":
            session["watermark"] = "no"
        else:
            return await m.reply("❌ Invalid! Type `A` to add watermark or `S` to skip.")
        await execute_dispatch_hardsub(user_id, m)

async def execute_dispatch_hardsub(user_id, msg: Message):
    data = users_data.pop(user_id)
    
    st = await msg.reply(f"⏳ **Task Dispatched to Kaggle!**\nProcessing will begin instantly on Account #{current_running_acc or '1'}...")
    wm_link = "none"
    wm_pos = "right"
    if data.get("watermark") == "yes":
        wm_link = await get_pinned_file_link(data["chat_id"], "watermark")
        wm_pos = wm_positions.get(data["chat_id"], "right")

    task_payload = {
        "task_id": f"task_{int(time.time())}_{user_id}",
        "task_type": "hardsub",
        "video_id": f"https://t.me/c/{str(data['chat_id'])[4:]}/{data['video_msg_id']}",
        "sub_id": data.get("sub_msg_link", "none"),
        "chat_id": data["chat_id"],
        "user_id": user_id,
        "resolution": "none",
        "wm_id": wm_link,
        "wm_pos": wm_pos,
        "rename": data.get("rename", "none"),
        "font_link": await get_pinned_file_link(data["chat_id"], "file"),
        "trigger_msg_id": st.id
    }
    # Enqueue task to DESK_CHANNEL_ID
    await app.send_message(DESK_CHANNEL_ID, f"[TASK_QUEUE] {json.dumps(task_payload)}")

# --- CONCEL TIMEOUT EVENTS ---
@app.on_callback_query(filters.regex("cancel_active_run"))
async def cancel_run_callback(c, q: CallbackQuery):
    if q.from_user.id not in [OWNER_ID, ALLOWED_USER]:
        return await q.answer("❌ You are not authorized to cancel tasks.", show_alert=True)
    
    # We can delete queued task messages inside DESK_CHANNEL_ID easily
    await q.message.edit("🛑 **Task Queue Canceled successfully.**")
    await q.answer("Aborted", show_alert=True)

# --- WEB SERVER FOR PORT BINDING ---
class Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot Operational")

async def main():
    # Start HTTP server
    threading.Thread(target=lambda: HTTPServer(("0.0.0.0", PORT), Health).serve_forever(), daemon=True).start()
    print(f"📡 Web server bound to port {PORT}")
    
    await app.start()
    print("🚀 Frontend Bot Connected Successfully!")
    
    # Purge any old notebook instances from previous runs on boot
    print("🧹 Cleaning up old active Kaggle notebooks on boot...")
    await kill_all_kaggle_kernels_internal()
    
    # Run the background keep-alive loop
    asyncio.create_task(kaggle_keep_alive_scheduler())
    
    await idle()
    await app.stop()

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
