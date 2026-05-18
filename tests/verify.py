"""
精确验证: 按标题匹配文件
"""
import os, re

OUTPUT_DIR = r"e:\GitHub\AudioBook_dl\audiobook-dl\tests\download_output"

# 需要的30个章节标题（从v3测试输出）
expected_titles = [
    "0001_序章 缘起",
    "0002_寻仙",
    "0003_拒仙（1）",
    "0004_拒仙（2）",
    "0005_不信仙（1）",
    "0006_不信仙（2）",
    "0007_闯院",
    "0008_方士（1）",
    "0009_方士（2）",
    "0010_除虎（1）",
    "0011_除虎（2）",
    "0012_除虎（3）",
    "0013_出山",
    "0014_道观（1）",
    "0015_道观（2）",
    "0016_蛛妖",
    "0017_相助（1）",
    "0018_相助（2）",
    "0019_责任（1）",
    "0020_责任（2）",
    "0021_悬榜（1）",
    "0022_悬榜（2）",
    "0023_查案",
    "0024_道姑（1）",
    "0025_道姑（2）",
    "0026_人心（1）",
    "0027_人心（2）",
    "0028_惊变",
    "0029_明河（1）",
    "0030_明河（2）",
]

files = [f for f in os.listdir(OUTPUT_DIR) if f.endswith('.mp3')]

# 按照标题中的核心部分匹配
def extract_core(name):
    """提取核心标题（去掉前面的序号前缀部分）"""
    # 文件名格式可能是: "0001_序章 缘起.mp3" 或 "0001_0002_寻仙.mp3"
    name = name.replace('.mp3', '')
    # 尝试去掉0或1个数字前缀
    m = re.match(r'^(\d{4})_(.*)', name)
    if m:
        rest = m.group(2)
        # 如果rest也以数字开头，说明是双重编号(如0001_0002_寻仙)
        m2 = re.match(r'^(\d{4})_(.*)', rest)
        if m2:
            return m2.group(2)
        return rest
    return name

results = {}
for num, title in enumerate(expected_titles, 1):
    core = extract_core(title)
    best_file = None
    best_size = 0
    for f in files:
        fpath = os.path.join(OUTPUT_DIR, f)
        fcore = extract_core(f)
        if fcore == core:
            sz = os.path.getsize(fpath)
            if sz > best_size:
                best_size = sz
                best_file = (fpath, sz)
    results[num] = {
        "title": title,
        "file": best_file[0].replace(OUTPUT_DIR + '\\', '') if best_file else None,
        "size": best_file[1] if best_file else 0,
    }

print("📊 精确验证结果")
print("=" * 60)
has = 0
missing = []
for num in sorted(results):
    r = results[num]
    if r["file"]:
        print(f"   [{num:02d}] ✅ {r['title']} → {r['file']} ({r['size']/1024:.0f}KB)")
        has += 1
    else:
        print(f"   [{num:02d}] ❌ {r['title']} → MISSING")
        missing.append(num)

print(f"\n   ✅ {has}/30 集  |  ❌ {len(missing)}/30 缺失")

# 找出无用的文件
used_files = set(r["file"] for r in results.values() if r["file"])
for f in files:
    if f not in used_files:
        fpath = os.path.join(OUTPUT_DIR, f)
        sz = os.path.getsize(fpath)
        print(f"   🗑️  多余: {f} ({sz/1024:.0f}KB)")
        os.remove(fpath)

print(f"\n✅ 验证完成")