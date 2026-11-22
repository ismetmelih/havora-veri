"""200 hPa Rossby dalgaları: GEFS (31 senaryo) ortalamasının 200 hPa rüzgârından, Gün 1–5 ve Gün 6–10 ortalamaları.

Hesap (1° ızgarada, kutuplar hariç):
- girdaplık ζ ve ıraksama D (küresel koordinatta sonlu farklar)
- akım fonksiyonu ψ (∇²ψ = ζ) ve hız potansiyeli χ (∇²χ = D): boylamda Fourier, enlemde üç köşegenli çözüm
- dalga treni ψ′ = ψ − kuşak ortalaması
- dalga aktivite akısı (Takaya ve Nakamura 2001, durağan dalga; temel akış: kuşak ortalaması batı rüzgârı, 200 hPa)
- jet dalga kılavuzu: zaman ortalaması rüzgâr hızı > 30 m/s
χ anomali değil, toplamdır (küresel ortalaması çıkarılmış): negatif = üst seviye ıraksama (yükselen hava, konveksiyon).
Çıktı: out/analiz/rossby.json (2° ızgara). Yeni GEFS koşusu gelince (00Z/12Z, 240 saati hazır) yeniden hesaplanır.
"""
import datetime as dt
import json
import os
import tempfile
import time
import urllib.request

import eccodes
import numpy as np
from scipy.linalg import solve_banded

OUT = os.path.join('out', 'analiz', 'rossby.json')
UA = {'User-Agent': 'havaxpro-veri/1.0'}
NOMADS = 'https://nomads.ncep.noaa.gov'
A = 6.371e6           # Dünya yarıçapı (m)
WINDOWS = {'g1_5': range(12, 121, 12), 'g6_10': range(132, 241, 12)}


def log(*a):
    print(dt.datetime.utcnow().strftime('%H:%M:%S'), 'rossby', *a, flush=True)


def get(url, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
                return r.read()
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))


def exists(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA, method='HEAD'), timeout=30) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def latest_run():
    now = dt.datetime.utcnow()
    for h in range(0, 72, 6):
        t = (now - dt.timedelta(hours=h)).replace(minute=0, second=0, microsecond=0)
        t = t.replace(hour=t.hour - t.hour % 12)   # 00Z ve 12Z
        d = f'gefs.{t:%Y%m%d}/{t:%H}/atmos/pgrb2ap5'
        if exists(f'{NOMADS}/pub/data/nccf/com/gens/prod/{d}/geavg.t{t:%H}z.pgrb2a.0p50.f240.idx'):
            return t
    return None


def wind200(run, st):
    """GEFS ortalaması 200 hPa u, v (0,5° dünya) → 1° (kutup satırları hariç): enlem 89,5 … −89,5, boylam 0 … 359"""
    q = (f'dir=%2Fgefs.{run:%Y%m%d}%2F{run:%H}%2Fatmos%2Fpgrb2ap5&file=geavg.t{run:%H}z.pgrb2a.0p50.f{st:03d}'
         '&var_UGRD=on&var_VGRD=on&lev_200_mb=on')
    time.sleep(0.6)
    data = get(f'{NOMADS}/cgi-bin/filter_gefs_atmos_0p50a.pl?{q}')
    out = {}
    with tempfile.NamedTemporaryFile(suffix='.grb2') as tmp:
        tmp.write(data)
        tmp.flush()
        with open(tmp.name, 'rb') as f:
            while True:
                h = eccodes.codes_grib_new_from_file(f)
                if h is None:
                    break
                ni, nj = eccodes.codes_get(h, 'Ni'), eccodes.codes_get(h, 'Nj')
                v = eccodes.codes_get_values(h).reshape(nj, ni)
                if eccodes.codes_get(h, 'latitudeOfFirstGridPointInDegrees') < 0:
                    v = v[::-1]
                out[eccodes.codes_get(h, 'shortName')] = v[1:-1:2, ::2]
                eccodes.codes_release(h)
    return out['u'], out['v']


def poisson(F, phi):
    """∇²X = F küre üstünde (a yarıçaplı); F: (enlem, boylam), phi: enlemler (radyan, kuzeyden güneye)"""
    n, m = F.shape
    dp = abs(phi[1] - phi[0])
    c = np.cos(phi)
    ch = np.cos(np.concatenate([[phi[0] + dp / 2], (phi[:-1] + phi[1:]) / 2, [phi[-1] - dp / 2]]))   # yarım noktalarda
    ch[0] = ch[-1] = 0.0   # kutup: akı yok
    Fh = np.fft.rfft(F, axis=1) * A * A
    X = np.zeros_like(Fh)
    for k in range(Fh.shape[1]):
        up = ch[1:-1] / (dp * dp * c[:-1])      # j → j+1
        lo = ch[1:-1] / (dp * dp * c[1:])       # j → j−1
        main = -(ch[:-1] + ch[1:]) / (dp * dp * c) - (k * k) / (c * c)
        ab = np.zeros((3, n), dtype=complex)
        ab[0, 1:] = up
        ab[1] = main
        ab[2, :-1] = lo
        b = Fh[:, k].copy()
        if k == 0:   # sabit belirsiz: ilk noktayı sıfıra sabitle
            ab[1, 0], ab[0, 1], b[0] = 1.0, 0.0, 0.0
        X[:, k] = solve_banded((1, 1), ab, b)
    out = np.fft.irfft(X, n=m, axis=1)
    w = np.cos(phi)[:, None]
    return out - (out * w).sum() / (w.sum() * m)


