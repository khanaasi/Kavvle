import os
import re
import json
import base64
import asyncio
import subprocess
import datetime
import http.server
import threading
import psutil
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums import ChatType

# ── CONFIGURATION & ENVIRONMENT ──────────────────────────────────────
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

OWNER_ID = int(os.environ.get("OWNER_ID", "5344078567"))
ALLOWED_USER = int(os.environ.get("ALLOWED_USER", "5351848105"))
GROUP_ID = int(os.environ.get("GROUP_ID", "-1003899919015"))
DESK_CHANNEL_ID = int(os.environ.get("DESK_CHANNEL_ID", "-1003700822969"))

KERNEL_PREFIX = "hsbot-"

# Multi-Account Loader for Kaggle
def load_kaggle_accounts():
    accs = []
    i = 1
    while True:
        u = os.environ.get(f"KAG_USER{i}")
        k = os.environ.get(f"KAG_KEY{i}")
        if not u or not k:
            break
        accs.append({"idx": i, "user": u, "key": k})
        i += 1
    if not accs:
        raise RuntimeError("Koi bhi KAG_USER1/KAG_KEY1 environment variable set nahi hai.")
    return accs

KAG_ACCOUNTS = load_kaggle_accounts()

app = Client("HarsubBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workers=16)

# States and configurations
users_data = {}
wm_positions = {}  # chat_id -> "left" | "right"

task_queue = asyncio.Queue()
account_busy = {a["idx"]: False for a in KAG_ACCOUNTS}
running_tasks = {}  # task_id -> {"account_idx": idx, "kernel": slug, "cancel": bool}
task_counter = 0

# ── HELPERS & PRIVACY ────────────────────────────────────────────────
def current_hw_mode():
    # IST calculation: Raat ke 12 se dopehar ke 12 tak CPU, dopehar se raat tak GPU
    ist = datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)
    return "cpu" if 0 <= ist.hour < 12 else "gpu"

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

async def get_pinned_file_link(chat_id, target_name):
    try:
        chat = await app.get_chat(chat_id)
        if chat.pinned_message and chat.pinned_message.text and f"Name – {target_name}" in chat.pinned_message.text:
            match = re.search(r"Link – (https://\S+)", chat.pinned_message.text)
            if match: 
                return match.group(1)
        async for msg in app.get_chat_history(chat_id, limit=50):
            if msg.text and f"Name – {target_name}" in msg.text:
                match = re.search(r"Link – (https://\S+)", msg.text)
                if match: 
                    return match.group(1)
    except: 
        pass
    return "none"

# ── KAGGLE CONTROL HELPERS ──────────────────────────────────────────
def _kaggle_env(account):
    env = os.environ.copy()
    env["KAGGLE_USERNAME"] = account["user"]
    env["KAGGLE_KEY"] = account["key"]
    return env

def kaggle_list_kernels(account):
    try:
        out = subprocess.run(
            ["kaggle", "kernels", "list", "-m", "--csv"],
            env=_kaggle_env(account), capture_output=True, text=True, timeout=60
        )
        lines = out.stdout.strip().splitlines()
        refs = []
        for line in lines[1:]:
            ref = line.split(",")[0].strip()
            if KERNEL_PREFIX in ref:
                refs.append(ref)
        return refs
    except Exception:
        return []

def kaggle_delete_kernel(account, ref):
    try:
        subprocess.run(["kaggle", "kernels", "delete", "-y", ref],
                        env=_kaggle_env(account), capture_output=True, text=True, timeout=60)
        return True
    except Exception:
        return False

def kill_all_notebooks():
    deleted = []
    for acc in KAG_ACCOUNTS:
        for ref in kaggle_list_kernels(acc):
            if kaggle_delete_kernel(acc, ref):
                deleted.append(ref)
    return deleted

