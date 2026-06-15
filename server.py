from __future__ import annotations

import cgi
import ctypes
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import platform
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    import cv2
except Exception:
    cv2 = None


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", app_dir())).resolve()
    return app_dir()


ROOT = app_dir()
STATIC_ROOT = resource_dir()
UPLOAD_DIR = ROOT / "uploads"
OUTPUT_DIR = ROOT / "outputs"
WORK_DIR = ROOT / "work"
LOG_DIR = ROOT / "logs"
DOWNLOAD_DIR = ROOT / "downloads"
MANIFEST_FILE = ROOT / "asset_manifest.json"
LOG_FILE = LOG_DIR / "workbench.log"
MAX_BATCH_FILES = 300
MAX_TOTAL_ASSETS = 1000
MANIFEST_LOCK = threading.Lock()
LAST_FFMPEG_ERROR: dict | None = None
FACE_CROP_CACHE: dict[str, dict | None] = {}
FACE_CROP_LOCK = threading.Lock()


def configure_windows_error_mode() -> None:
    if os.name != "nt":
        return
    try:
        sem_failcriticalerrors = 0x0001
        sem_nogpfault_errorbox = 0x0002
        sem_noopenfile_errorbox = 0x8000
        ctypes.windll.kernel32.SetErrorMode(
            sem_failcriticalerrors | sem_nogpfault_errorbox | sem_noopenfile_errorbox
        )
    except Exception:
        pass


def hidden_subprocess_kwargs() -> dict:
    if os.name != "nt":
        return {}
    startupinfo = subprocess.STARTUPINFO()
    startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = 0
    return {
        "startupinfo": startupinfo,
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0),
    }


configure_windows_error_mode()


def find_ffmpeg() -> tuple[Path, Path]:
    workspace = ROOT.parents[1] if len(ROOT.parents) > 1 else ROOT
    candidates = [
        ROOT / "ffmpeg" / "ffmpeg.exe",
        ROOT / "ffmpeg" / "bin" / "ffmpeg.exe",
        STATIC_ROOT / "ffmpeg" / "ffmpeg.exe",
        STATIC_ROOT / "ffmpeg" / "bin" / "ffmpeg.exe",
        workspace / "OpenMontage" / ".local" / "ffmpeg" / "ffmpeg-8.1.1-essentials_build" / "bin" / "ffmpeg.exe",
        workspace / "OpenMontage" / "remotion-composer" / "node_modules" / "@remotion" / "compositor-win32-x64-msvc" / "ffmpeg.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate, candidate.with_name("ffprobe.exe")
    raise RuntimeError("找不到 ffmpeg.exe")


FFMPEG, FFPROBE = find_ffmpeg()


def ensure_dirs() -> None:
    for path in [UPLOAD_DIR / "talking", UPLOAD_DIR / "environment", OUTPUT_DIR, WORK_DIR, LOG_DIR, DOWNLOAD_DIR]:
        path.mkdir(parents=True, exist_ok=True)
    if not MANIFEST_FILE.exists():
        MANIFEST_FILE.write_text("{}", encoding="utf-8")


def log_event(event: str, detail: dict | None = None) -> None:
    ensure_dirs()
    entry = {
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "event": event,
        "detail": detail or {},
    }
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def load_manifest() -> dict:
    ensure_dirs()
    try:
        return json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def save_manifest(manifest: dict) -> None:
    MANIFEST_FILE.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def tail_json_log(path: Path, limit: int = 200) -> list[dict]:
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    items = []
    for line in lines:
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            items.append({"raw": line})
    return items


def debug_report() -> dict:
    manifest = load_manifest()
    return {
        "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "system": {
            "platform": platform.platform(),
            "python": sys.version,
            "frozen": bool(getattr(sys, "frozen", False)),
            "executable": sys.executable,
        },
        "paths": {
            "root": str(ROOT),
            "staticRoot": str(STATIC_ROOT),
            "ffmpeg": str(FFMPEG),
            "ffprobe": str(FFPROBE),
            "uploads": str(UPLOAD_DIR),
            "outputs": str(OUTPUT_DIR),
            "work": str(WORK_DIR),
            "logs": str(LOG_DIR),
        },
        "pathChecks": {
            "ffmpeg": inspect_path(FFMPEG),
            "ffprobe": inspect_path(FFPROBE),
            "root": {"path": str(ROOT), "exists": ROOT.exists()},
            "outputs": {"path": str(OUTPUT_DIR), "exists": OUTPUT_DIR.exists()},
        },
        "manifest": {
            "count": len(manifest),
            "assets": [
                {
                    "id": asset.get("id"),
                    "lane": asset.get("lane"),
                    "name": asset.get("name"),
                    "path": asset.get("path"),
                    "exists": Path(asset.get("path", "")).exists(),
                    "size": Path(asset.get("path", "")).stat().st_size if Path(asset.get("path", "")).exists() else None,
                }
                for asset in manifest.values()
            ],
        },
        "lastFfmpegError": LAST_FFMPEG_ERROR,
        "recentBackendLogs": tail_json_log(LOG_FILE),
    }


