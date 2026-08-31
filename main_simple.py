"""
屏幕数字比较识别工具 v4

功能:
  - 框选屏幕区域识别数学表达式中的数字（内置识别，无需安装外部 OCR）
  - 后台静默识别，不在屏幕上显示任何计算结果
  - 按【L】键（全局热键）才真正模拟鼠标划线
    （鼠标按下 -> 拖动 -> 抬起，把 > < = 像手写一样画到目标程序上）
  - 识别框 / 绘制框可拖动缩放（1:1 精确跟随）
  - 🔒 锁定模式：框点击穿透，不挡目标程序的鼠标操作
  - 位置与模式自动记忆，重启恢复

使用方式:
    python main_simple.py
"""

import ctypes
from ctypes import wintypes
import tkinter as tk
import threading
import time
import random
import math
import sys
import os
import json
import shutil

# 导入依赖
try:
    import mss
    import numpy as np
    from PIL import Image, ImageDraw, ImageFont
    import cv2
except ImportError as e:
    print(f"[错误] 缺少依赖: {e}")
    print("请运行: pip install -r requirements_simple.txt")
    sys.exit(1)

# 可选: Tesseract（若已安装则作为辅助，未安装也能正常工作）
try:
    import pytesseract
    HAS_TESSERACT_LIB = True
except ImportError:
    HAS_TESSERACT_LIB = False

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")

# 划线触发键（虚拟键码: 0x4C = L，可改为其他键）
HOTKEY_VK = 0x4C

# Windows 窗口样式常量
GWL_EXSTYLE = -20
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020


# ============================================================
#  DPI 感知与窗口点击穿透
# ============================================================

def set_dpi_aware():
    """进程级 DPI 感知：让 tkinter/mss/鼠标坐标使用物理像素，三者一致"""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor aware
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def set_click_through(widget, enabled):
    """让窗口点击穿透（锁定模式用），enabled=False 恢复可交互"""
    try:
        hwnd = ctypes.windll.user32.GetParent(widget.winfo_id())
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if enabled:
            style |= WS_EX_LAYERED | WS_EX_TRANSPARENT
        else:
            style &= ~WS_EX_TRANSPARENT
            style |= WS_EX_LAYERED
        ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)
        # 刷新窗口样式
        ctypes.windll.user32.SetWindowPos(
            hwnd, 0, 0, 0, 0, 0,
            0x0001 | 0x0002 | 0x0010 | 0x0020  # NOMOVE|NOSIZE|NOZORDER|FRAMECHANGED
        )
    except Exception:
        pass


# ============================================================
#  全局热键（WH_KEYBOARD_LL，任何窗口聚焦时都生效）
# ============================================================

class GlobalHotkey:
    """低层键盘钩子，监听指定键，无需窗口聚焦"""

    WH_KEYBOARD_LL = 13
    WM_KEYDOWN = 0x0100
    WM_SYSKEYDOWN = 0x0104
    WM_QUIT = 0x0012

    def __init__(self, vk_code=HOTKEY_VK, on_press=None):
        self.vk_code = vk_code
        self._on_press = on_press
        self._running = False
        self._thread = None
        self._thread_id = None
        self._hook = None
        self._proc_ref = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="global-hotkey")
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread_id:
            try:
                ctypes.windll.user32.PostThreadMessageW(
                    self._thread_id, self.WM_QUIT, 0, 0)
            except Exception:
                pass

    def _run(self):
        self._thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
        user32 = ctypes.windll.user32

        class KBDLLHOOKSTRUCT(ctypes.Structure):
            _fields_ = [
                ("vkCode", wintypes.DWORD),
                ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_ssize_t),
            ]

        HOOKPROC = ctypes.CFUNCTYPE(
            ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

        # 显式声明参数类型，避免 64 位下 LPARAM 指针值溢出
        user32.SetWindowsHookExW.argtypes = [
            ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD]
        user32.SetWindowsHookExW.restype = wintypes.HHOOK
        user32.CallNextHookEx.argtypes = [
            wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM]
        user32.CallNextHookEx.restype = wintypes.LPARAM
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND,
            wintypes.UINT, wintypes.UINT]
        user32.GetMessageW.restype = ctypes.c_int
        user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]

        def proc(nCode, wParam, lParam):
            if nCode >= 0 and wParam in (self.WM_KEYDOWN, self.WM_SYSKEYDOWN):
                kb = ctypes.cast(lParam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
                if kb.vkCode == self.vk_code:
                    try:
                        if self._on_press:
                            self._on_press()
                    except Exception:
                        pass
            return user32.CallNextHookEx(None, nCode, wParam, lParam)

        self._proc_ref = HOOKPROC(proc)  # 保持引用，防止被垃圾回收
        self._hook = user32.SetWindowsHookExW(
            self.WH_KEYBOARD_LL, self._proc_ref, None, 0)
        if not self._hook:
            print("[警告] 全局热键注册失败，按 L 键划线不可用")
            return

        # 消息循环（钩子回调在此线程执行）
        msg = wintypes.MSG()
        while self._running:
            r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if r <= 0:
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._hook:
            user32.UnhookWindowsHookEx(self._hook)
            self._hook = None


# ============================================================
#  真实鼠标划线器（把屏幕当触控屏）
# ============================================================

class MouseDrawer:
    """用真实鼠标事件在屏幕上画出符号。
    带随机晃动 + 弧线弯曲模拟人手，用于通过人机验证。
    每笔保证「按下 -> 连续拖动 -> 抬起」一笔画完，中途绝不松开。"""

    LEFTDOWN = 0x0002
    LEFTUP = 0x0004
    STEP_MS = 0.0015  # 每步基础间隔（快档；反外挂判定只看轨迹是否连续，
                   # 连续性 = 插值完整 + 步距 ≤10px + 无瞬移，全部保留）
    STEPS = 10        # 每段插值步数上限（短距离自动减少，见 move_to）

    def __init__(self, jitter=1.8):
        self.user32 = ctypes.windll.user32
        self.jitter = jitter  # 随机晃动幅度（像素）

    def position(self):
        pt = ctypes.wintypes.POINT()
        self.user32.GetCursorPos(ctypes.byref(pt))
        return pt.x, pt.y

    def move_to(self, x, y, steps=None, jitter=None):
        """
        从当前位置平滑移动到目标点。
        中间随机晃动 + 弧线弯曲 + 人手速度包络（起笔慢→中段快→收笔慢），
        起点终点精确收敛。轨迹连续无跳变：这是通过人机验证的关键。
        """
        if jitter is None:
            jitter = self.jitter
        cx, cy = self.position()
        dx, dy = x - cx, y - cy
        dist = math.hypot(dx, dy)
        if dist < 2:
            # 已在目标点：直接落点，避免起笔时晃出多余墨点
            self.user32.SetCursorPos(int(x), int(y))
            return
        if steps is None:
            # 距离自适应：短距离（用户压小的框）步数自动减少，
            # 长距离封顶 STEPS。快手感：步距 ~10px 内（人手拖动远超此跨度）
            steps = max(4, min(self.STEPS, int(dist / 10) + 4))
        # 垂直方向单位向量（产生弧线弯曲）
        ux, uy = dx / dist, dy / dist
        px, py = -uy, ux
        bow = random.uniform(-0.12, 0.12) * dist  # 弯曲幅度随机
        for i in range(1, steps + 1):
            t = i / steps
            # 晃动幅度按正弦调制：两端为 0（精确收尾），中间最大
            wobble = jitter * math.sin(math.pi * t)
            tx = (cx + dx * t
                  + px * bow * math.sin(math.pi * t)
                  + random.uniform(-wobble, wobble))
            ty = (cy + dy * t
                  + py * bow * math.sin(math.pi * t)
                  + random.uniform(-wobble, wobble))
            self.user32.SetCursorPos(int(tx), int(ty))
            # 人手速度包络：起笔慢(≈2x) → 冲刺(≈0.5x) → 收笔慢(≈2x)
            # 而非匀速，避免被识别为机械鼠标
            speed_env = 0.45 + 1.15 * math.sin(math.pi * t) ** 2
            # 每步再叠加随机变速（人手忽快忽慢）
            time.sleep(self.STEP_MS * speed_env * random.uniform(0.7, 1.3))

    def press(self):
        self.user32.mouse_event(self.LEFTDOWN, 0, 0, 0, 0)
        time.sleep(random.uniform(0.008, 0.014))  # 按下后顿一下（人手迟疑）

    def release(self):
        time.sleep(random.uniform(0.005, 0.009))  # 抬起前顿一下
        self.user32.mouse_event(self.LEFTUP, 0, 0, 0, 0)

    def stroke(self, points):
        """
        一笔画完：平滑移到起点 -> 按下 -> 连续拖动经过所有点 -> 终点抬起。
        整笔过程中鼠标左键保持按下，绝不在中途抬起。
        全程连续移动（无瞬移跳变），轨迹完整可被逐帧采样。
        """
        # 起笔慢挪（未按下不产生墨迹）：步数按距离自适应，短距离少步快挪
        self.move_to(*points[0])
        self.press()
        for p in points[1:]:
            self.move_to(*p)
        self.release()
        time.sleep(random.uniform(0.008, 0.014))  # 笔画/符号间停顿

    def draw_symbol(self, symbol, x, y, w, h):
        """在 (x,y,w,h) 区域内画出符号。整体位置微随机偏移，更自然。"""
        m = 0.14  # 边距比例
        # 每次绘制整体偏移 1-2px（每次画的形状略有差异）
        x += random.uniform(-1.5, 1.5)
        y += random.uniform(-1.5, 1.5)
        if symbol == ">":
            p1 = (x + w * m,       y + h * 0.20)
            p2 = (x + w * (1 - m), y + h * 0.50)
            p3 = (x + w * m,       y + h * 0.80)
            self.stroke([p1, p2, p3])   # 一笔画完
        elif symbol == "<":
            p1 = (x + w * (1 - m), y + h * 0.20)
            p2 = (x + w * m,       y + h * 0.50)
            p3 = (x + w * (1 - m), y + h * 0.80)
            self.stroke([p1, p2, p3])   # 一笔画完
        elif symbol == "=":
            # 等号必须两笔（两横线），每笔都保证一笔画完
            self.stroke([(x + w * m,       y + h * 0.35),
                         (x + w * (1 - m), y + h * 0.35)])
            self.stroke([(x + w * m,       y + h * 0.65),
                         (x + w * (1 - m), y + h * 0.65)])


# ============================================================
#  配置持久化（记住位置和模式）
# ============================================================

class Config:
    """简单的 JSON 配置存取"""

    def __init__(self, path=CONFIG_PATH):
        self.path = path
        self.data = {}
        self.load()

    def exists(self):
        return os.path.exists(self.path)

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            self.data = {}

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"[警告] 配置保存失败: {e}")

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value


