# -*- mode: python ; coding: utf-8 -*-
import os

THIS_DIR = os.path.dirname(os.path.abspath(__file__))
SPEC_PATH = os.path.join(THIS_DIR, "packaging", "ting13_gui.spec")

with open(SPEC_PATH, "r", encoding="utf-8") as f:
    exec(compile(f.read(), SPEC_PATH, "exec"))
