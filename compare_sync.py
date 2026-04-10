import re

user_data = """번호,시작 시간,종료 시간,자막 텍스트
1,00:00:22;00,00:00:33;00,나의 시선이 나의 마음을 향할 때자꾸 초라한 모습과 두려움뿐이네
2,00:00:33;13,00:00:43;13,나의 시선을 당신께 돌릴 때비로소 내 맘 평안해
3,00:00:44;15,00:00:48;24,비로소 내 맘 평안해
4,00:00:49;22,00:01:00;09,주를 바라보며 그 맘을 구합니다나의 모든 것 다 품으신 주님의 사랑
5,00:01:01;12,00:01:11;12,나의 생각보다 더 크신 주님의 계획그 앞에 내 삶 드립니다
6,00:01:12;22,00:01:17;25,주님께 내 삶 드립니다
8,00:01:29;08,00:01:39;12,나의 시선이 나의 마음을 향할 때작고 초라한 모습과 두려움뿐이네
9,00:01:40;01,00:01:50;11,나의 시선을 당신께 돌릴 때비로소 내 맘 평안해
10,00:01:51;04,00:01:56;06,비로소 내 맘 평안해
11,00:01:56;06,00:02:06;08,주를 바라보며 그 맘을 구합니다나의 모든 것 다 품으신 주님의 사랑
12,00:02:07;13,00:02:18;08,나의 생각보다 더 크신 주님의 계획그 앞에 내 삶 드립니다"""

import sys
sys.path.insert(0, ".")
from karaoke import parse_words, remap_words_with_lyrics

words = parse_words("outputs/test_sub.ko.json3")
remapped, lyric_lines = remap_words_with_lyrics(words, "lyrics_seeGod.txt")

print("| 가사 | 수기 타이밍 | 유튜브 자동 자막 타이밍 | 차이 |")
print("|---|---|---|---|")

user_lines = user_data.strip().split("\n")[1:]
yt_idx = 0

for u_line in user_lines:
    parts = u_line.split(",")
    if len(parts) < 4: continue
    
    start_str = parts[1].replace(";", ".")
    end_str = parts[2].replace(";", ".")
    text = parts[3][:15] + "..."
    
    # 00:00:22.00 -> ms
    def time_to_ms(t_str):
        h, m, s = t_str.split(":")
        s, ms_part = s.split(".")
        return int(h)*3600000 + int(m)*60000 + int(s)*1000 + int(ms_part)*10  # roughly since ;00 is frames? Assuming 30fps it's different. If it's ;00 let's just treat as 1/100s for display or frames.
        
    def time_to_sec(t_str):
        h, m, s = t_str.split(":")
        s, frames = s.split(".")
        return int(h)*3600 + int(m)*60 + int(s) + int(frames)/30.0

    u_start = time_to_sec(start_str)
    
    # Get corresponding youtube timing (approx by text length)
    yt_start = 0
    yt_text = ""
    if yt_idx < len(lyric_lines):
        yt_start_ms = lyric_lines[yt_idx]["start_ms"]
        yt_start = yt_start_ms / 1000.0
        yt_text = "".join([w["word"] for w in lyric_lines[yt_idx]["words"]])
        
        diff = yt_start - u_start
        diff_str = f"{diff:+.1f}초 밀림" if diff > 0 else f"{diff:+.1f}초 빠름"
        
        print(f"| {text} | {start_str}~{end_str} | {yt_start_ms//60000:02.0f}:{(yt_start_ms%60000)/1000:05.2f}~ | **{diff_str}** |")
        
        # Advance yt_idx
        yt_idx += 2 if len(parts[3]) > 20 else 1

