#!/usr/bin/env python3
"""兼容模块：转发到新的 legacy 位置。"""

import os
import sys

BASE_DIR = os.path.dirname(__file__)
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.abspath(os.path.join(BASE_DIR, "..")))

from ting13.legacy.ting13_downloader import *  # noqa: F401,F403
from ting13.legacy.ting13_downloader import main


if __name__ == "__main__":
    main()
