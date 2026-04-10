"""
karaoke.py — 로컬 영상과 수동 가사 타이밍 파일(CSV 형태)을 이용하여 노래방 스타일 가사 하이라이트를 합성합니다.

사용법:
    python karaoke.py --video <로컬영상.mp4> --manual-sync manual_sync.txt
    python karaoke.py --video <로컬영상.mp4> --manual-sync manual_sync.txt --output output.mp4
"""

import os
import re
import argparse
import subprocess
import platform
import csv
import json
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

# ── 전역 상수 ─────────────────────────────────────────────────────────────────
INTERLUDE_GAP_MS = 8000   # 이 시간 이상 공백이면 간주 판정

# ── 한국어 폰트 경로 ─────────────────────────────────────────────────────────
def _get_korean_font(size: int) -> ImageFont.FreeTypeFont:
    candidates = {
        "Darwin": [
            "/System/Library/Fonts/AppleSDGothicNeo.ttc",
            "/System/Library/Fonts/Supplemental/AppleGothic.ttf",
        ],
        "Linux": [
            "/usr/share/fonts/truetype/nanum/NanumGothicBold.ttf",
            "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
        ],
    }
    for path in candidates.get(platform.system(), []):
        if Path(path).exists():
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return ImageFont.load_default()

# ── 시간 문자열 파싱 ─────────────────────────────────────────────────────────
def parse_time_str(time_str: str) -> int:
    """
    형식: HH:MM:SS;FF (또는 HH:MM:SS,FF / HH:MM:SS.FF)
    예: 00:00:22;00 -> 22000 ms, 00:01:00;09 -> 60300 ms (초당 30프레임 가정: 09/30초 = 300ms)
    """
    time_str = time_str.strip()
    match = re.match(r"(\d+):(\d+):(\d+)[;,.](\d+)", time_str)
    if not match:
        raise ValueError(f"시간 형식이 잘못되었습니다: {time_str}")
    
    h, m, s, f = map(int, match.groups())
    
    # 프레임을 밀리초로 변환 (30fps 가정: 1프레임 = 약 33.3ms)
    ms_from_frame = int((f / 30.0) * 1000)
    
    total_ms = (h * 3600 * 1000) + (m * 60 * 1000) + (s * 1000) + ms_from_frame
    return total_ms

# ── CSV 수동 동기화 파일 파싱 ──────────────────────────────────────────────────
def parse_manual_sync(file_path: str) -> list[dict]:
    """
    수기 작성 파일 파싱
    형식: 번호,시작 시간,종료 시간,자막 텍스트
    반환: [{"id": int, "start_ms": int, "end_ms": int, "text": str}, ...]
    """
    lines = []
    with open(file_path, "r", encoding="utf-8") as f:
        reader = csv.reader(f)
        header_skipped = False
        for row in reader:
            if not header_skipped:
                # 첫 줄(헤더) 무시 (번호,시작시간... 등)
                if row and "시작" in (row[1] if len(row) > 1 else ""):
                    header_skipped = True
                    continue
                else:
                    header_skipped = True # 헤더가 없다면 그냥 처리
            
            if not row or len(row) < 4:
                continue
            
            try:
                # 번호가 숫자 형태가 아닐 수 있으므로 문자열로 저장
                line_id = row[0]
                start_ms = parse_time_str(row[1])
                end_ms = parse_time_str(row[2])
                text = row[3].strip()
                
                # 가사 내에 공백이나 여러 줄이 포함되어 있다면 정리
                text = text.replace('\n', ' ').strip()
                if not text:
                    continue
                    
                lines.append({
                    "id": line_id,
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "text": text
                })
            except Exception as e:
                print(f"⚠️ 경고: 라인 파싱 실패 - {row} ({e})")
                
    # 시작 시간 순으로 정렬
    lines.sort(key=lambda x: x["start_ms"])
    return lines

# ── 1줄 UI 및 디스플레이 상태 관리 (단순화) ──────────────────────────────────
def calculate_one_line_display(lines: list[dict]) -> list[dict]:
    """
    모든 프레임에 대해 딱 그 시간에 해당하는 1줄의 가사만 계산합니다.
    """
    # 1. 간주(인터루드) 계산
    interludes = []
    
    # 인트로 (처음부터 첫 가사까지)
    if lines and lines[0]["start_ms"] > INTERLUDE_GAP_MS / 2:
        interludes.append({
            "start_ms": 0,
            "end_ms": lines[0]["start_ms"],
            "is_intro": True
        })
        
    for i in range(len(lines) - 1):
        gap = lines[i+1]["start_ms"] - lines[i]["end_ms"]
        if gap >= INTERLUDE_GAP_MS:
            interludes.append({
                "start_ms": lines[i]["end_ms"] + 1000, # 노래 끝나고 1초 뒤 간주 표시
                "end_ms": lines[i+1]["start_ms"],
                "is_intro": False
            })
            
    # 2. 1줄 디스플레이 로직 (타임스탬프 완벽 일치)
    display_lines = []
    
    for i, line in enumerate(lines):
        # 화면 시작 시간과 종료 시간: 사용자가 적어준 수기 텍스트 파일(CSV) 시간과 100% 동일하게 맞춥니다.
        # 앞뒤로 여운(1초 전, 1초 후)을 주는 쓸데없는 친절함은 오히려 가사 겹침을 유발하므로 완전히 제거합니다.
        display_start = line["start_ms"]
        display_end = line["end_ms"]

        # 100% 사용자의 시간에 맞추기 때문에 겹침 방지나 간주 계산과 부딪히지 않고 1줄씩 깔끔하게 등장/퇴장합니다.
        display_lines.append({
            "line": line,
            "position": "center",
            "display_start": display_start,
            "display_end": display_end
        })
        
    return display_lines, interludes

