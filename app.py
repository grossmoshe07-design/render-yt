from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
import os
import tempfile
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Quality settings by role
QUALITY_PRESETS = {
    'admin': {
        'format': (
            'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/'
            'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/'
            'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/'
            'best[height<=1080]/best[height<=720]/best[height<=480]/best'
        ),
        'max_filesize': 200 * 1024 * 1024,  # 200 MB
        'label': '1080p'
    },
    'enterprise': {
        'format': (
            'bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/'
            'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/'
            'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/'
            'best[height<=1080]/best[height<=720]/best[height<=480]/best'
        ),
        'max_filesize': 200 * 1024 * 1024,  # 200 MB
        'label': '1080p'
    },
    'pro_plus': {
        'format': (
            'bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/'
            'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/'
            'best[height<=720]/best[height<=480]/best'
        ),
        'max_filesize': 50 * 1024 * 1024,  # 50 MB
        'label': '720p'
    },
    'pro_user': {
        'format': (
            'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/'
            'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/'
            'best[height<=480]/best[height<=360]/best'
        ),
        'max_filesize': 35 * 1024 * 1024,  # 35 MB
        'label': '480p'
    },
    'premium': {
        'format': (
            'bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/'
            'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/'
            'best[height<=480]/best[height<=360]/best'
        ),
        'max_filesize': 35 * 1024 * 1024,  # 35 MB
        'label': '480p'
    },
    'user': {
        'format': (
            'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/'
            'bestvideo[height<=240][ext=mp4]+bestaudio[ext=m4a]/'
            'best[height<=360]/best[height<=240]/worst'
        ),
        'max_filesize': 25 * 1024 * 1024,  # 25 MB
        'label': '360p'
    },
    'standard': {
        'format': (
            'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/'
            'bestvideo[height<=240][ext=mp4]+bestaudio[ext=m4a]/'
            'best[height<=360]/best[height<=240]/worst'
        ),
        'max_filesize': 25 * 1024 * 1024,  # 25 MB
        'label': '360p'
    },
    'guest': {
        'format': (
            'bestvideo[height<=240][ext=mp4]+bestaudio[ext=m4a]/'
            'best[height<=240]/worst'
        ),
        'max_filesize': 15 * 1024 * 1024,  # 15 MB
        'label': '240p'
    },
    'free': {
        'format': (
            'bestvideo[height<=240][ext=mp4]+bestaudio[ext=m4a]/'
            'best[height<=240]/worst'
        ),
        'max_filesize': 15 * 1024 * 1024,  # 15 MB
        'label': '240p'
    },
    'denied': {
        'format': None,
        'max_filesize': 0,
        'label': 'DENIED'
    },
    'suspended': {
        'format': None,
        'max_filesize': 0,
        'label': 'SUSPENDED'
    },
    'banned': {
        'format': None,
        'max_filesize': 0,
        'label': 'BANNED'
    },
    'closed': {
        'format': None,
        'max_filesize': 0,
        'label': 'CLOSED'
    }
}

# Remove emojis & weird characters from title
def sanitize_filename(name: str) -> str:
    if not name:
        return "video"
    name = re.sub(r'[^\x00-\x7F]+', '', name)           # remove non-ASCII
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '_', name)  # remove invalid chars
    name = name.strip(" .").replace('"', "'")[:100]
    return name or "video"

@app.get("/download")
async def download_video(
    url: str = Query(...),
    quality: str = Query(default='user', description='Role-based quality: admin, enterprise, pro_plus, pro_user, premium, user, standard, guest, free, denied, suspended, banned, closed')
):
    # Validate and get quality preset
    quality_key = quality.lower()
    
    # Backward compatibility: if quality not recognized, default to 'user' (360p)
    if quality_key not in QUALITY_PRESETS:
        quality_key = 'user'
    
    quality_preset = QUALITY_PRESETS[quality_key]
    
    # Check if role is denied/restricted
    if quality_preset['format'] is None:
        return {"error": "ACCESS_DENIED", "status": quality_preset['label'], "message": "This user role does not have download access."}
    
    ydl_opts = {
        'format': quality_preset['format'],
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'outtmpl': '%(id)s.%(ext)s',
        'quiet': False,
        'cachedir': '/tmp/yt_dlp_cache',
        'format_sort': ['+size', '+br', '+res'],
        'max_filesize': quality_preset['max_filesize'],
        'cookiefile': 'cookies.txt',
        'extractor_args': {'youtube': {'skip': ['dash']}},
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        },
        'socket_timeout': 30,
        'retries': 3,
        'fragment_retries': 3,
    }
    
    temp_dir = tempfile.TemporaryDirectory()
    ydl_opts['outtmpl'] = os.path.join(temp_dir.name, '%(id)s.%(ext)s')
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            final_filename = ydl.prepare_filename(info)
            safe_title = sanitize_filename(info.get("title", "video"))
        
        def file_streamer():
            try:
                with open(final_filename, "rb") as f:
                    yield from f
            finally:
                temp_dir.cleanup()
        
        return StreamingResponse(
            file_streamer(),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_title}.mp4"',
                "Cache-Control": "no-cache",
                "X-Video-Quality": quality_preset['label']  # Add quality info to header
            }
        )
    except Exception as e:
        temp_dir.cleanup()
        return {"error": str(e)}

@app.get("/quality-info")
async def get_quality_info():
    """Endpoint to check available quality presets"""
    return {
        "available_qualities": {
            role: {
                "label": preset['label'],
                "max_filesize_mb": preset['max_filesize'] / (1024 * 1024) if preset['max_filesize'] > 0 else "Denied",
                "enabled": preset['format'] is not None
            }
            for role, preset in QUALITY_PRESETS.items()
        },
        "default_quality": "user (360p)",
        "backward_compatible": True,
        "note": "If quality parameter is omitted or invalid, defaults to 'user' (360p)"
    }

@app.get("/")
async def root():
    """Root endpoint with API information"""
    return {
        "service": "YouTube Video Downloader API",
        "version": "2.1",
        "endpoints": {
            "/download": "Download video with optional quality parameter",
            "/quality-info": "Get available quality presets"
        },
        "backward_compatible": True,
        "usage": {
            "old_version": "/download?url=VIDEO_URL (defaults to 360p)",
            "new_version": "/download?url=VIDEO_URL&quality=ROLE (admin, enterprise, pro_plus, pro_user, premium, user, standard, guest, free, denied, suspended, banned, closed)"
        },
        "example": "/download?url=https://youtube.com/watch?v=xxx&quality=pro_plus"
    }

# Local testing only
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)