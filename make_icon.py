from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent
out = ROOT / "icon.ico"


def make(size: int) -> Image.Image:
    img = Image.new("RGBA", (size, size), (7, 11, 20, 255))
    draw = ImageDraw.Draw(img)
    pad = max(2, size // 16)
    width = max(2, size // 18)
    draw.rounded_rectangle(
        (pad, pad, size - pad - 1, size - pad - 1),
        radius=max(4, size // 8),
        outline=(62, 224, 212, 255),
        width=width,
    )
    s = size
    pts = [
        (int(s * 0.18), int(s * 0.56)),
        (int(s * 0.34), int(s * 0.56)),
        (int(s * 0.44), int(s * 0.26)),
        (int(s * 0.58), int(s * 0.76)),
        (int(s * 0.68), int(s * 0.46)),
        (int(s * 0.82), int(s * 0.46)),
    ]
    draw.line(pts, fill=(62, 224, 212, 255), width=max(2, size // 14), joint="curve")
    return img


sizes = [16, 24, 32, 48, 64, 128, 256]
images = [make(s) for s in sizes]
images[-1].save(out, format="ICO", sizes=[(s, s) for s in sizes], append_images=images[:-1])
print(out)
