"""HAVORA model haritaları için veri üretici.

GFS (NOAA), ECMWF (açık veri) ve ICON-EU (DWD) modellerinin ham GRIB2 çıktılarını indirir,
iki bölgeyi keser ve sitenin okuyacağı küçük dosyalara çevirir:
  tr: Türkiye ve çevresi (30–46°K, 18–50°D, 0,25°, 3 saatte bir)
  eu: Avrupa, Atlantik ve Kuzey Afrika (20–72°K, 50°B–70°D, 0,5°, 6 saatte bir) — sistemlerin gelişini görmek için
  world: bütün dünya (kutuptan kutba, 1°, 6 saatte bir)

Çıktı: out/<model>/<koşu YYYYMMDDHH>/meta.json ve .../<alan>.<bölge>.bin.gz, out/index.json
  Her model için son KEEP koşu saklanır (dünya bölgesi yalnızca en yeni KEEP_WORLD koşuda).
  .bin.gz: gzip'li, adım adım art arda dizilmiş int16 ızgaralar; değer = ham * scale, eksik değer: -32768.
  Daha iyi sıkışsın diye her satır soldan farklarla (16 bit taşmalı) yazılır, sonra baytlar
  ayrılır: önce bütün düşük baytlar, ardından bütün yüksek baytlar.

Kullanım: python uret.py [gfs ecmwf icon] [--steps 0,3,6]  (adım verilmezse tamamı)
Yalnızca herkese açık veriler kullanılır; hiçbir anahtar ya da gizli bilgi gerekmez.
"""
import bz2
import datetime as dt
import gzip
import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.request

import eccodes
import numpy as np

from endeks import endeksler

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')

# Hedef ızgaralar: kuzeyden güneye, batıdan doğuya. every: kaç saatte bir adım yazılır
REGIONS = {
    'tr': {'latN': 46.0, 'latS': 30.0, 'lonW': 18.0, 'lonE': 50.0, 'step': 0.25, 'every': 3},
    'eu': {'latN': 72.0, 'latS': 20.0, 'lonW': -50.0, 'lonE': 70.0, 'step': 0.5, 'every': 6},
    'world': {'latN': 90.0, 'latS': -90.0, 'lonW': -180.0, 'lonE': 179.0, 'step': 1.0, 'every': 6},
}
# Bölgelerin noktaları tek bir düz dizide art arda tutulur; böylece birikmiş yağış gibi
# hesaplar iki bölge için aynı anda yapılır, write() en sonda bölgelere ayırır.
NPTS = 0
for _r in REGIONS.values():
    _r['lats'] = np.round(np.arange(_r['latN'], _r['latS'] - 1e-6, -_r['step']), 2)
    _r['lons'] = np.round(np.arange(_r['lonW'], _r['lonE'] + 1e-6, _r['step']), 2)
    _r['ny'], _r['nx'] = len(_r['lats']), len(_r['lons'])
    _r['off'] = NPTS
    NPTS += _r['ny'] * _r['nx']
# İndirilecek alan: bölgelerin hepsini kapsayan kutu
BOX_N = max(r['latN'] for r in REGIONS.values())
BOX_S = min(r['latS'] for r in REGIONS.values())
BOX_W = min(r['lonW'] for r in REGIONS.values())
BOX_E = max(r['lonE'] for r in REGIONS.values())

# Alanlar: ad -> (birim, ölçek). Değerler int16 olarak ham/ölçek şeklinde saklanır.
FIELDS = {
    't850': ('°C', 0.1),     # 850 hPa sıcaklık
    'z850': ('m', 1),        # 850 hPa jeopotansiyel yükseklik
    'z500': ('m', 1),        # 500 hPa jeopotansiyel yükseklik
    'mslp': ('hPa', 0.1),    # deniz seviyesi basıncı
    't2m': ('°C', 0.1),      # 2 m sıcaklık
    'gust': ('km/sa', 1),    # rüzgar hamlesi
    'pr6': ('mm', 0.1),      # son 6 saatte yağış
    'sn6': ('cm', 0.1),      # son 6 saatte kar (10:1 oranıyla)
    't500': ('°C', 0.1),     # 500 hPa sıcaklık
    'pr24': ('mm', 0.1),     # son 24 saatte yağış
    'sn24': ('cm', 0.1),     # son 24 saatte kar
    'z500a': ('m', 1),       # 500 hPa yüksekliğin 1990–2019 ortalamasından farkı
    't850a': ('°C', 0.1),    # 850 hPa sıcaklığın 1990–2019 ortalamasından farkı
    'snacc': ('cm', 0.1),    # koşu başından beri toplam kar
    'sdep': ('cm', 1),       # yerdeki kar kalınlığı
    'frz': ('m', 10),        # donma seviyesi (0 °C yüksekliği)
    'z50': ('m', 1),         # 50 hPa yükseklik (stratosfer)
    't50': ('°C', 0.1),      # 50 hPa sıcaklık
    'z10': ('m', 2),         # 10 hPa yükseklik (kutup girdabı)
    't10': ('°C', 0.1),      # 10 hPa sıcaklık
    'z30': ('m', 1),         # 30 hPa yükseklik (yalnızca GFS)
    't30': ('°C', 0.1),
    'z70': ('m', 1),         # 70 hPa yükseklik (yalnızca GFS)
    't70': ('°C', 0.1),
    'z100': ('m', 1),        # 100 hPa yükseklik (tropopoz civarı)
    't100': ('°C', 0.1),
}
# Yalnızca bazı bölgelerde yazılan alanlar (stratosfer haritaları kutup/dünya görünümü için)
FIELD_REGIONS = {k: ('world',) for k in ('z50', 't50', 'z10', 't10', 'z30', 't30', 'z70', 't70', 'z100', 't100')}
# GFS stratosfer kesitleri için seviyeler (hPa; 1° ayrı paket, 20°K kuzeyi)
PROF_LEVELS = [1, 2, 3, 5, 7, 10, 20, 30, 50, 70, 100, 150, 200, 250, 300, 500, 700, 850, 1000]
ARCHIVE_DAYS = 60   # geçmiş analiz arşivi (ECMWF 00Z/12Z başlangıç haritaları) kaç gün saklanır

