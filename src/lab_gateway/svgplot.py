"""A very small SVG plotter for the run page: hairline axes, 2 px lines, 8 px markers,
native <title> tooltips on every mark, light and dark colors from one style block."""

from __future__ import annotations

import math
from html import escape

LIGHT = {"bg": "#ffffff", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781", "grid": "#e1e0d9",
         "axis": "#c3c2b7", "band": "#f1f0ec", "s1": "#2a78d6", "s2": "#eb6834", "s3": "#1baf7a", "s4": "#eda100"}
DARK = {"bg": "#1a1a19", "ink": "#f0efec", "ink2": "#c3c2b7", "muted": "#898781", "grid": "#2c2c2a",
        "axis": "#383835", "band": "#242422", "s1": "#3987e5", "s2": "#d95926", "s3": "#199e70", "s4": "#c98500"}


def _rules(c: dict) -> str:
    r = [f".bg{{fill:{c['bg']}}}", f".ink{{fill:{c['ink']}}}", f".ink2{{fill:{c['ink2']}}}",
         f".muted{{fill:{c['muted']}}}", f".grid{{stroke:{c['grid']}}}", f".axis{{stroke:{c['axis']}}}",
         f".band{{fill:{c['band']}}}", f".held{{stroke:{c['ink2']}}}"]
    for i in range(1, 5):
        r.append(f".s{i}{{stroke:{c[f's{i}']}}} .f{i}{{fill:{c[f's{i}']}}}".replace("} .", "}\n."))
    return "\n".join(r)


STYLE = ("<style>\ntext{font-family:'Helvetica Neue',Helvetica,Arial,sans-serif;font-weight:300}\n"
         ".t{font-size:13px}.s{font-size:11px}.h{font-size:14px;font-weight:400}\n"
         "line,polyline,path,rect.o{fill:none}\n.grid,.axis{stroke-width:1}\n"
         + _rules(LIGHT) + "\n@media (prefers-color-scheme: dark){\n" + _rules(DARK) + "\n}\n</style>")


def nice_ticks(lo: float, hi: float, n: int = 5) -> list[float]:
    span = hi - lo
    step = 10 ** math.floor(math.log10(span / n))
    for m in (1, 2, 2.5, 5, 10):
        if span / (step * m) <= n:
            step *= m
            break
    first = math.ceil(lo / step) * step
    out, v = [], first
    while v <= hi + 1e-9:
        out.append(round(v, 10))
        v += step
    return out


def fmt(v: float) -> str:
    return f"{v:g}"


class Canvas:
    def __init__(self, width: int, height: int, title: str, desc: str = "") -> None:
        self.w, self.h = width, height
        self.parts = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" '
                      f'width="{width}" height="{height}" role="img">',
                      f"<title>{escape(title)}</title>", f"<desc>{escape(desc)}</desc>", STYLE,
                      f'<rect class="bg" x="0" y="0" width="{width}" height="{height}"/>']

    def add(self, s: str) -> None:
        self.parts.append(s)

    def text(self, x, y, s, cls="t ink2", anchor="start", tip: str | None = None) -> None:
        inner = escape(s) + (f"<title>{escape(tip)}</title>" if tip else "")
        self.add(f'<text x="{x:.1f}" y="{y:.1f}" class="{cls}" text-anchor="{anchor}">{inner}</text>')

    def svg(self) -> str:
        return "\n".join(self.parts + ["</svg>"]) + "\n"