# ── 텍스트 렌더링 (배경 제거, 아웃라인 두껍게) ──────────────────────────────────
def draw_text_with_outline(draw, text, font, x, y, fill_color, outline_color=(0, 0, 0, 255), outline_width=3):
    # 두꺼운 아웃라인(Stroke) 그리기
    for dx in range(-outline_width, outline_width + 1):
        for dy in range(-outline_width, outline_width + 1):
            if dx*dx + dy*dy > outline_width*outline_width:
                continue
            draw.text((x + dx, y + dy), text, font=font, fill=outline_color)
    draw.text((x, y), text, font=font, fill=fill_color)

def make_subtitle_frame(
    width: int,
    height: int,
    current_ms: int,
    display_lines: list[dict],
    interludes: list[dict],
    font_size: int = 56,
) -> Image.Image:
    """
    현재 시간에 맞는 노래방 자막 프레임(RGBA)을 생성합니다. (1줄 버전)
    와이프(Wipe) 방식: 흰 글씨 위에 노란색이 왼쪽에서 오른쪽으로 덮어집니다.
    반투명한 검정 박스 배경은 모두 제거되었습니다.
    """
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = _get_korean_font(font_size)
    
    # 1. 상태 파악 (간주/전주 중인지)
    is_interlude = False
    interlude_text = ""
    for itl in interludes:
        if itl["start_ms"] <= current_ms < itl["end_ms"]:
            is_interlude = True
            interlude_text = "♪  전주중  ♪" if itl.get("is_intro") else "♪  간주중  ♪"
            break

    # Y 좌표 설정 (1줄이므로 화면 가장 아래쪽 1개만 사용)
    bottom_y = height - font_size - 60

    # 2. 화면 그리기
    if is_interlude:
        tw = draw.textlength(interlude_text, font=font)
        x = (width - tw) / 2
        y = bottom_y
        
        # 배경 없음, 글씨 테두리만 진하게
        draw_text_with_outline(draw, interlude_text, font, x, y, fill_color=(180, 220, 255, 255), outline_width=4)
        return img

    # 현재 화면에 보여야 할 가사들 필터링 (1줄)
    visible_lines = [dl for dl in display_lines if dl["display_start"] <= current_ms <= dl["display_end"]]

    for dl in visible_lines:
        line_data = dl["line"]
        text = line_data["text"]
        
        y = bottom_y
        tw = draw.textlength(text, font=font)
        x = (width - tw) / 2
        
        # ── Wipe(와이프) 효과 마스크 계산 ──
        t_start = line_data["start_ms"]
        t_end = line_data["end_ms"]
        
        if current_ms < t_start:
            ratio = 0.0
        elif current_ms >= t_end:
            ratio = 1.0
        else:
            ratio = (current_ms - t_start) / max(t_end - t_start, 1)

        fill_x = x + tw * ratio

        # 레이어 1: 흰색 기본 텍스트 (안 부른 부분) - 배경 박스 없이 아웃라인만 두껍게
        white_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        wd = ImageDraw.Draw(white_layer)
        draw_text_with_outline(wd, text, font, x, y, fill_color=(255, 255, 255, 255), outline_width=4)

        # 레이어 2: 노란색 진행 텍스트 (부른 부분)
        yellow_layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        yd = ImageDraw.Draw(yellow_layer)
        draw_text_with_outline(yd, text, font, x, y, fill_color=(255, 220, 0, 255), outline_width=4)

        # 마스크 (fill_x를 기준으로 왼쪽은 투명(255), 오른쪽은 불투명(0))
        mask = Image.new("L", (width, height), 0)
        ImageDraw.Draw(mask).rectangle([0, 0, int(fill_x), height], fill=255)

        # 합성: 바탕(img) 위에 -> 흰 글씨 올리고 -> 그 위에 마스크 씌운 노란 글씨 올림
        img = Image.alpha_composite(img, white_layer)
        
        # 노란 글씨는 마스크에 의해 왼쪽부터 채워짐
        yellow_composite = Image.composite(yellow_layer, Image.new("RGBA", img.size, (0,0,0,0)), mask)
        img = Image.alpha_composite(img, yellow_composite)

    return img