KEEP = 4         # model başına saklanan koşu sayısı
MIN_H = 12       # yeni koşu, ilk bu kadar saati hazır olunca yayınlanır (kalanı geldikçe eklenir)
# Koşunun meta.json → series'inde olması gereken hesaplar; eksikse koşu yeniden işlenir
SERIES_NEEDED = {'gfs': ('vt100', 'wave1_10', 'u60_prof'), 'ecmwf': ('vt100', 'wave1_10')}
CACHE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'onbellek')   # indirilen ham adımlar (yayınlanmaz)
KEEP_WORLD = 2   # dünya bölgesi (en büyük dosyalar) bu kadar yeni koşuda tutulur
KLIMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'klima')

UA = {'User-Agent': 'havora-veri/1.0 (+https://github.com/ismetmelih/havora-veri)'}


def log(*a):
    print(time.strftime('%H:%M:%S'), *a, flush=True)


def get(url, tries=4):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
                return r.read()
        except Exception as e:  # noqa: BLE001
            if i == tries - 1:
                raise
            log('tekrar deneniyor', url[:90], e)
            time.sleep(5 * (i + 1))


def exists(url):
    try:
        req = urllib.request.Request(url, headers=UA, method='HEAD')
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def messages(data):
    """GRIB2 baytlarından (kısa ad, seviye türü, seviye, 2B dizi, enlemler, boylamlar, birikim başlangıcı) üret."""
    with tempfile.NamedTemporaryFile(suffix='.grb2', delete=False) as tmp:
        tmp.write(data)
        path = tmp.name
    try:
        with open(path, 'rb') as f:
            while True:
                h = eccodes.codes_grib_new_from_file(f)
                if h is None:
                    break
                try:
                    ni, nj = eccodes.codes_get(h, 'Ni'), eccodes.codes_get(h, 'Nj')
                    vals = eccodes.codes_get_values(h).reshape(nj, ni)
                    # değerlerle aynı sırada (0° boylamını aşan kesitlerde de doğru)
                    lats = np.round(eccodes.codes_get_array(h, 'latitudes').reshape(nj, ni)[:, 0], 4)
                    lons = np.round(eccodes.codes_get_array(h, 'longitudes').reshape(nj, ni)[0, :], 4)
                    missing = eccodes.codes_get(h, 'missingValue')
                    vals = np.where(vals == missing, np.nan, vals)
                    yield (eccodes.codes_get(h, 'shortName'), eccodes.codes_get(h, 'typeOfLevel'),
                           eccodes.codes_get(h, 'level'), vals, lats, lons, eccodes.codes_get(h, 'startStep'))
                finally:
                    eccodes.codes_release(h)
    finally:
        os.unlink(path)


def subset(vals, lats, lons):
    """Kaynak ızgaradan bütün bölgelerin noktalarını seç → NPTS uzunlukta düz dizi.
    Kaynağın kapsamadığı noktalar (ör. ICON-EU'nun dışı) NaN olur."""
    lons = np.where(lons > 180, lons - 360, lons)
    parts = []
    for r in REGIONS.values():
        yi = np.array([np.argmin(np.abs(lats - la)) for la in r['lats']])
        xi = np.array([np.argmin(np.abs(lons - lo)) for lo in r['lons']])
        dy, dx = np.abs(lats[yi] - r['lats']), np.abs(lons[xi] - r['lons'])
        inside_y = (r['lats'] >= lats.min() - 0.01) & (r['lats'] <= lats.max() + 0.01)
        inside_x = (r['lons'] >= lons.min() - 0.01) & (r['lons'] <= lons.max() + 0.01)
        if (dy[inside_y] > 0.01).any() or (dx[inside_x] > 0.01).any():
            raise ValueError('kaynak ızgara hedef ızgarayla hizalı değil')
        a = vals[np.ix_(yi, xi)].astype(float)
        a[~inside_y, :] = np.nan
        a[:, ~inside_x] = np.nan
        parts.append(a.ravel())
    return np.concatenate(parts)


def cumulative_to(steps, cum, hours=6):
    """Başlangıçtan beri birikmiş değerlerden 'son <hours> saatte' toplamı (ilk <hours> saatte yok)."""
    out = {}
    for s in steps:
        if s in cum and s >= hours and (s - hours) in cum:
            out[s] = np.maximum(cum[s] - cum[s - hours], 0)
    return out