class Axes:
    """A plot area inside a canvas. y may be log10."""

    def __init__(self, cv: Canvas, x0, y0, w, h, xlim, ylim, *, ylog=False, xflip=False) -> None:
        self.cv, self.x0, self.y0, self.w, self.h = cv, x0, y0, w, h
        self.xlim, self.ylim, self.ylog, self.xflip = xlim, ylim, ylog, xflip
        self.clip = f"c{abs(hash((x0, y0, w, h))) % 10 ** 8}"
        cv.add(f'<clipPath id="{self.clip}"><rect x="{x0}" y="{y0}" width="{w}" height="{h}"/></clipPath>')

    def X(self, v: float) -> float:
        f = (v - self.xlim[0]) / (self.xlim[1] - self.xlim[0])
        return self.x0 + (1 - f if self.xflip else f) * self.w

    def Y(self, v: float) -> float:
        lo, hi = self.ylim
        if self.ylog:
            v, lo, hi = math.log10(max(v, 1e-12)), math.log10(lo), math.log10(hi)
        return self.y0 + self.h - (v - lo) / (hi - lo) * self.h

    def frame(self, xlabel: str, ylabel: str, xticks=None, yticks=None, ygrid=True) -> None:
        cv = self.cv
        xticks = xticks or nice_ticks(*self.xlim)
        yticks = yticks or nice_ticks(*self.ylim)
        for v in yticks:
            y = self.Y(v)
            if ygrid:
                cv.add(f'<line class="grid" x1="{self.x0}" x2="{self.x0 + self.w}" y1="{y:.1f}" y2="{y:.1f}"/>')
            cv.text(self.x0 - 6, y + 4, fmt(v), "s muted", "end")
        base = self.y0 + self.h
        cv.add(f'<line class="axis" x1="{self.x0}" x2="{self.x0 + self.w}" y1="{base}" y2="{base}"/>')
        for v in xticks:
            x = self.X(v)
            cv.add(f'<line class="axis" x1="{x:.1f}" x2="{x:.1f}" y1="{base}" y2="{base + 4}"/>')
            cv.text(x, base + 16, fmt(v), "s muted", "middle")
        cv.text(self.x0 + self.w / 2, base + 32, xlabel, "s muted", "middle")
        cv.text(self.x0, self.y0 - 8, ylabel, "s muted", "start")

    def line(self, xs, ys, cls="s1", width=2.0, dash: str | None = None, tip: str | None = None) -> None:
        pts = " ".join(f"{self.X(x):.1f},{self.Y(y):.1f}" for x, y in zip(xs, ys))
        d = f' stroke-dasharray="{dash}"' if dash else ""
        t = f"<title>{escape(tip)}</title>" if tip else ""
        self.cv.add(f'<polyline class="{cls}" clip-path="url(#{self.clip})" stroke-width="{width}" '
                    f'stroke-linejoin="round" stroke-linecap="round"{d} points="{pts}">{t}</polyline>')

    def dot(self, x, y, cls="f1", r=4.0, tip: str | None = None) -> None:
        t = f"<title>{escape(tip)}</title>" if tip else ""
        self.cv.add(f'<circle class="{cls}" cx="{self.X(x):.1f}" cy="{self.Y(y):.1f}" r="{r}" '
                    f'stroke-width="2" style="stroke:var(--ring,transparent)">{t}</circle>')

    def hband(self, y_lo, y_hi, label: str | None = None) -> None:
        y1, y2 = self.Y(y_hi), self.Y(y_lo)
        self.cv.add(f'<rect class="band" x="{self.x0}" y="{y1:.1f}" width="{self.w}" height="{y2 - y1:.1f}"/>')
        if label:
            self.cv.text(self.x0 + self.w - 4, y1 + 12, label, "s muted", "end")

    def hline(self, y, label: str | None = None, dash="4 3") -> None:
        yy = self.Y(y)
        self.cv.add(f'<line class="axis" stroke-dasharray="{dash}" x1="{self.x0}" x2="{self.x0 + self.w}" '
                    f'y1="{yy:.1f}" y2="{yy:.1f}"/>')
        if label:
            self.cv.text(self.x0 + self.w - 4, yy - 4, label, "s muted", "end")


def legend(cv: Canvas, x, y, items: list[tuple[str, str]]) -> None:
    """items: (class suffix number, label). Swatch is a short 2 px line."""
    for i, (n, label) in enumerate(items):
        yy = y + i * 18
        cv.add(f'<line class="s{n}" stroke-width="2" x1="{x}" x2="{x + 16}" y1="{yy}" y2="{yy}"/>')
        cv.text(x + 22, yy + 4, label, "s ink2")


def envelope(xs, ys, x_lo, x_hi, bins: int):
    """Min/max decimation so peaks survive down-sampling."""
    import numpy as np
    xs, ys = np.asarray(xs), np.asarray(ys)
    m = (xs >= min(x_lo, x_hi)) & (xs <= max(x_lo, x_hi))
    xs, ys = xs[m], ys[m]
    order = np.argsort(xs)
    xs, ys = xs[order], ys[order]
    edges = np.linspace(xs[0], xs[-1], bins + 1)
    idx = np.clip(np.searchsorted(edges, xs, side="right") - 1, 0, bins - 1)
    out_x, out_y = [], []
    for b in range(bins):
        sel = idx == b
        if not sel.any():
            continue
        seg = ys[sel]
        cx = float(xs[sel].mean())
        out_x += [cx, cx]
        out_y += [float(seg.min()), float(seg.max())]
    return out_x, out_y