# ── 비디오 정보 ──────────────────────────────────────────────────────────────
def get_video_info(video_path: str) -> tuple[float, int, int, float]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-show_entries", "format=duration",
            "-of", "json",
            video_path,
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=True,
    )
    info = json.loads(result.stdout)
    fmt    = info.get("format", {})
    stream = info.get("streams", [{}])[0]

    duration = float(fmt.get("duration", 0))
    width    = int(stream.get("width", 1920))
    height   = int(stream.get("height", 1080))

    fps_str = stream.get("r_frame_rate", "30/1")
    num, den = fps_str.split("/")
    fps = float(num) / float(den)

    return duration, width, height, fps


# ── 메인 파이프라인 ───────────────────────────────────────────────────────────
def build_karaoke_video(video_path: str, lines: list[dict], output_path: str, progress_callback=None):
    duration, width, height, fps = get_video_info(video_path)

    display_lines, interludes = calculate_one_line_display(lines)

    # 폰트 사이즈 보정
    font_size = max(24, int(height * 0.045))

    print(f"🎬 영상 정보: {width}x{height} @ {fps:.2f}fps, {duration:.1f}초 (폰트: {font_size}px)")
    print(f"📝 총 {len(lines)}줄의 가사 동기화 데이터 적용")
    print(f"🎸 전주/간주 구간 {len(interludes)}개 감지")

    if progress_callback:
        progress_callback(0.0, "프레임 생성 중...")

    total_frames = int(duration * fps) + 1
    frame_ms_step = 1000 / fps

    # FFmpeg 파이프 프로세스 시작
    # 자막 레이어(RGBA raw)를 stdin으로 스트리밍하고, 원본 영상과 overlay 합성
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        # 원본 영상
        "-i", video_path,
        # 자막 레이어: stdin에서 rawvideo로 받음
        "-f", "rawvideo",
        "-vcodec", "rawvideo",
        "-pix_fmt", "rgba",
        "-s", f"{width}x{height}",
        "-r", str(fps),
        "-i", "pipe:0",
        # overlay 합성
        "-filter_complex", "[0:v][1:v]overlay=0:0",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-c:a", "copy",
        output_path,
    ]

    print(f"🖼  자막 프레임 스트리밍 중 (총 {total_frames}프레임)...")
    proc = subprocess.Popen(
        ffmpeg_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    try:
        for fi in range(total_frames):
            current_ms = fi * frame_ms_step
            frame_img = make_subtitle_frame(
                width, height, current_ms,
                display_lines=display_lines,
                interludes=interludes,
                font_size=font_size,
            )
            proc.stdin.write(frame_img.tobytes())

            if fi % 500 == 0:
                print(f"  {fi}/{total_frames} ({fi/total_frames*100:.1f}%)")
                if progress_callback:
                    progress_callback(fi / total_frames, f"프레임 생성 중... {fi/total_frames*100:.0f}%")

        proc.stdin.close()
        proc.wait()
    except BrokenPipeError:
        stderr = proc.stderr.read().decode(errors="ignore")
        raise RuntimeError(f"FFmpeg 오류:\n{stderr}")

    if proc.returncode != 0:
        stderr = proc.stderr.read().decode(errors="ignore")
        raise RuntimeError(f"FFmpeg 오류 (코드 {proc.returncode}):\n{stderr}")

    if progress_callback:
        progress_callback(1.0, "완료!")
    print(f"✅ 완료! 출력 파일: {output_path}")


# ── CLI 진입점 ────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(
        description="수동 가사 동기화 파일(CSV)을 사용하여 고퀄리티 2줄 노래방 자막 영상을 만듭니다."
    )
    parser.add_argument("--video", required=True, help="사용할 로컬 영상 파일 경로 (.mp4)")
    parser.add_argument("--manual-sync", required=True, help="수기 작성된 CSV 형태의 가사 파일 (.txt)")
    parser.add_argument("--output", "-o", default="", help="출력 파일 경로 (기본: outputs/파일명_karaoke.mp4)")
    args = parser.parse_args()

    out_dir = Path("outputs")
    out_dir.mkdir(exist_ok=True)

    if not Path(args.video).exists():
        print(f"❌ 영상 파일을 찾을 수 없습니다: {args.video}")
        return
        
    if not Path(args.manual_sync).exists():
        print(f"❌ 가사 파일을 찾을 수 없습니다: {args.manual_sync}")
        return

    # 가사 파싱
    lines = parse_manual_sync(args.manual_sync)
    if not lines:
        print("❌ 가사 파일에서 데이터를 추출하지 못했습니다. 형식을 확인하세요.")
        return

    # 출력 경로 결정
    if args.output:
        output_path = args.output
    else:
        stem = Path(args.video).stem
        output_path = str(out_dir / f"{stem}_karaoke.mp4")

    # 비디오 생성
    build_karaoke_video(args.video, lines, output_path)

if __name__ == "__main__":
    main()