def accumulations(f, steps, rain, snow):
    """Birikmiş yağış ve kardan 6 ve 24 saatlik toplamlar, koşu başından beri toplam kar"""
    f['pr6'], f['sn6'] = cumulative_to(steps, rain, 6), cumulative_to(steps, snow, 6)
    f['pr24'], f['sn24'] = cumulative_to(steps, rain, 24), cumulative_to(steps, snow, 24)
    f['snacc'] = {s: np.maximum(snow[s], 0) for s in steps if s in snow}


class Climate:
    """Uzun yıllar ortalaması (ERA5 1990–2019, WeatherBench2): her 5 günde bir, 1,5°.
    z500: m, t850: 0,1 °C. Bölge noktalarına çift doğrusal, günler arasında doğrusal ara değer."""

    def __init__(self, var):
        d = np.load(os.path.join(KLIMA, f'{var}_era5_1990_2019.npz'))
        self.z, self.doy = d[var].astype(float) * (0.1 if var == 't850' else 1), d['doy']
        lat, lon = d['lat'], d['lon']          # lat 90 → -90, lon 0 → 358.5
        plat = np.concatenate([np.repeat(r['lats'], r['nx']) for r in REGIONS.values()])
        plon = np.concatenate([np.tile(r['lons'], r['ny']) for r in REGIONS.values()]) % 360
        step = lat[0] - lat[1]
        fy, fx = (lat[0] - plat) / step, (plon - lon[0]) / step
        self.y0 = np.clip(np.floor(fy).astype(int), 0, len(lat) - 2)
        self.x0 = np.floor(fx).astype(int) % len(lon)
        self.x1 = (self.x0 + 1) % len(lon)
        self.ty, self.tx = fy - self.y0, fx - np.floor(fx)

    def at(self, when):
        doy = when.timetuple().tm_yday
        k = np.searchsorted(self.doy, doy, side='right') - 1
        k2 = (k + 1) % len(self.doy)
        span = (self.doy[k2] - self.doy[k]) % 366 or 5
        w = ((doy - self.doy[k]) % 366) / span
        g = self.z[k] * (1 - w) + self.z[k2] * w
        y0, y1 = self.y0, self.y0 + 1
        return ((g[y0, self.x0] * (1 - self.tx) + g[y0, self.x1] * self.tx) * (1 - self.ty)
                + (g[y1, self.x0] * (1 - self.tx) + g[y1, self.x1] * self.tx) * self.ty)


def add_anomaly(f, run):
    """500 hPa ve 850 hPa anomali: model değeri eksi o günün uzun yıllar ortalaması"""
    for var in ('z500', 't850'):
        if f.get(var):
            clim = Climate(var)
            f[f'{var}a'] = {s: z - clim.at(run + dt.timedelta(hours=s)) for s, z in f[var].items()}


def heat_flux(v, t, lats):
    """Kuşak ortalaması girdap ısı akısı [v'T'] (K·m/s), 45–75°K kosinüs ağırlıklı ortalama.
    v, t: (enlem, boylam) ızgaraları. Pozitif ve büyükse girdaba yukarı doğru güçlü dalga enerjisi gidiyor demektir."""
    band = (lats >= 45) & (lats <= 75)
    vp = v[band] - np.nanmean(v[band], axis=1, keepdims=True)
    tp = t[band] - np.nanmean(t[band], axis=1, keepdims=True)
    w = np.cos(np.radians(lats[band]))
    return round(float(np.nansum(np.nanmean(vp * tp, axis=1) * w) / w.sum()), 2)


def wave_amps(z, lats):
    """60°K'de yüksekliğin dalga-1 ve dalga-2 genliği (m): dalga-1 girdabın kaymasını, dalga-2 bölünmesini gösterir"""
    row = z[np.argmin(np.abs(lats - 60))]
    c = np.fft.rfft(row - row.mean())
    return [round(float(2 * abs(c[k]) / len(row)), 0) for k in (1, 2)]


def strat_series(f, steps, diag=None):
    """Kutup girdabı: 60°K'de 10 hPa kuşak ortalaması batı rüzgarı (m/s) ve 60–90°K ortalama 10 hPa sıcaklığı (°C);
    10 hPa dalga-1/2 genliği, 100 hPa ısı akısı; diag varsa (GFS) seviye seviye 60°K rüzgarı ve kutup sıcaklığı"""
    w = REGIONS['world']
    n = w['ny'] * w['nx']
    lats = w['lats']
    grid = lambda a: a[w['off']:w['off'] + n].reshape(w['ny'], w['nx'])  # noqa: E731
    out = {'u60_10': [], 'tpolar_10': [], 'wave1_10': [], 'wave2_10': [], 'vt100': []}
    for s in steps:
        z = f.get('z10', {}).get(s)
        a1, a2 = wave_amps(grid(z), lats) if z is not None else (None, None)
        out['wave1_10'].append(a1)
        out['wave2_10'].append(a2)
        v, t = f.get('v100', {}).get(s), f.get('t100', {}).get(s)
        out['vt100'].append(heat_flux(grid(v), grid(t), lats) if v is not None and t is not None else (diag or {}).get(s, {}).get('vt100'))
    if diag:
        out['prof_levels'] = PROF_LEVELS
        out['u60_prof'] = [diag.get(s, {}).get('u60') for s in steps]
        out['tcap_prof'] = [diag.get(s, {}).get('tcap') for s in steps]
    cap = lats >= 60
    weights = np.cos(np.radians(lats[cap]))
    for s in steps:
        u, t = f.get('u10', {}).get(s), f.get('t10', {}).get(s)
        if u is None or t is None:
            out['u60_10'].append(None)
            out['tpolar_10'].append(None)
            continue
        u = u[w['off']:w['off'] + n].reshape(w['ny'], w['nx'])
        t = t[w['off']:w['off'] + n].reshape(w['ny'], w['nx'])
        out['u60_10'].append(round(float(np.nanmean(u[np.argmin(np.abs(lats - 60))])), 1))
        out['tpolar_10'].append(round(float(np.nansum(np.nanmean(t[cap], axis=1) * weights) / weights.sum()), 1))
    return out


