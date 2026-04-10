"""
app.py — 가사 영상 만들기 웹 서버

실행:
    pip install flask yt-dlp
    python app.py
그 다음 브라우저에서 http://localhost:5000 접속
"""

import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_file

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 4GB

UPLOAD_DIR = Path("uploads")
OUTPUT_DIR = Path("outputs")
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)

# job_id -> {"status": "processing"|"done"|"error", "progress": 0-100, ...}
progress_store: dict = {}


# ── SRT 파싱 ─────────────────────────────────────────────────────────────────
def parse_srt(srt_path: str) -> list[dict]:
    with open(srt_path, "r", encoding="utf-8") as f:
        content = f.read()

    blocks = re.split(r"\n\n+", content.strip())
    lines = []

    for block in blocks:
        parts = block.strip().split("\n")
        timing_line = None
        text_parts = []

        for i, part in enumerate(parts):
            if "-->" in part:
                timing_line = part
                text_parts = parts[i + 1 :]
                break

        if not timing_line:
            continue

        # 00:00:22,349 --> 00:00:27,950
        m = re.match(
            r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)",
            timing_line,
        )
        if not m:
            continue

        h1, m1, s1, ms1, h2, m2, s2, ms2 = map(int, m.groups())
        start_ms = h1 * 3600000 + m1 * 60000 + s1 * 1000 + ms1
        end_ms = h2 * 3600000 + m2 * 60000 + s2 * 1000 + ms2

        # HTML 태그 제거 후 텍스트 합치기
        text = " ".join(text_parts)
        text = re.sub(r"<[^>]+>", "", text).strip()
        if not text:
            continue

        lines.append(
            {
                "id": str(len(lines) + 1),
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": text,
            }
        )

    return lines


# ── 라우트 ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/parse-sync", methods=["POST"])
def parse_sync():
    if "file" not in request.files:
        return jsonify({"error": "파일이 필요합니다"}), 400

    f = request.files["file"]
    filename = f.filename or ""
    content = f.read().decode("utf-8", errors="ignore")

    # SRT 형식 감지 (-->)
    if "-->" in content:
        tmp = UPLOAD_DIR / f"sync_{uuid.uuid4().hex[:8]}.srt"
        tmp.write_text(content, encoding="utf-8")
        try:
            lines = parse_srt(str(tmp))
        finally:
            tmp.unlink(missing_ok=True)
    else:
        # CSV 형식 (기존 manual_sync.txt)
        import io, csv as csv_mod
        lines = []
        reader = csv_mod.reader(io.StringIO(content))
        header_skipped = False
        for row in reader:
            if not header_skipped:
                header_skipped = True
                if row and "시작" in (row[1] if len(row) > 1 else ""):
                    continue
            if not row or len(row) < 4:
                continue
            try:
                from karaoke import parse_time_str
                start_ms = parse_time_str(row[1])
                end_ms = parse_time_str(row[2])
                text = row[3].strip().replace("\n", " ")
                if text:
                    lines.append({"id": str(len(lines)+1), "start_ms": start_ms, "end_ms": end_ms, "text": text})
            except Exception:
                continue

    if not lines:
        return jsonify({"error": "파일을 파싱하지 못했습니다. CSV 또는 SRT 형식인지 확인하세요."}), 400

    return jsonify({"lines": lines})


@app.route("/extract", methods=["POST"])
def extract():
    data = request.get_json()
    url = (data or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "URL이 필요합니다"}), 400

    tmp_dir = UPLOAD_DIR / f"sub_{uuid.uuid4().hex[:8]}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    try:
        # 한국어 자막 먼저 시도, 없으면 전체 언어
        for lang_args in [["--sub-langs", "ko"], []]:
            cmd = [
                "yt-dlp",
                "--write-auto-subs",
                *lang_args,
                "--convert-subs", "srt",
                "--skip-download",
                "--output", str(tmp_dir / "subtitle"),
                url,
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=90)

            srt_files = list(tmp_dir.glob("*.srt"))
            if srt_files:
                break

        if not srt_files:
            stderr = result.stderr[-500:] if result.stderr else ""
            return jsonify({"error": f"자막을 찾을 수 없습니다. 자동 자막이 없는 영상일 수 있어요.\n{stderr}"}), 404

        lines = parse_srt(str(srt_files[0]))
        if not lines:
            return jsonify({"error": "자막 파싱에 실패했습니다"}), 500

        return jsonify({"lines": lines})

    except subprocess.TimeoutExpired:
        return jsonify({"error": "자막 추출 시간 초과 (90초)"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        shutil.rmtree(str(tmp_dir), ignore_errors=True)


@app.route("/generate", methods=["POST"])
def generate():
    if "video" not in request.files:
        return jsonify({"error": "영상 파일이 필요합니다"}), 400

    video_file = request.files["video"]
    lines_json = request.form.get("lines")
    if not lines_json:
        return jsonify({"error": "가사 데이터가 필요합니다"}), 400

    lines = json.loads(lines_json)
    if not lines:
        return jsonify({"error": "가사가 비어 있습니다"}), 400

    job_id = uuid.uuid4().hex[:8]
    video_path = UPLOAD_DIR / f"{job_id}_{video_file.filename}"
    video_file.save(str(video_path))

    output_filename = f"{job_id}_karaoke.mp4"
    output_path = OUTPUT_DIR / output_filename

    progress_store[job_id] = {"status": "processing", "progress": 0, "message": "시작 중..."}

    def run_job():
        try:
            from karaoke import build_karaoke_video

            def on_progress(ratio: float, message: str = ""):
                progress_store[job_id]["progress"] = int(ratio * 100)
                if message:
                    progress_store[job_id]["message"] = message

            build_karaoke_video(str(video_path), lines, str(output_path), progress_callback=on_progress)
            progress_store[job_id] = {
                "status": "done",
                "progress": 100,
                "message": "완료!",
                "filename": output_filename,
            }
        except Exception as e:
            progress_store[job_id] = {"status": "error", "error": str(e), "progress": 0}
        finally:
            # 업로드된 원본 영상 삭제
            video_path.unlink(missing_ok=True)

    threading.Thread(target=run_job, daemon=True).start()
    return jsonify({"job_id": job_id})


@app.route("/progress/<job_id>")
def progress(job_id):
    return jsonify(progress_store.get(job_id, {"status": "not_found"}))


@app.route("/download/<filename>")
def download(filename):
    # 경로 탈출 방지
    safe_path = (OUTPUT_DIR / filename).resolve()
    if not str(safe_path).startswith(str(OUTPUT_DIR.resolve())):
        return jsonify({"error": "잘못된 경로"}), 400
    if not safe_path.exists():
        return jsonify({"error": "파일을 찾을 수 없습니다"}), 404
    return send_file(str(safe_path), as_attachment=True)


if __name__ == "__main__":
    import webbrowser
    threading.Timer(1.0, lambda: webbrowser.open("http://127.0.0.1:5000")).start()
    app.run(debug=False, host="127.0.0.1", port=5000)
