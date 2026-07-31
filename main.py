import os, re, json, base64, asyncio, subprocess, datetime, http.server, threading, psutil, shutil, sys, random, string
from pathlib import Path
import pyrogram.utils
from pyrogram import Client, filters, idle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from pyrogram.enums import ChatType

# --- PYROGRAM PEER ID FIX ---
pyrogram.utils.get_peer_type = lambda p: "channel" if str(p).startswith("-100") else "chat" if str(p).startswith("-") else "user"

# --- FASTAPI HEALTH SERVER ---
from fastapi import FastAPI
import uvicorn
web_app = FastAPI()

@web_app.get("/")
def read_root():
    return {"status": "Kavvle Controller Live"}

def run_web_server():
    # Render always sets PORT env var, else fallback 10000
    port = int(os.environ.get("PORT", "10000"))
    uvicorn.run(web_app, host="0.0.0.0", port=port, log_level="warning")
# ----------------------------

# --- CONFIGURATION ---
API_ID = int(os.environ.get("API_ID", "0"))
API_HASH = os.environ.get("API_HASH", "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()

OWNER_ID = int(os.environ.get("OWNER_ID", "5344078567"))
ALLOWED_USER = int(os.environ.get("ALLOWED_USER", "5351848105"))
GROUP_ID = int(os.environ.get("GROUP_ID", "-1003899919015"))
DESK_CHANNEL_ID = int(os.environ.get("DESK_CHANNEL_ID", "-1003700822969"))

# Dynamic Instance Hash
BOT_INSTANCE_HASH = "".join(random.choices(string.ascii_lowercase, k=4))
KERNEL_PREFIX = f"hs-{BOT_INSTANCE_HASH}-"

# --- SESSION STRING STORE ---
BOT_SESSION_STRING = None

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
        raise RuntimeError("No Kaggle credentials configured.")
    return accs

KAG_ACCOUNTS = load_kaggle_accounts()

app = Client("HarsubBot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN, workers=16)

users_data = {}
wm_positions = {}

task_queue = asyncio.Queue()
account_busy = {a["idx"]: False for a in KAG_ACCOUNTS}
running_tasks = {}
task_counter = 0

# --- KAGGLE COMMAND HELPER (Thread-safe isolated config) ---
def run_kaggle_command(account, cmd_args, task_ref_id, timeout=60):
    config_dir = Path(f"/tmp/kaggle_config_{task_ref_id}")
    config_dir.mkdir(parents=True, exist_ok=True)

    creds_file = config_dir / "kaggle.json"
    with open(creds_file, "w", encoding="utf-8") as f:
        json.dump({"username": account["user"], "key": account["key"]}, f)
    try:
        creds_file.chmod(0o600)
    except:
        pass

    env = os.environ.copy()
    env["KAGGLE_CONFIG_DIR"] = str(config_dir)
    env["KAGGLE_USERNAME"] = account["user"]
    env["KAGGLE_KEY"] = account["key"]

    res = subprocess.run(cmd_args, env=env, capture_output=True, text=True, timeout=timeout)

    try:
        shutil.rmtree(config_dir)
    except:
        pass
    return res

def ensure_kaggle_installed():
    if shutil.which("kaggle") is None:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "kaggle"], check=False)

# --- UTILITIES ---
def current_hw_mode():
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

async def copy_pinned_file_to_desk(chat_id, target_name):
    link = await get_pinned_file_link(chat_id, target_name)
    if link == "none":
        return "none"
    try:
        msg_id = int(link.rstrip("/").split("/")[-1])
        msg = await app.get_messages(chat_id, msg_id)
        if msg:
            copied = await msg.copy(DESK_CHANNEL_ID)
            return str(copied.id)
    except Exception as e:
        print(f"[copy_pinned_file_to_desk] Failed to duplicate asset: {e}")
    return "none"

