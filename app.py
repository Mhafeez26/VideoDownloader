from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS
import yt_dlp
import os
import uuid
import threading
import time
import shutil
import multiprocessing

app = Flask(__name__)
CORS(app)

DOWNLOAD_FOLDER = "downloads"
os.makedirs(DOWNLOAD_FOLDER, exist_ok=True)

CPU_COUNT = multiprocessing.cpu_count()
download_progress = {}
_lock = threading.Lock()

def has_ffmpeg():
    return shutil.which('ffmpeg') is not None

def cleanup_file(filepath, delay=600):
    def remove():
        time.sleep(delay)
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
        except:
            pass
    threading.Thread(target=remove, daemon=True).start()

def cleanup_dir(dirpath, delay=600):
    def remove():
        time.sleep(delay)
        try:
            if os.path.exists(dirpath):
                shutil.rmtree(dirpath, ignore_errors=True)
        except:
            pass
    threading.Thread(target=remove, daemon=True).start()

def progress_hook(d, task_id):
    if d['status'] == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
        downloaded = d.get('downloaded_bytes', 0)
        speed = d.get('speed', 0) or 0
        eta = d.get('eta', 0) or 0
        percent = round((downloaded / total * 100), 1) if total > 0 else 0
        with _lock:
            download_progress[task_id] = {
                'status': 'downloading',
                'percent': percent,
                'speed': speed,
                'eta': eta,
                'downloaded': downloaded,
                'total': total
            }
    elif d['status'] == 'finished':
        with _lock:
            prev = download_progress.get(task_id, {})
            download_progress[task_id] = {**prev, 'status': 'processing', 'percent': 99}

def segment_progress_hook(d, task_id, seg_index, total_segs):
    if d['status'] == 'downloading':
        total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
        downloaded = d.get('downloaded_bytes', 0)
        speed = d.get('speed', 0) or 0
        seg_pct = (downloaded / total * 100) if total > 0 else 0
        overall_pct = round(((seg_index + seg_pct / 100) / total_segs) * 95, 1)
        with _lock:
            download_progress[task_id] = {
                'status': 'downloading',
                'percent': overall_pct,
                'speed': speed,
                'eta': d.get('eta', 0) or 0,
                'downloaded': downloaded,
                'total': total,
                'segment': seg_index + 1,
                'total_segments': total_segs,
                'seg_percent': round(seg_pct, 1)
            }
    elif d['status'] == 'finished':
        with _lock:
            prev = download_progress.get(task_id, {})
            download_progress[task_id] = {**prev, 'status': 'processing', 'percent': 99}

