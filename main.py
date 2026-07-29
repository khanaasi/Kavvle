import os
import re
import time
import asyncio
import threading
import shutil
import json
import psutil
from fastapi import FastAPI
import uvicorn
from pyrogram import Client, filters, idle
from pyrogram.types import Message, CallbackQuery
from pyrogram.enums import ChatType

# --- CONFIGURATIONS ---
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
PORT = int(os.getenv("PORT", 10000))

OWNER_ID = 5344078567
ALLOWED_USER = 5351848105
GROUP_ID = -1003899919015
DESK_CHANNEL_ID = -1003700822969

# 4 Kaggle Accounts Setup
KAG_ACCOUNTS = []
for i in range(1, 5):
    user = os.getenv(f"KAG_ACC{i}_USER", "").strip()
    key = os.getenv(f"KAG_ACC{i}_KEY", "").strip()
    if user and key:
        KAG_ACCOUNTS.append({"user": user, "key": key, "active_gpu": 0, "active_cpu": 0})

MAX_GPU_PER_ACC = 1
MAX_CPU_PER_ACC = 1

app = Client("RenderManagerBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workers=16)

# Queue and Task Managers
task_queue = []
active_tasks = {}  # format: {task_id: {account_idx: x, type: 'cpu'/'gpu', msg: Message, ...}}
users_data = {}
wm_positions = {}

# Ensure Kaggle config folder exists
os.makedirs(os.path.expanduser("~/.kaggle"), exist_ok=True)

# --- WEB SERVER FOR RENDER HEALTH CHECK ---
web_app = FastAPI()

@web_app.get("/")
def read_root():
    return {"status": "operational", "queue_len": len(task_queue), "active_jobs": len(active_tasks)}

def run_web_server():
    uvicorn.run(web_app, host="0.0.0.0", port=PORT, log_level="warning")

threading.Thread(target=run_web_server, daemon=True).start()

# --- AUTHORIZATION AND CONTROLLERS ---
def is_authorized(m: Message):
    if not m.from_user: return False
    u_id = m.from_user.id
    if u_id in [OWNER_ID, ALLOWED_USER]: return True
    if m.chat and m.chat.id == GROUP_ID: return True
    return False

async def check_command_privacy(c, m: Message):
    is_pm = m.chat.type == ChatType.PRIVATE
    if is_pm and m.from_user.id in [OWNER_ID, ALLOWED_USER]: return True
    if is_pm:
        try:
            chat_info = await c.get_chat(GROUP_ID)
            invite_link = chat_info.invite_link or "https://t.me/Mangajii"
        except:
            invite_link = "https://t.me/Mangajii"
        await m.reply(f"❌ **Aap is Bot ko Private mein use nahi kar sakte!**\n\n👉 Humara [Official Group]({invite_link}) join karein.", disable_web_page_preview=True)
        return False
    return is_authorized(m)

async def get_pinned_file_link(chat_id, target_name):
    try:
        chat = await app.get_chat(chat_id)
        if chat.pinned_message and chat.pinned_message.text and f"Name – {target_name}" in chat.pinned_message.text:
            match = re.search(r"Link – (https://\S+)", chat.pinned_message.text)
            if match: return match.group(1)
        async for msg in app.get_chat_history(chat_id, limit=30):
            if msg.text and f"Name – {target_name}" in msg.text:
                match = re.search(r"Link – (https://\S+)", msg.text)
                if match: return match.group(1)
    except:
        pass
    return "none"

# --- KAGGLE EXECUTION ENGINE ---
def authenticate_kaggle(username, key):
    creds = {"username": username, "key": key}
    with open(os.path.expanduser("~/.kaggle/kaggle.json"), "w") as f:
        json.dump(creds, f)
    # Import inside function to reload credentials
    import kaggle
    from kaggle.api.kaggle_api_extended import KaggleApi
    api = KaggleApi()
    api.authenticate()
    return api

def run_kaggle_kernel(username, key, payload, slot_type):
    try:
        api = authenticate_kaggle(username, key)
        slug = f"samia-worker-{slot_type}"
        work_dir = f"kaggle_run_{username}_{slot_type}"
        if os.path.exists(work_dir):
            shutil.rmtree(work_dir)
        os.makedirs(work_dir, exist_ok=True)

        # Create worker_launcher.py with embedded config variables
        launcher_code = f"""import os
os.environ["API_ID"] = "{API_ID}"
os.environ["API_HASH"] = "{API_HASH}"
os.environ["BOT_TOKEN"] = "{BOT_TOKEN}"
os.environ["TASK_TYPE"] = "{payload['task_type']}"
os.environ["VIDEO_ID"] = "{payload['video_id']}"
os.environ["SUB_ID"] = "{payload['sub_id']}"
os.environ["CHAT_ID"] = "{payload['chat_id']}"
os.environ["USER_ID"] = "{payload['user_id']}"
os.environ["RESOLUTION"] = "{payload['resolution']}"
os.environ["WM_ID"] = "{payload['wm_id']}"
os.environ["WM_POS"] = "{payload['wm_pos']}"
os.environ["RENAME"] = "{payload['rename']}"
os.environ["FONT_LINK"] = "{payload['font_link']}"
os.environ["TRIGGER_MSG_ID"] = "{payload['trigger_msg_id']}"

import worker
import asyncio
asyncio.run(worker.main())
"""
        with open(os.path.join(work_dir, "worker_launcher.py"), "w") as f:
            f.write(launcher_code)

        # Copy original worker.py to launch folder
        shutil.copy("worker.py", os.path.join(work_dir, "worker.py"))

        # Create kernel-metadata.json
        meta = {
            "id": f"{username}/{slug}",
            "title": slug,
            "code_file": "worker_launcher.py",
            "language": "python",
            "kernel_type": "script",
            "is_private": True,
            "enable_gpu": True if slot_type == "gpu" else False,
            "enable_internet": True,
            "dataset_sources": [],
            "kernel_sources": [],
            "competition_sources": []
        }
        with open(os.path.join(work_dir, "kernel-metadata.json"), "w") as f:
            json.dump(meta, f)

        api.kernels_push(work_dir)
        return True, slug
    except Exception as e:
        return False, str(e)

async def monitor_queue_and_tasks():
    while True:
        await asyncio.sleep(15)
        # Check active runs
        for task_id, info in list(active_tasks.items()):
            acc_idx = info["account_idx"]
            acc = KAG_ACCOUNTS[acc_idx]
            slot_type = info["type"]
            try:
                api = await asyncio.to_thread(authenticate_kaggle, acc["user"], acc["key"])
                status_res = await asyncio.to_thread(api.kernel_status, acc["user"], f"samia-worker-{slot_type}")
                status = status_res.get("status", "error")
                
                if status in ["complete", "error", "stopped"]:
                    if slot_type == "gpu":
                        acc["active_gpu"] = max(0, acc["active_gpu"] - 1)
                    else:
                        acc["active_cpu"] = max(0, acc["active_cpu"] - 1)
                    active_tasks.pop(task_id, None)
                    print(f"Task finished on {acc['user']} ({slot_type}). Status: {status}")
            except Exception as e:
                print(f"Error checking status for {acc['user']}: {e}")

        # Deploy waiting items from queue
        if task_queue:
            for task in list(task_queue):
                # Check task requirements
                task_type = task["payload"]["task_type"]
                slot_needed = "gpu" if task_type in ["transcribe", "whisper"] else "cpu"
                
                allocated = False
                for idx, acc in enumerate(KAG_ACCOUNTS):
                    if slot_needed == "gpu" and acc["active_gpu"] < MAX_GPU_PER_ACC:
                        acc["active_gpu"] += 1
                        allocated = True
                    elif slot_needed == "cpu" and acc["active_cpu"] < MAX_CPU_PER_ACC:
                        acc["active_cpu"] += 1
                        allocated = True
                        
                    if allocated:
                        task_queue.remove(task)
                        task_id = f"task_{int(time.time())}_{idx}"
                        
                        # Trigger task
                        success, desc = await asyncio.to_thread(
                            run_kaggle_kernel, acc["user"], acc["key"], task["payload"], slot_needed
                        )
                        if success:
                            active_tasks[task_id] = {"account_idx": idx, "type": slot_needed, "payload": task["payload"]}
                            try:
                                await app.edit_message_text(
                                    int(task["payload"]["chat_id"]), 
                                    int(task["payload"]["trigger_msg_id"]),
                                    f"🚀 **Task launched successfully on Kaggle!**\nServer: `{acc['user']}`\nSlot Type: `{slot_needed.upper()}`\nProcessing starting now..."
                                )
                            except: pass
                        else:
                            # Re-add to queue on failure and release slot
                            if slot_needed == "gpu": acc["active_gpu"] = max(0, acc["active_gpu"] - 1)
                            else: acc["active_cpu"] = max(0, acc["active_cpu"] - 1)
                            task_queue.insert(0, task)
                            try:
                                await app.edit_message_text(
                                    int(task["payload"]["chat_id"]), 
                                    int(task["payload"]["trigger_msg_id"]),
                                    f"⚠️ Kaggle trigger failed on `{acc['user']}`. Re-queuing task...\nError: `{desc}`"
                                )
                            except: pass
                        break

# --- BOT COMMANDS IMPLEMENTATION ---
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
        q_len = len(task_queue)
        act_len = len(active_tasks)
        await m.reply(f"📊 **System Status:**\n🖥️ CPU: `{cpu}%`\n💾 RAM: `{ram.percent}%`\n⏳ Queued Tasks: `{q_len}`\n⚙️ Active Processing: `{act_len}`")
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

RES_CMD_MAP = {"1080g": "1080p", "720g": "720p", "480g": "480p"}

@app.on_message(filters.command(["1080g", "720g", "480g"]))
async def compress_cmd(c, m: Message):
    if not await check_command_privacy(c, m): return
    media = m.reply_to_message.video or m.reply_to_message.document or m.reply_to_message.animation if m.reply_to_message else None
    if not media: 
        return await m.reply("❌ Compression task ke liye kisi valid video/document par reply karein.")
    
    cmd = RES_CMD_MAP[m.command[0].lower()]
    orig_name = getattr(media, "file_name", "output.mp4")
    
    st = await m.reply("⏳ **Task added to queue...** Waiting for free Kaggle slot.")
    font_link = await get_pinned_file_link(m.chat.id, "file")

    payload = {
        "task_type": "compress", "video_id": f"https://t.me/c/{str(m.chat.id)[4:]}/{m.reply_to_message.id}",
        "sub_id": "none", "chat_id": str(m.chat.id), "user_id": str(m.from_user.id),
        "resolution": cmd, "wm_id": "none", "wm_pos": "none", "rename": orig_name, 
        "font_link": font_link, "trigger_msg_id": str(st.id)
    }
    task_queue.append({"payload": payload})

@app.on_message(filters.command("sub"))
async def hsub_cmd(c, m: Message):
    if not await check_command_privacy(c, m): return
    media = m.reply_to_message.video or m.reply_to_message.document or m.reply_to_message.animation if m.reply_to_message else None
    if not media: 
        return await m.reply("❌ Hardsub ke liye kisi forwarded video par reply karein.")

    orig_name = getattr(media, "file_name", "output.mp4")
    await m.reply("Send subtitle file (vtt/srt/ass) or type `S` to skip.")
    users_data[m.from_user.id] = {"video_msg_id": m.reply_to_message.id, "chat_id": m.chat.id, "state": "WAIT_SUB", "rename": "none", "orig_name": orig_name}

# --- ADD TRANSCRIPTION (WHISPER) COMMAND ---
@app.on_message(filters.command("transcribe"))
async def transcribe_cmd(c, m: Message):
    if not await check_command_privacy(c, m): return
    media = m.reply_to_message.video or m.reply_to_message.document or m.reply_to_message.audio if m.reply_to_message else None
    if not media:
        return await m.reply("❌ Audio extraction aur subtitle transcription ke liye video ya audio par reply karein.")

    st = await m.reply("⏳ **Transcription task added to queue...** (Demands GPU slot)")
    payload = {
        "task_type": "transcribe", "video_id": f"https://t.me/c/{str(m.chat.id)[4:]}/{m.reply_to_message.id}",
        "sub_id": "none", "chat_id": str(m.chat.id), "user_id": str(m.from_user.id),
        "resolution": "none", "wm_id": "none", "wm_pos": "none", "rename": "none",
        "font_link": "none", "trigger_msg_id": str(st.id)
    }
    task_queue.append({"payload": payload})

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
            await m.reply("❌ Invalid format! Please send a subtitle file (.srt, .ass, .vtt) or type `S` to skip.")
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
        if not text: 
            return await m.reply("❌ Please send a valid text name.")
        raw_name = m.text.strip()
        if raw_name.lower().endswith(".mp4"): 
            raw_name = raw_name[:-4]
        session["rename"] = re.sub(r'[^\w\-_]', '_', raw_name) + ".mp4"
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
    
    st = await msg.reply("⏳ **Hardsub task added to queue...** Waiting for slot.")
    wm_link = "none"
    wm_pos = "right"
    if data.get("watermark") == "yes":
        wm_link = await get_pinned_file_link(data["chat_id"], "watermark")
        wm_pos = wm_positions.get(data["chat_id"], "right")

    payload = {
        "task_type": "hardsub", "video_id": f"https://t.me/c/{str(data['chat_id'])[4:]}/{data['video_msg_id']}",
        "sub_id": data.get("sub_msg_link", "none"), "chat_id": str(data["chat_id"]), "user_id": str(user_id),
        "resolution": "none", "wm_id": wm_link, "wm_pos": wm_pos, "rename": data.get("rename", "none"),
        "font_link": await get_pinned_file_link(data["chat_id"], "file"), "trigger_msg_id": str(st.id)
    }
    task_queue.append({"payload": payload})

# --- KILL / CANCEL WORKFLOW RUNS ---
@app.on_message(filters.command("kill"))
async def kill_task_cmd(c, m: Message):
    if m.from_user.id not in [OWNER_ID, ALLOWED_USER]:
        return await m.reply("❌ You are not authorized to abort tasks.")
    
    task_queue.clear()
    aborted_counts = 0
    
    st = await m.reply("⚙️ Aborting all running Kaggle kernels across all accounts. Please wait...")
    
    for acc in KAG_ACCOUNTS:
        try:
            api = authenticate_kaggle(acc["user"], acc["key"])
            for slot_type in ["cpu", "gpu"]:
                api.kernel_cancel(f"{acc['user']}/samia-worker-{slot_type}")
                aborted_counts += 1
            acc["active_gpu"] = 0
            acc["active_cpu"] = 0
        except Exception as e:
            print(f"Abort error for account {acc['user']}: {e}")
            
    active_tasks.clear()
    await st.edit(f"🛑 **All queues cleared and {aborted_counts} Kaggle worker slots force stopped successfully!**")

async def main_run():
    asyncio.create_task(monitor_queue_and_tasks())
    await app.start()
    print("🚀 Render Manager Bot connected!")
    await idle()
    await app.stop()

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main_run())
