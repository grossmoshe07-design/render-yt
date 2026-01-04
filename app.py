# app.py - FULL PROFESSIONAL YOUTUBE BOT (2026 Edition)

from fastapi import FastAPI, Request, BackgroundTasks, Query
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
import os
import tempfile
import re
import requests
import json
from datetime import datetime, timedelta
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from typing import List, Dict
import hashlib

# Google API
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload
import io

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ====================== CONFIG ======================
CONFIG = {
    "GMAIL_BOT": "yourbot@gmail.com",                    # ← CHANGE
    "GMAIL_APP_PASSWORD": "abcd efgh ijkl mnop",         # ← CHANGE (App Password)
    "DRIVE_FOLDER_NAME": "YouTube Bot Downloads",
    "MAX_ATTACHMENT_MB": 24,
    "MAX_VIDEO_SIZE_MB": 200,
    "YOUTUBE_API_KEY": "YOUR_YOUTUBE_API_KEY",           # ← CHANGE
    "SPREADSHEET_ID": "1Potx9BeXT-USmEKeBR4ouw7r1JY6MgCmSLYctqfdaXI",  # ← Your log sheet
    "SERVICE_ACCOUNT_FILE": "/etc/secrets/service_account.json",  # Render secret path
    "USAGE_RESET_HOURS": 24
}

