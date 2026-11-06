import html

S = {
 "title":"text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;fontSize=22;fontStyle=1;fontColor=#1f3b57;",
 "sub":"text;html=1;strokeColor=none;fillColor=none;align=left;verticalAlign=middle;fontSize=13;fontColor=#5a5a5a;",
 "lane":"rounded=0;whiteSpace=wrap;html=1;fillColor=none;strokeColor=#9a9a9a;dashed=1;verticalAlign=top;fontSize=15;fontStyle=1;fontColor=#333333;spacingTop=8;",
 "user":"rounded=1;arcSize=40;whiteSpace=wrap;html=1;fillColor=#d5e8d4;strokeColor=#82b366;fontSize=13;",
 "chan":"rounded=1;arcSize=40;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;fontSize=13;",
 "bus":"rounded=1;arcSize=40;whiteSpace=wrap;html=1;fillColor=#b1ddf0;strokeColor=#10739e;fontSize=13;",
 "gw":"rounded=1;arcSize=40;whiteSpace=wrap;html=1;fillColor=#ffe6cc;strokeColor=#d79b00;fontSize=13;",
 "ag":"rounded=1;arcSize=40;whiteSpace=wrap;html=1;fillColor=#e1d5e7;strokeColor=#9673a6;fontSize=13;",
 "md":"rounded=1;arcSize=40;whiteSpace=wrap;html=1;fillColor=#fff2cc;strokeColor=#d6b656;fontSize=13;",
 "dec":"rhombus;whiteSpace=wrap;html=1;fillColor=#ffe6cc;strokeColor=#b85450;fontSize=12;",
 "stop":"rounded=1;arcSize=50;whiteSpace=wrap;html=1;fillColor=#f8cecc;strokeColor=#b85450;fontSize=12;",
 "note":"shape=note;whiteSpace=wrap;html=1;backgroundOutline=1;darkOpacity=0.05;size=14;fillColor=#ffff88;strokeColor=#36393d;fontSize=11;align=left;verticalAlign=top;spacingTop=6;",
 "head":"rounded=0;whiteSpace=wrap;html=1;fillColor=#f2f2f2;strokeColor=#b3b3b3;fontSize=12;",
}
E  = "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;fontSize=11;strokeColor=#333333;"
EB = "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;fontSize=11;strokeColor=#10739e;"
ER = "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;fontSize=11;strokeColor=#b85450;dashed=1;"
EG = "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;fontSize=11;strokeColor=#82b366;"
EO = "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;fontSize=11;strokeColor=#d79b00;"
EY = "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;fontSize=11;strokeColor=#d6b656;"
EP = "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;fontSize=11;strokeColor=#9673a6;"
EF = "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;fontSize=11;strokeColor=#333333;dashed=1;"

def q(s):
    return html.escape(s, quote=True).replace(chr(10), "&#10;")

def cjk_width(s):
    w = 0
    for ch in s:
        w += 2 if ord(ch) > 0x2000 and ch not in "·—―" else 1
    return w

class Page:
    def __init__(self, name, title, sub):
        self.name, self.title, self.sub = name, title, sub
        self.cells, self.edges, self.n = [], [], 1
    def A(self, x, y, w, h, st, *lines, **kw):
        cid = "n%d" % self.n; self.n += 1
        style = S[st]
        base = 13
        import re
        m = re.search(r"fontSize=(\d+)", style)
        if m: base = int(m.group(1))
        if kw.get("fit", True) and not style.startswith("text;"):
            need = max((cjk_width(ln) for ln in lines), default=0)
            size = base
            while size > 7 and need * (size / float(base)) > w - 16:
                size -= 1
            if size != base:
                style += "fontSize=%d;" % size
        self.cells.append('        <mxCell id="%s" value="%s" style="%s" vertex="1" parent="1">' % (cid, q(chr(10).join(lines)), style))
        self.cells.append('          <mxGeometry x="%s" y="%s" width="%s" height="%s" as="geometry" />' % (x, y, w, h))
        self.cells.append('        </mxCell>')
        return cid
    def L(self, a, b, label="", st=None):
        eid = "e%d" % self.n; self.n += 1
        self.edges.append('        <mxCell id="%s" value="%s" style="%s" edge="1" parent="1" source="%s" target="%s">' % (eid, q(label), st or E, a, b))
        self.edges.append('          <mxGeometry relative="1" as="geometry" />')
        self.edges.append('        </mxCell>')
    def xml(self):
        o = ['  <diagram id="%s" name="%s">' % (self.name, self.name)]
        o.append('    <mxGraphModel dx="1600" dy="900" grid="1" gridSize="10" guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" pageScale="1" pageWidth="2300" pageHeight="1700" math="0" shadow="0">')
        o.append('      <root>')
        o.append('        <mxCell id="0" />')
        o.append('        <mxCell id="1" parent="0" />')
        o.extend(self.cells); o.extend(self.edges)
        o.append('      </root>')
        o.append('    </mxGraphModel>')
        o.append('  </diagram>')
        return o

def write(pages, out):
    doc = ['<mxfile host="draw.io" agent="DeepsClaw" version="24.0.0" type="device">']
    for p in pages:
        doc.extend(p.xml())
    doc.append('</mxfile>')
    open(out, "w", encoding="utf-8", newline=chr(10)).write(chr(10).join(doc) + chr(10))
    return out