def write(model, run, steps, fields, extra=None, regions=tuple(REGIONS), base=None, keep=True, diag=None):
    add_anomaly(fields, run)
    if fields.get('u10') and fields.get('t10'):
        extra = {**(extra or {}), 'series': {'steps': steps, **strat_series(fields, steps, diag)}}
    d = os.path.join(base or os.path.join(OUT, model), f'{run:%Y%m%d%H}')
    os.makedirs(d, exist_ok=True)
    meta = {
        'model': model, 'run': run.strftime('%Y-%m-%dT%H:00Z'), 'generated': dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%MZ'),
        'encoding': 'row-delta+byte-split', 'regions': {}, 'fields': {}, **(extra or {}),
    }
    for key in regions:
        r = REGIONS[key]
        rsteps = [s for s in steps if s % r['every'] == 0]
        meta['regions'][key] = {'latN': r['latN'], 'lonW': r['lonW'], 'step': r['step'], 'ny': r['ny'], 'nx': r['nx'], 'steps': rsteps}
        n = r['ny'] * r['nx']
        for name, (unit, scale) in FIELDS.items():
            if name not in fields or key not in FIELD_REGIONS.get(name, (key,)):
                continue
            buf = np.full((len(rsteps), n), -32768, dtype='<i2')
            have = 0
            for i, s in enumerate(rsteps):
                a = fields[name].get(s)
                if a is None:
                    continue
                q = np.round(a[r['off']:r['off'] + n] / scale)
                q = np.where(np.isfinite(q), np.clip(q, -32767, 32767), -32768)
                buf[i] = q.astype('<i2')
                have += 1
            if not have:
                continue
            u = buf.view('<u2').reshape(len(rsteps), r['ny'], r['nx'])
            diff = u.copy()
            diff[:, :, 1:] = u[:, :, 1:] - u[:, :, :-1]  # uint16: taşma sarar, geri dönüşte birebir aynı
            packed = diff.astype('<u2').view(np.uint8).reshape(-1, 2).T.tobytes()
            with gzip.open(os.path.join(d, f'{name}.{key}.bin.gz'), 'wb', compresslevel=9) as f:
                f.write(packed)
            meta['fields'][name] = {'unit': unit, 'scale': scale}
    with open(os.path.join(d, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False)
    log(model, run, len(steps), 'adım,', ', '.join(meta['fields']), 'yazıldı')
    if keep:
        prune(model)


def runs_in(d):
    """Bir klasördeki koşu klasörleri (YYYYMMDDHH), en yeni önce"""
    if not os.path.isdir(d):
        return []
    return sorted((x for x in os.listdir(d) if re.fullmatch(r'\d{10}', x) and os.path.exists(os.path.join(d, x, 'meta.json'))), reverse=True)


def runs_of(model):
    return runs_in(os.path.join(OUT, model))


def prune(model):
    """Eski koşuları sil; dünya bölgesini yalnızca en yeni KEEP_WORLD koşuda tut; eski düz yapıyı temizle"""
    d = os.path.join(OUT, model)
    for x in os.listdir(d):  # önceki sürümün out/<model>/*.bin.gz ve meta.json dosyaları
        if os.path.isfile(os.path.join(d, x)):
            os.remove(os.path.join(d, x))
    runs = runs_of(model)
    for old in runs[KEEP:]:
        shutil.rmtree(os.path.join(d, old))
    for i, r in enumerate(runs[:KEEP]):
        if i < KEEP_WORLD:
            continue
        rd = os.path.join(d, r)
        removed = False
        for x in os.listdir(rd):
            if x.endswith('.world.bin.gz'):
                os.remove(os.path.join(rd, x))
                removed = True
        if removed:
            with open(os.path.join(rd, 'meta.json'), encoding='utf-8') as fh:
                meta = json.load(fh)
            meta['regions'].pop('world', None)
            with open(os.path.join(rd, 'meta.json'), 'w', encoding='utf-8') as fh:
                json.dump(meta, fh, ensure_ascii=False)


def published(model, run):
    return f'{run:%Y%m%d%H}' in runs_of(model)


def recent_runs(hours):
    now = dt.datetime.utcnow()
    base = now.replace(minute=0, second=0, microsecond=0)
    for back in range(0, 48):
        t = base - dt.timedelta(hours=back)
        if t.hour in hours:
            yield t


def standard_steps(maxh):
    return list(range(0, min(maxh, 120) + 1, 3)) + list(range(126, maxh + 1, 6))


# ---------------------------------------------------------------- Kademeli üretim
# Merkezler bir koşunun saatlerini sırayla yayınlar (GFS'in 16. günü başlangıçtan ~5 saat sonra çıkar).
# Beklemek yerine ilk MIN_H saat hazır olunca koşu yayınlanır ("complete": false), sonraki turlarda
# gelen saatler eklenir. İndirilen her adım CACHE'te saklanır; aynı adım iki kez indirilmez.

def meta_of(model, rid):
    path = os.path.join(OUT, model, rid, 'meta.json')
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


def cached(key, fetch):
    """Önbellekteki baytlar ya da fetch() ile indirilip önbelleğe yazılanlar"""
    path = os.path.join(CACHE, key)
    if os.path.exists(path):
        with open(path, 'rb') as fh:
            return fh.read()
    data = fetch()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + '.tmp', 'wb') as fh:
        fh.write(data)
    os.replace(path + '.tmp', path)
    return data


