# language: Python 3.11+, file: app.py, target: Windows/Linux/Mac
# YouTube downloader backend — yt-dlp + Flask

import re
import uuid
import threading
import os
import shutil
import sys
from pathlib import Path
from flask import Flask, request, jsonify, send_file, abort
from flask_cors import CORS
import yt_dlp

# ============================================================
#  CONFIG
# ============================================================
BASE_DIR     = Path(__file__).parent.resolve()
DOWNLOAD_DIR = BASE_DIR / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)

# FFmpeg Setup
FFMPEG_DIR = str(BASE_DIR)
if not (BASE_DIR / 'ffmpeg.exe').exists():
    sys_ffmpeg = shutil.which('ffmpeg')
    if sys_ffmpeg:
        FFMPEG_DIR = str(Path(sys_ffmpeg).parent)
print(f"[FFmpeg] {FFMPEG_DIR}")

# COOKIES SETUP (Direct GitHub file se)
COOKIE_FILE = None
if (BASE_DIR / 'www.youtube.com_cookies.txt').exists():
    COOKIE_FILE = BASE_DIR / 'www.youtube.com_cookies.txt'
    print("[Cookies] Loaded from www.youtube.com_cookies.txt")
elif (BASE_DIR / 'cookies.txt').exists():
    COOKIE_FILE = BASE_DIR / 'cookies.txt'
    print("[Cookies] Loaded from cookies.txt")
else:
    print("[Cookies] WARNING: No cookies found!")

JOBS = {}
JOBS_LOCK = threading.Lock()
app = Flask(__name__, static_folder=str(BASE_DIR), static_url_path="")
CORS(app)

# ============================================================
#  HELPERS
# ============================================================
VIDEO_ID_RE = re.compile(r'(?:youtu\.be/|v=|embed/|shorts/|live/)([a-zA-Z0-9_-]{11})')

def extract_video_id(url: str):
    if not url: return None
    url = url.strip()
    m = VIDEO_ID_RE.search(url)
    if m: return m.group(1)
    if re.fullmatch(r'[a-zA-Z0-9_-]{11}', url): return url
    return None

def human_size(n):
    if not n: return None
    for unit in ['B', 'KB', 'MB', 'GB']:
        if n < 1024: return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"

def safe_filename(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip()[:150]

# ============================================================
#  YT-DLP INFO EXTRACTION
# ============================================================
def get_base_opts():
    opts = {
        'quiet': True,
        'no_warnings': True,
        'noplaylist': True,
        'ffmpeg_location': FFMPEG_DIR,
        # PO Token Bypass (mweb, tv, web_safari use karega)
        'extractor_args': {'youtube': ['player_client=mweb,tv,web_safari']},
    }
    if COOKIE_FILE and COOKIE_FILE.exists():
        opts['cookiefile'] = str(COOKIE_FILE)
    return opts

def extract_info(url: str):
    opts = get_base_opts()
    opts['skip_download'] = True
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

def build_quality_list(info):
    formats = info.get('formats') or []
    heights = {}
    for f in formats:
        if f.get('vcodec') in (None, 'none'): continue
        h = f.get('height')
        if not h: continue
        is_mp4 = (f.get('ext') in ('mp4', 'm4v'))
        has_audio = f.get('acodec') not in (None, 'none')
        score = (is_mp4, has_audio, f.get('tbr') or 0)
        if h not in heights or score > heights[h]['score']:
            heights[h] = {'score': score, 'fps': f.get('fps'), 'filesize': f.get('filesize') or f.get('filesize_approx')}

    mp4_list = []
    if heights:
        max_h = max(heights.keys())
        for h in sorted(heights.keys(), reverse=True):
            info_h = heights[h]
            sub = []
            if h >= 2160: sub.append('4K Ultra HD')
            elif h >= 1440: sub.append('2K Quad HD')
            elif h >= 1080: sub.append('Full HD')
            elif h >= 720: sub.append('HD')
            else: sub.append('SD')
            if info_h['fps'] and info_h['fps'] >= 50: sub.append(f"{int(info_h['fps'])}fps")
            if info_h['filesize']: sub.append(human_size(info_h['filesize']))
            mp4_list.append({'id': str(h), 'label': f"{h}p", 'sub': ' · '.join(sub), 'badge': 'BEST' if h == max_h else None})

    best_audio_abr = 0
    for f in formats:
        if f.get('acodec') not in (None, 'none') and f.get('vcodec') in (None, 'none'):
            abr = f.get('abr') or 0
            if abr > best_audio_abr: best_audio_abr = abr
    if best_audio_abr == 0:
        for f in formats:
            if f.get('acodec') not in (None, 'none'):
                abr = f.get('abr') or 0
                if abr > best_audio_abr: best_audio_abr = abr

    ladder = [320, 256, 192, 128, 96]
    usable = [b for b in ladder if b <= (best_audio_abr or 320) + 16]
    if not usable: usable = [128]

    mp3_list = []
    for b in usable:
        mp3_list.append({'id': str(b), 'label': f"{b} kbps", 'sub': 'Highest quality' if b == usable[0] and b >= 256 else 'Good', 'badge': 'BEST' if b == usable[0] else None})

    return {'mp4': mp4_list, 'mp3': mp3_list}

# ============================================================
#  DOWNLOAD WORKER
# ============================================================
def run_download(job_id, url, fmt, quality):
    try:
        with JOBS_LOCK:
            JOBS[job_id]['status'] = 'downloading'
            JOBS[job_id]['progress'] = 0

        outtmpl = str(DOWNLOAD_DIR / f"{job_id}_%(title)s.%(ext)s")
        ydl_opts = get_base_opts()
        ydl_opts['outtmpl'] = outtmpl
        ydl_opts['progress_hooks'] = [lambda d: _hook(job_id, d)]

        if fmt == 'mp4':
            h = int(quality)
            ydl_opts['format'] = f"best[height<={h}][ext=mp4]/bestvideo[height<={h}]+bestaudio/best"
            ydl_opts['merge_output_format'] = 'mp4'
        else:
            ydl_opts['format'] = 'bestaudio/best'
            ydl_opts['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': str(quality)}]

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.extract_info(url, download=True)
            path = _resolve_final_path(job_id)

        with JOBS_LOCK:
            JOBS[job_id]['status'] = 'done'
            JOBS[job_id]['progress'] = 100
            JOBS[job_id]['path'] = str(path)
            JOBS[job_id]['filename'] = safe_filename(Path(path).name.replace(f"{job_id}_", ""))

    except Exception as e:
        with JOBS_LOCK:
            JOBS[job_id]['status'] = 'error'
            JOBS[job_id]['error'] = str(e)

