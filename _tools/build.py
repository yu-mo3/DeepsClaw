import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dxlib, pg1, pg2, pg3, pg3b, pg4, pg5

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 图表分组：文件名 -> 页列表（顺序即页签顺序）
GROUPS = [
    ("DeepsClaw-框架图.drawio",   [pg1.build(), pg5.build()]),
    ("DeepsClaw-业务流程图.drawio", [pg2.build(), pg3.build(), pg3b.build(), pg4.build()]),
    ("DeepsClaw-技术栈.drawio",   [pg5.build()]),
]

def tw(s):
    w = 0
    for ch in s:
        w += 2 if ord(ch) > 0x2000 and ch not in "·—―" else 1
    return w

total_pages = 0
total_cells = 0
problems = []
for fname, pages in GROUPS:
    out = os.path.join(ROOT, fname)
    dxlib.write(pages, out)
    total_pages += len(pages)
    for p in pages:
        total_cells += len(p.geom)
        for cid, (x, y, w, h, label) in p.geom.items():
            if x < 0 or y < 0:
                problems.append((fname, p.name, cid, "negative origin", label[:30]))
            if x + w > 1620:
                problems.append((fname, p.name, cid, "exceeds page width", label[:40]))
            if label and tw(max(label.split(chr(10)), key=tw)) > w - 12:
                problems.append((fname, p.name, cid, "text overflow", label.split(chr(10))[0][:30]))
        items = list(p.geom.items())
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a, b = items[i][1], items[j][1]
                azone = a[4].startswith(tuple("①②③④⑤⑥⑦⑧")) or a[2] >= 560 or a[3] >= 320
                bzone = b[4].startswith(tuple("①②③④⑤⑥⑦⑧")) or b[2] >= 560 or b[3] >= 320
                if azone or bzone:
                    continue
                ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
                if a[0] < bx2 and b[0] < ax2 and a[1] < by2 and b[1] < ay2:
                    small = min(a[2] * a[3], b[2] * b[3])
                    if small < 60000:
                        problems.append((fname, p.name, items[i][0], items[j][0],
                                         "OVERLAP: " + a[4].split(chr(10))[0][:24] + " <> " + b[4].split(chr(10))[0][:24]))
    print("written: %-28s pages=%d bytes=%d" % (fname, len(pages), os.path.getsize(out)))

print("groups=%d pages=%d cells=%d problems=%d" % (len(GROUPS), total_pages, total_cells, len(problems)))
for pr in problems[:30]:
    print("  !", pr)