def compute(u, v):
    lat = np.arange(89.5, -90, -1.0)
    phi, c = np.radians(lat), np.cos(np.radians(lat))[:, None]
    dl, dp = np.radians(1.0), np.radians(1.0)
    ddl = lambda a: (np.roll(a, -1, 1) - np.roll(a, 1, 1)) / (2 * dl)   # noqa: E731
    ddp = lambda a: np.gradient(a, -dp, axis=0)                           # noqa: E731  (enlem kuzeyden güneye azalır)
    zeta = (ddl(v) - ddp(u * c)) / (A * c)
    div = (ddl(u) + ddp(v * c)) / (A * c)
    psi, chi = poisson(zeta, phi), poisson(div, phi)
    pe = psi - psi.mean(axis=1, keepdims=True)                          # dalga treni
    # Takaya–Nakamura akısı (durağan, temel akış kuşak ortalaması U, V = 0)
    U = u.mean(axis=1, keepdims=True)
    px, py = ddl(pe), ddp(pe)
    pxx, pxy = ddl(px), ddp(px)
    with np.errstate(invalid='ignore', divide='ignore'):
        k = 0.2 * c / (2 * np.abs(U))
        wx = k * (U / (A * A * c * c)) * (px * px - pe * pxx)
        wy = k * (U / (A * A * c)) * (px * py - pe * pxy)
    bad = (U < 5) | (np.abs(lat)[:, None] < 12)
    wx, wy = np.where(bad, np.nan, wx), np.where(bad, np.nan, wy)
    return {'psi': pe, 'chi': chi, 'spd': np.hypot(u, v), 'wx': wx, 'wy': wy, 'lat': lat}


def coarse(a):
    """1° → 2° (2×2 blok ortalaması)"""
    return np.nanmean(a.reshape(a.shape[0] // 2, 2, a.shape[1] // 2, 2), axis=(1, 3))


def main():
    run = latest_run()
    if run is None:
        log('240 saati hazır GEFS koşusu yok')
        return
    rid = f'{run:%Y%m%d%H}'
    if os.path.exists(OUT):
        with open(OUT, encoding='utf-8') as fh:
            if json.load(fh).get('run_id') == rid:
                log('güncel', rid)
                return
    res = {'run_id': rid, 'run': run.strftime('%Y-%m-%dT%H:00Z'), 'generated': dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%MZ'),
           'source': 'NOAA GEFS 31 senaryo ortalaması, 200 hPa rüzgâr', 'grid': {'lat0': 89, 'lon0': 0.5, 'step': 2, 'ny': 90, 'nx': 180},
           'windows': {}}
    for key, steps in WINDOWS.items():
        us, vs = [], []
        for st in steps:
            u, v = wind200(run, st)
            us.append(u)
            vs.append(v)
        r = compute(np.mean(us, axis=0), np.mean(vs, axis=0))
        # Akı okları: her 6° boylam × 5° enlem, 15–80°K ve 15–70°G
        arrows = []
        for la in list(range(80, 14, -5)) + list(range(-15, -71, -5)):
            j = int(round(89.5 - la))
            for lo in range(0, 360, 6):
                wx, wy = r['wx'][j, lo], r['wy'][j, lo]
                if np.isfinite(wx) and np.isfinite(wy) and np.hypot(wx, wy) > 4:
                    arrows.append([lo, la, round(float(wx), 1), round(float(wy), 1)])
        res['windows'][key] = {
            'from': (run + dt.timedelta(hours=steps[0] - 12)).strftime('%Y-%m-%dT%H:00Z'),
            'to': (run + dt.timedelta(hours=steps[-1])).strftime('%Y-%m-%dT%H:00Z'),
            'psi': np.round(coarse(r['psi']) / 1e6, 1).ravel().tolist(),       # ×10⁶ m²/s
            'chi': np.round(coarse(r['chi']) / 1e6, 2).ravel().tolist(),
            'spd': np.round(coarse(r['spd'])).astype(int).ravel().tolist(),    # m/s
            'arrows': arrows,
        }
        log(key, 'hazır', len(arrows), 'ok')
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as fh:
        json.dump(res, fh, ensure_ascii=False, separators=(',', ':'))
    log('yazıldı', rid, os.path.getsize(OUT) // 1024, 'KB')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log('HATA', repr(e))