# --- KAGGLE KERNEL MANAGEMENT (NO PREFIX FILTER) ---
def kaggle_list_kernels_verbose(account):
    dummy_id = "".join(random.choices(string.ascii_lowercase, k=5))
    try:
        out = run_kaggle_command(account, ["kaggle", "kernels", "list", "--user", account["user"], "--csv"], dummy_id, timeout=45)
        if out.returncode != 0:
            return [], f"Account #{account['idx']} (`{account['user']}`) list fail: `{(out.stderr or out.stdout).strip()[:200]}`"
        lines = out.stdout.strip().splitlines()
        refs = []
        for line in lines[1:]:
            parts = line.split(",")
            if parts:
                ref = parts[0].strip()
                # Remove prefix filter – kill ALL notebooks
                refs.append(ref)
        return refs, None
    except Exception as e:
        return [], f"Account #{account['idx']} (`{account['user']}`) exception: `{e}`"

def kaggle_delete_kernel_verbose(account, ref):
    dummy_id = "".join(random.choices(string.ascii_lowercase, k=5))
    try:
        res = run_kaggle_command(account, ["kaggle", "kernels", "delete", "-k", ref], dummy_id, timeout=45)
        if res.returncode == 0:
            return True, None
        return False, f"`{ref}` delete fail: `{(res.stderr or res.stdout).strip()[:150]}`"
    except Exception as e:
        return False, f"`{ref}` exception: `{e}`"

def kaggle_kernel_status(account, ref):
    dummy_id = "".join(random.choices(string.ascii_lowercase, k=5))
    try:
        out = run_kaggle_command(account, ["kaggle", "kernels", "status", ref], dummy_id, timeout=30)
        return out.stdout.strip()
    except Exception as e:
        return str(e)

def kill_all_notebooks_verbose():
    deleted, errors = [], []
    for acc in KAG_ACCOUNTS:
        refs, err = kaggle_list_kernels_verbose(acc)
        if err:
            errors.append(err)
            continue
        for ref in refs:
            ok, derr = kaggle_delete_kernel_verbose(acc, ref)
            if ok:
                deleted.append(ref)
            else:
                errors.append(derr)
    return deleted, errors

def kaggle_push_kernel(account, slug, payload: dict, hw_mode: str, task_id):
    workdir = f"/tmp/{slug}"
    os.makedirs(workdir, exist_ok=True)

    asi_path = os.path.join(os.path.dirname(__file__), "asi.py")
    if not os.path.exists(asi_path):
        return False, "Error: asi.py template missing on controller!"

    asi_code = open(asi_path, "r", encoding="utf-8").read()

    payload["hardware_mode"] = hw_mode
    # Inject session string if available
    if BOT_SESSION_STRING:
        payload["session_string"] = BOT_SESSION_STRING
    cfg = json.dumps(payload)
    cfg_b64 = base64.b64encode(cfg.encode()).decode()
    asi_code = re.sub(r'CONFIG_B64\s*=\s*["\']["\']', f'CONFIG_B64 = "{cfg_b64}"', asi_code)

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

    out = run_kaggle_command(account, ["kaggle", "kernels", "push", "-p", workdir], task_id, timeout=90)

    try:
        shutil.rmtree(workdir)
    except:
        pass

    return out.returncode == 0, out.stdout + out.stderr

def kaggle_delete_kernel(account, ref):
    ok, _ = kaggle_delete_kernel_verbose(account, ref)
    return ok

# --- WORKER PROCESSES ---
async def account_worker(account):
    idx = account["idx"]
    account_user = account["user"]
    while True:
        task = await task_queue.get()
        if task.get("cancelled"):
            task_queue.task_done()
            continue
        task_id = task["task_id"]
        task_hash = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
        slug = f"{KERNEL_PREFIX}{task_id}-{task_hash}"

        try:
            account_busy[idx] = True
            hw_mode = current_hw_mode()
            running_tasks[task_id] = {"account_idx": idx, "kernel": f"{account_user}/{slug}", "cancel": False}

            status_text = (
                f"🚀 **Task processing on Kaggle!**\n\n"
                f"👤 **Account:** Account #{idx} (`{account_user}`)\n"
                f"⚡ **Hardware Mode:** `{hw_mode.upper()}`\n"
                f"⚙️ *Worker container initialising and fetching tools...*"
            )
            try:
                await app.edit_message_text(
                    task["chat_id"], task["status_msg_id"], status_text,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Cancel Task", callback_data="cancel_active_run")]])
                )
            except Exception:
                pass

            ok, log = await asyncio.to_thread(kaggle_push_kernel, account, slug, task["payload"], hw_mode, task_id)
            if not ok:
                await report_error(task, f"Kaggle push fail (account {idx}):\n{log[-800:]}")
                continue

            for _ in range(2160):
                if running_tasks.get(task_id, {}).get("cancel"):
                    await asyncio.to_thread(kaggle_delete_kernel, account, f"{account_user}/{slug}")
                    break
                st = await asyncio.to_thread(kaggle_kernel_status, account, f"{account_user}/{slug}")
                if "complete" in st.lower() or "error" in st.lower() or "cancel" in st.lower():
                    break
                await asyncio.sleep(20)

            await asyncio.to_thread(kaggle_delete_kernel, account, f"{account_user}/{slug}")
        except Exception as e:
            await report_error(task, f"Worker exception occurred: {e}")
        finally:
            account_busy[idx] = False
            running_tasks.pop(task_id, None)
            task_queue.task_done()