def is_cached(key):
    return os.path.exists(os.path.join(CACHE, key))


def candidates(model, ready):
    """İşlenecek koşular: ilk MIN_H saati hazır en yeni koşu ve yarım yayınlanmış koşular (eskiden yeniye)"""
    runs = []
    for t in recent_runs((0, 6, 12, 18)):
        if ready(t, 0) and ready(t, MIN_H):
            runs.append(t)
            break
    for rid in runs_of(model):
        m = meta_of(model, rid)
        if m and m.get('complete') is False:
            runs.append(dt.datetime.strptime(rid, '%Y%m%d%H'))
    runs = sorted(set(runs))
    # Önbellekte artık işlenmeyecek koşuların adımlarını sil
    cdir = os.path.join(CACHE, model)
    keep = {f'{r:%Y%m%d%H}' for r in runs}
    if os.path.isdir(cdir):
        for x in os.listdir(cdir):
            if x not in keep:
                shutil.rmtree(os.path.join(cdir, x), ignore_errors=True)
    return runs


def progressive(model, run, want, ready, fetch, steps_filter):
    """Hazır adımları sırayla topla. Dönen: (adımlar, tamam mı) ya da yapılacak iş yoksa None"""
    rid = f'{run:%Y%m%d%H}'
    old = meta_of(model, rid)
    # Yeni hesaplar eklenince (ör. stratosfer kesitleri) eski biçimde yazılmış koşu bir kez yeniden işlenir
    stale = bool(old) and any(k not in old.get('series', {}) for k in SERIES_NEEDED.get(model, ()))
    if old and old.get('complete', True) and not steps_filter and not stale:
        log(model, 'yeni koşu yok', run)
        return None
    had = 0 if stale else (len(old['regions']['tr']['steps']) if old else 0)
    got = []
    for st in want:
        if not is_cached(f'{model}/{rid}/{st}') and not ready(run, st):
            break   # sonraki adımlar henüz yayınlanmadı
        try:
            fetch(st)
        except urllib.error.HTTPError as e:
            log(model, run, st, 'adım alınamadı', e.code)
            break
        got.append(st)
    tr_got = [x for x in got if x % REGIONS['tr']['every'] == 0]
    if not got or (len(tr_got) <= had and not steps_filter):
        log(model, run, 'yeni adım yok', f'({len(got)}/{len(want)})')
        return None
    log(model, run, f'{len(got)}/{len(want)} adım hazır')
    return got, len(got) == len(want)


# ---------------------------------------------------------------- GFS (NOAA)

GFS_BASE = 'https://nomads.ncep.noaa.gov'


def gfs(steps_filter=None):
    def ready(run, st):
        d = f'gfs.{run:%Y%m%d}/{run:%H}/atmos'
        return exists(f'{GFS_BASE}/pub/data/nccf/com/gfs/prod/{d}/gfs.t{run:%H}z.pgrb2.0p25.f{st:03d}.idx')

    for run in candidates('gfs', ready):
        gfs_run(run, ready, steps_filter)


