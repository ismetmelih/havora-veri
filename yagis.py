"""İl il aylık yağış (1981–bugün) ve kuraklık endeksi SPI (1, 3, 6, 12 ay).

Kaynak: CHIRPS v2.0 aylık (UCSB Climate Hazards Center; uydu + istasyon, 0,05°), IRI Data Library üzerinden yalnız
Türkiye kutusu. İl ortalaması: il sınırı içindeki bütün hücrelerin ortalaması (sınırlar: cihadturhan/tr-geojson).
SPI: her takvim ayı ve süre için 1991–2020'ye gama dağılımı uydurulup standart normale çevrilir (McKee 1993).
Kaynak ayda bir güncellenir (son ay ~2 ay geriden); her gün yalnız son ay bilgisine bakılır.
Çıktı (out/iklim/): yagis-il.json (aylık mm), spi.json (son 10 yılın SPI'si)
"""
import json
import os
import time
import urllib.request

import numpy as np
from scipy import stats
from scipy.io import netcdf_file

OUT = os.path.join('out', 'iklim')
UA = {'User-Agent': 'havaxpro-veri/1.0'}
BASE = 'https://iridl.ldeo.columbia.edu/SOURCES/.UCSB/.CHIRPS/.v2p0/.monthly/.global/.precipitation'
BOX = 'X/25.5/45/RANGEEDGES/Y/35.5/42.5/RANGEEDGES'
ILLER = 'https://cdn.jsdelivr.net/gh/cihadturhan/tr-geojson@master/geo/tr-cities-utf8.json'
MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
SCALES = (1, 3, 6, 12)
BASE_YEARS = (1991, 2020)


def get(url, timeout=300):
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return r.read()
        except Exception:  # noqa: BLE001
            if i == 2:
                raise
            time.sleep(10 * (i + 1))


def month_index(t):
    """CHIRPS T (1960-01'den bu yana ay, ortası .5) → 'YYYY-MM'"""
    m = int(t)
    return f'{1960 + m // 12}-{m % 12 + 1:02d}'


def fetch(y0, y1):
    """y0–y1 yılları: (aylar, dizi [ay, enlem, boylam], enlemler, boylamlar)"""
    url = f'{BASE}/{BOX}/T/(Jan%20{y0})/(Dec%20{y1})/RANGEEDGES/data.nc'
    path = os.path.join(OUT, '_chirps.nc')
    with open(path, 'wb') as f:
        f.write(get(url))
    try:
        with netcdf_file(path, mmap=False) as ds:
            a = ds.variables['precipitation'].data.astype(np.float32)
            t = ds.variables['T'].data.copy()
            lat = ds.variables['Y'].data.astype(float).copy()
            lon = ds.variables['X'].data.astype(float).copy()
    finally:
        os.remove(path)
    a = np.where(a < 0, np.nan, a)
    return [month_index(x) for x in t], a, lat, lon


def inside(px, py, ring):
    """Işın yöntemiyle noktalar halkanın içinde mi (vektörel)"""
    x, y = np.array(ring)[:, 0], np.array(ring)[:, 1]
    res = np.zeros(px.shape, bool)
    j = len(x) - 1
    for i in range(len(x)):
        cross = ((y[i] > py) != (y[j] > py)) & (px < (x[j] - x[i]) * (py - y[i]) / (y[j] - y[i] + 1e-12) + x[i])
        res ^= cross
        j = i
    return res


def masks(lat, lon):
    geo = json.loads(get(ILLER))
    gx, gy = np.meshgrid(lon, lat)
    out = {}
    for f in geo['features']:
        polys = f['geometry']['coordinates'] if f['geometry']['type'] == 'MultiPolygon' else [f['geometry']['coordinates']]
        m = np.zeros(gx.shape, bool)
        for poly in polys:
            ring = np.array(poly[0])
            sel = (gx >= ring[:, 0].min()) & (gx <= ring[:, 0].max()) & (gy >= ring[:, 1].min()) & (gy <= ring[:, 1].max())
            if not sel.any():
                continue
            hit = inside(gx[sel], gy[sel], poly[0])
            for hole in poly[1:]:
                hit &= ~inside(gx[sel], gy[sel], hole)
            m[sel] |= hit
        out[f['properties']['name']] = m
    return out


