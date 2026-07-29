import os
import sys
import subprocess
import time
import asyncio
import re
import shutil
import requests
import html
import gc
import pyrogram.utils

# Fast automatic setup on Kaggle startup for missing libraries
def native_setup():
    print("📦 Checking system dependencies...")
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
    except Exception:
        subprocess.run(["apt-get", "update", "-y"], check=False)
        subprocess.run(["apt-get", "install", "-y", "ffmpeg"], check=False)

    packages_to_install = []
    import importlib.util
    import importlib.metadata
    
    if importlib.util.find_spec("pysubs2") is None: packages_to_install.append("pysubs2")
    if importlib.util.find_spec("fontTools") is None: packages_to_install.append("fontTools")
    if importlib.util.find_spec("stable_whisper") is None: packages_to_install.append("stable-ts")
    if importlib.util.find_spec("faster_whisper") is None: packages_to_install.append("faster-whisper")
    if importlib.util.find_spec("librosa") is None: packages_to_install.append("librosa")
    if importlib.util.find_spec("numpy") is None:
        packages_to_install.append("numpy<2.0.0")
    else:
        try:
            major_ver = int(importlib.metadata.version("numpy").split(".")[0])
            if major_ver >= 2: packages_to_install.append("numpy<2.0.0")
        except: pass

    if packages_to_install:
        print(f"🚀 Installing packages: {packages_to_install}")
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-cache-dir", "--prefer-binary"] + packages_to_install, check=True)

native_setup()

# Import packages safely after installation
import pysubs2
from fontTools.ttLib import TTFont
import librosa
import numpy as np
import stable_whisper
from pyrogram import Client
from pyrogram.enums import ParseMode

pyrogram.utils.get_peer_type = lambda p: "channel" if str(p).startswith("-100") else "chat" if str(p).startswith("-") else "user"

# Get environment variables passed by Launcher
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
TASK_TYPE = os.getenv("TASK_TYPE", "compress")
VIDEO_ID = os.getenv("VIDEO_ID", "")
SUB_ID = os.getenv("SUB_ID", "none")
CHAT_ID = int(os.getenv("CHAT_ID", "0"))
USER_ID = int(os.getenv("USER_ID", "0"))
RESOLUTION = os.getenv("RESOLUTION", "none")
WM_ID = os.getenv("WM_ID", "none")
WM_POS = os.getenv("WM_POS", "right")
RENAME = os.getenv("RENAME", "output.mp4")
FONT_LINK = os.getenv("FONT_LINK", "none")
TRIGGER_MSG_ID = os.getenv("TRIGGER_MSG_ID", "none")

DESK_CHANNEL_ID = -1003700822969

last_time = 0
start_time = 0
status_msg_id = None
os.makedirs("fonts", exist_ok=True)

# --- HYBRID GENDER CLASSIFICATION SYSTEM ---
FEMALE_KEYWORDS = {
    'girl', 'woman', 'female', 'lady', 'she', 'her', 'hers', 'miss', 'mrs', 'ms',
    'sister', 'mother', 'mom', 'daughter', 'queen', 'princess', 'madam', 'maam',
    'chan', 'hime', 'oneesan', 'obaasan', 'atashi', 'uchi', 'gal', 'bitch',
    'hinata', 'sakura', 'tsunade', 'nami', 'robin', 'mikasa', 'nezuko', 'yor', 'aria', 'emma', 'alice', 'lucy'
}

MALE_KEYWORDS = {
    'boy', 'man', 'male', 'guy', 'dude', 'he', 'him', 'his', 'mr', 'sir',
    'brother', 'father', 'dad', 'son', 'king', 'prince', 'gentleman',
    'kun', 'boku', 'ore', 'oniisan', 'ojiisan', 'sama',
    'naruto', 'sasuke', 'luffy', 'zoro', 'goku', 'vegeta', 'deku', 'tanjiro', 'levi', 'ichigo'
}