def gfs_run(run, ready, steps_filter):
    rid = f'{run:%Y%m%d%H}'
    want = steps_filter or standard_steps(384)
    f = {k: {} for k in ('t850', 'z850', 'z500', 't500', 'mslp', 't2m', 'gust', 'sdep', 'frz', 'z50', 't50', 'z10', 't10', 'u10',
                         'z30', 't30', 'z70', 't70', 'z100', 't100')}
    apcp, csnow = {}, {}
    diag = {}

    def fetch_prof(st):
        """1° ayrı paket (20–90°K): 19 seviyede u ve T, 100 hPa'da v → 60°K rüzgar profili, kutup sıcaklık profili, ısı akısı"""
        lev = ''.join(f'&lev_{x}_mb=on' for x in PROF_LEVELS)
        q = (f'dir=%2Fgfs.{run:%Y%m%d}%2F{run:%H}%2Fatmos&file=gfs.t{run:%H}z.pgrb2.1p00.f{st:03d}'
             f'&var_UGRD=on&var_VGRD=on&var_TMP=on{lev}&subregion=&toplat=90&leftlon=0&rightlon=359&bottomlat=20')

        def dl():
            time.sleep(0.6)
            return get(f'{GFS_BASE}/cgi-bin/filter_gfs_1p00.pl?{q}')
        u, t, v = {}, {}, {}
        lats = None
        for name, ltype, lv, vals, la, _lo, _ in messages(cached(f'gfs/{rid}/{st}.p1', dl)):
            if ltype != 'isobaricInhPa':
                continue
            lats = la   # messages(): değerler (enlem, boylam) ızgarası, la satırların enlemleri
            {'u': u, 't': t, 'v': v}.get(name, {})[int(lv)] = vals
        if lats is None:
            return
        cap = lats >= 60
        wcap = np.cos(np.radians(lats[cap]))
        r60 = int(np.argmin(np.abs(lats - 60)))
        diag[st] = {
            'u60': [round(float(np.nanmean(u[x][r60])), 1) if x in u else None for x in PROF_LEVELS],
            'tcap': [round(float(np.nansum(np.nanmean(t[x][cap], axis=1) * wcap) / wcap.sum()) - 273.15, 1) if x in t else None for x in PROF_LEVELS],
            'vt100': heat_flux(v[100], t[100], lats) if 100 in v and 100 in t else None,
        }

    def fetch(st):
        q = (f'dir=%2Fgfs.{run:%Y%m%d}%2F{run:%H}%2Fatmos&file=gfs.t{run:%H}z.pgrb2.0p25.f{st:03d}'
             '&var_TMP=on&var_HGT=on&var_PRMSL=on&var_APCP=on&var_CSNOW=on&var_GUST=on&var_SNOD=on&var_UGRD=on'
             '&lev_850_mb=on&lev_500_mb=on&lev_100_mb=on&lev_70_mb=on&lev_50_mb=on&lev_30_mb=on&lev_10_mb=on&lev_mean_sea_level=on&lev_surface=on'
             '&lev_2_m_above_ground=on&lev_0C_isotherm=on'
             f'&subregion=&toplat={BOX_N}&leftlon={BOX_W}&rightlon={BOX_E}&bottomlat={BOX_S}')

        def dl():
            time.sleep(0.6)  # NOMADS'a nazik ol (dakikada en fazla ~120 istek)
            return get(f'{GFS_BASE}/cgi-bin/filter_gfs_0p25.pl?{q}')
        for name, ltype, lev, vals, lats, lons, start in messages(cached(f'gfs/{rid}/{st}', dl)):
            a = subset(vals, lats, lons)
            if name == 't' and lev == 850 and ltype == 'isobaricInhPa':
                f['t850'][st] = a - 273.15
            elif name == 't' and lev == 500 and ltype == 'isobaricInhPa':
                f['t500'][st] = a - 273.15
            elif name == 't' and lev in (100, 70, 50, 30, 10) and ltype == 'isobaricInhPa':
                f[f't{lev}'][st] = a - 273.15
            elif name == 'gh' and ltype == 'isothermZero':
                f['frz'][st] = a
            elif name == 'gh' and ltype == 'isobaricInhPa' and lev in (850, 500, 100, 70, 50, 30, 10):
                f[f'z{lev}'][st] = a
            elif name == 'u' and lev == 10 and ltype == 'isobaricInhPa':
                f['u10'][st] = a
            elif name == 'sde':
                f['sdep'][st] = a * 100       # m → cm
            elif name == 'prmsl':
                f['mslp'][st] = a / 100
            elif name == '2t':
                f['t2m'][st] = a - 273.15
            elif name == 'gust':
                f['gust'][st] = a * 3.6
            elif name == 'tp' and start == 0:
                apcp[st] = a  # koşu başından beri toplam (dosyada bir de 6 saatlik kova var, o atlanır)
            elif name == 'csnow':
                csnow[st] = a
        try:
            fetch_prof(st)
        except Exception as e:  # noqa: BLE001 — kesit paketi gelmezse harita verisi yine yayınlansın
            log('GFS kesit paketi alınamadı', st, repr(e))

    res = progressive('gfs', run, want, ready, fetch, steps_filter)
    if not res:
        return
    steps, complete = res
    zero = np.zeros(NPTS)
    cum, cum_snow, prev = {0: zero}, {0: zero}, 0
    for st in sorted(steps)[1:]:
        if st not in apcp or prev not in cum:
            continue
        cum[st] = apcp[st]
        # Kar: önceki adımdan bu yana yağışı, adım anındaki "kar yağıyor" bayrağıyla say (yaklaşık)
        cum_snow[st] = cum_snow[prev] + np.maximum(apcp[st] - cum[prev], 0) * csnow.get(st, 0)
        prev = st
    accumulations(f, steps, cum, cum_snow)
    write('gfs', run, steps, f, {'name': 'GFS', 'source': 'NOAA NCEP', 'resolution': '0,25°', 'complete': complete, 'expected': want[-1]}, diag=diag)


# ---------------------------------------------------------------- ECMWF (açık veri)

ECMWF_BASE = 'https://data.ecmwf.int/forecasts'


def ecmwf_stem(run, st):
    return f'{ECMWF_BASE}/{run:%Y%m%d}/{run:%H}z/ifs/0p25/oper/{run:%Y%m%d%H}0000-{st}h-oper-fc'


def ecmwf(steps_filter=None):
    def ready(run, st):
        return exists(f'{ecmwf_stem(run, st)}.index')

    for run in candidates('ecmwf', ready):
        ecmwf_run(run, ready, steps_filter)