def _hook(job_id, d):
    if d.get('status') == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate') or 0
        done = d.get('downloaded_bytes') or 0
        if total:
            pct = int(done * 100 / total)
            with JOBS_LOCK:
                if job_id in JOBS: JOBS[job_id]['progress'] = pct
    elif d.get('status') == 'finished':
        with JOBS_LOCK:
            if job_id in JOBS: JOBS[job_id]['progress'] = 100

def _resolve_final_path(job_id):
    candidates = sorted([p for p in DOWNLOAD_DIR.iterdir() if p.name.startswith(f"{job_id}_")], key=lambda p: p.stat().st_mtime, reverse=True)
    if not candidates: raise RuntimeError("File not found")
    return candidates[0]

# ============================================================
#  ROUTES
# ============================================================
@app.route('/')
def index(): return send_file(BASE_DIR / 'index.html')

@app.route('/api/info')
def api_info():
    raw = request.args.get('url', '').strip()
    vid = extract_video_id(raw)
    if not vid: return jsonify({'error': 'Invalid URL'}), 400
    try:
        info = extract_info(f"https://www.youtube.com/watch?v={vid}")
        return jsonify({
            'id': vid, 'title': info.get('title'), 'channel': info.get('uploader') or info.get('channel'),
            'duration': info.get('duration'), 'views': info.get('view_count'),
            'thumbnail': info.get('thumbnail') or f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            'qualities': build_quality_list(info)
        })
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/download', methods=['POST'])
def api_download():
    data = request.get_json(force=True)
    vid = extract_video_id(data.get('url', '').strip())
    fmt = data.get('format', 'mp4')
    quality = data.get('quality', '')
    if not vid or fmt not in ('mp4', 'mp3') or not quality:
        return jsonify({'error': 'Invalid request'}), 400

    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {'status': 'queued', 'progress': 0}
    threading.Thread(target=run_download, args=(job_id, f"https://www.youtube.com/watch?v={vid}", fmt, quality), daemon=True).start()
    return jsonify({'job_id': job_id})

@app.route('/api/progress/<job_id>')
def api_progress(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    return jsonify(job) if job else (jsonify({'error': 'Unknown job'}), 404)

@app.route('/api/file/<job_id>')
def api_file(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job or job.get('status') != 'done': abort(404)
    return send_file(job['path'], as_attachment=True, download_name=job['filename'])

# ============================================================
#  ENTRY
# ============================================================
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
