#!/usr/bin/env python3
"""生成「中信跟单」应用图标。

输出：
  assets/AppIcon.png                                512×512，给 Tk 窗口用
  启动中信跟单.app/Contents/Resources/AppIcon.icns   macOS 全套尺寸
  启动中信跟单.app/Contents/Resources/AppIcon.png

设计要点（改之前先读）：
  - 画布 1024×1024，内容区 824×824（四周留白 100），圆角 185 —— 这是 macOS
    Big Sur 之后系统图标的度量，跟着改才能在 Dock 里和其他图标一样大。
  - 背景用纵向渐变 + 顶部柔光 + 底部内阴影 + 落影，纯色块会显得很廉价。
  - 小尺寸（16/32/64/128）单独出图：用加粗的单字「中」。「中信」双字在 16px
    上会糊成一团，icns 的每个槽位可以放不同母版，所以按尺寸分派。
  - 需要 Pillow；系统 python3 可能没有，先确认 `python3 -c "import PIL"`。

用法：  python3 tools/gen_icon.py
"""

import os
import shutil
import subprocess

from PIL import Image, ImageDraw, ImageFont, ImageFilter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUNDLE = os.path.join(ROOT, "启动中信跟单.app", "Contents", "Resources")

S = 1024            # 画布
M = 100             # 留白（内容区 824）
C = S - 2 * M
R = 185             # 圆角半径

FONT = "/System/Library/Fonts/STHeiti Medium.ttc"

# 主题色：蓝，渐变收到底部压暗
BLUE = [(0.0, (74, 158, 255)), (0.55, (28, 112, 240)), (1.0, (8, 62, 178))]


# ---------------------------------------------------------------------------
# 基础部件
# ---------------------------------------------------------------------------

def rounded_mask(size, radius, scale=4):
    big = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(big).rounded_rectangle(
        [0, 0, size * scale - 1, size * scale - 1], radius=radius * scale, fill=255)
    return big.resize((size, size), Image.LANCZOS)


def vgrad(size, stops):
    """纵向渐变。stops 形如 [(0.0, (r,g,b)), (1.0, (r,g,b))]。"""
    w, h = size
    col = Image.new("RGB", (1, h))
    px = col.load()
    stops = sorted(stops)
    for y in range(h):
        t = y / max(1, h - 1)
        for i in range(len(stops) - 1):
            t0, c0 = stops[i]
            t1, c1 = stops[i + 1]
            if t0 <= t <= t1:
                k = (t - t0) / max(1e-6, t1 - t0)
                px[0, y] = tuple(int(c0[j] + (c1[j] - c0[j]) * k) for j in range(3))
                break
        else:
            px[0, y] = stops[-1][1]
    return col.resize((w, h), Image.BICUBIC)


def base(stops):
    """圆角方块：渐变底色 + 顶部柔光 + 底部内阴影 + 落影。"""
    grad = vgrad((C, C), stops).convert("RGBA")

    gloss = Image.new("L", (C, C), 0)
    gd = ImageDraw.Draw(gloss)
    hh = int(C * 0.5)
    for i in range(hh):
        gd.line([(0, i), (C, i)], fill=int(52 * (1 - i / hh) ** 2.2))
    grad = Image.alpha_composite(grad, Image.merge("RGBA", (
        Image.new("L", (C, C), 255),) * 3 + (gloss,)))

    shade = Image.new("L", (C, C), 0)
    sd = ImageDraw.Draw(shade)
    bh = int(C * 0.22)
    for i in range(bh):
        sd.line([(0, C - 1 - i), (C, C - 1 - i)], fill=int(38 * (1 - i / bh) ** 2))
    grad = Image.alpha_composite(grad, Image.merge("RGBA", (
        Image.new("L", (C, C), 0),) * 3 + (shade,)))
    grad.putalpha(rounded_mask(C, R))

    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    layer.paste(grad, (M, M), grad)

    shadow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    sm = Image.new("L", (S, S), 0)
    ImageDraw.Draw(sm).rounded_rectangle(
        [M, M + 12, M + C - 1, M + C - 1], radius=R, fill=105)
    sm = sm.filter(ImageFilter.GaussianBlur(20))
    shadow.paste((0, 0, 0, 255), (0, 0), sm)
    return Image.alpha_composite(shadow, layer)