def analyze_audio_pitch(audio_path, start_time, end_time):
    try:
        duration = max(0.2, end_time - start_time)
        slice_y, sr = librosa.load(audio_path, sr=16000, offset=start_time, duration=min(duration, 4.0), mono=True)
        if len(slice_y) < int(16000 * 0.15): return None, 0.0
        f0 = librosa.yin(slice_y, fmin=65, fmax=400, sr=sr)
        valid_f0 = f0[(f0 > 65) & (f0 < 400) & (~np.isnan(f0))]
        if len(valid_f0) < 3: return None, 0.0
        median_f0 = float(np.median(valid_f0))
        centroid = float(np.mean(librosa.feature.spectral_centroid(y=slice_y, sr=sr)))
        return median_f0, centroid
    except Exception: return None, 0.0

def analyze_text_context(text):
    clean_text = re.sub(r'[^\w\s]', '', text.lower())
    words = clean_text.split()
    f_count = sum(1 for w in words if w in FEMALE_KEYWORDS)
    m_count = sum(1 for w in words if w in MALE_KEYWORDS)
    is_addressing = any(term in clean_text for term in ['you', 'youre', 'you are', 'hey', 'tum', 'tu'])
    return f_count, m_count, is_addressing

def detect_gender_slice(audio_path, start_time, end_time, text=""):
    pitch, centroid = analyze_audio_pitch(audio_path, start_time, end_time)
    f_count, m_count, is_addressing = analyze_text_context(text)
    if pitch is None:
        pitch_gender, pitch_confident = "boy", False
    elif pitch >= 165.0:
        pitch_gender, pitch_confident = "girl", True
    elif pitch < 150.0:
        pitch_gender, pitch_confident = "boy", True
    else:
        pitch_gender = "girl" if centroid > 2200 else "boy"
        pitch_confident = False

    if f_count > m_count and m_count == 0:
        if is_addressing: return f"{pitch_gender} - to girl"
        if not pitch_confident: return "girl"
        return pitch_gender
    elif m_count > f_count and f_count == 0:
        if is_addressing: return f"{pitch_gender} - to boy"
        if not pitch_confident: return "boy"
        return pitch_gender
    return pitch_gender

# --- UTILITIES ---
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

def format_time(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int(round((seconds - int(seconds)) * 1000))
    if millis >= 1000:
        secs += 1
        millis = 0
    if secs >= 60:
        minutes += 1
        secs = 0
    if minutes >= 60:
        hours += 1
        minutes = 0
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

def _sync_http_edit(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": CHAT_ID, 
        "message_id": status_msg_id, 
        "text": text, 
        "parse_mode": "HTML"
    }
    try: requests.post(url, json=payload, timeout=5)
    except: pass

async def update_http_status(text):
    await asyncio.to_thread(_sync_http_edit, text)

async def prog(c, t, app_instance, step_name):
    global last_time, start_time, status_msg_id
    now = time.time()
    if start_time == 0:
        start_time = now
        last_time = now
        return
        
    if now - last_time > 12 or c == t:
        elapsed = now - start_time
        speed = c / elapsed if elapsed > 0 else 0
        speed_mb = (speed / 1024) / 1024
        percent = (c / t) * 100 if t > 0 else 0
        
        if step_name in ["hardsub_download", "compress_download", "transcribe_download"]:
            text = f"📥 **Downloading File**\n{get_download_bar(percent)} [{percent:.1f}%]\n🚀 Speed: **{speed_mb:.2f} MB/s**\n📦 {c/1048576:.1f}MB / {t/1048576:.1f}MB"
        else:
            text = f"📤 **Sending Processed File**\n{get_send_bar(percent)} [{percent:.1f}%]\n🚀 Speed: **{speed_mb:.2f} MB/s**\n📦 {c/1048576:.1f}MB / {t/1048576:.1f}MB"
        
        asyncio.create_task(update_http_status(text))
        last_time = now

def convert_to_clean_ass(input_sub, output_ass):
    try:
        subs = pysubs2.load(input_sub)
        subs.styles["Default"] = pysubs2.SSAStyle(fontname="Arial", fontsize=24, primarycolor=pysubs2.Color(255, 255, 255), outlinecolor=pysubs2.Color(0, 0, 0), outline=2, shadow=1, marginl=20, marginr=20, marginv=15
