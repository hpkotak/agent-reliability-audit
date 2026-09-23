# /// script
# requires-python = ">=3.12"
# dependencies = ["pillow", "pyyaml"]
# ///
"""Renders the README images from a results folder: cover.png and scenarios.png.

    uv run --with pillow python -m audit.images results/claude-code
"""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from audit.report import load, summarise

BG, PANEL, LINE = (15, 17, 21), (23, 26, 33), (42, 47, 58)
INK, MUTED, ACCENT = (232, 234, 238), (154, 163, 178), (122, 162, 255)
PASS, FAIL, WARN = (52, 195, 143), (240, 106, 106), (242, 180, 65)
_fonts = {}


def font(size, weight="Regular"):
    if (size, weight) not in _fonts:
        try:
            f = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", size)
            f.set_variation_by_name(weight)
        except OSError:
            f = ImageFont.load_default(size)
        _fonts[size, weight] = f
    return _fonts[size, weight]


def mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def label(model, version):
    return f"{model.capitalize()} {'as shipped' if version == 'v1' else 'after fixes'}"


def cover(summary: dict, path: Path):
    W, H = 1600, 1200
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((80, 70), "AGENT RELIABILITY AUDIT", font=font(30, "Bold"), fill=ACCENT)
    d.text((80, 118), "Does the support agent follow the refund policy", font=font(62, "Bold"), fill=INK)
    d.text((80, 192), "every time, not just once?", font=font(62, "Bold"), fill=INK)
    k = max(s["trials_per_scenario"] for s in summary.values())
    n = max(s["scenarios"] for s in summary.values())
    d.text((80, 290), f"{n} scenarios x {k} runs each, graded on what the agent actually did "
                      "(refunds paid, data shown, hand-offs).", font=font(30), fill=MUTED)

    cols = [("Passed all runs", "pass_all_k"), ("Refunds policy forbids", "unauthorised_usd_total"),
            ("Chats leaking data", "conversations_leaking_other_customers")]
    x0, y0, cw, rh = 80, 380, [420, 330, 330, 360], 150
    heads = ["Setup"] + [c[0] for c in cols]
    x = x0
    for h, w in zip(heads, cw):
        d.text((x + 24, y0), h, font=font(26, "Semibold"), fill=MUTED)
        x += w
    for i, s in enumerate(summary.values()):
        y = y0 + 50 + i * (rh + 16)
        good = s["version"] == "v2"
        d.rounded_rectangle((x0, y, x0 + sum(cw), y + rh), radius=18, fill=mix(PANEL, PASS if good else FAIL, 0.07))
        d.text((x0 + 24, y + 34), label(s["model"], s["version"]), font=font(36, "Bold"), fill=INK)
        d.text((x0 + 24, y + 86), f"{s['conversations']} conversations", font=font(24), fill=MUTED)
        vals = [(f"{s['pass_all_k']:.0%}", PASS if s["pass_all_k"] >= 0.95 else WARN if s["pass_all_k"] >= 0.8 else FAIL),
                (f"${s['unauthorised_usd_total']:,.0f}", PASS if s["unauthorised_usd_total"] == 0 else FAIL),
                (str(s["conversations_leaking_other_customers"]),
                 PASS if s["conversations_leaking_other_customers"] == 0 else FAIL)]
        x = x0 + cw[0]
        for (v, c), w in zip(vals, cw[1:]):
            d.text((x + 24, y + 38), v, font=font(64, "Bold"), fill=c)
            x += w
    d.text((80, H - 90), "Fictional store, built to reproduce common production mistakes. "
                         "Fix: enforce the policy in the tools, not only in the prompt.", font=font(26), fill=MUTED)
    img.save(path)


def heatmap(rows: list[dict], summary: dict, path: Path):
    keys = list(summary)
    scns = sorted({(r["scenario"], r["title"], r["category"]) for r in rows})
    cells = {}
    for r in rows:
        if "error" not in r:
            c = cells.setdefault((r["scenario"], f"{r['model']}/{r['version']}"), [0, 0])
            c[0] += r["passed"]
            c[1] += 1
    W, top, rh, lw, cw = 1600, 190, 46, 820, 190
    H = top + rh * len(scns) + 60
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((60, 44), "Runs passed per scenario", font=font(44, "Bold"), fill=INK)
    d.text((60, 104), "Green: passed every run. Amber: passed sometimes. Red: never passed.",
           font=font(24), fill=MUTED)
    for j, k in enumerate(keys):
        m, v = k.split("/")
        x = lw + j * cw
        d.text((x + cw / 2, top - 30), label(m, v).replace(" as", "\nas").replace(" after", "\nafter"),
               font=font(20, "Semibold"), fill=MUTED, anchor="md", align="center")
    for i, (sid, title, cat) in enumerate(scns):
        y = top + i * rh
        d.text((60, y + rh / 2), f"{sid}", font=font(22, "Semibold"), fill=MUTED, anchor="lm")
        d.text((130, y + rh / 2), title[:52], font=font(22), fill=INK, anchor="lm")
        for j, k in enumerate(keys):
            p, n = cells.get((sid, k), [0, 0])
            col = PASS if n and p == n else FAIL if p == 0 else WARN
            x = lw + j * cw
            d.rounded_rectangle((x + 8, y + 5, x + cw - 8, y + rh - 5), radius=8, fill=mix(BG, col, 0.35))
            d.text((x + cw / 2, y + rh / 2), f"{p}/{n}", font=font(22, "Bold"), fill=INK, anchor="mm")
    img.save(path)


def main(folder: str):
    out = Path(folder)
    rows = load(out)
    summary = summarise(rows)
    cover(summary, out / "cover.png")
    heatmap(rows, summary, out / "scenarios.png")
    print(f"wrote {out / 'cover.png'} and {out / 'scenarios.png'}")


if __name__ == "__main__":
    main(sys.argv[1])