def spi(series, months):
    """Aylık yağış dizisi → {süre: SPI dizisi}; her takvim ayı ayrı uydurulur"""
    x = np.array(series, float)
    years = np.array([int(m[:4]) for m in months])
    mon = np.array([int(m[5:]) for m in months])
    res = {}
    for k in SCALES:
        acc = np.full(len(x), np.nan)
        for i in range(k - 1, len(x)):
            w = x[i - k + 1:i + 1]
            if np.isfinite(w).all():
                acc[i] = w.sum()
        out = np.full(len(x), np.nan)
        for m in range(1, 13):
            sel = mon == m
            ref = acc[sel & (years >= BASE_YEARS[0]) & (years <= BASE_YEARS[1])]
            ref = ref[np.isfinite(ref)]
            if len(ref) < 20:
                continue
            q0 = np.mean(ref == 0)                      # hiç yağış olmayan ayların payı
            pos = ref[ref > 0]
            a, _, scale = stats.gamma.fit(pos, floc=0)
            v = acc[sel]
            p = q0 + (1 - q0) * stats.gamma.cdf(v, a, loc=0, scale=scale)
            p = np.clip(p, 1e-6, 1 - 1e-6)
            out[sel] = np.where(np.isfinite(v), stats.norm.ppf(p), np.nan)
        res[k] = out
    return res


def main():
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, 'yagis-il.json')
    try:
        with open(path, encoding='utf-8') as f:
            old = json.load(f)
    except (OSError, ValueError):
        old = None
    if old and time.time() - os.path.getmtime(path) < 20 * 3600:
        print('Yağış: güncel')
        return
    last = month_index(float(get(f'{BASE}/T/last/VALUE/T/data.tsv', 60).split()[0]))
    if old and old['months'][-1] == last:
        os.utime(path)
        print('Yağış: yeni ay yok')
        return
    # Tam arşiv (ilk sefer) ya da yalnız son iki yıl (sonraki aylar; ön sürüm aylar düzelmiş olabilir)
    y_last = int(last[:4])
    spans = [(y, min(y + 9, y_last)) for y in range(1981, y_last + 1, 10)] if not old else [(y_last - 1, y_last)]
    months, prov, lat_lon = [], {}, None
    for y0, y1 in spans:
        mm, a, lat, lon = fetch(y0, y1)
        if lat_lon is None:
            lat_lon = (lat, lon)
            mk = masks(lat, lon)
        for name, m in mk.items():
            vals = [round(float(np.nanmean(a[i][m])), 1) if m.any() else None for i in range(len(mm))]
            prov.setdefault(name, []).extend(vals)
        months += mm
    if old:
        keep = [i for i, m in enumerate(old['months']) if m < months[0]]
        months = [old['months'][i] for i in keep] + months
        prov = {k: [old['prov'][k][i] for i in keep] + v for k, v in prov.items()}
    # Türkiye: illerin alan ağırlıklı ortalaması
    area = {k: int(m.sum()) for k, m in mk.items()}
    tot = sum(area.values())
    tr = [round(sum(prov[k][i] * area[k] for k in prov if prov[k][i] is not None) / tot, 1) for i in range(len(months))]
    with open(path, 'w', encoding='utf-8') as f:
        json.dump({'generated': time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime()), 'months': months, 'tr': tr, 'prov': prov,
                   'source': 'CHIRPS v2.0 (UCSB CHC; uydu + istasyon, 0,05°), il sınırı içindeki ortalama'},
                  f, ensure_ascii=False, separators=(',', ':'))
    # SPI: son 10 yıl (120 ay) yazılır
    n = 120
    r2 = lambda arr: [None if not np.isfinite(v) else round(float(v), 2) for v in arr[-n:]]   # noqa: E731
    out = {'generated': time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime()), 'months': months[-n:], 'base': f'{BASE_YEARS[0]}–{BASE_YEARS[1]}',
           'tr': {k: r2(v) for k, v in spi(tr, months).items()}, 'prov': {}}
    for name, series in prov.items():
        out['prov'][name] = {k: r2(v) for k, v in spi(series, months).items()}
    with open(os.path.join(OUT, 'spi.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    print(f'Yağış: {len(months)} ay, {len(prov)} il, son ay {months[-1]}')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # noqa: BLE001  (bu iş aksarsa model işi sürsün)
        print('Yağış alınamadı:', e)