async def report_error(task, text):
    try:
        await app.edit_message_text(task["chat_id"], task["status_msg_id"], f"❌ **Error Occurred:**\n\n{text}")
    except Exception:
        try:
            await app.send_message(task["chat_id"], f"❌ **Error Occurred:**\n\n{text}")
        except Exception:
            pass

async def enqueue_task(chat_id, status_msg_id, payload):
    global task_counter
    task_counter += 1
    task_id = task_counter
    payload["trigger_msg_id"] = str(status_msg_id)
    payload["api_id"] = API_ID
    payload["api_hash"] = API_HASH
    payload["bot_token"] = BOT_TOKEN
    # session string will be injected by kaggle_push_kernel automatically

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

# --- COMMANDS ---
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

    st = await m.reply("⏳ **Task Registered!** Copying file to secure channel...")
    
    try:
        copied_video = await m.reply_to_message.copy(DESK_CHANNEL_ID)
    except Exception as e:
        return await st.edit(f"❌ Secure Channel Copy Error: `{e}`")

    font_msg_id = await copy_pinned_file_to_desk(m.chat.id, "file")

    payload = {
        "task_type": "compress",
        "video_msg_id": str(copied_video.id),
        "sub_msg_id": "none", 
        "chat_id": str(m.chat.id), 
        "user_id": str(m.from_user.id),
        "resolution": cmd, 
        "wm_msg_id": "none", 
        "wm_pos": "none", 
        "rename": orig_name,
        "font_msg_id": font_msg_id, 
        "trigger_msg_id": str(st.id)
    }
    await enqueue_task(m.chat.id, st.id, payload)

