"""
集成测试: 通过 DownloadEngine 下载 yuetingba.cn 30集
完全模拟 CLI 入口的调用流程
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ting13.sources import find_source
from ting13.core.models import BookInfo, Chapter
from ting13.core.download import DownloadEngine, DownloadCallbacks

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "download_output")
os.makedirs(OUTPUT_DIR, exist_ok=True)

BOOK_URL = "http://www.yuetingba.cn/book/detail/3a20a667-3381-cb4f-f597-92364e9a0039/0"

print("=" * 70)
print("  集成测试: DownloadEngine + yuetingba.cn")
print("=" * 70)

# Step 1: 识别站点
print(f"\n📖 Step 1: find_source")
source = find_source(BOOK_URL)
print(f"   ✅ 识别为: {source.names[0]}")

# Step 2: 判断URL类型
url_type = source.detect_url_type(BOOK_URL)
print(f"   ✅ URL类型: {url_type}")

# Step 3: 解析
print(f"\n📖 Step 2: parse_book")
book = source.parse_book(BOOK_URL)
print(f"   ✅ {book.title} ({len(book.chapters)}集)")
print(f"   作者: {book.author}, 演播: {book.announcer}")

# 只取前30集
book.chapters = book.chapters[:30]
print(f"   下载范围: 第1~30集")

# Step 4: 创建回调
status_msgs = []

def on_log(msg):
    print(msg)

def on_status(text):
    status_msgs.append(text)

def on_info(text):
    print(f"[info] {text}")

def on_progress(val, label):
    pass  # 静默

callbacks = DownloadCallbacks(
    on_log=on_log,
    on_status=on_status,
    on_info=on_info,
    on_progress=on_progress,
    is_stopped=lambda: False,
)

# Step 5: 创建引擎并运行
print(f"\n📥 Step 3: 下载 (DownloadEngine)")
print("-" * 50)

engine = DownloadEngine(
    source=source,
    callbacks=callbacks,
    clash_rotator=None,
    proxy_pool=None,
    rotate_interval=0,
    download_workers=1,
    url_fetch_workers=1,
    fast_mode=False,
    batch_fetch=False,
    extra_delay=0.0,
)

engine.run(book, OUTPUT_DIR, start=1, end=30)

# Step 6: 验证
from ting13.core.utils import sanitize_filename

book_dir = os.path.join(OUTPUT_DIR, sanitize_filename(book.title))

print(f"\n\n🔍 验证结果")
print("=" * 50)
files = [f for f in os.listdir(book_dir) if f.endswith('.mp3') and not f.startswith('.')]
total_sz = sum(os.path.getsize(os.path.join(book_dir, f)) for f in files)

# 按章节号统计
chapters_have = []
for f in sorted(files):
    import re
    m = re.match(r'^(\d+)_', f)
    if m:
        chapters_have.append(int(m.group(1)))
    sz = os.path.getsize(os.path.join(book_dir, f))
    print(f"   {f} ({sz/1024:.0f}KB)")

print(f"\n   ✅ 已下载 {len(files)} 个文件, 总大小 {total_sz/1024/1024:.1f} MB")

missing = sorted(set(range(1, 31)) - set(chapters_have))
if missing:
    print(f"   ❌ 缺失: {missing}")
else:
    print(f"   🎉 全部30集下载成功!")

print(f"\n✅ 集成测试完成")