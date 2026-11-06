import xml.etree.ElementTree as ET, os, re, glob
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "_tools", "preview")
os.makedirs(OUT, exist_ok=True)
for old in glob.glob(os.path.join(OUT, "*.png")):
    os.remove(old)

FILL = {"dae8fc": (218,232,252), "d5e8d4": (213,232,212), "ffe6cc": (255,230,204),
        "e1d5e7": (225,213,231), "fff2cc": (255,242,204), "f8cecc": (248,206,204),
        "f5f5f5": (245,245,245), "b1ddf0": (177,221,240), "ffff88": (255,255,136)}
STROKE = {"6c8ebf": (108,142,191), "82b366": (130,179,102), "d79b00": (215,155,0),
          "9673a6": (150,115,166), "d6b656": (214,182,86), "b85450": (184,84,80),
          "666666": (102,102,102), "10739e": (16,115,158), "36393d": (54,57,61)}

def colour(style, table, dflt):
    m = re.search(r"(?:fillColor|strokeColor)=#([0-9a-fA-F]{6})", style or "")
    return table.get(m.group(1).lower(), dflt) if m else dflt

scale = 0.62
for f in sorted(glob.glob(os.path.join(ROOT, "DeepsClaw-*.drawio"))):
    base = os.path.basename(f)[:-len(".drawio")]
    for idx, diagram in enumerate(ET.parse(f).getroot().findall("diagram"), start=1):
        cells = diagram.findall(".//mxCell[@vertex='1']")
        img = Image.new("RGB", (int(1700*scale), int(1180*scale)), (255,255,255))
        d = ImageDraw.Draw(img)
        for c in cells:
            g = c.find("mxGeometry")
            x, y, w, h = (float(g.get(k)) for k in ("x","y","width","height"))
            st = c.get("style") or ""
            txt = (c.get("value") or "").replace("&#10;", chr(10))
            fill = colour(st, FILL, None) or (255,255,255)
            stroke = colour(st, STROKE, (60,60,60))
            box = [x*scale, y*scale, (x+w)*scale, (y+h)*scale]
            if "ellipse" in st:
                d.ellipse(box, fill=fill, outline=stroke)
            elif "rhombus" in st:
                cx, cy = (box[0]+box[2])/2, (box[1]+box[3])/2
                d.polygon([(cx,box[1]),(box[2],cy),(cx,box[3]),(box[0],cy)], fill=fill, outline=stroke)
            elif "hexagon" in st:
                k = (box[2]-box[0])*0.15
                d.polygon([(box[0]+k,box[1]),(box[2]-k,box[1]),(box[2],(box[1]+box[3])/2),
                           (box[2]-k,box[3]),(box[0]+k,box[3]),(box[0],(box[1]+box[3])/2)], fill=fill, outline=stroke)
            else:
                d.rectangle(box, fill=fill, outline=stroke)
            if txt and len(txt) < 90:
                d.multiline_text((box[0]+3, box[1]+2), txt, fill=(20,20,20))
        img.save(os.path.join(OUT, "%s-%d_%s.png" % (base, idx, diagram.get("id"))))
        print("rendered", base, "/", diagram.get("id"), "cells", len(cells))