@app.on_message(filters.command("sub"))
async def hsub_cmd(c, m: Message):
    if not await check_command_privacy(c, m):
        return
    media = m.reply_to_message.video or m.reply_to_message.document or m.reply_to_message.animation if m.reply_to_message else None
    if not media:
        return await m.reply("❌ Hardsub ke liye kisi forwarded video/document par reply karein.")

    orig_name = getattr(media, "file_name", "output.mp4")
    st_copy = await m.reply("⏳ **Securing Video File...**")
    try:
        copied_video = await m.reply_to_message.copy(DESK_CHANNEL_ID)
        await st_copy.delete()
    except Exception as e:
        return await st_copy.edit(f"❌ Secure Channel Copy Error: `{e}`")

    await m.reply("📝 Subtitle file (.vtt/.srt/.ass) reply karke bhejo ya `S` type karke skip karo.")
    users_data[m.from_user.id] = {
        "video_msg_id": str(copied_video.id),
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
            st_sub = await m.reply("⏳ **Securing Subtitle File...**")
            try:
                copied_sub = await m.copy(DESK_CHANNEL_ID)
                session["sub_msg_id"] = str(copied_sub.id)
                await st_sub.delete()
            except Exception as e:
                return await st_sub.edit(f"❌ Subtitle Copy Error: `{e}`")
                
            session["state"] = "WAIT_RENAME_CHOICE"
            await m.reply("✏️ Video rename karna hai? Type `R` to Rename ya `S` for Same Name.")
        elif text == "S":
            session["sub_msg_id"] = "none"
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

    wm_msg_id = "none"
    wm_pos = "right"
    if data.get("watermark") == "yes":
        wm_msg_id = await copy_pinned_file_to_desk(data["chat_id"], "watermark")
        wm_pos = wm_positions.get(data["chat_id"], "right")

    font_msg_id = await copy_pinned_file_to_desk(data["chat_id"], "file")

    payload = {
        "task_type": "hardsub",
        "video_msg_id": data["video_msg_id"],
        "sub_msg_id": data.get("sub_msg_id", "none"), 
        "chat_id": str(data["chat_id"]), 
        "user_id": str(user_id),
        "resolution": "none", 
        "wm_msg_id": wm_msg_id, 
        "wm_pos": wm_pos, 
        "rename": data.get("rename", "none"),
        "font_msg_id": font_msg_id, 
        "trigger_msg_id": str(st.id)
    }
    await enqueue_task(data["chat_id"], st.id, payload)

# --- CANCEL & KILL ---
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
        return await m.reply_text("❌ Aap is command ke liye authorized nahi hain.")

    if not KAG_ACCOUNTS:
        return await m.reply_text("❌ Koi Kaggle account configured nahi hai.")

    msg = await m.reply_text("🗑️ Saare active notebooks abort aur cache clean kar raha hoon...")

    for tid in list(running_tasks.keys()):
        running_tasks[tid]["cancel"] = True

    deleted, errors = await asyncio.to_thread(kill_all_notebooks_verbose)

    text = f"✅ `{len(deleted)}` Kaggle notebook(s) delete kiye gaye."
    if deleted:
        text += "\n" + "\n".join(f"• `{d}`" for d in deleted[:15])
    if errors:
        text += "\n\n⚠️ **Errors:**\n" + "\n".join(f"• {e}" for e in errors[:6])
    await msg.edit_text(text)

@app.on_message(filters.command("clean"))
async def clean_cmd(_, m: Message):
    if not is_authorized(m):
        return
    had = users_data.pop(m.from_user.id, None)
    await m.reply_text("🔄 **Session refreshed.** Pichla /sub flow reset ho gaya." if had else "🔄 **Already clean.**")

# --- SYSTEM STARTUP ---
if __name__ == "__main__":
    from pyrogram.errors import FloodWait

    async def run_bot_safe():
        global BOT_SESSION_STRING
        try:
            threading.Thread(target=run_web_server, daemon=True).start()
            print("📡 Web server bound successfully.")

            ensure_kaggle_installed()
            await app.start()
            print("🚀 Controller Bot Connected!")

            # Export session string – ye 24 ghante valid rehta hai, isko payload me bhejenge
            BOT_SESSION_STRING = await app.export_session_string()
            print(f"[startup] Session string exported ({len(BOT_SESSION_STRING)} chars).")

            deleted, _ = await asyncio.to_thread(kill_all_notebooks_verbose)
            print(f"[startup] {len(deleted)} active kernels successfully aborted.")

            for acc in KAG_ACCOUNTS:
                asyncio.create_task(account_worker(acc))
            print(f"[startup] {len(KAG_ACCOUNTS)} Account workers successfully initiated.")

            await idle()
            await app.stop()
            
        except FloodWait as e:
            print(f"⚠️ Telegram FloodWait triggered! Sleeping for {e.value} seconds to clear rate limit.")
            await asyncio.sleep(e.value + 10)
            try:
                print("🔄 Attempting startup after waiting out the rate limit...")
                await app.start()
                print("✅ Bot successfully started after recovery!")
                for acc in KAG_ACCOUNTS:
                    asyncio.create_task(account_worker(acc))
                await idle()
            except Exception as retry_err:
                print(f"❌ Failed to start bot after recovery attempt: {retry_err}")
                sys.exit(1)
        except Exception as startup_err:
            print(f"❌ Critical error on startup: {startup_err}")
            sys.exit(1)

    try:
        loop = asyncio.get_event_loop()
        loop.run_until_complete(run_bot_safe())
    except (KeyboardInterrupt, SystemExit):
        print("🔌 Bot execution terminated cleanly.")
    except Exception as e:
        print(f"🚨 Fatal exception in main execution loop: {e}")
