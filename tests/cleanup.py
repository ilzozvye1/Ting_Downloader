"""
清理重复文件 + 验证最终结果
"""
import os, re

OUTPUT_DIR = r"e:\GitHub\AudioBook_dl\audiobook-dl\tests\download_output"

# 章节期望列表 (chapters 1-30)
expected = set()
for i in range(1, 31):
    expected.add(i)

# 根据文件名中的数字前缀匹配章节号
files = [f for f in os.listdir(OUTPUT_DIR) if f.endswith('.mp3')]

# 构建 (章节号, 文件路径) 映射
chapter_files = {}
orphans = []

for f in files:
    fpath = os.path.join(OUTPUT_DIR, f)
    sz = os.path.getsize(fpath)
    m = re.match(r'^(\d{4})_', f)
    if m:
        num = int(m.group(1))
        if num in chapter_files:
            existing_sz = chapter_files[num][1]
            if sz > existing_sz:
                os.remove(chapter_files[num][0])
                chapter_files[num] = (fpath, sz, f)
            else:
                os.remove(fpath)
        else:
            chapter_files[num] = (fpath, sz, f)
    else:
        # 可能是非标编号,通过标题内容推断
        num_match = re.match(r'^(\d+)_', f)
        if num_match:
            num = int(num_match.group(1))
            if 1 <= num <= 30:
                clean_name = re.sub(r'^\d+_', '', f)
                # 查找是否有 0000 格式的同名
                found = False
                for cn, (cp, csz, cn_name) in list(chapter_files.items()):
                    if re.sub(r'^\d{4}_', '', cn_name) == clean_name:
                        if sz > csz:
                            os.remove(cp)
                            chapter_files[cn] = (fpath, sz, f)
                        else:
                            os.remove(fpath)
                        found = True
                        break
                if not found:
                    orphans.append((fpath, sz, f))
            else:
                orphans.append((fpath, sz, f))
        else:
            orphans.append((fpath, sz, f))

print(f"📊 最终统计")
print(f"   章节文件: {len(chapter_files)}")
print(f"   孤儿文件: {len(orphans)}")

print(f"\n   已下载章节:")
downloaded = set(chapter_files.keys())
for num in sorted(downloaded):
    path, sz, name = chapter_files[num]
    print(f"   [{num:02d}] {name} ({sz/1024:.0f}KB)")

missing = expected - downloaded
if missing:
    print(f"\n   ❌ 缺失: {missing}")
else:
    print(f"\n   ✅ 30集全部下载!")

# 清理孤儿
for opath, osz, oname in orphans:
    os.remove(opath)
    print(f"   🗑️  删除孤儿: {oname}")

total = sum(sz for p, sz, n in chapter_files.values())
print(f"\n   📦 总计: {len(chapter_files)} 集, {total/1024/1024:.1f} MB")

print(f"\n✅ 清理完成")