def kaggle_push_kernel(account, slug, payload: dict, hw_mode: str):
    workdir = f"/tmp/{slug}"
    os.makedirs(workdir, exist_ok=True)

    asi_path = os.path.join(os.path.dirname(__file__), "asi.py")
    if not os.path.exists(asi_path):
        return False, "Worker file asi.py not found on controller!"

    asi_code = open(asi_path, "r", encoding="utf-8").read()
    with open(os.path.join(workdir, f"{slug}.py"), "w", encoding="utf-8") as f:
        f.write(asi_code)

    meta = {
        "id": f"{account['user']}/{slug}",
        "title": slug,
        "code_file": f"{slug}.py",
        "language": "python",
        "kernel_type": "script",
        "is_private": True,
        "enable_gpu": hw_mode == "gpu",
        "enable_internet": True,
        "keywords": [],
        "dataset_sources": [],
        "kernel_sources": [],
        "competition_sources": [],
        "model_sources": []
    }
    with open(os.path.join(workdir, "kernel-metadata.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f)

    cfg = json.dumps(payload)
    cfg_b64 = base64.b64encode(cfg.encode()).decode()
    with open(os.path.join(workdir, f"{slug}.py"), "a", encoding="utf-8") as f:
        f.write(f"\n\n# __INJECTED_CONFIG__\nCONFIG_B64 = {cfg_b64!r}\n")

    r = subprocess.run(["kaggle", "kernels", "push", "-p", workdir],
                        env=_kaggle_env(account), capture_output=True, text=True, timeout=90)
    return r.returncode == 0, r.stdout + r.stderr

def kaggle_kernel_status(account, ref):
    try:
        r = subprocess.run(["kaggle", "kernels", "status", ref],
                            env=_kaggle_env(account), capture_output=True, text=True, timeout=30)
        return r.stdout.strip()
    except Exception as e:
        return str(e)

# ── QUEUE WORKER THREADS ─────────────────────────────────────────────
async def account_worker(account):
    idx = account["idx"]
    account_user = account["user"]
    while True:
        task = await task_queue.get()
        if task.get("cancelled"):
            task_queue.task_done()
            continue
        task_id = task["task_id"]
        try:
            account_busy[idx] = True
            hw_mode = current_hw_mode()
            slug = f"{KERNEL_PREFIX}{task_id}"
            running_tasks[task_id] = {"account_idx": idx, "kernel": f"{account_user}/{slug}", "cancel": False}

            # Update the user about Account & Mode being used
            status_text = (
                f"🚀 **Task is being processed!**\n\n"
                f"👤 **Account:** Worker Account #{idx} (`{account_user}`)\n"
                f"⚡ **Hardware Mode:** `{hw_mode.upper()}`\n"
                f"⚙️ *Worker has been initialized and is downloading media...*"
            )
            try:
                await app.edit_message_text(
                    task["chat_id"], task["status_msg_id"], status_text,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Cancel Task", callback_data="cancel_active_run")]])
                )
            except Exception:
                pass

            ok, log = await asyncio.to_thread(kaggle_push_kernel, account, slug, task["payload"], hw_mode)
            if not ok:
                await report_error(task, f"Kaggle push fail (account {idx}):\n{log[-800:]}")
                continue

            # Poll status until complete/error/cancel
            for _ in range(2160):  # ~12hr max
                if running_tasks.get(task_id, {}).get("cancel"):
                    await asyncio.to_thread(kaggle_delete_kernel, account, f"{account_user}/{slug}")
                    break
                st = await asyncio.to_thread(kaggle_kernel_status, account, f"{account_user}/{slug}")
                if "complete" in st.lower() or "error" in st.lower() or "cancel" in st.lower():
                    break
                await asyncio.sleep(20)

            await asyncio.to_thread(kaggle_delete_kernel, account, f"{account_user}/{slug}")
        except Exception as e:
            await report_error(task, f"Worker error: {e}")
        finally:
            account_busy[idx] = False
            running_tasks.pop(task_id, None)
            task_queue.task_done()

async def report_error(task, text):
    try:
        await app.send_message(task["chat_id"], f"❌ Error:\n{text}")
    except Exception:
        pass

async def enqueue_task(chat_id, status_msg_id, payload):
    global task_counter
    task_counter += 1
    task_id = task_counter
    payload["trigger_msg_id"] = str(status_msg_id)
    
    await task_queue.put({
        "task_id": task_id, "chat_id": chat_id,
        "status_msg_id": status_msg_id, "payload": payload
    })
    
    pos = task_queue.qsize()
    if pos > 0:
        try:
            await app.edit_message_text(
                chat_id, status_msg_id, 
                f"⏳ **Task Queued!**\n\n🔢 **Queue Position:** `{pos}`\nServer abhi busy hai, aapka task queue me lag gaya hai."
            )
        except Exception:
            pass
    return task_id

# ── COMMANDS & UTILITIES ─────────────────────────────────────────────
@app.on_message(filters.command(["start", "stats", "addposition", "admark", "deletmark", "addfont", "removefont"]))
async def general_cmds(c, m: Message):
    cmd = m.command[0]
    if cmd == "start" and m.chat.type == ChatType.PRIVATE:
        if m.from_user.id in [OWNER_ID, ALLOWED_USER]: 
            return await m.reply("🙋‍♂️ Welcome Owner!")
        return await check_command_privacy(c, m)
    if not await check_command_privacy(c, m): 
        return

    if cmd == "stats":
        ram = psutil.virtual_memory()
        cpu = psutil.cpu_percent()
        busy = sum(1 for v in account_busy.values() if v)
        await m.reply(
            f"📊 **Bot Diagnostics & Server Stats:**\n\n"
            f"🖥️ Controller CPU: `{cpu}%`\n"
            f"💾 Controller RAM: `{ram.percent}%`\n"
            f"👥 Kaggle Accounts: `{len(KAG_ACCOUNTS)}`\n"
            f"🔥 Busy Workers: `{busy}`\n"
            f"⏳ Queue Size: `{task_queue.qsize()}`\n"
            f"⚡ Active HW Mode: `{current_hw_mode().upper()}`"
        )
    elif cmd == "addposition":
        if len(m.command) < 2 or m.command[1].lower() not in ["left", "right"]: 
            return await m.reply("❌ Usage: `/addposition left|right`")
        wm_positions[m.chat.id] = m.command[1].lower()
        await m.reply(f"✅ Watermark position updated: **{m.command[1].upper()}**")
    elif cmd in ["admark", "addfont"]:
        if not m.reply_to_message or not (m.reply_to_message.photo or m.reply_to_message.document): 
            return await m.reply("❌ Reply to a valid file/image.")
        msg_link = f"https://t.me/c/{str(m.chat.id)[4:]}/{m.reply_to_message.id}"
        t_name = "watermark" if cmd == "admark" else "file"
        pinned = await m.reply(f"ID – {m.from_user.id}\nLink – {msg_link}\nName – {t_name}")
        await pinned.pin()
        await m.reply(f"✅ Configuration Registry Saved.")
    elif cmd in ["deletmark", "removefont"]:
        chat = await c.get_chat(m.chat.id)
        t_name = "watermark" if cmd == "deletmark" else "file"
        if chat.pinned_message and f"Name – {t_name}" in chat.pinned_message.text:
            await chat.pinned_message.unpin()
            await m.reply("🗑️ Registry successfully removed.")
        else: 
            await m.reply("❌ Registry not found.")

# ── COMPRESS COMMANDS ────────────────────────────────────────────────
RES_CMD_MAP = {"1080g": "1080p", "720g": "720p", "480g": "480p"}

@app.on_message(filters.command(["1080g", "720g", "480g"]))
async def compress_cmd(c, m: Message):
    if not await check_command_privacy(c, m): 
        return
    media = m.reply_to_message.video or m.reply_to_message.document or m.reply_to_message.animation if m.reply_to_message else None
    if not media: 
        return await m.reply("❌ Compression task ke liye kisi valid video/document par reply karein.")
    
    cmd = RES_CMD_MAP[m.command[0].lower()]
    orig_name = getattr(media, "file_name", "output.mp4")
    
    st = await m.reply("⏳ **Task Registered!** Queue me insert kiya jaa raha hai...")
    font_link = await get_pinned_file_link(m.chat.id, "file")

    payload = {
        "task_type": "compress", 
        "video_id": f"https://t.me/c/{str(m.chat.id)[4:]}/{m.reply_to_message.id}",
        "sub_id": "none", "chat_id": str(m.chat.id), "user_id": str(m.from_user.id),
        "resolution": cmd, "wm_id": "none", "wm_pos": "none", "rename": orig_name, 
        "font_link": font_link, "trigger_msg_id": str(st.id)
    }
    await enqueue_task(m.chat.id, st.id, payload)

# ── HARDSUB INTERACTIVE WORKFLOW ─────────────────────────────────────
@app.on_message(filters.command("sub"))
async def hsub_cmd(c, m: Message):
    if not await check_command_privacy(c, m): 
        return
    media = m.reply_to_message.video or m.reply_to_message.document or m.reply_to_message.animation if m.reply_to_message else None
    if not media: 
        return await m.reply("❌ Hardsub ke liye kisi forwarded video/document par reply karein.")

    orig_name = getattr(media, "file_name", "output.mp4")
    await m.reply("📝 Subtitle file (.vtt/.srt/.ass) reply karke bhejo ya `S` type karke skip karo.")
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
        await m.reply("🖼️ Watermark add karna hai? Type `A` to Add ya `S` to skip.")
    else:
        session["watermark"] = "no"
        await execute_dispatch_hardsub(user_id, m)

@app.on_message(filters.text | filters.document)
async def replies_controller(c, m: Message):
    if not m.from_user or (m.text and m.text.startswith("/")): 
        return
    user_id = m.from_user.id
    if user_id not in users_data: 
        return
    session = users_data[user_id]
    if session["chat_id"] != m.chat.id: 
        return
    
    state = session.get("state")
    text = m.text.strip().upper() if m.text else ""
    
    if state == "WAIT_SUB":
        if m.document and m.document.file_name and m.document.file_name.lower().endswith(('.srt', '.ass', '.vtt', '.txt')):
            session["sub_msg_link"] = f"https://t.me/c/{str(m.chat.id)[4:]}/{m.id}"
            session["state"] = "WAIT_RENAME_CHOICE"
            await m.reply("✏️ Video rename karna hai? Type `R` to Rename ya `S` for Same Name.")
        elif text == "S":
            session["sub_msg_link"] = "none"
            session["state"] = "WAIT_RENAME_CHOICE"
            await m.reply("✏️ Video rename karna hai? Type `R` to Rename ya `S` for Same Name.")
        else: 
            await m.reply("❌ Invalid format! Sahi subtitle file (.srt, .ass, .vtt) bhejo ya `S` likh kar skip karo.")
        return

    if state == "WAIT_RENAME_CHOICE":
        if text == "R": 
            session["state"] = "WAIT_RENAME_VALUE"
            await m.reply("✍️ Naya file name bhejo (bina .mp4 lagaye):")
        elif text == "S": 
            session["rename"] = session["orig_name"]
            await prompt_watermark_or_execute(c, m, user_id, session)
        else: 
            await m.reply("❌ Invalid! Type `R` to rename ya `S` to skip.")
        return
            
    elif state == "WAIT_RENAME_VALUE":
        if not m.text: 
            return await m.reply("❌ Please send a valid text name.")
        raw_name = m.text.strip().replace("/", "_").replace("\\", "_")
        if raw_name.lower().endswith(".mp4"): 
            raw_name = raw_name[:-4]
        session["rename"] = raw_name + ".mp4"
        await prompt_watermark_or_execute(c, m, user_id, session)
        return
        
    elif state == "WAIT_WM_CHOICE":
        if text == "A": 
            session["watermark"] = "yes"
        elif text == "S": 
            session["watermark"] = "no"
        else: 
            return await m.reply("❌ Invalid! Type `A` to add watermark ya `S` to skip.")
        await execute_dispatch_hardsub(user_id, m)

async def execute_dispatch_hardsub(user_id, msg: Message):
    data = users_data.pop(user_id)
    st = await msg.reply("⏳ **Task Registered!** Queue me insert kiya jaa raha hai...")
    
    wm_link = "none"
    wm_pos = "right"
    if data.get("watermark") == "yes":
        wm_link = await get_pinned_file_link(data["chat_id"], "watermark")
        wm_pos = wm_positions.get(data["chat_id"], "right")

    payload = {
        "task_type": "hardsub", 
        "video_id": f"https://t.me/c/{str(data['chat_id'])[4:]}/{data['video_msg_id']}",
        "sub_id": data.get("sub_msg_link", "none"), "chat_id": str(data["chat_id"]), "user_id": str(user_id),
        "resolution": "none", "wm_id": wm_link, "wm_pos": wm_pos, "rename": data.get("rename", "none"),
        "font_link": await get_pinned_file_link(data["chat_id"], "file"), "trigger_msg_id": str(st.id)
    }
    await enqueue_task(data["chat_id"], st.id, payload)

# ── ABORT / CANCEL ACTION WORKER ─────────────────────────────────────
@app.on_callback_query(filters.regex("cancel_active_run"))
async def cancel_run_callback(c, q: CallbackQuery):
    if q.from_user.id not in [OWNER_ID, ALLOWED_USER]:
        return await q.answer("❌ Aap is task ko cancel nahi kar sakte.", show_alert=True)

    try:
        cancelled = False
        for tid, details in list(running_tasks.items()):
            details["cancel"] = True
            cancelled = True
        
        if cancelled:
            await q.message.edit("🛑 **Task Cancel Signal sent successfully!**")
            await q.answer("Task Aborted", show_alert=True)
        else: 
            await q.answer("Active status par koi task nahi mila.", show_alert=True)
    except Exception as e: 
        await q.answer(f"Abort Exception: {e}", show_alert=True)

@app.on_message(filters.command("kill"))
async def kill_cmd(_, m: Message):
    if not is_authorized(m): 
        return
    msg = await m.reply_text("🗑️ Saare notebooks delete aur active tasks cancel kar raha hoon...")
    deleted = await asyncio.to_thread(kill_all_notebooks)
    for tid in list(running_tasks.keys()):
        running_tasks[tid]["cancel"] = True
    await msg.edit_text(f"✅ {len(deleted)} notebook(s) delete ho gaye aur processes cancel ho gayi hain.")

# ── HEALTH SERVER (Render Ke Liye) ───────────────────────────────────
class Health(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *a): 
        pass

def run_health():
    port = int(os.environ.get("PORT", "8080"))
    http.server.HTTPServer(("0.0.0.0", port), Health).serve_forever()

# ── MAIN SYSTEM STARTUP ──────────────────────────────────────────────
async def main():
    threading.Thread(target=run_health, daemon=True).start()
    await app.start()
    print("🚀 Controller Bot Connected successfully!")

    # Startup cleanup to prevent double responses
    deleted = await asyncio.to_thread(kill_all_notebooks)
    print(f"[startup] {len(deleted)} purane running kernels clean kiye gaye.")

    workers = [asyncio.create_task(account_worker(acc)) for acc in KAG_ACCOUNTS]
    print(f"[startup] {len(KAG_ACCOUNTS)} Account workers successfully initiated.")

    await idle()
    await app.stop()

if __name__ == "__main__":
    asyncio.get_event_loop().run_until_complete(main())
