# -*- coding: utf-8 -*-
"""自检：渲染笔画路径 → 用识别器分类，验证形状可辨识"""
import sys, os
sys.path.insert(0, '.')
import numpy as np
from PIL import Image, ImageDraw
from main_calc import MouseDrawer, OCREngine

md = MouseDrawer()
engine = OCREngine()

print('每个数字的笔画渲染 → 识别器判断:')
all_ok = True
for ch in '0123456789':
    # 渲染笔画路径为粗线图像（白底黑线，模拟手写）
    W, H = 60, 80
    img = Image.new('L', (W, H), 255)
    d = ImageDraw.Draw(img)
    for stroke in md.DIGIT_STROKES[ch]:
        pts = [(int(fx * W), int(fy * H)) for fx, fy in stroke]
        for i in range(len(pts) - 1):
            d.line([pts[i], pts[i + 1]], fill=0, width=5)
    arr = np.array(img)
    # 反色：数字为白
    binary = 255 - arr
    # 裁剪到内容范围
    ys, xs = np.where(binary > 0)
    if len(xs) == 0:
        print(f'  {ch}: 空!')
        all_ok = False
        continue
    roi = binary[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    kind, val, score = engine.matcher.classify(roi)
    ok = kind == 'digit' and val == ch
    all_ok = all_ok and ok
    print(f'  {ch}: 识别为 {val} ({score:.2f}) {"OK" if ok else "FAIL!"}')

print()
print('自检:', '全部通过' if all_ok else '有失败')