# ============================================================
#  内置数字识别器（纯 OpenCV 模板匹配，零外部依赖）
# ============================================================

class DigitMatcher:
    """
    用系统常见字体渲染 0-9 模板，对截图中的数字做模板匹配。
    无需安装任何 OCR 引擎，对清晰截图识别快且准。

    匹配策略（抗混淆）:
      - 每个数字类别取全部字体模板的最高分
        （结构相似度 IoU 与归一化互相关 CCOEFF 各占一半）
      - 5/6 分数接近时用顶部横杠密度判别
        （5 有平顶横杠、6 顶部开口）
    """
    TW = 28   # 模板宽度
    TH = 40   # 模板高度
    FONT_FILES = [
        "arial.ttf", "arialbd.ttf",
        "calibri.ttf", "calibrib.ttf",
        "segoeui.ttf", "segoeuib.ttf",
        "consola.ttf", "consolab.ttf",
        "times.ttf", "timesbd.ttf",
        "simhei.ttf",           # 黑体
        "msyh.ttc", "msyhbd.ttc",  # 微软雅黑
        "comic.ttf",            # Comic Sans MS
        "tahoma.ttf", "tahomabd.ttf",
        "verdana.ttf", "verdanab.ttf",
        "cour.ttf", "courbd.ttf",
        "georgia.ttf", "georgiab.ttf",
        "simsun.ttc",           # 宋体
        "simfang.ttf",          # 仿宋
        "simkai.ttf",           # 楷体
        "bahnschrift.ttf",
    ]
    # 题目中可能出现的非数字符号（用于拒识，避免 ? < > = 被当成数字）
    SYMBOL_CHARS = "?><=+-*/|:."
    SYMBOL_FONTS = [
        "arial.ttf", "arialbd.ttf", "segoeui.ttf",
        "simhei.ttf", "simsun.ttc", "cour.ttf",
    ]

    def __init__(self):
        self.templates = []  # [(digit, tmpl, tmpl_inv), ...]
        self.by_digit = {}   # digit -> [(tmpl, tmpl_inv), ...]
        self.by_symbol = {}  # symbol -> [(tmpl, tmpl_inv), ...]
        self._build()

    def _font_path(self, ttf):
        windir = os.environ.get("WINDIR", "C:\\Windows")
        p = os.path.join(windir, "Fonts", ttf)
        return p if os.path.exists(p) else None

    def _render_digit(self, digit):
        """渲染单个数字为归一化二值模板，返回 (digit, tmpl, tmpl_inv)"""
        results = []
        for ttf in self.FONT_FILES:
            fp = self._font_path(ttf)
            if not fp:
                continue
            try:
                font = ImageFont.truetype(fp, 48)
                img = Image.new("L", (96, 96), 255)
                d = ImageDraw.Draw(img)
                bbox = d.textbbox((0, 0), digit, font=font)
                w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
                if w <= 0 or h <= 0:
                    continue
                d.text((-bbox[0], -bbox[1]), digit, fill=0, font=font)
                img = img.crop((0, 0, w, h))
                arr = np.array(img)
                arr = (arr < 128).astype(np.uint8) * 255  # 数字=255
                norm = self._normalize(arr)
                if norm is not None:
                    results.append((digit, norm, 255 - norm))
            except Exception:
                continue
        return results

    def _normalize(self, roi):
        """裁剪并缩放到固定画布（保持宽高比）"""
        ys, xs = np.where(roi > 0)
        if len(xs) == 0:
            return None
        x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
        roi = roi[y0:y1 + 1, x0:x1 + 1]
        h, w = roi.shape
        inner_h, inner_w = self.TH - 4, self.TW - 4
        scale = min(inner_h / h, inner_w / w)
        nh, nw = max(1, int(h * scale)), max(1, int(w * scale))
        resized = cv2.resize(roi, (nw, nh), interpolation=cv2.INTER_AREA)
        canvas = np.zeros((self.TH, self.TW), dtype=np.uint8)
        y_off = (self.TH - nh) // 2
        x_off = (self.TW - nw) // 2
        canvas[y_off:y_off + nh, x_off:x_off + nw] = resized
        return canvas

    def _build(self):
        for digit in "0123456789":
            for _, tmpl, _ in self._render_digit(digit):
                self.templates.append((digit, tmpl, 255 - tmpl))
                self.by_digit.setdefault(digit, []).append((tmpl, 255 - tmpl))
        # 去重：同一数字内高度相似的字体模板只留一个（加速且不掉精度）
        for digit, tlist in self.by_digit.items():
            kept = []
            for tmpl, tmpl_inv in tlist:
                dup = False
                for kt, _ in kept:
                    inter = np.logical_and(tmpl > 0, kt > 0).sum()
                    union = np.logical_or(tmpl > 0, kt > 0).sum()
                    if inter / max(1, union) > 0.92:
                        dup = True
                        break
                if not dup:
                    kept.append((tmpl, tmpl_inv))
            self.by_digit[digit] = kept
        # 符号模板（用于拒识，不需要很多字体）
        for sym in self.SYMBOL_CHARS:
            for ttf in self.SYMBOL_FONTS:
                fp = self._font_path(ttf)
                if not fp:
                    continue
                try:
                    font = ImageFont.truetype(fp, 48)
                    img = Image.new("L", (96, 96), 255)
                    d = ImageDraw.Draw(img)
                    bbox = d.textbbox((0, 0), sym, font=font)
                    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
                    if w <= 0 or h <= 0:
                        continue
                    d.text((-bbox[0], -bbox[1]), sym, fill=0, font=font)
                    img = img.crop((0, 0, w, h))
                    arr = (np.array(img) < 128).astype(np.uint8) * 255
                    norm = self._normalize(arr)
                    if norm is not None:
                        self.by_symbol.setdefault(sym, []).append(
                            (norm, 255 - norm))
                except Exception:
                    continue
        # 答题反馈符号（对勾/叉）手绘拒识模板：
        # 划线后软件会在答案上打 ✓/✗，若落在识别框内，防止被误读成数字（1/7）
        for name, pts in (("check", [(18, 55), (42, 78), (80, 22)]),   # ✓ 两笔
                          ("cross", [(20, 20), (76, 76), (76, 20), (20, 76)])):  # ✗ 两笔
            arr = np.zeros((96, 96), dtype=np.uint8)
            if name == "check":
                cv2.line(arr, pts[0], pts[1], 255, 8)
                cv2.line(arr, pts[1], pts[2], 255, 8)
            else:
                cv2.line(arr, pts[0], pts[1], 255, 8)
                cv2.line(arr, pts[2], pts[3], 255, 8)
            norm = self._normalize(arr)
            if norm is not None:
                self.by_symbol.setdefault(name, []).append((norm, 255 - norm))

        total = sum(len(v) for v in self.by_digit.values())
        sym_total = sum(len(v) for v in self.by_symbol.values())
        print(f"[信息] 内置数字识别器已就绪 "
              f"({total} 数字模板 + {sym_total} 符号模板)")

    @staticmethod
    def _top_bar_stripe(bin_img):
        """顶部横杠判别：最顶 2 行「最大连续白段占比」的均值。
        5 的顶杠满宽且在第 1 行就出现（≈0.6+），6 顶部是开口弧线
        顶端（≈0.45-，且逐行递增），抗 arial/粗体/衬线字体干扰。"""
        ys, xs = np.where(bin_img > 0)
        if len(xs) == 0:
            return 0.0
        y0 = ys.min()
        w = bin_img.shape[1]
        vals = []
        for row in bin_img[y0:y0 + 2, :]:
            r = row > 0
            if not r.any():
                vals.append(0.0)
                continue
            mx = 0
            cur = 0
            for v in r:
                cur = cur + 1 if v else 0
                mx = max(mx, cur)
            vals.append(mx / w)
        return float(np.mean(vals)) if vals else 0.0

    def classify(self, roi):
        """
        分类单个 ROI（数字为白色255）。

        两轮策略（兼顾速度与精度）:
          第一轮: 用快速 CCOEFF 扫全部字体模板，得到每个数字类别的最高分，
                  同时扫符号模板
          第二轮: 仅对前两名数字候选算 IoU 结构相似度，融合后排序
          5/6 分数接近时用顶部横杠密度判别。
        返回: (kind, value, score)
              ('digit', '5', 0.9) / ('symbol', None, 0.8) / (None, None, 0.0)
        """
        norm = self._normalize(roi)
        if norm is None:
            return None, None, 0.0

        # 两级策略（提速 ~3x，精度无损）:
        #   粗筛: 每类前 2 个模板（arial/arialbd，主流字体代表）
        #   精算: 只对粗筛 top3 类别的全部模板跑 CCOEFF（原为全部 10 类全模板）
        quick = {}
        for digit, tlist in self.by_digit.items():
            best_s = -1.0
            for tmpl, tmpl_inv in tlist[:2]:
                s = cv2.matchTemplate(norm, tmpl, cv2.TM_CCOEFF_NORMED)[0, 0]
                if s > best_s:
                    best_s = s
            quick[digit] = best_s
        top3 = sorted(quick.items(), key=lambda kv: -kv[1])[:3]

        ccf_best = {}   # digit -> (best_ccf, best_tmpl)
        for digit, _ in top3:
            best_s, best_t = -1.0, None
            for tmpl, tmpl_inv in self.by_digit[digit]:
                s = cv2.matchTemplate(norm, tmpl, cv2.TM_CCOEFF_NORMED)[0, 0]
                if s > best_s:
                    best_s, best_t = s, tmpl
            ccf_best[digit] = (best_s, best_t)

        # 符号拒识：只跑 1 个代表字体（'?' '<' 等与数字区分度足够）
        sym_best = -1.0
        for sym, tlist in self.by_symbol.items():
            for tmpl, tmpl_inv in tlist[:1]:
                s = cv2.matchTemplate(norm, tmpl, cv2.TM_CCOEFF_NORMED)[0, 0]
                if s > sym_best:
                    sym_best = s

        # 若整体匹配分偏低，可能极性相反，用反色模板再扫一轮（top3 + 符号）
        # 阈值 0.45：正常极性单轮通过（题目为清晰白底黑字，几乎从不触发反色）
        top_ccf = max(v[0] for v in ccf_best.values())
        if top_ccf < 0.45:
            for digit, _ in top3:
                best_s, best_t = ccf_best[digit]
                for tmpl, tmpl_inv in self.by_digit[digit]:
                    s = cv2.matchTemplate(norm, tmpl_inv, cv2.TM_CCOEFF_NORMED)[0, 0]
                    if s > best_s:
                        best_s, best_t = s, tmpl
                ccf_best[digit] = (best_s, best_t)
            for sym, tlist in self.by_symbol.items():
                for tmpl, tmpl_inv in tlist[:1]:
                    s = cv2.matchTemplate(norm, tmpl_inv, cv2.TM_CCOEFF_NORMED)[0, 0]
                    if s > sym_best:
                        sym_best = s

        # 更像符号（? < > = 等）→ 不是数字
        best_digit_ccf = max(v[0] for v in ccf_best.values())
        if sym_best > best_digit_ccf:
            return ('symbol', None, sym_best)

        # 按 CCOEFF 排序，取前两名进入第二轮
        ranked = sorted(ccf_best.items(), key=lambda kv: -kv[1][0])[:2]
        if not ranked:
            return None, None, 0.0

        scored = []
        for digit, (ccf, tmpl) in ranked:
            inter = np.logical_and(norm > 0, tmpl > 0).sum()
            union = np.logical_or(norm > 0, tmpl > 0).sum()
            iou = inter / max(1, union)
            scored.append((digit, 0.5 * iou + 0.5 * ccf))
        scored.sort(key=lambda r: -r[1])

        d1, s1 = scored[0]
        d2, s2 = scored[1]
        # 5/6 并列判别：5 有平顶横杠，6 顶部开口
        if {d1, d2} == {"5", "6"} and (s1 - s2) < 0.08:
            return ('digit', ("5" if self._top_bar_stripe(norm) >= 0.55 else "6"),
                    max(s1, s2))
        return ('digit', d1, s1)

    def match(self, roi):
        """兼容旧接口：返回 (digit_str, score) 或 (None, 0.0)"""
        kind, value, score = self.classify(roi)
        if kind == 'digit':
            return value, score
        return None, 0.0


