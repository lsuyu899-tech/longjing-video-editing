from __future__ import annotations

import cgi
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


ROOT = Path(__file__).resolve().parent
UPLOAD_DIR = ROOT / "uploads"
OUTPUT_DIR = ROOT / "outputs"
WORK_DIR = ROOT / "work"
LOG_DIR = ROOT / "logs"
MANIFEST_FILE = ROOT / "asset_manifest.json"
LOG_FILE = LOG_DIR / "workbench.log"
MAX_BATCH_FILES = 300
MAX_TOTAL_ASSETS = 1000
MANIFEST_LOCK = threading.Lock()


def find_ffmpeg() -> tuple[Path, Path]:
    workspace = ROOT.parents[1]
    candidates = [
        workspace / "OpenMontage" / ".local" / "ffmpeg" / "ffmpeg-8.1.1-essentials_build" / "bin" / "ffmpeg.exe",
        workspace / "OpenMontage" / "remotion-composer" / "node_modules" / "@remotion" / "compositor-win32-x64-msvc" / "ffmpeg.exe",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate, candidate.with_name("ffprobe.exe")
    raise RuntimeError("找不到 ffmpeg.exe")


FFMPEG, FFPROBE = find_ffmpeg()


def ensure_dirs() -> None:
    for path in [UPLOAD_DIR / "talking", UPLOAD_DIR / "environment", OUTPUT_DIR, WORK_DIR, LOG_DIR]:
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


def safe_name(name: str) -> str:
    stem = Path(name).stem
    suffix = Path(name).suffix.lower()
    stem = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", stem, flags=re.UNICODE).strip("._")
    return f"{stem or 'asset'}{suffix or '.mp4'}"


def media_duration(path: Path) -> float:
    try:
        result = subprocess.run(
            [
                str(FFPROBE),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return max(0.1, float(result.stdout.strip()))
    except Exception:
        return 3.0


def has_audio(path: Path) -> bool:
    try:
        result = subprocess.run(
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
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())
    except Exception:
        return False


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


def is_image(path: Path) -> bool:
    return path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}


def run_cmd(cmd: list[str]) -> None:
    log_event("ffmpeg_command", {"cmd": cmd})
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        log_event("ffmpeg_error", {"stderr": result.stderr[-4000:], "stdout": result.stdout[-1000:]})
        raise RuntimeError(result.stderr[-1200:] or "ffmpeg 执行失败")


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
            str(source),
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
            str(out),
        ]
        run_cmd(cmd)
        return

    if has_audio(source):
        cmd = [
            str(FFMPEG),
            "-y",
            "-i",
            str(source),
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
            str(out),
        ]
    else:
        cmd = [
            str(FFMPEG),
            "-y",
            "-i",
            str(source),
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
            str(out),
        ]
    run_cmd(cmd)


def render_mix_segment(background: Path, talking: Path, out: Path, ratio: str) -> None:
    width, height = ratio_size(ratio)
    duration = min(3.0 if is_image(background) else media_duration(background), media_duration(talking))
    duration = max(0.3, duration)
    pip_size = max(160, int(min(width, height) * 0.28))
    if pip_size % 2:
        pip_size += 1
    margin = max(24, int(width * 0.05))
    bg_filter = ffmpeg_filter(width, height)
    circle_mask = (
        f"[1:v]scale={pip_size}:{pip_size}:force_original_aspect_ratio=increase,"
        f"crop={pip_size}:{pip_size},format=rgba,"
        "geq=r='r(X,Y)':g='g(X,Y)':b='b(X,Y)':"
        "a='if(lte((X-W/2)*(X-W/2)+(Y-H/2)*(Y-H/2),(W/2)*(W/2)),255,0)'[pip]"
    )
    filter_complex = (
        f"[0:v]{bg_filter}[bg];"
        f"{circle_mask};"
        f"[bg][pip]overlay=W-w-{margin}:{margin}:format=auto,format=yuv420p[v]"
    )
    out.parent.mkdir(parents=True, exist_ok=True)

    cmd = [str(FFMPEG), "-y"]
    if is_image(background):
        cmd.extend(["-loop", "1", "-t", f"{duration:.3f}", "-i", str(background)])
    else:
        cmd.extend(["-i", str(background)])
    cmd.extend(["-i", str(talking)])

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
            str(out),
        ]
    )
    run_cmd(cmd)


def concat_segments(segments: list[Path], output: Path) -> None:
    list_file = output.with_suffix(".concat.txt")
    lines = [f"file '{str(segment).replace(chr(92), '/')}'" for segment in segments]
    list_file.write_text("\n".join(lines), encoding="utf-8")
    cmd = [
        str(FFMPEG),
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(output),
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
        for clip_index, clip in enumerate(clips, start=1):
            asset = manifest.get(clip.get("assetId"))
            if not asset:
                raise RuntimeError(f"轨道 {track_index} 的素材不存在：{clip.get('assetId')}")
            source = Path(asset["path"])
            if not source.exists():
                raise RuntimeError(f"素材文件不存在：{source}")
            segment = job_dir / f"track_{track_index:02d}_clip_{clip_index:02d}.mp4"
            if style_id == "mix" and clip.get("lane") == "environment":
                talking_asset = nearest_talking_asset(clips, clip_index - 1, manifest)
                if talking_asset and Path(talking_asset["path"]).exists():
                    render_mix_segment(source, Path(talking_asset["path"]), segment, ratio)
                else:
                    render_segment(source, segment, ratio)
            else:
                render_segment(source, segment, ratio)
            segments.append(segment)

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
        if path.startswith("/uploads/"):
            self.serve_file(UPLOAD_DIR / path.removeprefix("/uploads/"))
            return
        if path.startswith("/outputs/"):
            self.serve_file(OUTPUT_DIR / path.removeprefix("/outputs/"))
            return
        relative = "index.html" if path in {"/", ""} else path.lstrip("/")
        self.serve_file(ROOT / relative)

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
            allowed_roots = [ROOT.resolve(), UPLOAD_DIR.resolve(), OUTPUT_DIR.resolve()]
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
                    filename = f"{asset_id}_{safe_name(field.filename)}"
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
            self.send_json({"ok": False, "error": str(exc)}, 500)

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
                initialdir=str(ROOT / "downloads"),
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
    log_event("server_start", {"port": port, "root": str(ROOT), "ffmpeg": str(FFMPEG)})
    print(f"Video workbench running: http://127.0.0.1:{port}/")
    server.serve_forever()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