# Load service account (will fail locally, but work on Render)
def get_drive_service():
    creds = Credentials.from_service_account_file(
        CONFIG["SERVICE_ACCOUNT_FILE"],
        scopes=["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    return build("drive", "v3", credentials=creds)

def get_sheets_service():
    creds = Credentials.from_service_account_file(
        CONFIG["SERVICE_ACCOUNT_FILE"],
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    return build("spreadsheets", "v4", credentials=creds)

# ====================== ROLE & USAGE ======================
ROLE_LIMITS = { ... }  # Keep same as before (admin to closed)

QUALITY_PRESETS = { ... }  # Same as your original

USAGE_DB = {}  # In-memory (or use Redis later)

def get_user_role_from_sheet(email: str) -> str:
    try:
        service = get_sheets_service()
        result = service.spreadsheets().values().get(
            spreadsheetId=CONFIG["SPREADSHEET_ID"],
            range="User Roles!A:B"
        ).execute()
        values = result.get("values", [])
        for row in values[1:]:  # Skip header
            if len(row) >= 2 and row[0].strip().lower() == email.lower():
                role = row[1].strip().lower().replace(" ", "_")
                if role in QUALITY_PRESETS:
                    return role
        return "guest"
    except:
        return "guest"

def check_and_increment_usage(email: str, action: str) -> dict:
    now = datetime.utcnow()
    key = hashlib.md5(email.lower().encode()).hexdigest()
    record = USAGE_DB.get(key, {"last_reset": now, "downloads": 0, "searches": 0})

    if (now - record["last_reset"]).total_seconds() > CONFIG["USAGE_RESET_HOURS"] * 3600:
        record = {"last_reset": now, "downloads": 0, "searches": 0}

    role = get_user_role_from_sheet(email)
    limits = ROLE_LIMITS.get(role, ROLE_LIMITS["guest"])
    current = record["downloads"] if action == "download" else record["searches"]
    max_allowed = limits["downloads"] if action == "download" else limits["searches"]

    if current >= max_allowed and max_allowed != float('inf'):
        return {"allowed": False, "message": f"Limit reached ({current}/{max_allowed})"}

    if action == "download":
        record["downloads"] += 1
    else:
        record["searches"] += 1

    USAGE_DB[key] = record
    return {"allowed": True, "role": role, "limits": limits}

# ====================== DRIVE UPLOAD ======================
def upload_to_drive(blob: bytes, filename: str) -> str:
    try:
        service = get_drive_service()
        # Find or create folder
        query = f"name='{CONFIG['DRIVE_FOLDER_NAME']}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        results = service.files().list(q=query, fields="files(id)").execute()
        folder_id = results.get("files", [{}])[0].get("id")
        if not folder_id:
            folder = service.files().create(body={"name": CONFIG["DRIVE_FOLDER_NAME"], "mimeType": "application/vnd.google-apps.folder"}).execute()
            folder_id = folder["id"]

        file_metadata = {"name": filename, "parents": [folder_id]}
        media = MediaIoBaseUpload(io.BytesIO(blob), mimetype="video/mp4")
        file = service.files().create(body=file_metadata, media_body=media, fields="id, webViewLink").execute()
        link = file.get("webViewLink")

        # Make anyone with link can view
        service.permissions().create(fileId=file["id"], body={"type": "anyone", "role": "reader"}).execute()
        return link
    except Exception as e:
        raise Exception(f"Drive upload failed: {str(e)}")

# ====================== DOWNLOAD & SEARCH (Full Logic) ======================
# Keep your original /download endpoint unchanged for backward compat

@app.post("/process-request")
async def process_request(req: Request, bg: BackgroundTasks):
    data = await req.json()
    bg.add_task(handle_full_request, data)
    return {"status": "queued"}

def handle_full_request(data: dict):
    sender = data["sender"]
    email = re.search(r'<([^>]+)>', sender)
    user_email = email.group(1) if email else sender.strip("<>")

    try:
        if data["type"] == "download":
            results = process_downloads(data["links"], user_email)
        else:
            results = process_search(data["query"], user_email)

        html = build_final_reply_html(results)
        attachments = results.get("attachments", [])

        send_email(user_email, "📺 Your Videos Are Ready!", html, attachments)

    except Exception as e:
        send_email(user_email, "⚠️ Error", f"<p>Sorry: {str(e)}</p>")

# Continue with full process_downloads(), process_search(), build_final_reply_html(), send_email() 
# (I'll give you the full beautiful HTML in next message)
# ====================== FULL DOWNLOAD PROCESSING ======================
def process_downloads(links: List[str], user_email: str) -> dict:
    usage_check = check_and_increment_usage(user_email, "download")
    if not usage_check["allowed"]:
        raise Exception(f"Download limit reached: {usage_check['message']}")

    role = usage_check["role"]
    limits = usage_check["limits"]
    quality_label = limits["quality"]
    preset = QUALITY_PRESETS.get(role, QUALITY_PRESETS["guest"])

    video_results = []
    attachments = []  # (bytes, filename)
    total_attachment_size_mb = 0

    for url in links:
        try:
            # Extract video ID
            video_id = re.search(r"(?:v=|\/)([0-9A-Za-z_-]{11}).*", url)
            if not video_id:
                raise Exception("Invalid YouTube URL")
            video_id = video_id.group(1)

            # Download video using yt_dlp
            blob, info = download_video_with_yt_dlp(url, preset)

            size_mb = round(len(blob) / (1024 * 1024), 1)
            if size_mb > CONFIG["MAX_VIDEO_SIZE_MB"]:
                raise Exception(f"Video too large ({size_mb} MB > {CONFIG['MAX_VIDEO_SIZE_MB']} MB)")

            raw_title = info.get("title", "Unknown Video")
            title = sanitize_filename(raw_title)
            channel = info.get("uploader", info.get("channel", "Unknown Channel"))
            duration_str = format_duration(info.get("duration", 0))
            thumbnail = info.get("thumbnail", "")
            views = info.get("view_count", 0)

            clean_filename = f"{title} - {channel}.mp4"

            delivery_method = "drive"
            drive_link = None

            # Try to attach if small enough
            if (size_mb <= CONFIG["MAX_ATTACHMENT_MB"] and 
                total_attachment_size_mb + size_mb <= CONFIG["MAX_ATTACHMENT_MB"]):
                attachments.append((blob, clean_filename))
                total_attachment_size_mb += size_mb
                delivery_method = "email"

            else:
                drive_link = upload_to_drive(blob, clean_filename)
                delivery_method = "drive"

            video_results.append({
                "success": True,
                "title": raw_title,
                "title_safe": title,
                "channel": channel,
                "duration": duration_str,
                "views": views,
                "thumbnail": thumbnail,
                "size_mb": size_mb,
                "quality": quality_label,
                "delivery": delivery_method,
                "drive_link": drive_link,
                "clean_filename": clean_filename
            })

        except Exception as e:
            video_results.append({
                "success": False,
                "error": str(e),
                "url": url
            })

    return {
        "type": "download",
        "results": video_results,
        "attachments": attachments,
        "total_attach_size_mb": round(total_attachment_size_mb, 1)
    }

def download_video_with_yt_dlp(url: str, preset: dict):
    ydl_opts = {
        'format': preset['format'],
        'merge_output_format': 'mp4',
        'outtmpl': '%(id)s.%(ext)s',
        'max_filesize': preset['max_filesize'],
        'noplaylist': True,
        'quiet': True,
        'no_warnings': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        }
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts['outtmpl'] = os.path.join(tmpdir, '%(id)s.%(ext)s')
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            with open(filename, "rb") as f:
                blob = f.read()
    return blob, info

# ====================== FULL SEARCH PROCESSING ======================
def process_search(query: str, user_email: str) -> dict:
    usage_check = check_and_increment_usage(user_email, "search")
    if not usage_check["allowed"]:
        raise Exception(f"Search limit reached: {usage_check['message']}")

    role = usage_check["role"]
    max_results = ROLE_LIMITS[role]["maxResults"]

    # Search
    search_url = "https://www.googleapis.com/youtube/v3/search"
    params = {
        "part": "snippet",
        "q": query,
        "maxResults": max_results,
        "type": "video",
        "key": CONFIG["YOUTUBE_API_KEY"]
    }
    search_response = requests.get(search_url, params=params)
    search_response.raise_for_status()
    search_data = search_response.json()

    items = search_data.get("items", [])
    if not items:
        return {"type": "search", "results": [], "query": query, "empty": True}

    video_ids = [item["id"]["videoId"] for item in items]

    # Get details
    videos_url = "https://www.googleapis.com/youtube/v3/videos"
    videos_params = {
        "part": "snippet,contentDetails,statistics",
        "id": ",".join(video_ids),
        "key": CONFIG["YOUTUBE_API_KEY"]
    }
    videos_response = requests.get(videos_url, params=videos_params)
    videos_response.raise_for_status()
    videos_data = videos_response.json()

    results = []
    for item in videos_data["items"]:
        snippet = item["snippet"]
        stats = item["statistics"]
        content = item["contentDetails"]

        results.append({
            "video_id": item["id"],
            "title": snippet["title"],
            "channel": snippet["channelTitle"],
            "thumbnail": snippet["thumbnails"]["high"]["url"],
            "duration": format_duration(content["duration"]),
            "views": int(stats.get("viewCount", 0)),
            "published": snippet["publishedAt"][:10],
            "link": f"https://youtu.be/{item['id']}"
        })

    return {
        "type": "search",
        "results": results,
        "query": query,
        "empty": False
    }

# ====================== BEAUTIFUL HTML TEMPLATES (Exact Same Design) ======================
def build_final_reply_html(data: dict) -> str:
    STYLE = "<style>@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&display=swap');</style>"

    if data["type"] == "search":
        return STYLE + build_search_results_html(data["results"], data["query"])
    
    # Download results
    return STYLE + build_download_results_html(data["results"], data["total_attach_size_mb"])

def build_download_results_html(results: list, total_attach_mb: float) -> str:
    cards = ""
    has_attachment = False
    has_drive = False

    for r in results:
        if not r["success"]:
            cards += f'<div style="background:#ffebee;padding:15px;border-left:4px solid #f44336;border-radius:8px;margin:15px 0;"><strong>❌ Failed:</strong> {r["error"]}</div>'
            continue

        if r["delivery"] == "email":
            has_attachment = True
            cards += f"""
            <div style="background:white;border-radius:12px;overflow:hidden;margin:20px 0;box-shadow:0 4px 12px rgba(0,0,0,0.1);border-left:4px solid #0f9d58;">
                <div style="position:relative;">
                    <img src="{r['thumbnail']}" style="width:100%;height:auto;display:block;">
                    <div style="position:absolute;bottom:8px;right:8px;background:rgba(0,0,0,0.8);color:white;padding:4px 8px;border-radius:4px;font-size:13px;">
                        {r['quality']}
                    </div>
                </div>
                <div style="padding:16px;">
                    <h3 style="margin:0 0 8px 0;font-size:18px;color:#111;line-height:1.3;">{r['title']}</h3>
                    <p style="margin:0 0 12px;color:#555;font-size:14px;">
                        <strong>{r['channel']}</strong> • {r['duration']} • {r['views']:,} views
                    </p>
                    <div style="background:#e8f5e9;padding:12px;border-radius:8px;">
                        <strong style="color:#0f9d58;">📎 Attached to this email</strong>
                        <span style="float:right;color:#555;">{r['size_mb']} MB</span>
                        <p style="margin:5px 0 0;font-size:14px;color:#333;">
                            File: <strong>{r['clean_filename']}</strong>
                        </p>
                    </div>
                </div>
            </div>
            """
        else:
            has_drive = True
            cards += f"""
            <div style="background:white;border-radius:12px;overflow:hidden;margin:20px 0;box-shadow:0 4px 12px rgba(0,0,0,0.1);border-left:4px solid #4285f4;">
                <div style="position:relative;">
                    <img src="{r['thumbnail']}" style="width:100%;height:auto;display:block;">
                    <div style="position:absolute;bottom:8px;right:8px;background:rgba(0,0,0,0.8);color:white;padding:4px 8px;border-radius:4px;font-size:13px;">
                        {r['quality']}
                    </div>
                </div>
                <div style="padding:16px;">
                    <h3 style="margin:0 0 8px 0;font-size:18px;color:#111;line-height:1.3;">{r['title']}</h3>
                    <p style="margin:0 0 12px;color:#555;font-size:14px;">
                        <strong>{r['channel']}</strong> • {r['duration']} • {r['views']:,} views
                    </p>
                    <div style="background:#e3f2fd;padding:12px;border-radius:8px;">
                        <strong style="color:#1976d2;">☁️ Google Drive</strong>
                        <span style="float:right;color:#555;">{r['size_mb']} MB</span>
                        <p style="margin:5px 0 0;font-size:14px;color:#333;">
                            File: <strong>{r['clean_filename']}</strong>
                        </p>
                        <a href="{r['drive_link']}" style="display:inline-block;margin-top:12px;background:#4285f4;color:white;padding:10px 20px;border-radius:6px;text-decoration:none;font-weight:bold;">
                            📥 Download from Drive
                        </a>
                    </div>
                </div>
            </div>
            """

    summary = ""
    if has_attachment and has_drive:
        summary = f"Small videos attached ({total_attach_mb} MB total). Larger ones uploaded to Google Drive."
    elif has_attachment:
        summary = f"All videos attached ({total_attach_mb} MB total). Click download icon in Gmail."
    elif has_drive:
        summary = "Videos uploaded to your Google Drive folder."

    return f"""
    <div style="font-family:'Roboto',Arial,sans-serif;max-width:750px;margin:0 auto;background:#f5f5f5;padding:20px;border-radius:16px;">
        <div style="background:#FF0000;padding:20px;text-align:center;border-radius:16px 16px 0 0;">
            <h1 style="margin:0;color:white;font-size:28px;font-weight:700;">YouTube Bot</h1>
            <p style="margin:8px 0 0;color:#fff8f8;font-size:16px;">Your videos are ready!</p>
        </div>
        <div style="background:white;padding:30px;border-radius:0 0 16px 16px;box-shadow:0 8px 20px rgba(0,0,0,0.1);">
            <p style="font-size:16px;color:#333;margin-bottom:30px;">{summary}</p>
            {cards}
            <hr style="border:none;border-top:1px dashed #ddd;margin:40px 0;">
            <p style="text-align:center;color:#777;font-size:13px;">
                Saved to folder: "<strong>{CONFIG['DRIVE_FOLDER_NAME']}</strong>" in your Google Drive
            </p>
        </div>
    </div>
    """

def build_search_results_html(results: list, query: str) -> str:
    cards = ""
    reply_email = CONFIG["GMAIL_BOT"]

    for r in results:
        cards += f"""
        <div style="background:#fafafa;padding:20px;border-radius:12px;margin:20px 0;border:1px solid #eee;">
            <table width="100%" cellpadding="0" cellspacing="0">
                <tr>
                    <td valign="top" width="160" style="padding-right:20px;">
                        <img src="{r['thumbnail']}" width="160" height="90" style="border-radius:8px;object-fit:cover;">
                    </td>
                    <td valign="top">
                        <h3 style="margin:0 0 8px;font-size:17px;color:#111;">{r['title'][:80]}{'...' if len(r['title'])>80 else ''}</h3>
                        <p style="margin:0 0 10px;color:#666;font-size:14px;">
                            {r['channel']} • {r['duration']} • {r['views']:,} views
                        </p>
                        <a href="mailto:{reply_email}?subject=ct&body={r['link']}"
                           style="background:#FF0000;color:white;padding:10px 20px;border-radius:50px;text-decoration:none;font-weight:bold;display:inline-block;margin-right:12px;">
                            ↓ Download This Video
                        </a>
                        <a href="{r['link']}" style="color:#0d47a1;text-decoration:none;font-size:14px;">Watch on YouTube →</a>
                    </td>
                </tr>
            </table>
        </div>
        """

    return f"""
    <div style="font-family:'Roboto',Arial,sans-serif;max-width:750px;margin:20px auto;background:white;border-radius:16px;box-shadow:0 8px 25px rgba(0,0,0,0.1);overflow:hidden;">
        <div style="background:#FF0000;padding:20px;text-align:center;">
            <h2 style="margin:0;color:white;font-size:24px;font-weight:700;">Search Results</h2>
        </div>
        <div style="padding:30px;">
            <h3 style="color:#333;margin-bottom:20px;">Top results for: <strong>"{query}"</strong></h3>
            <p style="color:#555;margin-bottom:30px;">Click "Download This Video" to get any video delivered to your email + Drive.</p>
            {cards if cards else "<p>No results found. Try a different search!</p>"}
        </div>
    </div>
    """

# ====================== UTILITIES ======================
def sanitize_filename(name: str) -> str:
    name = re.sub(r'[^\w\s.-]', '_', name)
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    return name.strip()[:150]

def format_duration(iso_or_seconds) -> str:
    if isinstance(iso_or_seconds, str):  # ISO duration
        match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', iso_or_seconds)
        if not match:
            return "N/A"
        h, m, s = (int(g) if g else 0 for g in match.groups())
    else:  # seconds
        h, rem = divmod(iso_or_seconds or 0, 3600)
        m, s = divmod(rem, 60)

    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"
