#!/usr/bin/env python3
"""生成两行文字快捷方式图标的矢量母版；不设置程序或界面图标。"""
import argparse
from pathlib import Path
from fontTools.ttLib import TTFont
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen

parser=argparse.ArgumentParser();parser.add_argument('--font',default='/System/Library/Fonts/Supplemental/Georgia Bold Italic.ttf');args=parser.parse_args()
root=Path(__file__).resolve().parent.parent;output=root/'assets/icons';output.mkdir(parents=True,exist_ok=True)
font=TTFont(args.font);glyphs=font.getGlyphSet();cmap=font.getBestCmap();units=font['head'].unitsPerEm
paths=[]
for word,baseline in [('SAFE',216),('CODE',410)]:
    names=[cmap[ord(letter)] for letter in word];width=sum(font['hmtx'][name][0] for name in names)
    sx=392/width;sy=187/units;x=58
    for name in names:
        pen=SVGPathPen(glyphs);glyphs[name].draw(TransformPen(pen,(sx,0,0,-sy,x,baseline)));paths.append(pen.getCommands());x+=font['hmtx'][name][0]*sx
shape=' '.join(paths)
svg=f'''<svg xmlns="http://www.w3.org/2000/svg" width="512" height="512" viewBox="0 0 512 512" role="img" aria-label="Safe Code 桌面快捷方式图标">
<defs>
 <linearGradient id="wine" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#70233f"/><stop offset=".55" stop-color="#4c142d"/><stop offset="1" stop-color="#2b0c21"/></linearGradient>
 <linearGradient id="gold" x1="0" y1="0" x2="0" y2="1"><stop stop-color="#fff2bd"/><stop offset=".52" stop-color="#f6c775"/><stop offset="1" stop-color="#dd8738"/></linearGradient>
</defs>
<rect x="6" y="6" width="500" height="500" rx="86" fill="url(#wine)"/>
<rect x="15" y="15" width="482" height="482" rx="78" fill="none" stroke="#e9ac66" stroke-opacity=".3" stroke-width="2"/>
<path d="{shape}" transform="translate(0 9)" fill="#20091c" stroke="#20091c" stroke-width="12" stroke-linejoin="round"/>
<path d="{shape}" fill="url(#gold)" stroke="#f9d99b" stroke-width="2.5" stroke-linejoin="round" paint-order="stroke fill"/>
</svg>'''
(output/'safe-code-shortcut.svg').write_text(svg,encoding='utf-8')
print(output/'safe-code-shortcut.svg')