def build_ydl_opts(output_template, format_id, audio_only, task_id):
    ffmpeg = has_ffmpeg()
    base = {
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
        'progress_hooks': [lambda d: progress_hook(d, task_id)],
        'concurrent_fragment_downloads': min(16, CPU_COUNT * 4),
        'http_chunk_size': 10485760,
        'retries': 10,
        'fragment_retries': 10,
        'file_access_retries': 5,
        'extractor_retries': 5,
        'socket_timeout': 30,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        },
        'buffersize': 1024 * 256,
    }
    if audio_only:
        base['format'] = 'bestaudio/best'
        if ffmpeg:
            base['postprocessors'] = [{'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '320'}]
        return base
    if ffmpeg:
        if format_id and format_id != 'best':
            base['format'] = f"{format_id}+bestaudio/{format_id}/bestvideo+bestaudio/best"
        else:
            base['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best'
        base['merge_output_format'] = 'mp4'
    else:
        base['format'] = f"{format_id}/best[ext=mp4]/best" if (format_id and format_id != 'best') else 'best[ext=mp4]/best'
    return base

def build_segment_ydl_opts(output_template, format_id, task_id, seg_index, total_segs, start_sec, end_sec):
    ffmpeg = has_ffmpeg()
    base = {
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
        'progress_hooks': [lambda d: segment_progress_hook(d, task_id, seg_index, total_segs)],
        'concurrent_fragment_downloads': min(16, CPU_COUNT * 4),
        'http_chunk_size': 10485760,
        'retries': 10,
        'fragment_retries': 10,
        'file_access_retries': 5,
        'extractor_retries': 5,
        'socket_timeout': 30,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        },
        'buffersize': 1024 * 256,
    }
    if ffmpeg:
        if format_id and format_id != 'best':
            base['format'] = f"{format_id}+bestaudio/{format_id}/bestvideo+bestaudio/best"
        else:
            base['format'] = 'bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best'
        base['merge_output_format'] = 'mp4'
        base['postprocessor_args'] = {
            'ffmpeg': [
                '-ss', str(start_sec),
                '-to', str(end_sec),
            ]
        }
        base['external_downloader'] = 'ffmpeg'
        base['external_downloader_args'] = {
            'ffmpeg_i': ['-ss', str(start_sec), '-to', str(end_sec)]
        }
    else:
        base['format'] = f"{format_id}/best[ext=mp4]/best" if (format_id and format_id != 'best') else 'best[ext=mp4]/best'
        base['download_ranges'] = yt_dlp.utils.download_range_func(None, [(start_sec, end_sec)])
    return base

@app.route('/info', methods=['POST'])
def get_info():
    data = request.json
    url = data.get('url', '').strip()
    if not url:
        return jsonify({'error': 'URL required'}), 400
    try:
        ffmpeg = has_ffmpeg()
        ydl_opts = {
            'quiet': True,
            'no_warnings': True,
            'socket_timeout': 15,
            'skip_download': True,
            'extract_flat': False,
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
            }
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            all_fmts = info.get('formats') or []
            formats = []
            seen_res = set()
            if ffmpeg:
                candidates = [f for f in all_fmts if f.get('vcodec') != 'none']
            else:
                candidates = [f for f in all_fmts if f.get('vcodec') != 'none' and f.get('acodec') != 'none']
            for f in candidates:
                res = f.get('height')
                if res and res not in seen_res:
                    seen_res.add(res)
                    formats.append({
                        'format_id': f['format_id'],
                        'resolution': f'{res}p',
                        'ext': f.get('ext', 'mp4'),
                        'filesize': f.get('filesize') or f.get('filesize_approx', 0),
                        'tbr': f.get('tbr', 0) or 0,
                        'fps': f.get('fps', 0) or 0,
                    })
            formats.sort(key=lambda x: (int(x['resolution'].replace('p', '')), x['tbr']), reverse=True)
            if not formats:
                formats.append({'format_id': 'best', 'resolution': 'Best', 'ext': 'mp4', 'filesize': 0, 'tbr': 0, 'fps': 0})

            duration = info.get('duration', 0) or 0
            return jsonify({
                'title': info.get('title', 'Unknown'),
                'thumbnail': info.get('thumbnail', ''),
                'duration': duration,
                'uploader': info.get('uploader', 'Unknown'),
                'platform': info.get('extractor_key', 'Unknown'),
                'view_count': info.get('view_count', 0),
                'formats': formats,
                'ffmpeg': ffmpeg,
                'concurrent_fragments': min(16, CPU_COUNT * 4),
                'needs_segments': duration > 1200
            })
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/download', methods=['POST'])
def download_video():
    data = request.json
    url = data.get('url', '').strip()
    format_id = data.get('format_id', 'best')
    audio_only = data.get('audio_only', False)
    use_segments = data.get('use_segments', False)
    segment_minutes = int(data.get('segment_minutes', 10))
    duration = int(data.get('duration', 0))

    if not url:
        return jsonify({'error': 'URL required'}), 400

    task_id = str(uuid.uuid4())
    with _lock:
        download_progress[task_id] = {'status': 'starting', 'percent': 0}

    if use_segments and duration > 0 and has_ffmpeg() and not audio_only:
        threading.Thread(target=run_segment_download,
                         args=(task_id, url, format_id, duration, segment_minutes),
                         daemon=True).start()
    else:
        threading.Thread(target=run_single_download,
                         args=(task_id, url, format_id, audio_only),
                         daemon=True).start()

    return jsonify({'task_id': task_id})

def run_single_download(task_id, url, format_id, audio_only):
    output_template = os.path.join(DOWNLOAD_FOLDER, f"{task_id}.%(ext)s")
    ydl_opts = build_ydl_opts(output_template, format_id, audio_only, task_id)
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
        found_file = None
        found_ext = 'mp4'
        for f in os.listdir(DOWNLOAD_FOLDER):
            if f.startswith(task_id) and os.path.isfile(os.path.join(DOWNLOAD_FOLDER, f)):
                found_file = f
                found_ext = f.rsplit('.', 1)[-1] if '.' in f else ('mp3' if audio_only else 'mp4')
                break
        if not found_file:
            with _lock:
                download_progress[task_id] = {'status': 'error', 'error': 'File not found after download'}
            return
        filepath = os.path.join(DOWNLOAD_FOLDER, found_file)
        safe_title = "".join(c for c in info.get('title', 'video') if c.isalnum() or c in ' -_')[:80]
        fsize = os.path.getsize(filepath)
        with _lock:
            download_progress[task_id] = {
                'status': 'done',
                'percent': 100,
                'filename': found_file,
                'title': safe_title,
                'ext': found_ext,
                'filesize': fsize,
                'segmented': False
            }
        cleanup_file(filepath)
    except Exception as e:
        with _lock:
            download_progress[task_id] = {'status': 'error', 'error': str(e)}

def run_segment_download(task_id, url, format_id, duration, segment_minutes):
    seg_dir = os.path.join(DOWNLOAD_FOLDER, task_id)
    os.makedirs(seg_dir, exist_ok=True)

    segment_sec = segment_minutes * 60
    segments = []
    t = 0
    idx = 0
    while t < duration:
        end = min(t + segment_sec, duration)
        segments.append((idx, t, end))
        t = end
        idx += 1

    total_segs = len(segments)

    with _lock:
        download_progress[task_id] = {
            'status': 'downloading',
            'percent': 0,
            'segment': 1,
            'total_segments': total_segs,
            'seg_percent': 0,
            'speed': 0,
            'eta': 0,
            'downloaded': 0,
            'total': 0
        }

    seg_files = []
    try:
        for seg_index, start_sec, end_sec in segments:
            with _lock:
                if download_progress.get(task_id, {}).get('status') == 'cancelled':
                    return

            out_tpl = os.path.join(seg_dir, f"seg{seg_index:03d}.%(ext)s")
            ydl_opts = build_segment_ydl_opts(out_tpl, format_id, task_id, seg_index, total_segs, start_sec, end_sec)

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)

            found = None
            for f in os.listdir(seg_dir):
                if f.startswith(f"seg{seg_index:03d}"):
                    found = os.path.join(seg_dir, f)
                    break
            if found:
                seg_files.append(found)

        with _lock:
            download_progress[task_id] = {**download_progress.get(task_id, {}), 'status': 'merging', 'percent': 96}

        safe_title = "".join(c for c in info.get('title', 'video') if c.isalnum() or c in ' -_')[:60]
        final_filename = f"{task_id}_full.mp4"
        final_path = os.path.join(DOWNLOAD_FOLDER, final_filename)

        concat_list = os.path.join(seg_dir, "concat.txt")
        with open(concat_list, 'w', encoding='utf-8') as cf:
            for sf in seg_files:
                abs_path = os.path.abspath(sf).replace('\\', '/')
                cf.write(f"file '{abs_path}'\n")

        ffmpeg_cmd = f'ffmpeg -y -f concat -safe 0 -i "{concat_list}" -c copy "{final_path}"'
        ret = os.system(ffmpeg_cmd)
        if ret != 0 or not os.path.exists(final_path):
            with _lock:
                download_progress[task_id] = {'status': 'error', 'error': 'FFmpeg merge fail hua'}
            return

        fsize = os.path.getsize(final_path)
        with _lock:
            download_progress[task_id] = {
                'status': 'done',
                'percent': 100,
                'filename': final_filename,
                'title': safe_title,
                'ext': 'mp4',
                'filesize': fsize,
                'segmented': True,
                'total_segments': total_segs
            }
        cleanup_file(final_path)
        cleanup_dir(seg_dir)

    except Exception as e:
        with _lock:
            download_progress[task_id] = {'status': 'error', 'error': str(e)}
        shutil.rmtree(seg_dir, ignore_errors=True)

@app.route('/progress/<task_id>')
def get_progress(task_id):
    with _lock:
        data = download_progress.get(task_id, {'status': 'not_found'})
    return jsonify(data)

@app.route('/file/<filename>')
def serve_file(filename):
    safe = os.path.basename(filename)
    filepath = os.path.join(DOWNLOAD_FOLDER, safe)
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404
    return send_file(filepath, as_attachment=True)

@app.route('/cancel/<task_id>', methods=['POST'])
def cancel_download(task_id):
    with _lock:
        if task_id in download_progress:
            download_progress[task_id] = {'status': 'cancelled'}
    return jsonify({'ok': True})

@app.route('/health')
def health():
    return jsonify({
        'ok': True,
        'ffmpeg': has_ffmpeg(),
        'cpu_cores': CPU_COUNT,
        'concurrent_fragments': min(16, CPU_COUNT * 4),
        'active': sum(1 for v in download_progress.values() if v.get('status') == 'downloading')
    })

if __name__ == '__main__':
    try:
        import waitress
        print(f"[VidSnatch] High-performance server: http://localhost:5000")
        print(f"[VidSnatch] CPU: {CPU_COUNT} cores | Fragments: {min(16, CPU_COUNT*4)} parallel")
        print(f"[VidSnatch] FFmpeg: {'READY' if has_ffmpeg() else 'NOT FOUND'}")
        waitress.serve(app, host='0.0.0.0', port=5000, threads=CPU_COUNT * 2)
    except ImportError:
        print(f"[VidSnatch] http://localhost:5000 | Tip: pip install waitress for faster server")
        app.run(debug=False, port=5000, threaded=True)