# app.py - FULL WORKING YOUTUBE BOT (January 2026)

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import yt_dlp
import os
import tempfile
import re
import requests
import json
from datetime import datetime
from typing import List
import hashlib
import traceback
import base64
import io

# Google APIs
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseUpload

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
    "DRIVE_FOLDER_NAME": "YouTube Bot Downloads",
    "MAX_ATTACHMENT_MB": 24,
    "MAX_VIDEO_SIZE_MB": 200,
    "YOUTUBE_API_KEY": os.getenv("YOUTUBE_API_KEY"),
    "SPREADSHEET_ID": os.getenv("SPREADSHEET_ID"),
    "SERVICE_ACCOUNT_JSON": os.getenv("SERVICE_ACCOUNT_JSON"),
    "EMAIL_WEBHOOK_URL": os.getenv("EMAIL_WEBHOOK_URL"),  # Apps Script URL
    "USAGE_RESET_HOURS": 24
}

# ====================== SERVICE ACCOUNT LOADING ======================
def get_service_account_info():
    json_str = CONFIG["SERVICE_ACCOUNT_JSON"]
    if not json_str:
        print("SERVICE_ACCOUNT_JSON not set")
        return None
    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        print(f"Invalid SERVICE_ACCOUNT_JSON: {e}")
        return None

def get_drive_service():
    info = get_service_account_info()
    if not info:
        return None
    try:
        creds = Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/drive", "https://www.googleapis.com/auth/drive.file"]
        )
        return build("drive", "v3", credentials=creds)
    except Exception as e:
        print(f"Drive service error: {e}")
        return None

