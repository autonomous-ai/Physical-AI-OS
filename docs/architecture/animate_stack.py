"""Build a script-free animated tour from the canonical architecture SVG.

Run after build_figures.py. Labels and geometry stay in the static source;
this companion only adds a sequential layer highlight for README readers.
"""
import argparse
import math
from pathlib import Path
import xml.etree.ElementTree as ET

HERE = Path(__file__).resolve().parent
NS = "http://www.w3.org/2000/svg"
ET.register_namespace("", NS)
COLORS = ["#ad5130", "#5a912f", "#5d579d", "#2f6193", "#23786c",
          "#96660f", "#a33b3b", "#46566d", "#67691f", "#565656", "#ad5130"]


def build(gif=False, png=None):
    root = ET.parse(HERE / "autonomous-stack.svg").getroot()
    width = float(root.attrib["viewBox"].split()[2])
    captions = [e for e in root.findall(f"{{{NS}}}text")
                if e.get("x") == "40" and e.get("font-weight") == "600"]
    assert len(captions) == len(COLORS), "Update the tour when stack layers change"
    root.set("role", "img")
    root.set("aria-labelledby", "tour-title tour-desc")
    title = ET.Element(f"{{{NS}}}title", id="tour-title")
    title.text = "Autonomous OS — one platform, every layer replaceable"
    desc = ET.Element(f"{{{NS}}}desc", id="tour-desc")
    desc.text = ("A slow visual tour of the platform layers, not an execution trace. "
                 "All labels remain visible. Dashed boxes mark extension points.")
    root.insert(0, title)
    root.insert(1, desc)
    style = ET.SubElement(root, f"{{{NS}}}style")
    style.text = """
      .layer-highlight { opacity: 0; animation: layer-tour 17.6s ease-in-out infinite; }
      @keyframes layer-tour { 0%, 13%, 100% { opacity: 0; } 3%, 8% { opacity: .9; } }
      @media (prefers-reduced-motion: reduce) { .layer-highlight { animation: none; } }
    """
    for i, (caption, color) in enumerate(zip(captions, COLORS)):
        cy = float(caption.attrib["y"]) - 2
        group = ET.SubElement(root, f"{{{NS}}}g", {
            "class": "layer-highlight", "style": f"animation-delay: {i * 1.4:.1f}s",
            "fill": "none", "stroke": color, "stroke-width": "3",
        })
        ET.SubElement(group, f"{{{NS}}}rect", {
            "x": "350", "y": f"{cy-42:g}", "width": f"{width-390:g}",
            "height": "84", "rx": "9",
        })
        ET.SubElement(group, f"{{{NS}}}path", {
            "d": f"M 19 {cy-22:g} V {cy+22:g}", "stroke-width": "5", "stroke-linecap": "round",
        })
    out = HERE / "autonomous-stack-animated.svg"
    ET.indent(root)
    out.write_text(ET.tostring(root, encoding="unicode") + "\n", encoding="utf-8")
    print(out)
    if gif:
        from PIL import Image, ImageDraw

        base = Image.open(png).convert("RGB")
        size = (1100, round(1100 * base.height / base.width))
        base = base.resize(size, Image.Resampling.LANCZOS)
        scale = size[0] / width
        frames, durations = [base], [1000]
        for caption, color in zip(captions, COLORS):
            cy = float(caption.attrib["y"]) - 2
            for step in range(12):
                alpha = int(230 * math.sin(math.pi * (step + 1) / 13))
                overlay = Image.new("RGBA", size)
                draw = ImageDraw.Draw(overlay)
                rgb = tuple(int(color[j:j+2], 16) for j in (1, 3, 5))
                draw.rounded_rectangle(
                    tuple(round(v * scale) for v in (350, cy-42, width-40, cy+42)),
                    radius=6, outline=(*rgb, alpha), width=2,
                )
                draw.line(tuple(round(v * scale) for v in (19, cy-22, 19, cy+22)),
                          fill=(*rgb, alpha), width=3)
                frames.append(Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB"))
                durations.append(100)
        frames.append(base)
        durations.append(1500)
        # A shared palette avoids text shimmer as the highlight moves.
        palette = base.quantize(colors=224)
        frames = [frame.quantize(palette=palette, dither=Image.Dither.NONE) for frame in frames]
        target = HERE / "autonomous-stack-animated.gif"
        frames[0].save(target, save_all=True, append_images=frames[1:], duration=durations,
                       loop=0, optimize=True, disposal=1)
        print(target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gif", action="store_true", help="also render the README GIF using Pillow and the static PNG")
    parser.add_argument("--png", type=Path, help="fresh PNG rendered from autonomous-stack.svg")
    args = parser.parse_args()
    if args.gif and not args.png:
        parser.error("--gif requires --png: render the current SVG first")
    build(gif=args.gif, png=args.png)
