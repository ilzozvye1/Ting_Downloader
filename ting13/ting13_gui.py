#!/usr/bin/env python3
"""兼容入口：转发到新的 GUI(v4) 位置。"""

import os
import sys

BASE_DIR = os.path.dirname(__file__)
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.abspath(os.path.join(BASE_DIR, "..")))

from ting13.apps.gui_v4.main import main


if __name__ == "__main__":
    main()