def ecmwf_run(run, ready, steps_filter):
    rid = f'{run:%Y%m%d%H}'
    maxh = 360 if run.hour in (0, 12) else 144  # 06/18 koşuları 6 gün
    want = steps_filter or standard_steps(maxh)
    params = {('t', '850'), ('t', '500'), ('gh', '850'), ('gh', '500'), ('msl', None), ('2t', None), ('10fg', None), ('tp', None), ('sf', None),
              ('t', '50'), ('gh', '50'), ('t', '10'), ('gh', '10'), ('u', '10'), ('sd', None), ('rsn', None),
              ('t', '100'), ('gh', '100'), ('v', '100')}
    f = {k: {} for k in ('t850', 'z850', 'z500', 't500', 'mslp', 't2m', 'gust', 'sdep', 'z50', 't50', 'z10', 't10', 'u10', 'z100', 't100', 'v100')}
    swe, rho = {}, {}
    tp, sf = {}, {}

    def fetch(st):
        stem = ecmwf_stem(run, st)

        def dl():
            index = [json.loads(line) for line in get(f'{stem}.index').decode().splitlines() if line.strip()]
            chunks = []
            for r in index:  # yalnızca gereken mesajları bayt aralığıyla indir
                if (r['param'], r.get('levelist')) not in params:
                    continue
                req = urllib.request.Request(f'{stem}.grib2', headers={**UA, 'Range': f"bytes={r['_offset']}-{r['_offset'] + r['_length'] - 1}"})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    chunks.append(resp.read())
            return b''.join(chunks)
        for name, ltype, lev, vals, lats, lons, _ in messages(cached(f'ecmwf/{rid}/{st}', dl)):
            a = subset(vals, lats, lons)
            if name == 't' and lev in (850, 500, 100, 50, 10):
                f[f't{lev}'][st] = a - 273.15
            elif name == 'gh' and lev in (850, 500, 100, 50, 10):
                f[f'z{lev}'][st] = a
            elif name == 'u' and lev == 10:
                f['u10'][st] = a
            elif name == 'v' and lev == 100:
                f['v100'][st] = a
            elif name == 'sd':
                swe[st] = a                # kar su eşdeğeri (m)
            elif name == 'rsn':
                rho[st] = a                # kar yoğunluğu (kg/m³)
            elif name == 'msl':
                f['mslp'][st] = a / 100
            elif name == '2t':
                f['t2m'][st] = a - 273.15
            elif name == '10fg':
                f['gust'][st] = a * 3.6
            elif name == 'tp':
                tp[st] = a * 1000          # m → mm
            elif name == 'sf':
                sf[st] = a * 1000          # m su eşdeğeri → mm ≈ cm kar (10:1)

    res = progressive('ecmwf', run, want, ready, fetch, steps_filter)
    if not res:
        return
    steps, complete = res
    tp.setdefault(0, np.zeros(NPTS))
    sf.setdefault(0, np.zeros(NPTS))
    for st in swe:
        if st in rho:
            f['sdep'][st] = np.where(rho[st] > 0, swe[st] * 1000 / np.maximum(rho[st], 50) * 100, 0)  # cm
    accumulations(f, steps, tp, sf)
    write('ecmwf', run, steps, f, {'name': 'ECMWF', 'source': 'ECMWF açık veri (CC BY 4.0)', 'resolution': '0,25°',
                                   'complete': complete, 'expected': want[-1]})


# ---------------------------------------------------------------- ICON-EU (DWD)

ICON_BASE = 'https://opendata.dwd.de/weather/nwp/icon-eu/grib'
# Her adımda indirilen değişkenler: (değişken, seviye); 0. saatte birikimli olanlar yok
ICON_VARS = [('t', 850), ('t', 500), ('fi', 850), ('fi', 500), ('pmsl', None), ('t_2m', None), ('h_snow', None), ('hzerocl', None)]
ICON_ACC = [('vmax_10m', None), ('tot_prec', None), ('snow_gsp', None), ('snow_con', None)]


def icon_url(run, var, st, level=None):
    kind = 'pressure-level' if level else 'single-level'
    lv = f'_{level}' if level else ''
    return f'{ICON_BASE}/{run:%H}/{var.lower()}/icon-eu_europe_regular-lat-lon_{kind}_{run:%Y%m%d%H}_{st:03d}{lv}_{var.upper()}.grib2.bz2'


def icon(steps_filter=None):
    def ready(run, st):
        # Adımın bütün değişkenleri yayınlanmış mı (DWD her değişkeni ayrı dosya olarak koyar)
        return all(exists(icon_url(run, v, st, lv)) for v, lv in ICON_VARS + (ICON_ACC if st else []))

    for run in candidates('icon', ready):
        icon_run(run, ready, steps_filter)


def icon_run(run, ready, steps_filter):
    rid = f'{run:%Y%m%d%H}'
    want = steps_filter or list(range(0, 121, 3))
    g = 9.80665
    f = {k: {} for k in ('t850', 'z850', 'z500', 't500', 'mslp', 't2m', 'gust', 'sdep', 'frz')}
    prec, snow = {}, {}

    def grab(var, st, level=None):
        data = cached(f'icon/{rid}/{st}/{var}{level or ""}', lambda: get(icon_url(run, var, st, level)))
        for _, _, _, vals, lats, lons, _ in messages(bz2.decompress(data)):
            return subset(vals, lats, lons)
        return None

    def fetch(st):
        row = {
            't850': grab('t', st, 850) - 273.15, 't500': grab('t', st, 500) - 273.15,
            'z850': grab('fi', st, 850) / g, 'z500': grab('fi', st, 500) / g,
            'mslp': grab('pmsl', st) / 100, 't2m': grab('t_2m', st) - 273.15,
            'sdep': grab('h_snow', st) * 100, 'frz': grab('hzerocl', st),
        }
        if st > 0:
            row['gust'] = grab('vmax_10m', st) * 3.6
            prec[st] = grab('tot_prec', st)
            snow[st] = grab('snow_gsp', st) + grab('snow_con', st)
        for k, v in row.items():
            f[k][st] = v

    res = progressive('icon', run, want, ready, fetch, steps_filter)
    if not res:
        return
    steps, complete = res
    prec[0] = np.zeros(NPTS)
    snow[0] = np.zeros(NPTS)
    accumulations(f, steps, prec, snow)
    write('icon', run, steps, f, {'name': 'ICON', 'source': 'DWD açık veri', 'resolution': '0,0625° → 0,25°', 'coverage': '29,5–70,5°K, 23,5°B–62,5°D',
                                  'complete': complete, 'expected': want[-1]}, regions=('tr', 'eu'))  # ICON-EU dünyayı kapsamaz


