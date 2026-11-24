"""Signal shapes for the simulators."""

from __future__ import annotations

import math

import numpy as np


def bigauss(t: np.ndarray, center: float, area_s: float, sigma_l: float, sigma_r: float) -> np.ndarray:
    """A tailing peak (left and right half-Gaussians). t and sigmas in min, area in signal x s."""
    height = area_s / (math.sqrt(math.pi / 2) * (sigma_l + sigma_r) * 60.0)
    s = np.where(t < center, sigma_l, sigma_r)
    return height * np.exp(-0.5 * ((t - center) / s) ** 2)


def expand_multiplet(ppm: float, n_h: float, couplings: list[tuple[float, int]], sfo1: float) -> list[tuple[float, float]]:
    """First-order splitting: [(J_hz, n_equivalent_partners), ...] -> [(ppm, amplitude), ...]."""
    lines = [(ppm, float(n_h))]
    for j_hz, n in couplings:
        coeffs = [math.comb(n, k) / 2 ** n for k in range(n + 1)]
        offsets = [(k - n / 2) * j_hz / sfo1 for k in range(n + 1)]
        lines = [(p + o, a * c) for p, a in lines for o, c in zip(offsets, coeffs)]
    return lines


def fid_from_lines(lines: list[tuple[float, float, float]], *, sw_hz: float, sfo1: float, o1p: float,
                   td: int, phase0_deg: float, noise_sd: float, rng: np.random.Generator) -> np.ndarray:
    """lines: (ppm, amplitude, linewidth_hz). Returns a complex FID of td points."""
    t = np.arange(td) / sw_hz
    fid = np.zeros(td, dtype=np.complex128)
    arr = np.array(lines, dtype=float)
    for chunk in np.array_split(arr, max(1, len(arr) // 48)):
        f_hz = (chunk[:, 0] - o1p) * sfo1
        decay = np.pi * chunk[:, 2]
        fid += (chunk[:, 1][:, None] * np.exp((2j * np.pi * f_hz[:, None] - decay[:, None]) * t[None, :])).sum(axis=0)
    fid *= np.exp(1j * math.radians(phase0_deg))
    fid += rng.normal(0, noise_sd, td) + 1j * rng.normal(0, noise_sd, td)
    return fid