def safe_name(name: str) -> str:
    stem = Path(name).stem
    suffix = Path(name).suffix.lower()
    stem = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", stem, flags=re.UNICODE).strip("._")
    return f"{stem or 'asset'}{suffix or '.mp4'}"


def run_media_probe(cmd: list[str], purpose: str, target: Path, timeout: int = 12) -> subprocess.CompletedProcess | None:
    started = time.time()
    try:
        result = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            **hidden_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired as exc:
        log_event(
            "ffprobe_timeout",
            {
                "purpose": purpose,
                "target": inspect_path(target),
                "cmd": cmd,
                "timeoutSeconds": timeout,
                "stdout": (exc.stdout or "")[-1000:] if isinstance(exc.stdout, str) else "",
                "stderr": (exc.stderr or "")[-2000:] if isinstance(exc.stderr, str) else "",
            },
        )
        return None
    except Exception as exc:
        log_event(
            "ffprobe_exception",
            {
                "purpose": purpose,
                "target": inspect_path(target),
                "cmd": cmd,
                "error": f"{type(exc).__name__}: {exc}",
            },
        )
        return None

    if result.returncode != 0:
        log_event(
            "ffprobe_error",
            {
                "purpose": purpose,
                "target": inspect_path(target),
                "cmd": cmd,
                "returncode": result.returncode,
                "elapsedSeconds": round(time.time() - started, 3),
                "stdout": result.stdout[-1000:],
                "stderr": result.stderr[-2000:],
            },
        )
    return result


def media_duration(path: Path) -> float:
    result = run_media_probe(
        [
            str(FFPROBE),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            cmd_path(path),
        ],
        "duration",
        path,
    )
    if result and result.returncode == 0:
        try:
            return max(0.1, float(result.stdout.strip()))
        except ValueError:
            log_event("ffprobe_parse_error", {"purpose": "duration", "target": inspect_path(path), "stdout": result.stdout})
    return 3.0


def has_audio(path: Path) -> bool:
    result = run_media_probe(
        [
            str(FFPROBE),
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            cmd_path(path),
        ],
        "audio_stream",
        path,
    )
    if result and result.returncode == 0:
        return bool(result.stdout.strip())
    return False


def ascii_temp_dir(name: str) -> Path:
    path = Path(tempfile.gettempdir()) / "longjing_video_editing" / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def ascii_cascade_path(filename: str) -> Path | None:
    if cv2 is None:
        return None
    target = ascii_temp_dir("opencv") / filename
    if target.exists():
        return target
    candidates = [
        STATIC_ROOT / "cv2" / "data" / filename,
        Path(getattr(cv2.data, "haarcascades", "")) / filename,
    ]
    for candidate in candidates:
        try:
            if candidate.exists():
                shutil.copyfile(candidate, target)
                return target
        except Exception:
            continue
    return None


def ascii_yunet_model_path() -> Path | None:
    target = ascii_temp_dir("opencv") / "face_detection_yunet_2023mar.onnx"
    if target.exists():
        return target
    candidates = [
        STATIC_ROOT / "models" / "face_detection_yunet_2023mar.onnx",
        ROOT / "models" / "face_detection_yunet_2023mar.onnx",
    ]
    for candidate in candidates:
        try:
            if candidate.exists():
                shutil.copyfile(candidate, target)
                return target
        except Exception:
            continue
    return None


