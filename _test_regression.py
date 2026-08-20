# -*- coding: utf-8 -*-
"""回归测试：合成题目图像（多字体）→ solve_problem 断言答案"""
import sys, os
sys.path.insert(0, '.')
import numpy as np
from PIL import Image, ImageFont, ImageDraw
import main_calc
from main_calc import OCREngine

W, H = 700, 140
FONTS = [
    ("arial.ttf", 64), ("arialbd.ttf", 64), ("simhei.ttf", 62),
    ("msyh.ttc", 60), ("simsun.ttc", 60), ("consola.ttf", 64),
    ("times.ttf", 64), ("comic.ttf", 64), ("impact.ttf", 60),
    ("cour.ttf", 64), ("georgia.ttf", 62), ("tahoma.ttf", 62),
]

def render_text(text, ttf, size):
    fp = os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", ttf)
    if not os.path.exists(fp):
        return None
    font = ImageFont.truetype(fp, size)
    img = Image.new("L", (W, H), 255)
    d = ImageDraw.Draw(img)
    bbox = d.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    d.text(((W - tw) // 2 - bbox[0], (H - th) // 2 - bbox[1]), text, fill=0, font=font)
    arr = np.array(img)
    return np.stack([arr] * 3, axis=-1)  # 转 BGR? 直接灰度副本 → engine 会自动转

engine = OCREngine(confidence=0.60)

CASES = [
    # (题目, 期望答案)  None 表示不应写出
    ("3 + 4", 7), ("3-8", -5), ("12 × 12", 144),
    ("8 ÷ 2", 4), ("9 - 3", 6), ("5 * 6", 30),
    ("? + 3 = 5", 2), ("5 - ? = 2", 3), ("6 × ? = 12", 2),
    ("12 ÷ ? = 3", 4), ("3 + 4 = ?", 7), ("1 * ? = 3", 3),
    ("1 + 1 * 2 = ?", 3), ("2 * 3 + 1", 7), ("10 - 2 * 3", 4),
    ("7 ÷ 2", None), ("5 ÷ 0", None), ("0 ÷ 5", 0),
    ("100 + 100", 200), ("20 ÷ 5", 4), ("11 + 22", 33),
    ("1 + 2 + 3", 6), ("4 × 4", 16), ("100 - 1", 99),
    # 全角变体
    ("？＋3＝5", 2), ("3＋4＝？", 7), ("6×？＝12", 2),
    # 紧凑排版
    ("3+4=?", 7), ("1+1*2=?", 3), ("5-?=2", 3),
]

passed = failed = skipped = 0
fail_list = []
run = 0
for text, expect in CASES:
    done = False
    for ttf, size in FONTS:
        img = render_text(text, ttf, size)
        if img is None:
            continue
        res = engine.solve_problem(img)
        got = res[0] if res else None
        run += 1
        if got == expect:
            passed += 1
            done = True
            break
        elif got is not None:
            failed += 1
            fail_list.append((text, ttf, expect, got))
            done = True
            break
        # got None 但期望答案 → 换字体再试（小尺寸字体可能识别不到）
    if not done and expect is not None and not any(t[0] == text for t in fail_list):
        skipped += 1
        fail_list.append((text, "ALL", expect, None))

print(f"合成图像回归: {run} 次识别, 通过 {passed}, 失败 {failed}, 未命中 {skipped}")
if fail_list:
    print("失败清单:")
    for text, ttf, expect, got in fail_list:
        print(f"  {text!r} [{ttf}] 期望 {expect}, 得到 {got}")
else:
    print("全部通过!")