def get_sheets_service():
    info = get_service_account_info()
    if not info:
        return None
    try:
        creds = Credentials.from_service_account_info(
            info,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        return build("spreadsheets", "v4", credentials=creds)
    except Exception as e:
        print(f"Sheets service error: {e}")
        return None

# ====================== ROLES & QUALITY ======================
ROLE_LIMITS = {
    "admin": {"downloads": float('inf'), "searches": float('inf'), "maxResults": 15, "quality": "1080p"},
    "enterprise": {"downloads": float('inf'), "searches": float('inf'), "maxResults": 15, "quality": "1080p"},
    "pro_plus": {"downloads": 25, "searches": 25, "maxResults": 15, "quality": "720p"},
    "pro_user": {"downloads": 12, "searches": 12, "maxResults": 12, "quality": "480p"},
    "premium": {"downloads": 15, "searches": 15, "maxResults": 12, "quality": "480p"},
    "user": {"downloads": 5, "searches": 5, "maxResults": 5, "quality": "360p"},
    "standard": {"downloads": 5, "searches": 5, "maxResults": 5, "quality": "360p"},
    "guest": {"downloads": 1, "searches": 5, "maxResults": 5, "quality": "240p"},
    "free": {"downloads": 2, "searches": 5, "maxResults": 5, "quality": "240p"},
}

QUALITY_PRESETS = {
    "admin": {"format": "bestvideo[height<=1080]+bestaudio/best", "max_filesize": 200*1024*1024},
    "enterprise": {"format": "bestvideo[height<=1080]+bestaudio/best", "max_filesize": 200*1024*1024},
    "pro_plus": {"format": "bestvideo[height<=720]+bestaudio/best", "max_filesize": 100*1024*1024},
    "pro_user": {"format": "bestvideo[height<=480]+bestaudio/best", "max_filesize": 50*1024*1024},
    "premium": {"format": "bestvideo[height<=480]+bestaudio/best", "max_filesize": 35*1024*1024},
    "user": {"format": "bestvideo[height<=360]+bestaudio/best", "max_filesize": 25*1024*1024},
    "standard": {"format": "bestvideo[height<=360]+bestaudio/best", "max_filesize": 25*1024*1024},
    "guest": {"format": "bestvideo[height<=240]+bestaudio/best", "max_filesize": 15*1024*1024},
    "free": {"format": "bestvideo[height<=240]+bestaudio/best", "max_filesize": 15*1024*1024},
}

USAGE_DB = {}

# ====================== ROLE LOOKUP ======================
def get_user_role_from_sheet(email: str) -> str:
    service = get_sheets_service()
    if not service:
        return "guest"
    try:
        result = service.spreadsheets().values().get(
            spreadsheetId=CONFIG["SPREADSHEET_ID"],
            range="User Roles!A:B"
        ).execute()
        rows = result.get("values", [])
        for row in rows[1:]:
            if len(row) >= 2 and row[0].strip().lower() == email.lower():
                role = row[1].strip().lower().replace(" ", "_")
                if role in ROLE_LIMITS:
                    return role
        return "guest"
    except Exception as e:
        print(f"Role lookup failed: {e}")
        return "guest"

# ====================== USAGE ======================
def check_and_increment_usage(email: str, action: str):
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

# ====================== LOGGING ======================
def log_to_sheet(event_type: str, user_email: str, details: dict = {}):
    service = get_sheets_service()
    if not service:
        return
    try:
        row = [
            datetime.utcnow().isoformat(),
            event_type,
            user_email,
            details.get("role", "guest"),
            details.get("type", ""),
            details.get("query", "") or ", ".join(details.get("links", [])),
            details.get("count", 0),
            details.get("success", 0),
            details.get("delivery", ""),
            details.get("status", ""),
            str(details.get("error", ""))[:500]
        ]
        service.spreadsheets().values().append(
            spreadsheetId=CONFIG["SPREADSHEET_ID"],
            range="Detailed Log!A:K",
            valueInputOption="USER_ENTERED",
            body={"values": [row]}
        ).execute()
    except Exception as e:
        print(f"Logging failed: {e}")

# ====================== EMAIL VIA APPS SCRIPT ======================
def send_email(to: str, subject: str, html: str, attachments: list = []):
    webhook_url = CONFIG["EMAIL_WEBHOOK_URL"]
    if not webhook_url:
        raise Exception("EMAIL_WEBHOOK_URL not set")

    att_list = []
    for blob_bytes, filename in attachments:
        b64 = base64.b64encode(blob_bytes).decode()
        att_list.append({
            "filename": filename,
            "mimeType": "video/mp4",
            "data": b64
        })

    payload = {
        "to": to,
        "subject": subject,
        "html": html,
        "attachments": att_list
    }

    try:
        r = requests.post(webhook_url, json=payload, timeout=30)
        r.raise_for_status()
        print(f"Email sent via Apps Script to {to}")
    except Exception as e:
        print(f"Email webhook failed: {e}")
        raise

# ====================== DOWNLOAD ======================
def download_video_with_yt_dlp(url: str, preset: dict):
    ydl_opts = {
        "format": preset["format"],
        "merge_output_format": "mp4",
        "outtmpl": "%(id)s.%(ext)s",
        "max_filesize": preset["max_filesize"],
        "noplaylist": True,
        "quiet": True,
        "http_headers": {"User-Agent": "Mozilla/5.0"},
        "cookiefile": "cookies.txt" if os.path.exists("cookies.txt") else None,
        "remote_components": "ejs:github"
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts["outtmpl"] = os.path.join(tmpdir, "%(id)s.%(ext)s")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)
            with open(filename, "rb") as f:
                blob = f.read()
    return blob, info

# ====================== DRIVE UPLOAD ======================
def upload_to_drive(blob: bytes, filename: str) -> str:
    service = get_drive_service()
    if not service:
        raise Exception("Drive API not available")
    try:
        query = f"name='{CONFIG['DRIVE_FOLDER_NAME']}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        results = service.files().list(q=query, fields="files(id)").execute()
        folder_id = results.get("files", [{}])[0].get("id")
        if not folder_id:
            folder = service.files().create(body={"name": CONFIG["DRIVE_FOLDER_NAME"], "mimeType": "application/vnd.google-apps.folder"}).execute()
            folder_id = folder["id"]

        media = MediaIoBaseUpload(io.BytesIO(blob), mimetype="video/mp4")
        file = service.files().create(
            body={"name": filename, "parents": [folder_id]},
            media_body=media,
            fields="id, webViewLink"
        ).execute()

        service.permissions().create(fileId=file["id"], body={"type": "anyone", "role": "reader"}).execute()
        return file.get("webViewLink")
    except Exception as e:
        raise Exception(f"Drive upload failed: {str(e)}")

# ====================== MAIN ENDPOINT ======================
@app.post("/process-request")
async def process_request(req: Request, bg: BackgroundTasks):
    data = await req.json()
    bg.add_task(handle_full_request, data)
    return {"status": "queued"}

def handle_full_request(data: dict):
    print("=== BACKGROUND TASK STARTED ===")
    print("Payload:", json.dumps(data, indent=2))

    sender = data["sender"]
    email_match = re.search(r'<([^>]+)>', sender)
    user_email = email_match.group(1) if email_match else sender.strip("<>")

    log_to_sheet("REQUEST_RECEIVED", user_email, {"type": data["type"]})

    try:
        role = get_user_role_from_sheet(user_email)
        if role == "guest":
            log_to_sheet("NEW_USER", user_email, {"role": "guest"})

        if data["type"] == "download":
            results = process_downloads(data["links"], user_email)
        else:
            results = process_search(data["query"], user_email)

        html = build_final_reply_html(results)
        send_email(user_email, "📺 Your Videos Are Ready!", html, results.get("attachments", []))

        log_to_sheet("SUCCESS", user_email, {"status": "DELIVERED"})

    except Exception as e:
        error_trace = traceback.format_exc()
        print("ERROR:", error_trace)
        log_to_sheet("FAILED", user_email, {"error": str(e)})
        send_email(user_email, "⚠️ Error", f"<pre>{error_trace}</pre>")

# ====================== PROCESS DOWNLOADS ======================
def process_downloads(links: List[str], user_email: str) -> dict:
    usage = check_and_increment_usage(user_email, "download")
    if not usage["allowed"]:
        raise Exception(usage["message"])

    role = usage["role"]
    preset = QUALITY_PRESETS.get(role, QUALITY_PRESETS["guest"])
    quality = ROLE_LIMITS[role]["quality"]

    video_results = []
    attachments = []
    total_attach_mb = 0

    for url in links:
        try:
            blob, info = download_video_with_yt_dlp(url, preset)
            size_mb = round(len(blob) / (1024*1024), 1)
            title = re.sub(r'[<>:"/\\|?*]', '_', info.get("title", "video"))[:100]
            channel = info.get("uploader", "Unknown")
            duration = format_duration(info.get("duration", 0))
            thumb = info.get("thumbnail", "")
            clean_name = f"{title} - {channel}.mp4"

            if size_mb <= CONFIG["MAX_ATTACHMENT_MB"] and total_attach_mb + size_mb <= CONFIG["MAX_ATTACHMENT_MB"]:
                attachments.append((blob, clean_name))
                total_attach_mb += size_mb
                delivery = "email"
                drive_link = None
            else:
                drive_link = upload_to_drive(blob, clean_name)
                delivery = "drive"

            video_results.append({
                "success": True,
                "title": info.get("title", "Unknown"),
                "channel": channel,
                "duration": duration,
                "size_mb": size_mb,
                "quality": quality,
                "thumbnail": thumb,
                "delivery": delivery,
                "drive_link": drive_link,
                "clean_filename": clean_name
            })
        except Exception as e:
            video_results.append({"success": False, "error": str(e)})

    return {
        "type": "download",
        "results": video_results,
        "attachments": attachments,
        "total_attach_size_mb": total_attach_mb
    }

# ====================== PROCESS SEARCH ======================
def process_search(query: str, user_email: str) -> dict:
    usage = check_and_increment_usage(user_email, "search")
    if not usage["allowed"]:
        raise Exception(usage["message"])

    role = usage["role"]
    max_results = ROLE_LIMITS[role]["maxResults"]

    search_url = "https://www.googleapis.com/youtube/v3/search"
    params = {"part": "snippet", "q": query, "maxResults": max_results, "type": "video", "key": CONFIG["YOUTUBE_API_KEY"]}
    search_res = requests.get(search_url, params=params).json()

    items = search_res.get("items", [])
    results = []

    if items:
        video_ids = [i["id"]["videoId"] for i in items]
        videos_url = "https://www.googleapis.com/youtube/v3/videos"
        v_params = {"part": "snippet,contentDetails,statistics", "id": ",".join(video_ids), "key": CONFIG["YOUTUBE_API_KEY"]}
        videos_res = requests.get(videos_url, params=v_params).json()
        for item in videos_res.get("items", []):
            s = item["snippet"]
            results.append({
                "video_id": item["id"],
                "title": s["title"],
                "channel": s["channelTitle"],
                "thumbnail": s["thumbnails"]["high"]["url"],
                "duration": format_duration(item["contentDetails"]["duration"]),
                "views": int(item["statistics"].get("viewCount", 0)),
                "link": f"https://youtu.be/{item['id']}"
            })

    return {"type": "search", "results": results, "query": query}

# ====================== HTML ======================
STYLE = "<style>@import url('https://fonts.googleapis.com/css2?family=Roboto:wght@400;500;700&display=swap');</style>"

def build_final_reply_html(data: dict) -> str:
    if data["type"] == "search":
        return STYLE + build_search_html(data["results"], data["query"])
    return STYLE + build_download_html(data["results"], data.get("total_attach_size_mb", 0))

def build_download_html(results: list, total_attach_mb: float):
    cards = ""
    for r in results:
        if not r.get("success"):
            cards += f'<div style="background:#ffebee;padding:15px;border-radius:8px;margin:15px 0;color:#c62828;">Failed: {r["error"]}</div>'
            continue

        if r["delivery"] == "email":
            cards += f'''
            <div style="background:white;border-radius:12px;overflow:hidden;margin:20px 0;box-shadow:0 4px 12px rgba(0,0,0,0.1);border-left:4px solid #0f9d58;">
                <img src="{r['thumbnail']}" style="width:100%;display:block;">
                <div style="padding:16px;">
                    <h3 style="margin:0 0 8px;font-size:18px;">{r['title']}</h3>
                    <p style="margin:0;color:#555;">{r['channel']} • {r['duration']} • {r['size_mb']} MB</p>
                    <div style="margin-top:12px;background:#e8f5e9;padding:12px;border-radius:8px;">
                        <strong style="color:#0f9d58;">📎 Attached</strong> - {r['clean_filename']}
                    </div>
                </div>
            </div>'''
        else:
            cards += f'''
            <div style="background:white;border-radius:12px;overflow:hidden;margin:20px 0;box-shadow:0 4px 12px rgba(0,0,0,0.1);border-left:4px solid #4285f4;">
                <img src="{r['thumbnail']}" style="width:100%;display:block;">
                <div style="padding:16px;">
                    <h3 style="margin:0 0 8px;font-size:18px;">{r['title']}</h3>
                    <p style="margin:0;color:#555;">{r['channel']} • {r['duration']} • {r['size_mb']} MB</p>
                    <div style="margin-top:12px;background:#e3f2fd;padding:12px;border-radius:8px;">
                        <strong style="color:#1976d2;">☁️ Google Drive</strong>
                        <a href="{r['drive_link']}" style="display:block;margin-top:8px;background:#4285f4;color:white;padding:10px;border-radius:6px;text-decoration:none;">Download</a>
                    </div>
                </div>
            </div>'''

    return f'''
    <div style="font-family:'Roboto',sans-serif;max-width:750px;margin:0 auto;background:#f5f5f5;padding:20px;border-radius:16px;">
        <div style="background:#FF0000;padding:20px;text-align:center;border-radius:16px 16px 0 0;">
            <h1 style="margin:0;color:white;">YouTube Bot</h1>
        </div>
        <div style="background:white;padding:30px;border-radius:0 0 16px 16px;">
            <p style="font-size:16px;color:#333;">Your videos are ready!</p>
            {cards}
        </div>
    </div>'''

def build_search_html(results: list, query: str):
    cards = ""
    bot_email = "yourbot@gmail.com"  # Change if needed
    for r in results:
        cards += f'''
        <div style="background:#fafafa;padding:20px;border-radius:12px;margin:20px 0;">
            <img src="{r['thumbnail']}" style="width:160px;height:90px;object-fit:cover;float:left;margin-right:20px;border-radius:8px;">
            <h3 style="margin:0 0 8px;">{r['title'][:80]}{'...' if len(r['title'])>80 else ''}</h3>
            <p style="margin:0 0 12px;color:#666;">{r['channel']} • {r['duration']} • {r['views']:,} views</p>
            <a href="mailto:{bot_email}?subject=ct&body={r['link']}" style="background:#FF0000;color:white;padding:10px 20px;border-radius:50px;text-decoration:none;">Download This Video</a>
        </div>'''

    return f'''
    <div style="font-family:'Roboto',sans-serif;max-width:750px;margin:20px auto;background:white;border-radius:16px;overflow:hidden;">
        <div style="background:#FF0000;padding:20px;text-align:center;">
            <h2 style="margin:0;color:white;">Search Results for "{query}"</h2>
        </div>
        <div style="padding:30px;">
            {cards or "<p>No results found. Try different keywords!</p>"}
        </div>
    </div>'''

# ====================== UTILS ======================
def format_duration(arg):
    if isinstance(arg, str):
        match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', arg)
        h,m,s = (int(match.group(i) or 0) for i in (1,2,3))
    else:
        h, rem = divmod(arg or 0, 3600)
        m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

# ====================== BACKWARD COMPATIBLE ======================
@app.get("/download")
async def download_video(url: str, quality: str = "user"):
    preset = QUALITY_PRESETS.get(quality.lower(), QUALITY_PRESETS["guest"])
    blob, info = download_video_with_yt_dlp(url, preset)
    safe_title = re.sub(r'[<>:"/\\|?*]', '_', info.get("title", "video"))[:100]
    return StreamingResponse(
        io.BytesIO(blob),
        media_type="video/mp4",
        headers={"Content-Disposition": f'attachment; filename="{safe_title}.mp4"'}
    )

@app.get("/")
async def root():
    return {"service": "YouTube Bot", "status": "running"}
