import xml.etree.ElementTree as ET, glob, os
ok = True
for f in sorted(glob.glob(r"C:\Users\我\Desktop\hqrj\project\DeepsClaw\DeepsClaw-*.drawio")):
    root = ET.parse(f).getroot()
    print(os.path.basename(f))
    for d in root.findall("diagram"):
        ids = {c.get("id") for c in d.findall(".//mxCell")}
        bad = []
        for e in d.findall(".//mxCell[@edge='1']"):
            for k in ("source", "target"):
                v = e.get(k)
                if v and v not in ids:
                    bad.append((e.get("id"), k, v))
        if "0" not in ids or "1" not in ids:
            bad.append("missing root cell")
        print("   %-22s cells=%-4d edges=%-3d dangling=%d" % (d.get("id"), len(ids),
              len(d.findall(".//mxCell[@edge='1']")), len(bad)))
        if bad:
            ok = False
            print("      !!", bad[:4])
print("XML structure OK" if ok else "XML structure HAS PROBLEMS")
