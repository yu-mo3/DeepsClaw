
import html, json, os, re, sys

S = {
 "title": "text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;fontSize=20;fontStyle=1;fontColor=#22405f;",
 "sub":   "text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;fontSize=12;fontColor=#666666;",
 "zone":  "rounded=0;whiteSpace=wrap;html=1;fillColor=none;strokeColor=#8c8c8c;dashed=1;verticalAlign=top;fontSize=13;fontStyle=1;fontColor=#555555;spacingTop=6;",
 "box":   "rounded=0;whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#666666;fontSize=12;",
 "blue":  "rounded=0;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;fontSize=12;",
 "green": "rounded=0;whiteSpace=wrap;html=1;fillColor=#d5e8d4;strokeColor=#82b366;fontSize=12;",
 "orange":"rounded=0;whiteSpace=wrap;html=1;fillColor=#ffe6cc;strokeColor=#d79b00;fontSize=13;",
 "purple":"rounded=0;whiteSpace=wrap;html=1;fillColor=#e1d5e7;strokeColor=#9673a6;fontSize=12;",
 "yellow":"rounded=0;whiteSpace=wrap;html=1;fillColor=#fff2cc;strokeColor=#d6b656;fontSize=11;",
 "red":   "rounded=0;whiteSpace=wrap;html=1;fillColor=#f8cecc;strokeColor=#b85450;fontSize=11;",
 "ext":   "rounded=0;whiteSpace=wrap;html=1;fillColor=#f5f5f5;strokeColor=#666666;dashed=1;fontSize=11;",
 "hex":   "shape=hexagon;perimeter=hexagonPerimeter2;whiteSpace=wrap;html=1;fixedSize=1;size=20;fillColor=#b1ddf0;strokeColor=#10739e;fontSize=13;fontStyle=1;",
 "fin":   "ellipse;whiteSpace=wrap;html=1;fillColor=#f8cecc;strokeColor=#b85450;fontSize=12;",
 "begin": "ellipse;whiteSpace=wrap;html=1;fillColor=#d5e8d4;strokeColor=#82b366;fontSize=12;",
 "dec":   "rhombus;whiteSpace=wrap;html=1;fillColor=#ffe6cc;strokeColor=#d79b00;fontSize=11;",
 "note":  "shape=note;whiteSpace=wrap;html=1;backgroundOutline=1;darkOpacity=0.05;size=14;fillColor=#ffff88;strokeColor=#36393d;fontSize=10;align=left;verticalAlign=top;spacingTop=4;",
 "proc":  "rounded=0;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;fontSize=11;",
 "cyl":   "shape=cylinder3;whiteSpace=wrap;html=1;boundedLbl=1;backgroundOutline=1;size=12;fillColor=#f8cecc;strokeColor=#b85450;fontSize=11;",
 "cyl2":  "shape=cylinder3;whiteSpace=wrap;html=1;boundedLbl=1;backgroundOutline=1;size=12;fillColor=#ffe6cc;strokeColor=#d79b00;fontSize=11;",
}
E  = "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;fontSize=10;strokeColor=#333333;"
EB = "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;fontSize=10;strokeColor=#10739e;"
EG = "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;fontSize=10;strokeColor=#82b366;"
ER = "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;fontSize=10;strokeColor=#b85450;dashed=1;"

def cjk_width(s):
    w = 0
    for ch in s:
        w += 2 if ord(ch) > 0x2000 and ch not in "·—―" else 1
    return w

def q(s):
    return html.escape(s, quote=True).replace(chr(10), "&#10;")

class Page:
    def __init__(self, name, title, sub):
        self.name, self.title, self.sub = name, title, sub
        self.cells, self.edges, self.geom, self.cid = [], [], {}, 1
    def A(self, x, y, w, h, st, *lines):
        cid = "n%d" % self.cid; self.cid += 1
        label = chr(10).join(lines)
        style = S[st]
        base = 12
        m = re.search(r"fontSize=(\d+)", style)
        if m:
            base = int(m.group(1))
        if not style.startswith("text;"):
            need = max((cjk_width(ln) for ln in lines), default=0)
            avail = w - 14
            size = base
            while size > 7 and need * (size / float(base)) > avail:
                size -= 1
            if size != base:
                style = style + "fontSize=%d;" % size
        self.cells.append('        <mxCell id="%s" value="%s" style="%s" vertex="1" parent="1">' % (cid, q(label), style))
        self.cells.append('          <mxGeometry x="%s" y="%s" width="%s" height="%s" as="geometry" />' % (x, y, w, h))
        self.cells.append('        </mxCell>')
        self.geom[cid] = (x, y, w, h, label)
        return cid
    def L(self, a, b, label="", st=None):
        eid = "e%d" % self.cid; self.cid += 1
        self.edges.append('        <mxCell id="%s" value="%s" style="%s" edge="1" parent="1" source="%s" target="%s">' % (eid, q(label), st or E, a, b))
        self.edges.append('          <mxGeometry relative="1" as="geometry" />')
        self.edges.append('        </mxCell>')
    def head(self):
        self.A(40, 12, 1300, 32, "title", self.title)
        self.A(40, 46, 1580, 24, "sub", self.sub)
    def xml(self):
        o = ['  <diagram id="%s" name="%s">' % (self.name, self.name)]
        o.append('    <mxGraphModel dx="1400" dy="800" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="1700" pageHeight="1300" math="0" shadow="0">')
        o.append('      <root>')
        o.append('        <mxCell id="0" />')
        o.append('        <mxCell id="1" parent="0" />')
        o.extend(self.cells); o.extend(self.edges)
        o.append('      </root>')
        o.append('    </mxGraphModel>')
        o.append('  </diagram>')
        return o

def write(pages, out):
    doc = ['<mxfile host="DeepsClaw" modified="2026-01-01T00:00:00.000Z" agent="DeepsClaw-doc" version="24.0.0" type="device">']
    for p in pages:
        doc.extend(p.xml())
    doc.append('</mxfile>')
    with open(out, "w", encoding="utf-8", newline=chr(10)) as f:
        f.write(chr(10).join(doc) + chr(10))
    return out