def extract_face_frame(video: Path, time_offset: float, output: Path) -> bool:
    cmd = [
        str(FFMPEG),
        "-y",
        "-ss",
        f"{time_offset:.3f}",
        "-i",
        cmd_path(video),
        "-frames:v",
        "1",
        "-q:v",
        "2",
        str(output),
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            **hidden_subprocess_kwargs(),
        )
        if result.returncode != 0:
            log_event("face_frame_extract_error", {"video": inspect_path(video), "cmd": cmd, "stderr": result.stderr[-1200:]})
            return False
        return output.exists() and output.stat().st_size > 0
    except Exception as exc:
        log_event("face_frame_extract_exception", {"video": inspect_path(video), "error": f"{type(exc).__name__}: {exc}"})
        return False


def detect_face_crop(video: Path) -> dict | None:
    if cv2 is None:
        log_event("face_detect_unavailable", {"reason": "cv2 import failed"})
        return None

    cache_key = str(video.resolve())
    with FACE_CROP_LOCK:
        if cache_key in FACE_CROP_CACHE:
            return FACE_CROP_CACHE[cache_key]

    cascade_path = ascii_cascade_path("haarcascade_frontalface_alt2.xml")
    eye_cascade_path = ascii_cascade_path("haarcascade_eye_tree_eyeglasses.xml") or ascii_cascade_path("haarcascade_eye.xml")
    yunet_path = ascii_yunet_model_path()
    has_yunet = bool(yunet_path and hasattr(cv2, "FaceDetectorYN_create"))
    if not cascade_path and not has_yunet:
        log_event("face_detect_unavailable", {"reason": "cascade file missing"})
        with FACE_CROP_LOCK:
            FACE_CROP_CACHE[cache_key] = None
        return None

    cascade = cv2.CascadeClassifier(str(cascade_path)) if cascade_path else None
    eye_cascade = cv2.CascadeClassifier(str(eye_cascade_path)) if eye_cascade_path else None
    if not has_yunet and (cascade is None or cascade.empty()):
        log_event("face_detect_unavailable", {"reason": "cascade load failed", "cascade": str(cascade_path)})
        with FACE_CROP_LOCK:
            FACE_CROP_CACHE[cache_key] = None
        return None

    duration = media_duration(video)
    sample_times = sorted({max(0.1, min(duration - 0.1, duration * ratio)) for ratio in [0.08, 0.18, 0.32, 0.5, 0.68]})
    frame_dir = ascii_temp_dir("face_frames")
    detections = []

    for sample_index, sample_time in enumerate(sample_times, start=1):
        frame_path = frame_dir / f"{uuid.uuid4().hex}_{sample_index}.jpg"
        try:
            if not extract_face_frame(video, sample_time, frame_path):
                continue
            frame = cv2.imread(str(frame_path))
            if frame is None:
                continue
            frame_height, frame_width = frame.shape[:2]
            if has_yunet:
                try:
                    detector = cv2.FaceDetectorYN_create(str(yunet_path), "", (frame_width, frame_height), 0.65, 0.3, 5000)
                    _, faces = detector.detect(frame)
                    if faces is not None:
                        for face in faces:
                            x, y, w, h = [int(round(value)) for value in face[:4]]
                            confidence = float(face[-1])
                            if w <= 0 or h <= 0:
                                continue
                            aspect = w / max(1, h)
                            if aspect < 0.58 or aspect > 1.45:
                                continue
                            detections.append(
                                {
                                    "x": max(0, x),
                                    "y": max(0, y),
                                    "w": min(w, frame_width),
                                    "h": min(h, frame_height),
                                    "score": int(w * h * max(1.0, confidence * 2)),
                                    "confidence": round(confidence, 4),
                                    "method": "yunet",
                                    "frameWidth": int(frame_width),
                                    "frameHeight": int(frame_height),
                                    "sampleTime": round(sample_time, 3),
                                }
                            )
                        if faces is not None and len(faces):
                            continue
                except Exception as exc:
                    log_event("face_detect_yunet_error", {"error": f"{type(exc).__name__}: {exc}", "model": str(yunet_path)})
                    has_yunet = False

            if cascade is None or cascade.empty():
                continue
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.equalizeHist(gray)
            faces = cascade.detectMultiScale(
                gray,
                scaleFactor=1.06,
                minNeighbors=4,
                minSize=(max(42, frame_width // 14), max(42, frame_height // 14)),
            )
            for x, y, w, h in faces:
                roi = gray[y : y + max(1, int(h * 0.68)), x : x + w]
                eyes = []
                if eye_cascade is not None and not eye_cascade.empty() and roi.size:
                    eyes = eye_cascade.detectMultiScale(
                        roi,
                        scaleFactor=1.08,
                        minNeighbors=3,
                        minSize=(max(12, w // 8), max(8, h // 12)),
                    )
                eye_count = len(eyes)
                if eye_count < 1:
                    continue
                score = int(w * h * (1 + min(eye_count, 2) * 0.35))
                detections.append(
                    {
                        "x": int(x),
                        "y": int(y),
                        "w": int(w),
                        "h": int(h),
                        "score": score,
                        "eyes": int(eye_count),
                        "frameWidth": int(frame_width),
                        "frameHeight": int(frame_height),
                        "sampleTime": round(sample_time, 3),
                    }
                )
        finally:
            try:
                frame_path.unlink(missing_ok=True)
            except Exception:
                pass

    if not detections:
        log_event("face_detect_none", {"video": inspect_path(video), "sampleTimes": sample_times})
        with FACE_CROP_LOCK:
            FACE_CROP_CACHE[cache_key] = None
        return None

    best = max(detections, key=lambda item: item["score"])
    frame_width = best["frameWidth"]
    frame_height = best["frameHeight"]
    face_cx = best["x"] + best["w"] / 2
    face_cy = best["y"] + best["h"] * 0.56
    crop_size = int(max(best["w"] * 2.05, best["h"] * 1.72, min(frame_width, frame_height) * 0.30))
    crop_size = max(80, min(crop_size, frame_width, frame_height))
    crop_x = int(round(face_cx - crop_size / 2))
    crop_y = int(round(face_cy - crop_size / 2))
    crop_x = max(0, min(crop_x, frame_width - crop_size))
    crop_y = max(0, min(crop_y, frame_height - crop_size))
    crop = {
        "x": crop_x,
        "y": crop_y,
        "size": crop_size,
        "face": best,
        "detections": len(detections),
    }
    log_event("face_detect_done", {"video": inspect_path(video), "crop": crop})

    with FACE_CROP_LOCK:
        FACE_CROP_CACHE[cache_key] = crop
    return crop


def ratio_size(ratio: str) -> tuple[int, int]:
    if ratio.startswith("1:1"):
        return 720, 720
    if ratio.startswith("16:9"):
        return 1280, 720
    return 720, 1280


def ffmpeg_filter(width: int, height: int) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:black,"
        "setsar=1,format=yuv420p"
    )


def ffmpeg_cover_filter(width: int, height: int) -> str:
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},"
        "setsar=1,format=yuv420p"
    )


def is_image(path: Path) -> bool:
    return path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def cmd_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return resolved.relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return str(resolved)


def inspect_path(path: Path) -> dict:
    try:
        return {
            "path": str(path),
            "cmdPath": cmd_path(path),
            "exists": path.exists(),
            "isFile": path.is_file(),
            "size": path.stat().st_size if path.exists() and path.is_file() else None,
            "parentExists": path.parent.exists(),
        }
    except Exception as exc:
        return {"path": str(path), "error": str(exc)}


def inspect_cmd(cmd: list[str]) -> dict:
    paths = []
    for item in cmd[1:]:
        if isinstance(item, str) and ("/" in item or "\\" in item) and not item.startswith("["):
            candidate = Path(item)
            if not candidate.is_absolute():
                candidate = ROOT / candidate
            if candidate.suffix:
                paths.append(inspect_path(candidate))
    return {"cwd": str(ROOT), "paths": paths}


def run_cmd(cmd: list[str]) -> None:
    global LAST_FFMPEG_ERROR
    started = time.time()
    log_event("ffmpeg_command", {"cmd": cmd, "inspect": inspect_cmd(cmd)})
    result = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **hidden_subprocess_kwargs(),
    )
    elapsed = round(time.time() - started, 3)
    if result.returncode != 0:
        LAST_FFMPEG_ERROR = {
            "returncode": result.returncode,
            "elapsedSeconds": elapsed,
            "cmd": cmd,
            "inspect": inspect_cmd(cmd),
            "stderr": result.stderr[-6000:],
            "stdout": result.stdout[-2000:],
        }
        log_event("ffmpeg_error", LAST_FFMPEG_ERROR)
        raise RuntimeError(result.stderr[-1200:] or f"ffmpeg 执行失败，退出码：{result.returncode}")


