"""Data processing behind the QC gate.

Independent of the simulators: the same functions run on data exported from a real CDS,
SCIEX OS or TopSpin. Chromatography: baseline, peak detection, drop-line integration,
USP tailing, plate count. Stability: first-order fit. NMR: apodization, Fourier
transform, zero-order phase, DSS referencing, linewidth, signal-to-noise, peak picking.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

import numpy as np


# ---------------------------------------------------------------- chromatography
@dataclass
class Peak:
    rt: float          # min
    height: float
    area: float        # signal x seconds
    start: float       # min
    end: float         # min
    fwhm: float        # min
    tailing: float     # USP tailing factor at 5 % height
    plates: float      # half-height method

    def to_dict(self) -> dict:
        return {k: round(v, 5) for k, v in asdict(self).items()}


def smooth(y: np.ndarray, n: int = 5) -> np.ndarray:
    if n <= 1:
        return y
    k = np.ones(n) / n
    pad = n // 2
    return np.convolve(np.pad(y, pad, mode="edge"), k, mode="valid")


def noise_sd(y: np.ndarray) -> float:
    d = np.diff(y)
    mad = np.median(np.abs(d - np.median(d)))
    return float(mad / 0.6745 / math.sqrt(2)) or 1e-12


def baseline(t: np.ndarray, y: np.ndarray, block_min: float = 1.0, q: float = 10.0) -> np.ndarray:
    edges = np.arange(t[0], t[-1] + block_min, block_min)
    centers, levels = [], []
    for a, b in zip(edges[:-1], edges[1:]):
        m = (t >= a) & (t < b)
        if m.sum() > 5:
            centers.append(t[m].mean())
            levels.append(np.percentile(y[m], q))
    if len(centers) < 2:
        return np.full_like(y, np.percentile(y, q))
    return np.interp(t, centers, levels)


def _crossing(t, y, apex, level, step, stop):
    i = apex
    while i != stop and y[i] > level:
        i += step
    if y[i] > level:
        return float(t[i])
    j = i - step
    if y[j] == y[i]:
        return float(t[i])
    frac = (y[j] - level) / (y[j] - y[i])
    return float(t[j] + frac * (t[i] - t[j]))


def find_peaks(t, y, *, t_start: float = 0.0, t_end: float | None = None,
               min_height: float = 0.0, k_noise: float = 8.0, min_sep_min: float = 0.05) -> list[Peak]:
    t = np.asarray(t, dtype=float)
    y = np.asarray(y, dtype=float)
    b = baseline(t, y)
    r = y - b
    rs = smooth(r, 5)
    noise = max(noise_sd(r), 1e-6 * float(np.max(np.abs(rs))))
    thr = max(min_height, k_noise * noise)
    t_end = t[-1] if t_end is None else t_end
    inside = (t >= t_start) & (t <= t_end)
    idx = np.where((rs[1:-1] > rs[:-2]) & (rs[1:-1] >= rs[2:]) & (rs[1:-1] > thr) & inside[1:-1])[0] + 1
    # merge maxima closer than min_sep, keeping the taller
    apexes: list[int] = []
    for i in idx:
        if apexes and t[i] - t[apexes[-1]] < min_sep_min:
            if rs[i] > rs[apexes[-1]]:
                apexes[-1] = i
        else:
            apexes.append(int(i))
    # bounds: back to baseline, or the valley between neighbours
    peaks: list[Peak] = []
    for n, a in enumerate(apexes):
        lo = apexes[n - 1] + int(np.argmin(rs[apexes[n - 1]:a])) if n > 0 else 0
        hi = a + int(np.argmin(rs[a:apexes[n + 1]])) if n + 1 < len(apexes) else len(t) - 1
        left = a
        while left > lo and rs[left] > noise:
            left -= 1
        right = a
        while right < hi and rs[right] > noise:
            right += 1
        seg = slice(left, right + 1)
        area = float(np.trapezoid(r[seg], t[seg] * 60.0))
        h = float(rs[a])
        fl = _crossing(t, rs, a, h / 2, -1, left)
        fr = _crossing(t, rs, a, h / 2, +1, right)
        l5 = _crossing(t, rs, a, 0.05 * h, -1, left)
        r5 = _crossing(t, rs, a, 0.05 * h, +1, right)
        fwhm = max(fr - fl, 1e-9)
        f = max(float(t[a]) - l5, 1e-9)
        peaks.append(Peak(rt=float(t[a]), height=h, area=area, start=float(t[left]), end=float(t[right]),
                          fwhm=fwhm, tailing=(r5 - l5) / (2 * f), plates=5.54 * (float(t[a]) / fwhm) ** 2))
    return peaks


def area_percent(peaks: list[Peak], disregard_pct: float = 0.0) -> tuple[float, Peak | None, list[dict]]:
    """Main-peak area percent after dropping peaks below the disregard limit."""
    if not peaks:
        return 0.0, None, []
    total = sum(p.area for p in peaks)
    kept = [p for p in peaks if p.area / total * 100 >= disregard_pct]
    total = sum(p.area for p in kept)
    main = max(kept, key=lambda p: p.area)
    table = [{"rt_min": round(p.rt, 3), "area_pct": round(p.area / total * 100, 3)} for p in kept]
    return main.area / total * 100, main, table


def rsd_pct(values) -> float:
    v = np.asarray(values, dtype=float)
    return float(np.std(v, ddof=1) / np.mean(v) * 100) if len(v) > 1 else 0.0


def largest_peak(t, y, window: tuple[float, float], k_noise: float = 8.0) -> Peak | None:
    peaks = find_peaks(t, y, t_start=window[0], t_end=window[1], k_noise=k_noise)
    return max(peaks, key=lambda p: p.area) if peaks else None


def window_area(t, y, center: float, half_width: float = 0.08, side: float = 0.2) -> float:
    """Quantitation-style area: integrate a fixed window at the expected retention time over a
    local baseline, so small responses (LLOQ, carryover) are measured instead of dropped."""
    t = np.asarray(t, dtype=float)
    y = np.asarray(y, dtype=float)
    d = np.abs(t - center)
    inside = d <= half_width
    ring = (d > half_width + 0.04) & (d <= half_width + 0.04 + side)
    base = float(np.median(y[ring])) if ring.any() else 0.0
    return float(np.trapezoid(y[inside] - base, t[inside] * 60.0))


# ---------------------------------------------------------------- stability
def fit_first_order(times_min, pct_remaining) -> dict:
    x = np.asarray(times_min, dtype=float)
    y = np.log(np.asarray(pct_remaining, dtype=float) / 100.0)
    slope, intercept = np.polyfit(x, y, 1)
    pred = slope * x + intercept
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - y.mean()) ** 2)) or 1e-12
    k = -float(slope)
    return {"k_per_min": k, "t_half_min": (math.log(2) / k) if k > 1e-9 else None,
            "r2": 1 - ss_res / ss_tot, "points": len(x)}


# ---------------------------------------------------------------- NMR
def process_1d(fid: np.ndarray, sw_hz: float, sfo1_mhz: float, o1p_ppm: float,
               lb_hz: float = 0.3, zero_fill: int = 4) -> dict:
    n = len(fid)
    t = np.arange(n) / sw_hz
    f = fid * np.exp(-np.pi * lb_hz * t)
    f[0] *= 0.5
    spec = np.fft.fftshift(np.fft.fft(f, n * zero_fill))
    hz = np.fft.fftshift(np.fft.fftfreq(n * zero_fill, d=1.0 / sw_hz))
    ppm = o1p_ppm + hz / sfo1_mhz
    phi = float(np.angle(np.sum(spec)))   # zero-order phase: the integral of absorption is real and positive
    real = np.real(spec * np.exp(-1j * phi))
    return {"ppm": ppm, "real": real, "phase0_deg": math.degrees(phi)}


def reference_and_baseline(ppm, real, noise_region, ref_window=(-0.15, 0.15)) -> tuple[np.ndarray, np.ndarray, float]:
    m = (ppm >= ref_window[0]) & (ppm <= ref_window[1])
    shift = float(ppm[m][np.argmax(real[m])])
    ppm = ppm - shift
    nm = (ppm >= noise_region[0]) & (ppm <= noise_region[1])
    real = real - float(np.median(real[nm]))
    return ppm, real, shift


def linewidth_hz(ppm, real, center_ppm: float, sfo1_mhz: float, half_window_ppm: float = 0.05) -> float:
    m = np.where((ppm >= center_ppm - half_window_ppm) & (ppm <= center_ppm + half_window_ppm))[0]
    a = int(m[np.argmax(real[m])])
    h = real[a] / 2
    left = _crossing(ppm, real, a, h, -1, int(m[0]))
    right = _crossing(ppm, real, a, h, +1, int(m[-1]))
    return abs(right - left) * sfo1_mhz


def nmr_noise(ppm, real, noise_region) -> float:
    nm = (ppm >= noise_region[0]) & (ppm <= noise_region[1])
    return float(np.std(real[nm]))


def snr(ppm, real, signal_region, noise_region) -> float:
    sm = (ppm >= signal_region[0]) & (ppm <= signal_region[1])
    return float(real[sm].max() / (2 * nmr_noise(ppm, real, noise_region)))


def pick_peaks(ppm, real, threshold: float) -> list[tuple[float, float]]:
    y = real
    idx = np.where((y[1:-1] > y[:-2]) & (y[1:-1] >= y[2:]) & (y[1:-1] > threshold))[0] + 1
    return [(float(ppm[i]), float(y[i])) for i in idx]


def singlets_in(ppm, real, window, sfo1_mhz: float, threshold: float,
                j_range_hz=(3.0, 9.0), ratio=(0.5, 2.0)) -> list[float]:
    """Peaks in the window with no partner of similar height at a coupling distance."""
    peaks = [p for p in pick_peaks(ppm, real, threshold) if window[0] <= p[0] <= window[1]]
    out = []
    for p in peaks:
        coupled = any(j_range_hz[0] <= abs(p[0] - q[0]) * sfo1_mhz <= j_range_hz[1]
                      and ratio[0] <= q[1] / p[1] <= ratio[1] for q in peaks if q is not p)
        if not coupled:
            out.append(round(p[0], 3))
    return out
