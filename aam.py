"""Atmosferin göreli açısal momentumu (AAM) ve GWO (Küresel Rüzgâr Salınımı) fazları.

NOAA PSL'nin hazır AAM serisi 2014'te bitti; burada GFS'ten kendimiz hesaplıyoruz:
  M = (2π a³ / g) ∫∫ [u] cos²φ dφ dp     ([u]: kuşak ortalaması batı rüzgârı, 1000–100 hPa, 10 seviye, 1°)
- Geçmiş: her günün 00Z GFS analizi (AWS açık veri, 1 Haziran 2021'den bugüne). Eksik günler turda en çok
  PER_RUN gün olarak doldurulur; günlük değerler out/analiz/aam-gunluk.json'da birikir.
- Tahmin: en yeni GFS 00Z koşusunun 24–384. saatleri (günde bir).
- Normal: bu serinin harmonik uyumu (ortalama + yılın 3 harmoniği); fark ve eğilim kendi standart sapmalarıyla ölçeklenir.
- GWO fazı (Weickmann ve Berry 2009): x = AAM farkı, y = eğilim (gün başına); faz 1 en düşük AAM (La Niña benzeri),
  faz 5 en yüksek AAM (El Niño benzeri), arada saat yönünde 8 faz.
Çıktı: out/analiz/aam.json
"""
import datetime as dt
import json
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.request

import eccodes
import numpy as np

OUT = os.path.join('out', 'analiz')
DAILY = os.path.join(OUT, 'aam-gunluk.json')
UA = {'User-Agent': 'havaxpro-veri/1.0'}
S3 = 'https://noaa-gfs-bdp-pds.s3.amazonaws.com'
LEVELS = (1000, 850, 700, 500, 400, 300, 250, 200, 150, 100)
START = dt.date(2021, 6, 1)
PER_RUN = 160
_GRIB = threading.Lock()   # eccodes iş parçacığı güvenli değil: GRIB okuma sırayla
A, G = 6.371e6, 9.80665


def log(*a):
    print(dt.datetime.utcnow().strftime('%H:%M:%S'), 'aam', *a, flush=True)


def get(url, headers=None, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={**UA, **(headers or {})}), timeout=90) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            if i == tries - 1:
                raise
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                raise
        time.sleep(4 * (i + 1))


def stem(day, run=0, fh=0):
    d = f'gfs.{day:%Y%m%d}/{run:02d}'
    sub = '' if day < dt.date(2021, 3, 23) else '/atmos'
    return f'{S3}/{d}{sub}/gfs.t{run:02d}z.pgrb2.1p00.f{fh:03d}'


def aam_of(url):
    """Bir GFS dosyasındaki 10 seviyenin u'sundan göreli AAM (×10²⁵ kg m² s⁻¹)"""
    idx = get(url + '.idx').decode().splitlines()
    rows = [ln.split(':') for ln in idx]
    want = {f'{lv} mb' for lv in LEVELS}
    chunks = []
    for i, r in enumerate(rows):
        if r[3] == 'UGRD' and r[4] in want:
            a = int(r[1])
            b = int(rows[i + 1][1]) - 1 if i + 1 < len(rows) else ''
            chunks.append(get(url, {'Range': f'bytes={a}-{b}'}))
    prof = {}
    with _GRIB, tempfile.NamedTemporaryFile(suffix='.grb2') as tmp:
        tmp.write(b''.join(chunks))
        tmp.flush()
        with open(tmp.name, 'rb') as f:
            while True:
                h = eccodes.codes_grib_new_from_file(f)
                if h is None:
                    break
                lv = eccodes.codes_get(h, 'level')
                nj, ni = eccodes.codes_get(h, 'Nj'), eccodes.codes_get(h, 'Ni')
                prof[lv] = eccodes.codes_get_values(h).reshape(nj, ni).mean(axis=1)   # kuşak ortalaması, 90 → −90
                eccodes.codes_release(h)
    if len(prof) < len(LEVELS):
        raise ValueError('seviye eksik')
    lat = np.radians(np.linspace(90, -90, len(next(iter(prof.values())))))
    col = np.array([np.trapz(prof[lv] * np.cos(lat) ** 2, -lat) for lv in LEVELS])   # her seviye: ∫[u]cos²φ dφ
    p = np.array(LEVELS) * 100.0
    return float(2 * np.pi * A ** 3 / G * np.trapz(col[::-1], p[::-1]) / 1e25)


def load_daily():
    if os.path.exists(DAILY):
        with open(DAILY, encoding='utf-8') as fh:
            return json.load(fh)
    return {}