def render_segment(source: Path, out: Path, ratio: str) -> None:
    width, height = ratio_size(ratio)
    vf = ffmpeg_filter(width, height)
    out.parent.mkdir(parents=True, exist_ok=True)

    if is_image(source):
        cmd = [
            str(FFMPEG),
            "-y",
            "-loop",
            "1",
            "-t",
            "3",
            "-i",
                cmd_path(source),
            "-f",
            "lavfi",
            "-t",
            "3",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-vf",
            vf,
            "-shortest",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-ar",
            "44100",
            "-ac",
            "2",
            cmd_path(out),
        ]
        run_cmd(cmd)
        return

    if has_audio(source):
        cmd = [
            str(FFMPEG),
            "-y",
            "-i",
            cmd_path(source),
            "-vf",
            vf,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            cmd_path(out),
        ]
    else:
        cmd = [
            str(FFMPEG),
            "-y",
            "-i",
            cmd_path(source),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-vf",
            vf,
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
            "-shortest",
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            cmd_path(out),
        ]
    run_cmd(cmd)


def render_mix_segment(background: Path, talking: Path, out: Path, ratio: str) -> None:
    width, height = ratio_size(ratio)
    duration = media_duration(talking)
    duration = max(0.3, duration)
    pip_size = max(170, int(min(width, height) * 0.30))
    if pip_size % 2:
        pip_size += 1
    pip_scale = pip_size + max(70, int(pip_size * 0.48))
    if pip_scale % 2:
        pip_scale += 1
    margin = max(28, int(width * 0.075))
    pip_top = max(96, int(height * 0.22))
    bg_filter = ffmpeg_cover_filter(width, height)
    face_crop = detect_face_crop(talking)
    if face_crop:
        pip_source_filter = (
            f"[1:v]crop={face_crop['size']}:{face_crop['size']}:{face_crop['x']}:{face_crop['y']},"
            f"scale={pip_size}:{pip_size}:flags=lanczos,setsar=1,format=rgb24,"
        )
    else:
        pip_source_filter = (
            f"[1:v]scale={pip_scale}:{pip_scale}:force_original_aspect_ratio=increase,"
            f"crop={pip_size}:{pip_size}:(iw-ow)/2:(ih-oh)*0.20,setsar=1,format=rgb24,"
        )
    circle_mask = (
        f"{pip_source_filter}"
        "format=rgba,"
        "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
        "a='if(lte((X-W/2)*(X-W/2)+(Y-H/2)*(Y-H/2),(W/2)*(W/2)),255,0)'[pip]"
    )
    filter_complex = (
        f"[0:v]{bg_filter}[bg];"
        f"{circle_mask};"
        f"[bg][pip]overlay=W-w-{margin}:{pip_top}:format=auto,format=yuv420p[v]"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [str(FFMPEG), "-y"]
    if is_image(background):
        cmd.extend(["-loop", "1", "-t", f"{duration:.3f}", "-i", cmd_path(background)])
    else:
        cmd.extend(["-stream_loop", "-1", "-i", cmd_path(background)])
    cmd.extend(["-i", cmd_path(talking)])

    audio_index = "1:a:0"
    if not has_audio(talking):
        cmd.extend(["-f", "lavfi", "-t", f"{duration:.3f}", "-i", "anullsrc=channel_layout=stereo:sample_rate=44100"])
        audio_index = "2:a:0"

    cmd.extend(
        [
            "-t",
            f"{duration:.3f}",
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            audio_index,
            "-r",
            "30",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-c:a",
            "aac",
            "-ar",
            "44100",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            cmd_path(out),
        ]
    )
    run_cmd(cmd)


def concat_segments(segments: list[Path], output: Path) -> None:
    list_file = segments[0].parent / f"{output.stem}.concat.txt"
    lines = [f"file '{segment.name}'" for segment in segments]
    list_file.write_text("\n".join(lines), encoding="utf-8")
    cmd = [
        str(FFMPEG),
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        cmd_path(list_file),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        cmd_path(output),
    ]
    run_cmd(cmd)


def nearest_talking_asset(clips: list[dict], current_index: int, manifest: dict) -> dict | None:
    for index in range(current_index - 1, -1, -1):
        clip = clips[index]
        if clip.get("lane") == "talking":
            asset = manifest.get(clip.get("assetId"))
            if asset:
                return asset
    for index in range(current_index + 1, len(clips)):
        clip = clips[index]
        if clip.get("lane") == "talking":
            asset = manifest.get(clip.get("assetId"))
            if asset:
                return asset
    return None


def next_talking_asset(clips: list[dict], current_index: int, manifest: dict) -> tuple[int, dict] | None:
    next_index = current_index + 1
    if next_index >= len(clips):
        return None
    next_clip = clips[next_index]
    if next_clip.get("lane") != "talking":
        return None
    asset = manifest.get(next_clip.get("assetId"))
    if not asset:
        return None
    return next_index, asset


def render_tracks(payload: dict) -> list[dict]:
    manifest = load_manifest()
    tracks = payload.get("tracks") or []
    ratio = payload.get("ratio") or "9:16 竖屏"
    style_id = payload.get("styleId") or "simple"
    name_rule = payload.get("nameRule") or "视频_{序号}"
    job_id = time.strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:6]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    outputs: list[dict] = []

    log_event("generate_start", {"jobId": job_id, "trackCount": len(tracks), "ratio": ratio})

    for track_index, track in enumerate(tracks, start=1):
        clips = track.get("clips") or []
        if not clips:
            log_event("skip_empty_track", {"trackIndex": track_index})
            continue

        segments: list[Path] = []
        clip_index = 0
        segment_index = 1
        while clip_index < len(clips):
            clip = clips[clip_index]
            asset = manifest.get(clip.get("assetId"))
            if not asset:
                raise RuntimeError(f"轨道 {track_index} 的素材不存在：{clip.get('assetId')}")
            source = Path(asset["path"])
            if not source.exists():
                raise RuntimeError(f"素材文件不存在：{source}")
            segment = job_dir / f"track_{track_index:02d}_clip_{segment_index:02d}.mp4"
            if style_id == "mix" and clip.get("lane") == "environment":
                next_talking = next_talking_asset(clips, clip_index, manifest)
                if next_talking and Path(next_talking[1]["path"]).exists():
                    next_index, talking_asset = next_talking
                    render_mix_segment(source, Path(talking_asset["path"]), segment, ratio)
                    log_event(
                        "mix_pair_segment",
                        {
                            "trackIndex": track_index,
                            "environmentClipIndex": clip_index + 1,
                            "talkingClipIndex": next_index + 1,
                            "environment": asset.get("name"),
                            "talking": talking_asset.get("name"),
                        },
                    )
                    clip_index = next_index + 1
                else:
                    render_segment(source, segment, ratio)
                    clip_index += 1
            else:
                render_segment(source, segment, ratio)
                clip_index += 1
            segments.append(segment)
            segment_index += 1

        number = f"{track_index:02d}"
        filename = safe_name(name_rule.replace("{序号}", number)) if "{序号}" in name_rule else safe_name(f"{name_rule}_{number}.mp4")
        if not filename.lower().endswith(".mp4"):
            filename += ".mp4"
        output = OUTPUT_DIR / filename
        concat_segments(segments, output)
        duration = media_duration(output)
        outputs.append(
            {
                "name": output.name,
                "path": str(output),
                "url": f"/outputs/{output.name}",
                "duration": round(duration, 2),
                "trackIndex": track_index,
            }
        )

    report = {
        "jobId": job_id,
        "createdAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "outputs": outputs,
        "request": payload,
    }
    (OUTPUT_DIR / f"生成报告_{job_id}.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    log_event("generate_done", {"jobId": job_id, "outputCount": len(outputs)})
    return outputs


class WorkbenchHandler(BaseHTTPRequestHandler):
    server_version = "VideoWorkbench/0.1"

    def log_message(self, fmt: str, *args) -> None:
        log_event("http", {"message": fmt % args})

    def send_json(self, data: dict, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        if path == "/api/health":
            self.send_json({"ok": True, "ffmpeg": str(FFMPEG)})
            return
        if path == "/api/debug_report":
            self.send_json({"ok": True, "report": debug_report()})
            return
        if path.startswith("/uploads/"):
            self.serve_file(UPLOAD_DIR / path.removeprefix("/uploads/"))
            return
        if path.startswith("/outputs/"):
            self.serve_file(OUTPUT_DIR / path.removeprefix("/outputs/"))
            return
        relative = "index.html" if path in {"/", ""} else path.lstrip("/")
        self.serve_file(STATIC_ROOT / relative)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/upload":
            self.handle_upload(parsed.query)
            return
        if parsed.path == "/api/generate":
            self.handle_generate()
            return
        if parsed.path == "/api/delete_asset":
            self.handle_delete_asset()
            return
        if parsed.path == "/api/choose_directory":
            self.handle_choose_directory()
            return
        if parsed.path == "/api/save_outputs":
            self.handle_save_outputs()
            return
        if parsed.path == "/api/reset":
            self.handle_reset()
            return
        self.send_json({"ok": False, "error": "unknown endpoint"}, 404)

    def serve_file(self, path: Path) -> None:
        try:
            resolved = path.resolve()
            allowed_roots = [STATIC_ROOT.resolve(), UPLOAD_DIR.resolve(), OUTPUT_DIR.resolve()]
            if not any(resolved == root or root in resolved.parents for root in allowed_roots):
                self.send_error(403)
                return
            if not resolved.exists() or not resolved.is_file():
                self.send_error(404)
                return
            mime = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
            body = resolved.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except Exception as exc:
            log_event("serve_file_error", {"path": str(path), "error": str(exc)})
            self.send_error(500)

    def handle_upload(self, query: str) -> None:
        try:
            lane = parse_qs(query).get("lane", [""])[0]
            if lane not in {"talking", "environment"}:
                self.send_json({"ok": False, "error": "lane 参数错误"}, 400)
                return
            form = cgi.FieldStorage(
                fp=self.rfile,
                headers=self.headers,
                environ={
                    "REQUEST_METHOD": "POST",
                    "CONTENT_TYPE": self.headers.get("Content-Type", ""),
                    "CONTENT_LENGTH": self.headers.get("Content-Length", "0"),
                },
            )
            fields = form["files"] if "files" in form else []
            if not isinstance(fields, list):
                fields = [fields]
            if len(fields) > MAX_BATCH_FILES:
                fields = fields[:MAX_BATCH_FILES]
            saved = []
            with MANIFEST_LOCK:
                manifest = load_manifest()
                remaining = MAX_TOTAL_ASSETS - len(manifest)
                fields = fields[: max(0, remaining)]
                for field in fields:
                    if not getattr(field, "filename", ""):
                        continue
                    asset_id = uuid.uuid4().hex
                    suffix = Path(field.filename).suffix.lower() or ".mp4"
                    suffix = re.sub(r"[^a-z0-9.]+", "", suffix) or ".mp4"
                    filename = f"{asset_id}{suffix}"
                    target = UPLOAD_DIR / lane / filename
                    with target.open("wb") as f:
                        shutil.copyfileobj(field.file, f)
                    duration = media_duration(target)
                    mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
                    asset = {
                        "id": asset_id,
                        "lane": lane,
                        "name": field.filename,
                        "filename": filename,
                        "path": str(target),
                        "url": f"/uploads/{lane}/{filename}",
                        "size": target.stat().st_size,
                        "type": mime,
                        "kind": "图片" if is_image(target) else "视频",
                        "duration": f"{duration:.1f}s",
                    }
                    manifest[asset_id] = asset
                    saved.append(asset)
                save_manifest(manifest)
            log_event("upload", {"lane": lane, "count": len(saved)})
            self.send_json({"ok": True, "assets": saved, "limits": {"maxBatchFiles": MAX_BATCH_FILES, "maxTotalAssets": MAX_TOTAL_ASSETS}})
        except Exception as exc:
            log_event("upload_error", {"error": str(exc)})
            self.send_json({"ok": False, "error": str(exc)}, 500)

    def handle_generate(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            outputs = render_tracks(payload)
            self.send_json({"ok": True, "outputs": outputs})
        except Exception as exc:
            log_event("generate_error", {"error": str(exc)})
            self.send_json({"ok": False, "error": str(exc), "debug": debug_report()}, 500)

    def read_json_body(self) -> dict:
        length = int(self.headers.get("Content-Length", "0"))
        return json.loads(self.rfile.read(length).decode("utf-8")) if length else {}

    def handle_delete_asset(self) -> None:
        try:
            payload = self.read_json_body()
            asset_id = payload.get("assetId")
            if not asset_id:
                self.send_json({"ok": False, "error": "缺少 assetId"}, 400)
                return
            with MANIFEST_LOCK:
                manifest = load_manifest()
                asset = manifest.pop(asset_id, None)
                if not asset:
                    self.send_json({"ok": True, "deleted": False})
                    return
                save_manifest(manifest)
            path = Path(asset["path"])
            if path.exists() and path.is_file():
                path.unlink()
            log_event("delete_asset", {"assetId": asset_id, "name": asset.get("name")})
            self.send_json({"ok": True, "deleted": True})
        except Exception as exc:
            log_event("delete_asset_error", {"error": str(exc)})
            self.send_json({"ok": False, "error": str(exc)}, 500)

    def handle_choose_directory(self) -> None:
        try:
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            directory = filedialog.askdirectory(
                title="选择视频保存目录",
                initialdir=str(DOWNLOAD_DIR),
                mustexist=False,
            )
            root.destroy()
            log_event("choose_directory", {"directory": directory})
            self.send_json({"ok": True, "directory": directory})
        except Exception as exc:
            log_event("choose_directory_error", {"error": str(exc)})
            self.send_json({"ok": False, "error": str(exc)}, 500)

    def handle_save_outputs(self) -> None:
        try:
            payload = self.read_json_body()
            directory = str(payload.get("directory") or "").strip().strip('"')
            outputs = payload.get("outputs") or []
            if not directory:
                self.send_json({"ok": False, "error": "请先填写保存目录"}, 400)
                return
            target_dir = Path(directory).expanduser()
            target_dir.mkdir(parents=True, exist_ok=True)
            saved = []
            for output in outputs:
                name = output.get("name")
                if not name:
                    continue
                source = (OUTPUT_DIR / Path(name).name).resolve()
                if not source.exists():
                    continue
                target = target_dir / source.name
                shutil.copy2(source, target)
                saved.append({"name": target.name, "path": str(target)})
            log_event("save_outputs", {"directory": str(target_dir), "count": len(saved)})
            self.send_json({"ok": True, "directory": str(target_dir), "saved": saved})
        except Exception as exc:
            log_event("save_outputs_error", {"error": str(exc)})
            self.send_json({"ok": False, "error": str(exc)}, 500)

    def handle_reset(self) -> None:
        try:
            with MANIFEST_LOCK:
                for path in [UPLOAD_DIR, OUTPUT_DIR, WORK_DIR]:
                    if path.exists():
                        shutil.rmtree(path)
                ensure_dirs()
                save_manifest({})
            log_event("reset", {})
            self.send_json({"ok": True})
        except Exception as exc:
            log_event("reset_error", {"error": str(exc)})
            self.send_json({"ok": False, "error": str(exc)}, 500)


def main() -> None:
    ensure_dirs()
    port = int(os.environ.get("VIDEO_WORKBENCH_PORT", "8787"))
    server = ThreadingHTTPServer(("127.0.0.1", port), WorkbenchHandler)
    log_event("server_start", {"port": port, "root": str(ROOT), "staticRoot": str(STATIC_ROOT), "ffmpeg": str(FFMPEG)})
    print(f"Video workbench running: http://127.0.0.1:{port}/")
    server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