# ============================================================
#  OCR 引擎（内置识别 + 可选 Tesseract 辅助）
# ============================================================

class OCREngine:
    def __init__(self, confidence=0.62):
        # 0.62: 真实数字最低 0.63（18px 小字），而 ? x × 等符号最高 0.58，安全分界
        self.confidence = confidence
        self.matcher = DigitMatcher()
        self.tess_available = self._check_tesseract()
        if not self.tess_available:
            print("[信息] 使用内置数字识别（无需安装 Tesseract）")

    def _check_tesseract(self):
        """检查 Tesseract 是否可用（不可用则静默回退，不刷错误）"""
        if not HAS_TESSERACT_LIB:
            return False
        if sys.platform == "win32":
            for p in [
                r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
            ]:
                if os.path.exists(p):
                    pytesseract.pytesseract.tesseract_cmd = p
                    return True
        return shutil.which("tesseract") is not None

    def preprocess(self, image):
        """转灰度 + 自适应二值化，数字为白色(255)"""
        if len(image.shape) == 3:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
        else:
            gray = image.copy()
        binary = cv2.adaptiveThreshold(
            gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
            cv2.THRESH_BINARY_INV, 11, 2
        )
        return binary

    def extract_numbers(self, image):
        """返回识别出的数字列表（按从左到右的顺序）"""
        binary = self.preprocess(image)

        # 1) 内置模板匹配
        nums = self._extract_by_matcher(binary)
        if len(nums) >= 2:
            return nums

        # 2) 内置识别不足 2 个数字时，用 Tesseract 辅助（若可用）
        if self.tess_available:
            tnums = self._extract_by_tesseract(image)
            if len(tnums) > len(nums):
                return tnums

        return nums

    def _extract_by_matcher(self, binary):
        """
        轮廓检测 + 模板匹配，并把数字组合成两个数。

        优先用【问号分隔】：题目中间必有 '?'（或其曲线/符号），
        检测到后直接按它左右拆分，彻底防止 13?12 被读成 131>2。
        无 '?' 时回退到间距分组。
        """
        contours, _ = cv2.findContours(
            binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
        )
        h_img, w_img = binary.shape[:2]
        items = []    # 数字轮廓 [x, y, w, h, digit]
        symbols = []  # 符号轮廓 [x, y, w, h, sym]
        boxes = []    # 所有轮廓的框 (x, y, w, h)，含小圆点（用于 ? 检测）
        for c in contours:
            x, y, w, h = cv2.boundingRect(c)
            boxes.append((x, y, w, h))
            if w < 4 or h < 8:          # 太小
                continue
            if w > w_img * 0.9 or h > h_img * 0.95:  # 整行/整块（背景噪声）
                continue
            roi = binary[y:y + h, x:x + w]
            kind, value, score = self.matcher.classify(roi)
            if kind == 'digit' and score >= self.confidence:
                items.append([x, y, w, h, value])
            elif kind == 'symbol':
                symbols.append([x, y, w, h, value])

        # 问号曲线检测：'?' 由曲线 + 下方小圆点组成，真实数字下方没有小圆点
        q_curves = []
        keep = []
        for it in items:
            box = (it[0], it[1], it[2], it[3])
            if any(self._is_dot_below(box, b) for b in boxes if b != box):
                q_curves.append(it)   # 这是 '?' 的曲线，作为分隔符
            else:
                keep.append(it)
        items = keep

        # 分隔符位置 = 问号曲线 x 中心 + 其他符号 x 中心（取中位数）
        seps = [it[0] + it[2] / 2 for it in q_curves]
        seps += [s[0] + s[2] / 2 for s in symbols]
        if seps:
            sep_x = sorted(seps)[len(seps) // 2]
            left = [it for it in items if it[0] + it[2] / 2 < sep_x]
            right = [it for it in items if it[0] + it[2] / 2 >= sep_x]
            n1 = self._merge_group(left)
            n2 = self._merge_group(right)
            if n1 is not None and n2 is not None:
                return [n1, n2]

        # 无问号分隔 → 回退到间距分组
        items.sort(key=lambda t: t[0])
        return self._group_numbers(items)

    @staticmethod
    def _merge_group(items):
        """把一组数字轮廓按间距合并成一个整数；空则返回 None"""
        if not items:
            return None
        items = sorted(items, key=lambda t: t[0])
        text = ""
        prev_end = None
        prev_h = 0
        for x, y, w, h, d in items:
            if prev_end is not None and x - prev_end < 0.3 * min(prev_h, h):
                text += d
            else:
                text += d
            prev_end = x + w
            prev_h = h
        return int(text) if text.isdigit() else None

    @staticmethod
    def _is_dot_below(digit_box, other_box):
        """
        判断 other_box 是否是 digit_box 正下方的小圆点
        （'?' 的圆点特征：尺寸远小于主体、位于主体下方、水平对齐）
        """
        dx, dy, dw, dh = digit_box
        sx, sy, sw, sh = other_box
        if sw <= 0 or sh <= 0:
            return False
        # 小圆点：尺寸远小于数字主体
        if sw > dw * 0.45 or sh > dh * 0.45:
            return False
        # 位于数字主体下方（圆点顶部低于数字主体的 60% 高度处）
        if sy < dy + dh * 0.6:
            return False
        # 水平对齐
        dot_cx = sx + sw / 2
        if abs(dot_cx - (dx + dw / 2)) > dw * 0.9:
            return False
        return True

    @staticmethod
    def _group_numbers(items):
        """
        把检测到的数字轮廓组合成数（比较题恰好两个数）。

        策略:
          1. 同一数字内相邻字符间距 < 0.3 * 字高 → 合并（如 12、26）
          2. 若合并后仍 >= 3 组（有噪声/漏合并），在最大间距处切成 2 组
        """
        if not items:
            return []
        groups = []  # [start_x, end_x, text, height]
        for x, y, w, h, d in items:
            if not groups:
                groups.append([x, x + w, d, h])
            else:
                last = groups[-1]
                if x - last[1] < 0.3 * min(last[3], h):
                    last[1] = x + w
                    last[2] += d
                    last[3] = max(last[3], h)
                else:
                    groups.append([x, x + w, d, h])

        # 若组数 >= 3，按最大间距切成 2 组（两个数）
        if len(groups) >= 3:
            gaps = [groups[i][0] - groups[i - 1][1] for i in range(1, len(groups))]
            cut = gaps.index(max(gaps))
            left = "".join(g[2] for g in groups[:cut + 1])
            right = "".join(g[2] for g in groups[cut + 1:])
            groups = [left, right]

        numbers = []
        for g in groups:
            text = g if isinstance(g, str) else g[2]
            if text.isdigit():
                numbers.append(int(text))
        return numbers

    def _extract_by_tesseract(self, image):
        try:
            gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
            pil_img = Image.fromarray(gray)
            cfg = r'--oem 3 --psm 8 -c tessedit_char_whitelist=0123456789'
            data = pytesseract.image_to_data(
                pil_img, config=cfg, output_type=pytesseract.Output.DICT
            )
            items = []
            for i, text in enumerate(data["text"]):
                t = text.strip()
                if t.isdigit() and int(data["conf"][i]) >= self.confidence * 100:
                    items.append((data["left"][i], t))
            items.sort(key=lambda v: v[0])
            return [int(t) for _, t in items]
        except Exception:
            return []


# ============================================================
#  屏幕捕获
# ============================================================

class ScreenCapture:
    def __init__(self):
        self.sct = mss.mss()

    def capture_region(self, x, y, width, height):
        monitor = {"left": x, "top": y, "width": width, "height": height}
        screenshot = self.sct.grab(monitor)
        img = Image.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
        result = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
        # 显式释放临时对象，防止长跑内存增长
        del img, screenshot
        return result

    def capture_region_fast(self, x, y, width, height):
        """极速路径：mss 原生 grab 直转 BGR（省 PIL 中转，约快 40-60%）"""
        monitor = {"left": x, "top": y, "width": width, "height": height}
        screenshot = self.sct.grab(monitor)
        arr = np.frombuffer(screenshot.bgra, np.uint8).reshape(height, width, 4)
        del screenshot
        return cv2.cvtColor(arr, cv2.COLOR_BGRA2BGR)


# ============================================================
#  可拖动、可缩放的框（1:1 精确跟随，右下角大方块缩放）
# ============================================================

class DraggableFrame:
    HANDLE = 20  # 右下角调整手柄大小（px）

    def __init__(self, root, x, y, width, height, color="red",
                 label="", alpha=0.35, on_change=None, on_release=None):
        self.root = root
        self.x, self.y = x, y
        self.width, self.height = width, height
        self.color = color
        self.label = label
        self.on_change = on_change
        self.on_release_cb = on_release

        self.window = tk.Toplevel(root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", alpha)
        self.window.geometry(f"{width}x{height}+{x}+{y}")

        self.canvas = tk.Canvas(
            self.window, width=width, height=height,
            bg=color, highlightthickness=2, highlightbackground=color
        )
        self.canvas.pack()

        if label:
            self.label_id = self.canvas.create_text(
                width // 2, 15, text=label, fill="white",
                font=("Microsoft YaHei", 10, "bold"), tags="label"
            )

        # 右下角调整手柄（20px，带斜纹指示）
        self.handle = self.canvas.create_rectangle(
            width - self.HANDLE, height - self.HANDLE, width, height,
            fill="yellow", outline="black", tags="handle"
        )
        self.grip = self.canvas.create_line(
            width - 13, height, width, height - 13,
            fill="gray30", width=2, tags="handle"
        )
        self.canvas.tag_bind("handle", "<Button-1>", self.start_resize)
        self.canvas.tag_bind("handle", "<B1-Motion>", self.do_resize)
        self.canvas.tag_bind("handle", "<ButtonRelease-1>", self.on_release)

        self.canvas.bind("<Button-1>", self.start_drag)
        self.canvas.bind("<B1-Motion>", self.do_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)

        self.drag_data = {}
        self.resizing = False

    # ---- 拖动（基准点 + 当前偏移，1:1 跟随） ----
    def start_drag(self, event):
        if self.resizing:      # 正在缩放时忽略拖动手势（消除事件冲突）
            return
        self.drag_data = {"sx": event.x, "sy": event.y,
                          "wx": self.x, "wy": self.y}

    def do_drag(self, event):
        if self.resizing:
            return
        self.x = self.drag_data["wx"] + (event.x - self.drag_data["sx"])
        self.y = self.drag_data["wy"] + (event.y - self.drag_data["sy"])
        self.window.geometry(f"{self.width}x{self.height}+{self.x}+{self.y}")
        self._notify_change()

    # ---- 缩放（同样的基准点方式） ----
    def start_resize(self, event):
        self.resizing = True
        self.drag_data = {"sx": event.x, "sy": event.y,
                          "ww": self.width, "wh": self.height}

    def do_resize(self, event):
        if not self.resizing:
            return
        self.width = max(60, self.drag_data["ww"] + (event.x - self.drag_data["sx"]))
        self.height = max(50, self.drag_data["wh"] + (event.y - self.drag_data["sy"]))
        self.window.geometry(f"{self.width}x{self.height}+{self.x}+{self.y}")
        self.canvas.config(width=self.width, height=self.height)
        self._update_handle()
        if self.label:
            self.canvas.coords(self.label_id, self.width // 2, 15)
        self._notify_change()

    def _update_handle(self):
        self.canvas.coords(self.handle,
                           self.width - self.HANDLE, self.height - self.HANDLE,
                           self.width, self.height)
        self.canvas.coords(self.grip,
                           self.width - 13, self.height,
                           self.width, self.height - 13)

    def on_release(self, event=None):
        self.resizing = False
        if self.on_release_cb:
            self.on_release_cb(self.x, self.y, self.width, self.height)

    def _notify_change(self):
        if self.on_change:
            self.on_change(self.x, self.y, self.width, self.height)

    def get_region(self):
        return self.x, self.y, self.width, self.height

    def destroy(self):
        try:
            self.window.destroy()
        except Exception:
            pass


# ============================================================
#  控制面板（暂停 / 锁定 / 置顶 / 帮助 / 关闭）
# ============================================================

class ControlPanel:
    def __init__(self, root, on_pause, on_close, on_help,
                 on_toggle_top, on_toggle_lock,
                 x=10, y=10, on_release=None):
        self.root = root
        self.is_paused = False
        self.on_pause = on_pause
        self.on_close = on_close
        self.on_release_cb = on_release

        self.window = tk.Toplevel(root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", 0.92)
        self.window.geometry(f"+{x}+{y}")

        frame = tk.Frame(self.window, bg="gray20")
        frame.pack(fill=tk.BOTH, expand=True)

        self.pause_btn = self._btn(frame, "⏸", "orange", self.toggle_pause)
        self.pause_btn.pack(side=tk.LEFT, padx=2, pady=2)
        self.lock_btn = self._btn(frame, "🔒", "green", on_toggle_lock)
        self.lock_btn.pack(side=tk.LEFT, padx=2, pady=2)
        self._btn(frame, "📌", "blue", on_toggle_top).pack(side=tk.LEFT, padx=2, pady=2)
        self._btn(frame, "?", "purple", on_help).pack(side=tk.LEFT, padx=2, pady=2)
        self._btn(frame, "✕", "red", self.close).pack(side=tk.LEFT, padx=2, pady=2)

        self.status_label = tk.Label(
            frame, text="调整模式", bg="gray20", fg="yellow",
            font=("Microsoft YaHei", 8)
        )
        self.status_label.pack(side=tk.LEFT, padx=5)

        # 拖动面板（基准点方式，1:1）
        for w in (frame, self.status_label):
            w.bind("<Button-1>", self.start_drag)
            w.bind("<B1-Motion>", self.do_drag)
            w.bind("<ButtonRelease-1>", self.on_release)
        self.drag_data = {}

    def _btn(self, parent, text, bg, cmd):
        return tk.Button(
            parent, text=text, width=3, bg=bg, fg="white",
            font=("Arial", 10, "bold"), command=cmd, relief=tk.FLAT,
            activebackground=bg, bd=0
        )

    def start_drag(self, event):
        self.drag_data = {"sx": event.x, "sy": event.y,
                          "wx": self.window.winfo_x(),
                          "wy": self.window.winfo_y()}

    def do_drag(self, event):
        x = self.drag_data["wx"] + (event.x - self.drag_data["sx"])
        y = self.drag_data["wy"] + (event.y - self.drag_data["sy"])
        self.window.geometry(f"+{x}+{y}")

    def on_release(self, event=None):
        if self.on_release_cb:
            self.on_release_cb(self.window.winfo_x(), self.window.winfo_y())

    def toggle_pause(self):
        self.is_paused = not self.is_paused
        if self.is_paused:
            self.pause_btn.config(text="▶", bg="green")
            self.status_label.config(text="已暂停", fg="orange")
        else:
            self.pause_btn.config(text="⏸", bg="orange")
            self.status_label.config(text="运行中", fg="green")
        self.on_pause(self.is_paused)

    def set_lock(self, locked):
        """由应用调用：更新锁定按钮与状态"""
        if locked:
            self.lock_btn.config(text="🔓", bg="orange")
        else:
            self.lock_btn.config(text="🔒", bg="green")
        if not self.is_paused:
            self.status_label.config(text="运行中", fg="green")

    def close(self):
        self.on_close()

    def destroy(self):
        try:
            self.window.destroy()
        except Exception:
            pass


# ============================================================
#  帮助窗口
# ============================================================

class HelpWindow:
    def __init__(self, root):
        self.window = tk.Toplevel(root)
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", 0.95)
        self.window.geometry("380x360+150+150")

        frame = tk.Frame(self.window, bg="gray10")
        frame.pack(fill=tk.BOTH, expand=True)

        tk.Label(frame, text="屏幕数字比较工具",
                 bg="gray10", fg="cyan",
                 font=("Microsoft YaHei", 15, "bold")).pack(pady=10)

        help_text = """使用方法：

1. 拖动【红色框】到题目数字位置（如 "5 ? 3"）
   右下角黄块可调大小
2. 拖动【青色框】到要画符号的位置（答题框）
3. 程序后台静默识别，不在屏幕显示结果
4. 按【L 键】→ 现场抓帧识别当前题目并真实划线：
   鼠标按下-拖动-抬起，把 > < = 画到屏幕上
   （按 L 时保证画的是当前题目，不是上一题）

识别规则：
  - 题目中间的 ? 作为分隔符，左右各是一个数
  - 终端会打印识别到的题目和答案

控制按钮：
  ⏸ 暂停/继续     🔒 锁定/调整
  📌 置顶/取消      ? 帮助    ✕ 关闭

快捷键：L 划线（触发键可改 HOTKEY_VK）
        ESC 退出
"""
        tk.Label(frame, text=help_text, bg="gray10", fg="white",
                 font=("Microsoft YaHei", 10), justify=tk.LEFT).pack(padx=18, pady=5)

        tk.Button(frame, text="知道了", command=self.close,
                  bg="cyan", fg="black",
                  font=("Microsoft YaHei", 10, "bold")).pack(pady=8)

    def close(self):
        try:
            self.window.destroy()
        except Exception:
            pass


# ============================================================
#  主应用程序
# ============================================================

class ScreenCompareApp:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()

        self.is_paused = False
        self.is_running = True
        self.is_drawing = False
        self.locked = False
        self.draw_event = threading.Event()  # L 键触发
        self._last_hotkey_at = 0.0
        self.processing_thread = None
        self.process_interval = 0.02   # 20ms 一轮（极速扫描，截屏已提速无压力）
        self.draw_count = 0           # 已划线次数（用于定期内存报告）
        self.frame_count = 0          # 已处理帧数（用于定期 gc）

        self.config = Config()
        self.first_run = not self.config.exists()
        self.locked = bool(self.config.get("locked", False))

        # ---------- 自动监测（识别框内出现新题 → 等 30ms → 自动划线） ----------
        self.auto_monitor = True          # 自动监测开关
        self.auto_interval = 0.025        # 扫描周期（秒）：每帧扫描，新题即察觉
        self.confirm_delay = 0.01         # 察觉变化后 10ms 再识别（画面稳定即识别）
        self.last_frame_small = None      # 上次观察的缩略图（灰度 numpy）
        self._latest_full = None          # 最近一次扫描原图缓存（识别复用，省截图）
        self.pending_since = None         # 检测到画面变化的时刻
        self.last_done = None             # 上次已划线的题目 (n1, n2)
        self.last_done_at = 0.0           # 上次划线时刻（防识别弹跳用）
        self.max_number = 199             # 题目数字范围上限（100 以内题型）：
                                          # 超过视为识别错误（残影/反馈符号粘连），拒绝划线
        # 同一道题识别失败后的等待重试（等 2.5 秒再试一次，最多 max_retries 次）
        self.retry_delay = 2.5            # 同一题重试间隔（秒）
        self.max_retries = 5              # 同一题最多重试次数，超过则转补划循环
        self.retry_at = None              # 下次重试的时间戳（None = 无重试计划）
        self.retry_count = 0              # 同一题已重试次数
        # 补划机制：划线后 0.8 秒若画面仍无任何变化（软件没识别/没判定），
        # 重新识别并再划一次；识别到仍是同一道题（重复）则等 0.8 秒再划，
        # 直到软件有反应（画面变化/切题）。循环只在画面变化时终止
        self.redraw_at = None             # 补划触发时刻（None = 无补划计划）

        self.screen_capture = ScreenCapture()
        self.ocr_engine = OCREngine(confidence=0.62)
        self.mouse_drawer = MouseDrawer()
        self.hotkey = GlobalHotkey(vk_code=HOTKEY_VK, on_press=self._on_hotkey)

        self.setup_ui()
        self.apply_lock_state()
        self.hotkey.start()

        self.root.bind("<Escape>", lambda e: self.on_close())

    # ---------- UI ----------
    def setup_ui(self):
        sel = self.config.get("selection")
        if sel:
            sx, sy, sw, sh = sel["x"], sel["y"], sel["w"], sel["h"]
        else:
            sx, sy, sw, sh = 100, 200, 300, 100

        res = self.config.get("result")
        if res:
            rx, ry, rw, rh = res["x"], res["y"], res["w"], res["h"]
        else:
            rx, ry, rw, rh = 500, 200, 160, 90

        ctl = self.config.get("control")
        if ctl:
            cx, cy = ctl["x"], ctl["y"]
        else:
            cx, cy = 10, 10

        # 识别框（红色）
        self.selection_frame = DraggableFrame(
            self.root, sx, sy, sw, sh,
            color="red", label="识别区(拖动/右下角缩放)", alpha=0.35,
            on_release=lambda x, y, w, h: self._save_region("selection", x, y, w, h)
        )

        # 绘制框（青色，纯位置标记，不显示任何结果）
        self.result_frame = DraggableFrame(
            self.root, rx, ry, rw, rh,
            color="cyan", label="划线区(拖动/右下角缩放)", alpha=0.18,
            on_release=lambda x, y, w, h: self._save_region("result", x, y, w, h)
        )

        self.control_panel = ControlPanel(
            self.root,
            on_pause=self.on_pause,
            on_close=self.on_close,
            on_help=self.show_help,
            on_toggle_top=self.on_toggle_always_on_top,
            on_toggle_lock=self.toggle_lock,
            x=cx, y=cy,
            on_release=lambda x, y: self._save_control(x, y)
        )

        if self.first_run:
            self.help_window = HelpWindow(self.root)

    # ---------- 锁定模式（点击穿透） ----------
    def apply_lock_state(self):
        if self.locked:
            set_click_through(self.selection_frame.window, True)
            set_click_through(self.result_frame.window, True)
            self.selection_frame.window.attributes("-alpha", 0.15)
            self.result_frame.window.attributes("-alpha", 0.10)
        else:
            set_click_through(self.selection_frame.window, False)
            set_click_through(self.result_frame.window, False)
            self.selection_frame.window.attributes("-alpha", 0.35)
            self.result_frame.window.attributes("-alpha", 0.18)
        self.control_panel.set_lock(self.locked)

    def toggle_lock(self):
        self.locked = not self.locked
        self.config.set("locked", self.locked)
        self.config.save()
        self.apply_lock_state()
        print("[信息] 已锁定：框点击穿透，不挡鼠标" if self.locked
              else "[信息] 已解锁：可调整框的位置和大小")

    # ---------- 配置保存 ----------
    def _save_region(self, key, x, y, w, h):
        self.config.set(key, {"x": int(x), "y": int(y), "w": int(w), "h": int(h)})
        self.config.save()

    def _save_control(self, x, y):
        self.config.set("control", {"x": int(x), "y": int(y)})
        self.config.save()

    # ---------- 回调 ----------
    def on_pause(self, is_paused):
        self.is_paused = is_paused
        print("[信息] 已暂停" if is_paused else "[信息] 已继续")

    def _on_hotkey(self):
        """L 键按下（全局热键回调，运行在钩子线程）"""
        now = time.time()
        if now - self._last_hotkey_at < 0.5:  # 防连发
            return
        self._last_hotkey_at = now
        self.draw_event.set()

    def on_close(self):
        print("[信息] 正在关闭，保存位置...")
        self.is_running = False
        self.hotkey.stop()
        self._save_all_positions()
        if self.processing_thread and self.processing_thread.is_alive():
            self.processing_thread.join(timeout=2)
        self.selection_frame.destroy()
        self.result_frame.destroy()
        self.control_panel.destroy()
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass

    def _save_all_positions(self):
        self._save_region("selection", *self.selection_frame.get_region())
        self._save_region("result", *self.result_frame.get_region())
        self._save_control(
            self.control_panel.window.winfo_x(),
            self.control_panel.window.winfo_y()
        )

    def on_toggle_always_on_top(self):
        cur = self.selection_frame.window.attributes("-topmost")
        for w in (self.selection_frame.window,
                  self.result_frame.window,
                  self.control_panel.window):
            w.attributes("-topmost", not cur)

    def show_help(self):
        self.help_window = HelpWindow(self.root)

    # ---------- 真实划线（仅在 L 键按下时执行） ----------
    def draw_real(self, symbol, n1=None, n2=None):
        """用真实鼠标在划线区画出符号，并在终端打印识别到的题目"""
        if self.is_drawing or self.is_paused:
            return
        x, y, w, h = self.result_frame.get_region()
        if w < 30 or h < 30:
            return
        # 划线期间保证框不拦截鼠标（未锁定时临时点击穿透）
        if not self.locked:
            set_click_through(self.selection_frame.window, True)
            set_click_through(self.result_frame.window, True)
        self.is_drawing = True
        try:
            if n1 is not None and n2 is not None:
                print(f"[划线] 题目: {n1} ? {n2}  →  {n1} {symbol} {n2}")
            else:
                print(f"[划线] 画出 {symbol}")
            self.mouse_drawer.draw_symbol(symbol, x, y, w, h)
            self.draw_count += 1
            if self.draw_count % 100 == 0:
                print(f"[内存] 已划线 {self.draw_count} 次，当前占用 "
                      f"{self._get_memory_mb():.1f} MB")
        except Exception as e:
            print(f"[错误] 划线失败: {e}")
        finally:
            self.is_drawing = False
            if not self.locked:
                set_click_through(self.selection_frame.window, False)
                set_click_through(self.result_frame.window, False)
            # 清空扫描状态，划线结束后正常监测下一题；
            # 同时安排补划：0.8 秒后画面若无变化则再划一次（软件没识别时兜底）。
            # 每次划线（含补划）都重新安排 → 画面一直不动就一直补，直到软件有反应
            self.pending_since = None
            self.retry_at = None
            self.retry_count = 0
            self.redraw_at = time.time() + 0.8

    @staticmethod
    def _get_memory_mb():
        """获取当前进程内存占用（MB），用于长跑监控"""
        try:
            class P_M_C(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD),
                            ("PageFaultCount", wintypes.DWORD),
                            ("PeakWorkingSetSize", ctypes.c_size_t),
                            ("WorkingSetSize", ctypes.c_size_t),
                            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                            ("PagefileUsage", ctypes.c_size_t),
                            ("PeakPagefileUsage", ctypes.c_size_t)]
            fn = ctypes.windll.psapi.GetProcessMemoryInfo
            fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(P_M_C), wintypes.DWORD]
            fn.restype = wintypes.BOOL
            c = P_M_C()
            c.cb = ctypes.sizeof(P_M_C)
            fn(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb)
            return c.WorkingSetSize / 1024 / 1024
        except Exception:
            return 0.0

    # ---------- 按 L 键：现场抓帧识别（保证是当前题目的答案） ----------
    def draw_current(self):
        """
        按 L 时同步抓取当前屏幕识别并划线。
        不用缓存符号，彻底避免"画的是上一道题的答案"。
        识别不到时短暂重试（等待题目渲染完成）。
        """
        for attempt in range(5):
            try:
                x, y, w, h = self.selection_frame.get_region()
                if w <= 0 or h <= 0:
                    return
                image = self.screen_capture.capture_region(x, y, w, h)
                numbers = self.ocr_engine.extract_numbers(image)
                del image  # 释放帧内存
                if len(numbers) >= 2:
                    n1, n2 = numbers[0], numbers[1]
                    # 范围校验：三位数超出 100 以内题型 = 残影/反馈符号，
                    # 手动触发也不划错误答案
                    if n1 > self.max_number or n2 > self.max_number:
                        print(f"[提示] 识别出 {n1} ? {n2}（超出题目范围），跳过本次")
                        return
                    if n1 > n2:
                        symbol = ">"
                    elif n1 < n2:
                        symbol = "<"
                    else:
                        symbol = "="
                    self.draw_real(symbol, n1, n2)
                    return
            except Exception:
                pass
            time.sleep(0.08)  # 等题目渲染完成再试
        # 没有识别到 → 不执行任何操作（不使用缓存）
        print("[提示] 未能识别题目中的两个数字，跳过本次")

    # ---------- 自动监测：识别框内出现新题 → 等 confirm_delay → 自动划线 ----------
    CHANGE_THRESH = 30.0   # 缩略图 MSE 阈值：超过视为画面变化（新题出现）

    def _capture_both(self):
        """
        抓取识别框一次，同时产出：
          full  - 原尺寸 BGR 图（缓存供识别复用，省一次截图）
          small - 48x24 灰度缩略图（帧间变化比较）
        极速路径（mss 原生 grab + numpy 直转），开销约 1-3ms。
        """
        try:
            x, y, w, h = self.selection_frame.get_region()
            if w < 20 or h < 20:
                return None, None
            full = self.screen_capture.capture_region_fast(x, y, w, h)
            small = cv2.cvtColor(cv2.resize(full, (48, 24)), cv2.COLOR_BGR2GRAY)
            return full, small
        except Exception:
            return None, None

    @staticmethod
    def _mse(a, b):
        return float(np.mean((a.astype(np.int16) - b.astype(np.int16)) ** 2))

    def auto_scan(self):
        """一直扫描识别框：画面变化 = 新题出现，等 confirm_delay 后识别并自动划线"""
        if not self.auto_monitor or self.is_drawing or self.is_paused:
            return
        full, small = self._capture_both()
        if small is None:
            return
        if self.last_frame_small is None:
            self.last_frame_small = small
            self._latest_full = full   # 缓存首帧备用
            return

        mse = self._mse(small, self.last_frame_small)
        if mse >= self.CHANGE_THRESH:
            # 画面变化中（切题帧 / 对勾反馈动画 / 残影演变）：
            # 绝不识别——动画中间帧会把数字读成变体导致重复划线；
            # 等画面停止变化才识别。变化期间的吸收逻辑已移除，
            # 切题（单帧突变）下一帧即稳定，照常触发。
            # 有变化 = 软件有反应（切题/打勾）→ 取消补划
            self.retry_at = None
            self.retry_count = 0
            self.redraw_at = None
            if self.pending_since is None:
                self.pending_since = time.time()
            self.last_frame_small = small
            self._latest_full = full   # 缓存本帧，划过时免二次截图
            return

        # 画面稳定（无新变化）
        if self.pending_since is not None:
            # 变化结束后稳定且等待满 confirm_delay → 识别划线
            if time.time() - self.pending_since >= self.confirm_delay:
                self.pending_since = None
                self._auto_solve_and_draw(initial_img=full)
        elif self.retry_at is not None:
            # 同一道题识别失败：等 2.5 秒后再试一次
            if time.time() >= self.retry_at:
                self._auto_solve_and_draw(initial_img=full)
        elif self.redraw_at is not None:
            # 补划到点：画面 0.8 秒无变化 = 软件没识别/没判定，
            # 重新识别并再划一次；划线会重新安排下一次（直到画面变化）；
            # 识别失败/超范围则 0.8 秒后再试（循环不中断）
            if time.time() >= self.redraw_at:
                self.redraw_at = None
                self._auto_solve_and_draw(initial_img=full, force_redraw=True)
                if self.redraw_at is None:
                    # 补划未划线（识别失败/914），画面仍未变 → 继续循环
                    self.redraw_at = time.time() + 0.8
        self.last_frame_small = small

    def _auto_solve_and_draw(self, initial_img=None, force_redraw=False):
        """自动模式下识别划线；与上次已划题目相同则跳过（防重复）；
        首次识别复用扫描帧原图（省一次截图），失败则快速重试，
        仍失败则等 2.5 秒再试（最多 max_retries 次）。
        force_redraw=True（补划路径）：画面静止 0.8s 无变化，无条件再划
        （OCR 抖动/残留变体也照划，不中断循环——直到软件有反应）； 
        914/超范围仍拒绝，循环继续等待下一次。"""
        for attempt in range(2):
            try:
                if initial_img is not None:
                    image = initial_img
                    initial_img = None   # 仅首轮复用
                else:
                    x, y, w, h = self.selection_frame.get_region()
                    if w <= 0 or h <= 0:
                        return
                    image = self.screen_capture.capture_region_fast(x, y, w, h)
                numbers = self.ocr_engine.extract_numbers(image)
                del image  # 释放帧内存
                if len(numbers) >= 2:
                    n1, n2 = numbers[0], numbers[1]
                    # 范围校验：100 以内题型读到三位数 = 残影/反馈符号粘连，
                    # 拒绝划线，等画面变化后重新识别
                    if n1 > self.max_number or n2 > self.max_number:
                        print(f"[自动] 识别出 {n1} ? {n2}（超出题目范围），忽略，等待重识别")
                        return
                    if self.last_done != (n1, n2) or force_redraw:
                        # 差异处理：去重 + 范围校验已在上文完成，此处划线
                        self.last_done = (n1, n2)
                        self.last_done_at = time.time()
                        if n1 > n2:
                            symbol = ">"
                        elif n1 < n2:
                            symbol = "<"
                        else:
                            symbol = "="
                        self.draw_real(symbol, n1, n2)
                    else:
                        # 同题去重：画面有过变化却仍读到上一题（反馈动画后
                        # 残留/软件吃了线但没切题）→ 保持补划循环：
                        # 0.8 秒后再划一次，直到软件有反应（画面变化）。
                        # 补划路径（force）不经过这里——无条件再划
                        if self.redraw_at is None:
                            self.redraw_at = time.time() + 0.8
                    # 划线成功 → 清除重试计划
                    self.retry_at = None
                    self.retry_count = 0
                    return
            except Exception:
                pass
            time.sleep(0.03)  # 首帧失败 30ms 后再试（通常只在切题动画时发生）
        # 同一道题识别失败（可能只是画面闪烁/题目还没渲染好）：
        # 等 retry_delay 秒后再试一次；多次失败后转入补划循环
        #（每 0.8 秒再试，直到画面变化），不再静默跳过
        self.retry_count += 1
        if self.retry_count >= self.max_retries:
            print("[自动] 同一题多次识别失败，转补划循环（每 0.8 秒再试直到画面变化）")
            self.retry_count = 0
            self.retry_at = None
            self._latest_full = None
            self.last_frame_small = self._capture_both()[1]  # 重新锚定
            self.redraw_at = time.time() + 0.8
        else:
            self.retry_at = time.time() + self.retry_delay

    # ---------- 识别主循环（后台线程） ----------
    def process_screen(self):
        import gc
        while self.is_running:
            # L 键触发：现场抓帧识别（无缓存），识别不到就不操作
            if self.draw_event.is_set():
                self.draw_event.clear()
                if not self.is_paused and not self.is_drawing:
                    self.draw_current()

            # 长跑内存卫生：定期强制回收
            self.frame_count += 1
            if self.frame_count % 300 == 0:
                gc.collect()

            # 自动监测：每帧扫描识别框（约 0.06s 察觉新题 → 0.1s 后划线）
            self.auto_scan()

            time.sleep(self.process_interval)

    # ---------- 运行 ----------
    def run(self):
        print("[信息] 屏幕数字比较工具 v4 已启动")
        print("[信息] 拖动红框到题目数字、青框到答题位置")
        if self.auto_monitor:
            print(f"[信息] 自动监测已开启（极速）：新题出现 → {self.confirm_delay * 1000:.0f}ms 后自动划线")
        print(f"[信息] 兜底控制：按【{chr(HOTKEY_VK)}】键手动触发 → 真实模拟鼠标划线")
        print("[信息] 按 ESC 或点 ✕ 退出")
        self.processing_thread = threading.Thread(
            target=self.process_screen, daemon=True
        )
        self.processing_thread.start()
        self.root.mainloop()


# ============================================================
#  入口
# ============================================================

def main():
    set_dpi_aware()  # 必须在创建窗口前设置，保证坐标一致
    # 提升定时器精度到 1ms：让划线每步 sleep 精确，轨迹平滑自然（默认 15.6ms 会一步一顿）
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        pass

    print("=" * 50)
    print("  屏幕数字比较识别工具 v4")
    print("=" * 50)
    print()
    print("功能说明:")
    print("  - 后台静默识别数字表达式，不在屏幕显示结果")
    print("  - 【极速自动监测】识别框出现新题 → 0.05 秒后自动划线（全程约 0.2s）")
    print(f"  - 兜底控制：按【{chr(HOTKEY_VK)}】键手动触发（按下-拖动-抬起）")
    print("  - 框可拖动缩放（1:1 精确跟随），位置自动保存")
    print("  - 🔒 锁定后点击穿透，不挡鼠标")
    print()
    print("启动中...")
    print()

    app = ScreenCompareApp()
    app.run()


if __name__ == "__main__":
    main()