def ink_bbox(img):
    return img.split()[3].point(lambda v: 255 if v > 150 else 0).getbbox()


def text_img(txt, px, stroke=0):
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    f = ImageFont.truetype(FONT, px)
    d = ImageDraw.Draw(layer)
    bb = d.textbbox((0, 0), txt, font=f, stroke_width=stroke)
    d.text((S / 2 - bb[0] - (bb[2] - bb[0]) / 2,
            S / 2 - bb[1] - (bb[3] - bb[1]) / 2),
           txt, font=f, fill=(255, 255, 255, 255),
           stroke_width=stroke, stroke_fill=(255, 255, 255, 255))
    return layer


def put_ink(img, ink, cx, cy):
    """把 ink 的墨迹重心精确放到 (cx, cy)。

    不能直接把文字画在画布中心 —— 汉字的墨迹在字身框里是偏的（下方留白多），
    按字身框居中会看起来整体偏上。所以先量墨迹 bbox 再补偏移。
    """
    bb = ink_bbox(ink)
    if not bb:
        return img
    dx = cx - (bb[0] + (bb[2] - bb[0]) / 2)
    dy = cy - (bb[1] + (bb[3] - bb[1]) / 2)
    layer = Image.new("RGBA", img.size, (0, 0, 0, 0))
    layer.paste(ink, (int(round(dx)), int(round(dy))), ink)
    return Image.alpha_composite(img, layer)


def build(stops, txt, px, stroke=0, cx=512, cy=505):
    return put_ink(base(stops), text_img(txt, px, stroke=stroke), cx, cy)


# ---------------------------------------------------------------------------
# 出图
# ---------------------------------------------------------------------------

def main():
    # 大尺寸：完整的「中信」
    master = build(BLUE, "中信", 378)
    # 小尺寸：单字「中」并加粗，否则 16px 上认不出
    tiny = build(BLUE, "中", 470, stroke=60)

    assets = os.path.join(ROOT, "assets")
    os.makedirs(assets, exist_ok=True)
    master.resize((512, 512), Image.LANCZOS).save(os.path.join(assets, "AppIcon.png"))
    print("写出", os.path.relpath(os.path.join(assets, "AppIcon.png"), ROOT))

    iconset = "/tmp/LingTradeBridge.iconset"
    if os.path.isdir(iconset):
        shutil.rmtree(iconset)
    os.makedirs(iconset)

    slots = [
        (16, "icon_16x16.png", tiny),
        (32, "icon_16x16@2x.png", tiny),
        (32, "icon_32x32.png", tiny),
        (64, "icon_32x32@2x.png", tiny),
        (128, "icon_128x128.png", tiny),
        (256, "icon_128x128@2x.png", master),
        (256, "icon_256x256.png", master),
        (512, "icon_256x256@2x.png", master),
        (512, "icon_512x512.png", master),
        (1024, "icon_512x512@2x.png", master),
    ]
    for px, name, src in slots:
        src.resize((px, px), Image.LANCZOS).save(os.path.join(iconset, name))

    os.makedirs(BUNDLE, exist_ok=True)
    icns = os.path.join(BUNDLE, "AppIcon.icns")
    subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns], check=True)
    shutil.copy(os.path.join(assets, "AppIcon.png"),
                os.path.join(BUNDLE, "AppIcon.png"))
    shutil.rmtree(iconset)
    print("写出", os.path.relpath(icns, ROOT),
          f"({os.path.getsize(icns) // 1024} KB)")

    # 换图后要让 LaunchServices 忘掉旧的缩略图缓存
    subprocess.run([
        "/System/Library/Frameworks/CoreServices.framework/Frameworks/"
        "LaunchServices.framework/Support/lsregister", "-f",
        os.path.join(ROOT, "启动中信跟单.app"),
    ])


if __name__ == "__main__":
    main()