# ---------------------------------------------------------------- Geçmiş analiz arşivi

def archive():
    """ECMWF 00Z/12Z koşularının başlangıç (0. saat) haritaları: gerçekleşen durum. Son 5 günde eksik olanlar
    indirilir (ECMWF açık veri sunucusu birkaç gün geriye gider), ARCHIVE_DAYS günden eskiler silinir."""
    base = 'https://data.ecmwf.int/forecasts'
    adir = os.path.join(OUT, 'arsiv')
    have = set(runs_in(adir))
    now = dt.datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    want = {('gh', '500'), ('gh', '850'), ('t', '850'), ('msl', None), ('2t', None)}
    for back in range(0, 5 * 24):
        t = now - dt.timedelta(hours=back)
        if t.hour not in (0, 12) or f'{t:%Y%m%d%H}' in have:
            continue
        stem = f'{base}/{t:%Y%m%d}/{t:%H}z/ifs/0p25/oper/{t:%Y%m%d%H}0000-0h-oper-fc'
        if not exists(f'{stem}.index'):
            continue
        index = [json.loads(line) for line in get(f'{stem}.index').decode().splitlines() if line.strip()]
        chunks = []
        for r in (r for r in index if (r['param'], r.get('levelist')) in want):
            req = urllib.request.Request(f'{stem}.grib2', headers={**UA, 'Range': f"bytes={r['_offset']}-{r['_offset'] + r['_length'] - 1}"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                chunks.append(resp.read())
        f = {k: {} for k in ('z500', 'z850', 't850', 'mslp', 't2m')}
        for name, _, lev, vals, lats, lons, _ in messages(b''.join(chunks)):
            a = subset(vals, lats, lons)
            if name == 'gh':
                f[f'z{lev}'][0] = a
            elif name == 't':
                f['t850'][0] = a - 273.15
            elif name == 'msl':
                f['mslp'][0] = a / 100
            elif name == '2t':
                f['t2m'][0] = a - 273.15
        write('arsiv', t, [0], f, {'name': 'ECMWF analiz', 'source': 'ECMWF açık veri (CC BY 4.0)', 'resolution': '0,25°'},
              regions=('tr', 'eu'), base=adir, keep=False)
    limit = f'{now - dt.timedelta(days=ARCHIVE_DAYS):%Y%m%d%H}'
    for old in runs_in(adir):
        if old < limit:
            shutil.rmtree(os.path.join(adir, old))


def write_index():
    """index.json: her model için saklanan koşular, en yeni önce"""
    models_meta = {}
    for m in ('gfs', 'ecmwf', 'icon'):
        runs = []
        for r in runs_of(m):
            with open(os.path.join(OUT, m, r, 'meta.json'), encoding='utf-8') as mf:
                mm = json.load(mf)
            runs.append({'id': r, 'run': mm['run'], 'generated': mm['generated'],
                         'hours': mm['regions']['tr']['steps'][-1], 'regions': list(mm['regions']),
                         'complete': mm.get('complete', True), 'expected': mm.get('expected')})
        if runs:
            models_meta[m] = {'runs': runs}
    arsiv = [{'id': r, 'run': f'{r[:4]}-{r[4:6]}-{r[6:8]}T{r[8:]}:00Z'} for r in runs_in(os.path.join(OUT, 'arsiv'))]
    with open(os.path.join(OUT, 'index.json'), 'w', encoding='utf-8') as fh:
        json.dump({'version': 2, 'updated': dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%MZ'), 'models': models_meta, 'archive': arsiv},
                  fh, ensure_ascii=False)


def main():
    args = sys.argv[1:]
    steps = None
    if '--steps' in args:
        i = args.index('--steps')
        steps = [int(x) for x in args[i + 1].split(',')]
        args = args[:i] + args[i + 2:]
    models = args or ['gfs', 'ecmwf', 'icon']
    if not steps:
        try:
            endeksler(OUT, get, log)   # AO, NAO, PNA, AAO, MJO, QBO, ENSO
        except Exception as e:  # noqa: BLE001
            log('göstergeler', 'HATA', repr(e))
    failed = []
    for m in models:
        try:
            {'gfs': gfs, 'ecmwf': ecmwf, 'icon': icon}[m](steps)
        except Exception as e:  # noqa: BLE001 — bir model hata verirse diğerleri yine üretilsin
            log(m, 'HATA', repr(e))
            failed.append(m)
        write_index()  # her modelden sonra: biten modeller hemen görünsün
    if not steps:
        try:
            archive()
        except Exception as e:  # noqa: BLE001
            log('arşiv', 'HATA', repr(e))
        write_index()
    if failed and len(failed) == len(models):
        sys.exit(1)


if __name__ == '__main__':
    os.makedirs(OUT, exist_ok=True)
    main()
