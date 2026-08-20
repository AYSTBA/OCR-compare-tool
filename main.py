"""
屏幕数字比较识别工具（入口）

功能与 main_simple.py 完全相同（内置识别，无需安装任何 OCR 引擎，
位置自动记忆）。运行本文件等价于运行 main_simple.py。

使用方式:
    python main.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main_simple import main

if __name__ == "__main__":
    main()