def fill(daily):
    today = dt.datetime.utcnow().date()
    days = [START + dt.timedelta(d) for d in range((today - START).days)]
    missing = [d for d in days if d.isoformat() not in daily]
    # Önce son 120 gün (sayfa hemen dolsun), sonra eskiden yeniye arşiv
    recent = [d for d in missing if (today - d).days <= 120]
    order = sorted(recent, reverse=True) + [d for d in missing if d not in recent]
    def one(d):
        try:
            return d, round(aam_of(stem(d)), 4)
        except urllib.error.HTTPError:
            return d, None                       # dosya yok: bir daha denenmez
        except Exception as e:  # noqa: BLE001
            log(d, 'alınamadı', repr(e))
            return d, 'hata'
    done = 0
    with ThreadPoolExecutor(8) as ex:            # indirmeler paralel, GRIB okuma kilitli
        for d, v in ex.map(one, order[:PER_RUN]):
            if v != 'hata':
                daily[d.isoformat()] = v
                done += v is not None
    log(f'{done} gün eklendi, kalan {max(0, len(missing) - PER_RUN)}')


def forecast():
    """En yeni GFS 00Z koşusunun 24–384. saatleri (günde bir)"""
    now = dt.datetime.utcnow()
    for back in range(3):
        day = (now - dt.timedelta(days=back)).date()
        try:
            get(stem(day, 0, 384) + '.idx')
        except Exception:  # noqa: BLE001
            continue
        return day.isoformat(), [[(day + dt.timedelta(days=fh // 24)).isoformat(), round(aam_of(stem(day, 0, fh)), 4)] for fh in range(24, 385, 24)]
    return None, []


def harmonic(doy, y, k=3):
    t = 2 * np.pi * doy / 365.25
    X = np.column_stack([np.ones_like(t)] + [f(j * t) for j in range(1, k + 1) for f in (np.cos, np.sin)])
    coef, *_ = np.linalg.lstsq(X, y, rcond=None)
    return lambda d: np.column_stack([np.ones_like(d, dtype=float)] + [f(j * 2 * np.pi * np.asarray(d) / 365.25) for j in range(1, k + 1) for f in (np.cos, np.sin)]) @ coef


def main():
    os.makedirs(OUT, exist_ok=True)
    daily = load_daily()
    fill(daily)
    with open(DAILY, 'w', encoding='utf-8') as fh:
        json.dump(daily, fh, separators=(',', ':'))
    pts = sorted((dt.date.fromisoformat(k), v) for k, v in daily.items() if v is not None)
    if len(pts) < 365 * 2:
        log('normal için en az 2 yıl gerekli, şimdilik', len(pts), 'gün')
        return
    dates = [d for d, _ in pts]
    y = np.array([v for _, v in pts])
    doy = np.array([d.timetuple().tm_yday for d in dates], dtype=float)
    clim = harmonic(doy, y)
    anom = y - clim(doy)
    tend = np.gradient(anom)          # gün başına
    sa, st = float(np.std(anom)), float(np.std(tend))
    try:
        run, fc = forecast()
    except Exception as e:  # noqa: BLE001
        log('tahmin alınamadı', repr(e))
        run, fc = None, []

    def row(d, m, a, t):
        x, yv = a / sa, t / st
        th = np.degrees(np.arctan2(yv, x))
        phase = int((round((180 - th) / 45) % 8) + 1)
        return [d, round(m, 3), round(a, 3), round(x, 2), round(yv, 2), phase, round(float(np.hypot(x, yv)), 2)]

    hist = [row(d.isoformat(), m, a, t) for d, m, a, t in zip(dates, y, anom, tend)][-150:]
    fcr = []
    if fc:
        fd = [dt.date.fromisoformat(d) for d, _ in fc]
        fm = np.array([v for _, v in fc])
        fa = fm - clim(np.array([d.timetuple().tm_yday for d in fd], dtype=float))
        # Tahminin eğilimi: son analizle birleşik farkla
        fa_all = np.concatenate([[anom[-1]], fa])
        ft = np.gradient(fa_all)[1:]
        fcr = [row(d.isoformat(), m, a, t) for d, m, a, t in zip(fd, fm, fa, ft)]
    res = {'generated': dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%MZ'), 'days': len(pts), 'since': dates[0].isoformat(),
           'std': {'anom': round(sa, 3), 'tend': round(st, 4)}, 'unit': '×10²⁵ kg m² s⁻¹',
           'columns': ['tarih', 'aam', 'fark', 'x', 'y', 'faz', 'genlik'], 'history': hist, 'forecast_run': run, 'forecast': fcr}
    with open(os.path.join(OUT, 'aam.json'), 'w', encoding='utf-8') as fh:
        json.dump(res, fh, ensure_ascii=False, separators=(',', ':'))
    log('yazıldı', len(hist), 'gün,', len(fcr), 'tahmin')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log('HATA', repr(e))
