"""兼容模块：转发到新的 workers 位置。"""

import os
import sys

BASE_DIR = os.path.dirname(__file__)
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, os.path.abspath(os.path.join(BASE_DIR, "..")))

from ting13.workers.ting13_worker import worker_parse, worker_download

__all__ = ["worker_parse", "worker_download"]
