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
import contextlib
import fcntl
import datetime as dt
import gzip
import io
import json
import os
import re
import shutil
import sys
import tempfile
import threading
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
    'pracc': ('mm', 0.1),    # koşu başından beri toplam yağış
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
    'thk': ('m', 1),         # 1000–500 hPa kalınlığı (kar çizgisi: 540–552 dam)
    'ptype': ('kod', 1),     # yağış türü: 0 yok, 1 yağmur, 2 kar, 3 donan yağmur, 4 buz/sulu kar, 5 karla karışık yağmur
    'ws250': ('m/s', 0.1),   # 250 hPa rüzgar hızı (jet akımı)
    'u250': ('m/s', 0.1),
    'v250': ('m/s', 0.1),
    'rh700': ('%', 1),       # 700 hPa bağıl nem
    'u10m': ('m/s', 0.1),    # 10 m rüzgar (doğu bileşeni): canlı haritada rüzgar akışı ve rüzgar adı
    'v10m': ('m/s', 0.1),    # 10 m rüzgar (kuzey bileşeni)
    'pwat': ('mm', 0.1),     # yağış suyu (atmosferdeki toplam su buharı)
    'tcc': ('%', 1),         # toplam bulut örtüsü (GFS, ECMWF, AIFS, ICON, GEM, UKMO, GEFS)
    # ECMWF ENS (51 üye): ortalama alanlar yukarıdaki adlarla, belirsizlik (standart sapma) ve olasılıklar
    'z500s': ('m', 1),       # 500 hPa yükseklik belirsizliği
    't850s': ('°C', 0.1),    # 850 hPa sıcaklık belirsizliği
    'mslps': ('hPa', 0.1),   # yer basıncı belirsizliği
    'p_tp1': ('%', 1),       # 24 saatte >1 mm yağış olasılığı
    'p_tp10': ('%', 1),      # >10 mm
    'p_tp25': ('%', 1),      # >25 mm
    'p_c1': ('%', 1),        # 850 hPa sıcaklığın normalin 1 standart sapma altında olma olasılığı (soğuk)
    'p_c2': ('%', 1),        # 2 standart sapma altında (çok soğuk)
    'p_w1': ('%', 1),        # 1 standart sapma üstünde (sıcak)
}
# Yalnızca bazı bölgelerde yazılan alanlar (stratosfer haritaları kutup/dünya görünümü için)
FIELD_REGIONS = {k: ('world',) for k in ('z50', 't50', 'z10', 't10', 'z30', 't30', 'z70', 't70', 'z100', 't100')}
# GFS stratosfer kesitleri için seviyeler (hPa; 1° ayrı paket, 20°K kuzeyi)
PROF_LEVELS = [1, 2, 3, 5, 7, 10, 20, 30, 50, 70, 100, 150, 200, 250, 300, 500, 700, 850, 1000]
ARCHIVE_DAYS = 60   # geçmiş analiz arşivi (ECMWF 00Z/12Z başlangıç haritaları) kaç gün saklanır

KEEP = 4         # model başına saklanan koşu sayısı
MIN_H = 6        # yeni koşu, ilk bu kadar saati hazır olunca yayınlanır (kalanı geldikçe eklenir)
FIRST_STEPS = 24  # yeni koşunun ilk yazımında en çok bu kadar adım (kalanı sonraki turda)
# Koşunun meta.json → series'inde olması gereken hesaplar; eksikse koşu yeniden işlenir
SERIES_NEEDED = {'gfs': ('vt100', 'wave1_10', 'u60_prof'), 'ecmwf': ('vt100', 'wave1_10')}
_NEW = ('thk', 'ptype', 'ws250', 'rh700', 'pwat', 'pracc')
FIELDS_NEEDED = {'gfs': _NEW, 'ecmwf': _NEW, 'icon': _NEW}   # eksikse koşu bir kez yeniden işlenir
# Üretim sürümü: bir modelin hesabı düzeltilince artır, o sürümden eski koşular bir kez yeniden üretilir
GEN = {'gfs': 2}   # 2: dünya ızgarasında −180° sütunu boştu (dalga-1/2 NaN)
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


_GRIB = threading.Lock()   # eccodes çözümü tek iş parçacığında (indirmeler paralel, çözme sırayla)


def messages(data):
    """GRIB2 baytlarından (kısa ad, seviye türü, seviye, 2B dizi, enlemler, boylamlar, birikim başlangıcı) üret."""
    with _GRIB:
        yield from _messages(data)


def _messages(data):
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


def subset(vals, lats, lons, only=None):
    """Kaynak ızgaradan bütün bölgelerin noktalarını seç → NPTS uzunlukta düz dizi.
    Kaynağın kapsamadığı noktalar (ör. ICON-EU'nun dışı) ve only dışındaki bölgeler NaN olur."""
    lons = ((lons + 180) % 360) - 180   # 0–360 ızgaralarda 180° de −180° olur (yoksa −180 sütunu boş kalır)
    parts = []
    for key, r in REGIONS.items():
        if only and key not in only:
            parts.append(np.full(r['ny'] * r['nx'], np.nan))
            continue
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
    """Birikmiş yağış ve kardan 6 ve 24 saatlik toplamlar, koşu başından beri toplam kar ve toplam yağış"""
    f['pr6'], f['sn6'] = cumulative_to(steps, rain, 6), cumulative_to(steps, snow, 6)
    f['pr24'], f['sn24'] = cumulative_to(steps, rain, 24), cumulative_to(steps, snow, 24)
    f['snacc'] = {s: np.maximum(snow[s], 0) for s in steps if s in snow}
    f['pracc'] = {s: np.maximum(rain[s], 0) for s in steps if s in rain}


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


def derived(f, st):
    """Kalınlık ve 250 hPa rüzgar hızı (bileşenler varsa)"""
    if st in f.get('z500', {}) and st in f.get('z1000', {}):
        f['thk'][st] = f['z500'][st] - f['z1000'][st]
    if st in f.get('u250', {}) and st in f.get('v250', {}):
        f['ws250'][st] = np.hypot(f['u250'][st], f['v250'][st])


def ptype_code(rain=None, snow=None, frz=None, ice=None):
    """GFS'in evet/hayır yağış türü bayraklarından tek kod (öncelik: donan yağmur, buz, karışık, kar, yağmur)"""
    z = np.zeros(NPTS)
    r = (rain if rain is not None else z) >= 0.5
    sn = (snow if snow is not None else z) >= 0.5
    code = np.where(r, 1, 0)
    code = np.where(sn, 2, code)
    code = np.where(r & sn, 5, code)
    code = np.where((ice if ice is not None else z) >= 0.5, 4, code)
    code = np.where((frz if frz is not None else z) >= 0.5, 3, code)
    return code.astype(float)


# ECMWF ptype → bizim kod (1 yağmur, 3 donan yağmur, 5 kar, 6 ıslak kar, 7 karışık, 8 buz taneleri, 12 donan çisenti)
EC_PTYPE = {1: 1, 3: 3, 5: 2, 6: 2, 7: 5, 8: 4, 12: 3}
# ICON ww (WMO hava kodu) → bizim kod
def icon_ptype(ww):
    ww = np.nan_to_num(ww, nan=0).astype(int)
    out = np.zeros(ww.shape)
    out[np.isin(ww, list(range(51, 56)) + list(range(58, 66)) + [80, 81, 82])] = 1
    out[np.isin(ww, list(range(70, 79)) + [85, 86])] = 2
    out[np.isin(ww, [56, 57, 66, 67])] = 3
    out[np.isin(ww, [79, 87, 88, 89, 90])] = 4
    out[np.isin(ww, [68, 69, 83, 84])] = 5
    return out


def clean_json(x):
    """NaN/sonsuz sayıları null yap: tarayıcı JSON'u NaN içerince hiç okuyamaz"""
    if isinstance(x, dict):
        return {k: clean_json(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean_json(v) for v in x]
    if isinstance(x, float) and not np.isfinite(x):
        return None
    return x


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
    if not np.isfinite(row).all():
        if np.isfinite(row).sum() < len(row) * 0.9:
            return [None, None]
        idx = np.arange(len(row))
        row = np.interp(idx, idx[np.isfinite(row)], row[np.isfinite(row)])   # birkaç boş noktayı komşulardan doldur
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
    with yazim_kilidi():
        _write(model, run, steps, fields, extra, regions, base, keep, diag)


def _write(model, run, steps, fields, extra=None, regions=tuple(REGIONS), base=None, keep=True, diag=None):
    add_anomaly(fields, run)
    if fields.get('u10') and fields.get('t10'):
        extra = {**(extra or {}), 'series': {'steps': steps, **strat_series(fields, steps, diag)}}
    d = os.path.join(base or os.path.join(OUT, model), f'{run:%Y%m%d%H}')
    os.makedirs(d, exist_ok=True)
    meta = {
        'model': model, 'run': run.strftime('%Y-%m-%dT%H:00Z'), 'generated': dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%MZ'), 'gen': GEN.get(model, 1),
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
        json.dump(clean_json(meta), f, ensure_ascii=False, allow_nan=False)
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


def candidates(model, ready, hours=(0, 6, 12, 18)):
    """İşlenecek koşular: ilk MIN_H saati hazır en yeni koşu ve yarım yayınlanmış koşular (eskiden yeniye)"""
    runs = []
    for t in recent_runs(hours):
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


# Hazır adımlar bu kadar kanaldan aynı anda indirilir (GEM ve UKMO kendi içinde paralel)
PARALLEL = {'gfs': 4, 'icon': 4, 'ecmwf': 3, 'ecmwf9': 4, 'aifs': 3, 'ens': 2, 'gem': 3, 'gefs': 3, 'gefsk': 3}
LOCK = os.path.join(os.path.dirname(OUT), '.yazim.lock')


@contextlib.contextmanager
def yazim_kilidi():
    """out/ klasörüne yazma ile yayınlama (calis.sh, git) aynı anda olmasın; modeller paralel döngülerde çalışır"""
    with open(LOCK, 'a') as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def progressive(model, run, want, ready, fetch, steps_filter, budget=None):
    """Hazır adımları sırayla topla. Dönen: (adımlar, tamam mı) ya da yapılacak iş yoksa None.
    budget (sn): bu süre dolunca eldekiler yazılır, kalan adımlar sonraki turda eklenir (yavaş kaynaklar için)"""
    rid = f'{run:%Y%m%d%H}'
    t0 = time.time()
    old = meta_of(model, rid)
    # Yeni hesaplar eklenince (ör. stratosfer kesitleri) eski biçimde yazılmış koşu bir kez yeniden işlenir
    stale = bool(old) and (any(k not in old.get('series', {}) for k in SERIES_NEEDED.get(model, ()))
                           or any(k not in old.get('fields', {}) for k in FIELDS_NEEDED.get(model, ()))
                           or old.get('gen', 1) < GEN.get(model, 1))
    if old and old.get('complete', True) and not steps_filter and not stale:
        log(model, 'yeni koşu yok', run)
        return None
    had = 0 if stale else (len(old['regions']['tr']['steps']) if old else 0)
    from concurrent.futures import ThreadPoolExecutor
    # 1) Hazır adımlar: sekizerli gruplar halinde aynı anda kontrol, ilk hazır olmayan adımda durulur
    todo = []
    with ThreadPoolExecutor(8) as ex:
        for i in range(0, len(want), 8):
            chunk = want[i:i + 8]
            ok = list(ex.map(lambda st: is_cached(f'{model}/{rid}/{st}') or ready(run, st), chunk))
            n = ok.index(False) if False in ok else len(ok)
            todo += chunk[:n]
            if n < len(chunk):
                break   # sonraki adımlar henüz yayınlanmadı
    # Yeni koşunun bütün adımları birden hazırsa (ECMWF açık verisi tek seferde gelir) önce ilk FIRST_STEPS adım
    # yazılır, haritalar hemen görünsün; kalanı bir sonraki turda eklenir
    if not old and not steps_filter and len(todo) > FIRST_STEPS:
        todo = todo[:FIRST_STEPS]
    # 2) İndirme ve işleme: birkaç kanaldan, adım sırası korunur; hata ya da süre dolunca o adımda kesilir
    got = []
    with ThreadPoolExecutor(PARALLEL.get(model, 1)) as ex:
        for i in range(0, len(todo), 8):
            if budget and time.time() - t0 > budget:
                log(model, run, 'süre doldu, kalan adımlar sonraki turda')
                break
            chunk = todo[i:i + 8]
            def one(st):
                try:
                    fetch(st)
                    return True
                except urllib.error.HTTPError as e:
                    log(model, run, st, 'adım alınamadı', e.code)
                    return False
            res = list(ex.map(one, chunk))
            n = res.index(False) if False in res else len(res)
            got += chunk[:n]
            if n < len(chunk):
                break
    tr_got = [x for x in got if x % REGIONS['tr']['every'] == 0]
    # Beklenen adım sayısı değişip koşu zaten tamamlanmışsa (ör. UKMO kısa koşuları) bir kez 'tamam' diye yazılır
    finished = bool(old) and old.get('complete') is False and len(got) == len(want)
    if not got or (len(tr_got) <= had and not steps_filter and not finished):
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
                         'z30', 't30', 'z70', 't70', 'z100', 't100', 'z1000', 'thk', 'ptype', 'u250', 'v250', 'ws250', 'rh700', 'pwat',
                         'u10m', 'v10m', 'tcc')}
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
        # Türkiye ve Avrupa 0,25° yalnızca kendi kutusundan; dünya 1° ayrı paketten (bütün dünyayı 0,25° indirmek ~5 kat ağır)
        common = ('&var_TMP=on&var_HGT=on&var_PRMSL=on&var_APCP=on&var_CSNOW=on&var_CRAIN=on&var_CFRZR=on&var_CICEP=on'
                  '&var_GUST=on&var_SNOD=on&var_UGRD=on&var_VGRD=on&var_RH=on&var_PWAT=on&var_TCDC=on'
                  '&lev_1000_mb=on&lev_850_mb=on&lev_700_mb=on&lev_500_mb=on&lev_250_mb=on&lev_100_mb=on&lev_70_mb=on&lev_50_mb=on'
                  '&lev_30_mb=on&lev_10_mb=on&lev_mean_sea_level=on&lev_surface=on&lev_2_m_above_ground=on&lev_10_m_above_ground=on&lev_0C_isotherm=on'
                  '&lev_entire_atmosphere_%5C%28considered_as_a_single_layer%5C%29=on')
        d = f'dir=%2Fgfs.{run:%Y%m%d}%2F{run:%H}%2Fatmos'
        q_box = f'{d}&file=gfs.t{run:%H}z.pgrb2.0p25.f{st:03d}{common}&subregion=&toplat=72&leftlon=-50&rightlon=70&bottomlat=20'
        q_world = f'{d}&file=gfs.t{run:%H}z.pgrb2.1p00.f{st:03d}{common}'

        def dl(script, q):
            def go():
                time.sleep(0.6)  # NOMADS'a nazik ol (dakikada en fazla ~120 istek)
                return get(f'{GFS_BASE}/cgi-bin/{script}?{q}')
            return go
        merged = {}
        for name, ltype, lev, vals, lats, lons, start in messages(cached(f'gfs/{rid}/{st}.box', dl('filter_gfs_0p25.pl', q_box))):
            merged[(name, ltype, lev, start)] = subset(vals, lats, lons, only=('tr', 'eu'))
        for name, ltype, lev, vals, lats, lons, start in messages(cached(f'gfs/{rid}/{st}.w1', dl('filter_gfs_1p00.pl', q_world))):
            k = (name, ltype, lev, start)
            w = subset(vals, lats, lons, only=('world',))
            merged[k] = np.where(np.isnan(w), merged[k], w) if k in merged else w
        flags = {}
        for (name, ltype, lev, start), a in merged.items():
            if name in ('crain', 'csnow', 'cfrzr', 'cicep') and start == st:
                flags[name] = a           # anlık bayrak (dosyada bir de aralık ortalaması var)
            if name == 't' and lev == 850 and ltype == 'isobaricInhPa':
                f['t850'][st] = a - 273.15
            elif name == 't' and lev == 500 and ltype == 'isobaricInhPa':
                f['t500'][st] = a - 273.15
            elif name == 't' and lev in (100, 70, 50, 30, 10) and ltype == 'isobaricInhPa':
                f[f't{lev}'][st] = a - 273.15
            elif name == 'gh' and ltype == 'isothermZero':
                f['frz'][st] = a
            elif name == 'gh' and ltype == 'isobaricInhPa' and lev in (1000, 850, 500, 100, 70, 50, 30, 10):
                f[f'z{lev}'][st] = a
            elif name == 'u' and lev == 10 and ltype == 'isobaricInhPa':
                f['u10'][st] = a
            elif name in ('u', 'v') and lev == 250 and ltype == 'isobaricInhPa':
                f[f'{name}250'][st] = a
            elif name == 'r' and lev == 700 and ltype == 'isobaricInhPa':
                f['rh700'][st] = a
            elif name == 'pwat':
                f['pwat'][st] = a
            elif name == 'tcc' and ltype == 'atmosphere' and start == st:
                f['tcc'][st] = a              # anlık toplam bulut (dosyada bir de 6 saatlik ortalaması ve seviye seviye bulut var)
            elif name == 'sde':
                f['sdep'][st] = a * 100       # m → cm
            elif name == 'prmsl':
                f['mslp'][st] = a / 100
            elif name == '2t':
                f['t2m'][st] = a - 273.15
            elif name == 'gust':
                f['gust'][st] = a * 3.6
            elif name in ('10u', '10v'):
                f[f'{name[2]}10m'][st] = a
            elif name == 'tp' and start == 0:
                apcp[st] = a  # koşu başından beri toplam (dosyada bir de 6 saatlik kova var, o atlanır)
            elif name == 'csnow':
                csnow[st] = a
        if st > 0:
            f['ptype'][st] = ptype_code(flags.get('crain'), flags.get('csnow'), flags.get('cfrzr'), flags.get('cicep'))
        derived(f, st)
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


# ---------------------------------------------------------------- GEFS (NOAA topluluğu): ortalama ve kontrol senaryosu
# Canlı haritada GFS için "Ana senaryo (GFS) · Ortalama · Kontrol" seçimi. 0,5° ızgara, Türkiye ve Avrupa kutusu.

GEFS_KIND = {'gefs': ('geavg', 'GEFS ortalaması', '31 senaryonun ortalaması'), 'gefsk': ('gec00', 'GEFS kontrol', 'bozulmamış başlangıçlı senaryo')}


def gefs_url(run, kind, st, idx=False):
    d = f'gefs.{run:%Y%m%d}/{run:%H}/atmos/pgrb2ap5'
    return f'{GFS_BASE}/pub/data/nccf/com/gens/prod/{d}/{kind}.t{run:%H}z.pgrb2a.0p50.f{st:03d}' + ('.idx' if idx else '')


def gefs_model(model):
    def run_all(steps_filter=None):
        kind = GEFS_KIND[model][0]

        def ready(run, st):
            return exists(gefs_url(run, kind, st, idx=True))

        for run in candidates(model, ready):
            gefs_run(run, ready, steps_filter, model, kind)
    return run_all


def gefs_run(run, ready, steps_filter, model, kind):
    rid = f'{run:%Y%m%d%H}'
    want = steps_filter or standard_steps(384)
    f = {k: {} for k in ('t850', 'z850', 'z500', 't500', 'mslp', 't2m', 'sdep', 'z1000', 'thk', 'ptype', 'u250', 'v250', 'ws250',
                         'rh700', 'pwat', 'u10m', 'v10m', 'tcc')}
    bucket = {}   # adım → (dilim başı, yağış, kar payı); toplamlar sonra sırayla kurulur

    def fetch(st):
        q = (f'dir=%2Fgefs.{run:%Y%m%d}%2F{run:%H}%2Fatmos%2Fpgrb2ap5&file={kind}.t{run:%H}z.pgrb2a.0p50.f{st:03d}'
             '&var_TMP=on&var_HGT=on&var_PRMSL=on&var_APCP=on&var_CSNOW=on&var_CRAIN=on&var_CFRZR=on&var_CICEP=on&var_SNOD=on'
             '&var_UGRD=on&var_VGRD=on&var_RH=on&var_PWAT=on&var_TCDC=on'
             '&lev_1000_mb=on&lev_850_mb=on&lev_700_mb=on&lev_500_mb=on&lev_250_mb=on&lev_mean_sea_level=on&lev_surface=on'
             '&lev_2_m_above_ground=on&lev_10_m_above_ground=on&lev_entire_atmosphere=on'
             '&lev_entire_atmosphere_%5C%28considered_as_a_single_layer%5C%29=on'
             '&subregion=&toplat=72&leftlon=-50&rightlon=70&bottomlat=20')

        def dl():
            time.sleep(0.6)  # NOMADS'a nazik ol
            return get(f'{GFS_BASE}/cgi-bin/filter_gefs_atmos_0p50a.pl?{q}')
        flags, tp, start = {}, None, 0
        for name, ltype, lev, vals, lats, lons, st0 in messages(cached(f'{model}/{rid}/{st}', dl)):
            a = subset_interp(vals, lats, lons)     # 0,5° → bölge ızgaraları (çift doğrusal)
            if ltype == 'isobaricInhPa':
                if name == 't' and lev in (850, 500):
                    f[f't{lev}'][st] = a - 273.15
                elif name == 'gh' and lev in (1000, 850, 500):
                    f[f'z{lev}'][st] = a
                elif name in ('u', 'v') and lev == 250:
                    f[f'{name}250'][st] = a
                elif name == 'r' and lev == 700:
                    f['rh700'][st] = a
            elif name == 'prmsl':
                f['mslp'][st] = a / 100
            elif name == '2t':
                f['t2m'][st] = a - 273.15
            elif name in ('10u', '10v'):
                f[f'{name[2]}10m'][st] = a
            elif name == 'sde':
                f['sdep'][st] = a * 100
            elif name == 'pwat':
                f['pwat'][st] = a
            elif name == 'tcc':
                f['tcc'][st] = a
            elif name == 'tp':
                tp, start = a, st0
            elif name in ('crain', 'csnow', 'cfrzr', 'cicep'):
                flags[name] = a          # dilim boyunca ortalama (ortalamada: senaryoların payı)
        if st > 0 and tp is not None:
            bucket[st] = (start, np.maximum(tp, 0), np.clip(flags.get('csnow', 0), 0, 1))
            f['ptype'][st] = ptype_code(flags.get('crain'), flags.get('csnow'), flags.get('cfrzr'), flags.get('cicep'))
        derived(f, st)

    res = progressive(model, run, want, ready, fetch, steps_filter)
    if not res:
        return
    steps, complete = res
    zero = np.zeros(NPTS)
    cum, cum_snow = {0: zero}, {0: zero}
    for st in sorted(steps):
        if st in bucket and bucket[st][0] in cum:
            s0, pr, sn = bucket[st]
            cum[st] = cum[s0] + pr
            cum_snow[st] = cum_snow[s0] + pr * sn
    accumulations(f, steps, cum, cum_snow)
    name, desc = GEFS_KIND[model][1], GEFS_KIND[model][2]
    write(model, run, steps, f, {'name': name, 'source': 'NOAA NCEP GEFS', 'resolution': '0,5°', 'note': desc,
                                 'complete': complete, 'expected': want[-1]}, regions=('tr', 'eu'))


# ---------------------------------------------------------------- ECMWF (açık veri)

ECMWF_BASE = 'https://data.ecmwf.int/forecasts'


def ecmwf_stem(run, st, product='ifs'):
    return f'{ECMWF_BASE}/{run:%Y%m%d}/{run:%H}z/{product}/0p25/oper/{run:%Y%m%d%H}0000-{st}h-oper-fc'


def ecmwf(steps_filter=None):
    def ready(run, st):
        return exists(f'{ecmwf_stem(run, st)}.index')

    for run in candidates('ecmwf', ready):
        ecmwf_run(run, ready, steps_filter)


def aifs(steps_filter=None):
    """ECMWF AIFS: ECMWF'nin yapay zekâ modeli; aynı açık veri biçimi, 6 saatte bir 15 gün (rüzgar hamlesi, kar kalınlığı, yağış türü yok)"""
    def ready(run, st):
        return exists(f'{ecmwf_stem(run, st, "aifs-single")}.index')

    for run in candidates('aifs', ready):
        ecmwf_run(run, ready, steps_filter, model='aifs', product='aifs-single')


def ecmwf_run(run, ready, steps_filter, model='ecmwf', product='ifs'):
    rid = f'{run:%Y%m%d%H}'
    maxh = 360 if run.hour in (0, 12) else 144  # 06/18 koşuları 6 gün
    want = steps_filter or (list(range(0, 361, 6)) if model == 'aifs' else standard_steps(maxh))
    params = {('t', '850'), ('t', '500'), ('gh', '850'), ('gh', '500'), ('msl', None), ('2t', None), ('10fg', None), ('tp', None), ('sf', None),
              ('10u', None), ('10v', None),
              ('t', '50'), ('gh', '50'), ('t', '10'), ('gh', '10'), ('u', '10'), ('sd', None), ('rsn', None),
              ('t', '100'), ('gh', '100'), ('v', '100'),
              ('gh', '1000'), ('u', '250'), ('v', '250'), ('r', '700'), ('tcwv', None), ('ptype', None), ('tcc', None)}
    f = {k: {} for k in ('t850', 'z850', 'z500', 't500', 'mslp', 't2m', 'gust', 'sdep', 'z50', 't50', 'z10', 't10', 'u10', 'z100', 't100', 'v100',
                         'z1000', 'thk', 'ptype', 'u250', 'v250', 'ws250', 'rh700', 'pwat', 'u10m', 'v10m', 'tcc')}
    swe, rho = {}, {}
    tp, sf = {}, {}

    def fetch(st):
        stem = ecmwf_stem(run, st, product)

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
        for name, ltype, lev, vals, lats, lons, _ in messages(cached(f'{model}/{rid}/{st}', dl)):
            a = subset(vals, lats, lons)
            if name == 't' and lev in (850, 500, 100, 50, 10):
                f[f't{lev}'][st] = a - 273.15
            elif name == 'gh' and lev in (1000, 850, 500, 100, 50, 10):
                f[f'z{lev}'][st] = a
            elif name == 'u' and lev == 10:
                f['u10'][st] = a
            elif name == 'v' and lev == 100:
                f['v100'][st] = a
            elif name in ('u', 'v') and lev == 250:
                f[f'{name}250'][st] = a
            elif name == 'r' and lev == 700:
                f['rh700'][st] = a
            elif name == 'tcwv':
                f['pwat'][st] = a
            elif name == 'tcc':
                f['tcc'][st] = a * 100         # 0–1 → %
            elif name == 'ptype':
                code = np.zeros(NPTS)
                for k2, v2 in EC_PTYPE.items():
                    code[np.round(a) == k2] = v2
                code[np.isnan(a)] = np.nan
                f['ptype'][st] = code
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
            elif name in ('10u', '10v'):
                f[f'{name[2]}10m'][st] = a
            elif name == 'tp':
                tp[st] = a * 1000          # m → mm
            elif name == 'sf':
                sf[st] = a * 1000          # m su eşdeğeri → mm ≈ cm kar (10:1)
        derived(f, st)

    res = progressive(model, run, want, ready, fetch, steps_filter)
    if not res:
        return
    steps, complete = res
    tp.setdefault(0, np.zeros(NPTS))
    sf.setdefault(0, np.zeros(NPTS))
    for st in swe:
        if st in rho:
            f['sdep'][st] = np.where(rho[st] > 0, swe[st] * 1000 / np.maximum(rho[st], 50) * 100, 0)  # cm
    accumulations(f, steps, tp, sf)
    write(model, run, steps, f, {'name': 'ECMWF AIFS' if model == 'aifs' else 'ECMWF',
                                 'source': 'ECMWF açık veri, AIFS yapay zekâ modeli (CC BY 4.0)' if model == 'aifs' else 'ECMWF açık veri (CC BY 4.0)', 'resolution': '0,25°',
                                   'complete': complete, 'expected': want[-1]})


# ---------------------------------------------------------------- ECMWF IFS 9 km (Open-Meteo açık veri, erken)
# ECMWF'nin gerçek zamanlı 9 km IFS çıktısı (CC BY 4.0), Open-Meteo'nun AWS açık veri deposunda adım adım yayınlanır:
# 0,25° açık veriden ~2 saat önce. Yalnız yer seviyesi alanları var (850/500 hPa yok). Dosyalar ECMWF'nin O1280
# indirgenmiş Gauss ızgarasında; her adımda bölgemizin enlem bandı HTTP Range ile okunur, bizim ızgaraya
# çift doğrusal ara değerle aktarılır.

OM_BASE = 'https://openmeteo.s3.amazonaws.com/data_spatial/ecmwf_ifs'
EC9_REGIONS = ('tr', 'eu')
_O1280 = {}
_O1280_LOCK = threading.Lock()


def om_url(run, h):
    vt = run + dt.timedelta(hours=h)
    return f'{OM_BASE}/{run:%Y/%m/%d}/{run:%H%M}Z/{vt:%Y-%m-%dT%H%M}.om'


def om_hours(run):
    """Kaynaktaki dosyalar: 90 saate dek saatlik, 144'e dek 3 saatlik, sonra 6 saatlik (06/18 koşuları 144 saat)"""
    maxh = 360 if run.hour in (0, 12) else 144
    return list(range(0, 91)) + list(range(93, 145, 3)) + list(range(150, maxh + 1, 6))


def o1280():
    """O1280 ızgarası ve bölge noktaları için çift doğrusal ağırlıklar (bir kez hesaplanır)"""
    with _O1280_LOCK:
        if not _O1280:
            _o1280_build()
    return _O1280


def _o1280_build():
    n = 1280
    x, _ = np.polynomial.legendre.leggauss(2 * n)
    lat = np.degrees(np.arcsin(x))[::-1]                       # kuzeyden güneye
    npr = np.array([4 * i + 16 for i in range(1, n + 1)])
    npr = np.concatenate([npr, npr[::-1]])                     # her enlemde nokta sayısı
    start = np.concatenate([[0], np.cumsum(npr)[:-1]])
    plat = np.concatenate([np.repeat(REGIONS[k]['lats'], REGIONS[k]['nx']) for k in EC9_REGIONS])
    plon = np.concatenate([np.tile(REGIONS[k]['lons'], REGIONS[k]['ny']) for k in EC9_REGIONS]) % 360
    # Kaynak enlem bandı (bir satır pay)
    r0 = max(int(np.searchsorted(-lat, -plat.max())) - 2, 0)
    r1 = min(int(np.searchsorted(-lat, -plat.min())) + 2, 2 * n - 1)
    a, b = int(start[r0]), int(start[r1] + npr[r1])
    i0 = np.clip(np.searchsorted(-lat, -plat, side='right') - 1, r0, r1 - 1)   # hedefin kuzeyindeki satır
    i1 = i0 + 1
    wy = (lat[i0] - plat) / (lat[i0] - lat[i1])
    idx, w = [], []
    for row, wr in ((i0, 1 - wy), (i1, wy)):
        m = npr[row]
        fx = plon / 360 * m
        j0 = np.floor(fx).astype(int) % m
        j1 = (j0 + 1) % m
        wx = fx - np.floor(fx)
        idx += [start[row] + j0 - a, start[row] + j1 - a]
        w += [wr * (1 - wx), wr * wx]
    # Bölge noktaları NPTS düzeninde: EC9_REGIONS dışındakiler NaN
    sel = np.concatenate([np.arange(REGIONS[k]['off'], REGIONS[k]['off'] + REGIONS[k]['ny'] * REGIONS[k]['nx']) for k in EC9_REGIONS])
    _O1280.update(a=a, b=b, idx=np.array(idx), w=np.array(w), sel=sel)


class RangeFS:
    """omfiles için uzak dosya: HTTP Range ile 1 MB'lık bloklar (bir değişkenin bandı dosyada art arda durur)"""
    BLOCK = 1 << 20

    def __init__(self):
        self.blocks, self.sizes = {}, {}

    def size(self, path, **_):
        if path not in self.sizes:
            req = urllib.request.Request(path, headers=UA, method='HEAD')
            with urllib.request.urlopen(req, timeout=60) as r:
                self.sizes[path] = int(r.headers['Content-Length'])
        return self.sizes[path]

    def _block(self, path, i):
        if (path, i) not in self.blocks:
            a = i * self.BLOCK
            b = min(a + self.BLOCK, self.size(path)) - 1
            for k in range(4):
                try:
                    req = urllib.request.Request(path, headers={**UA, 'Range': f'bytes={a}-{b}'})
                    with urllib.request.urlopen(req, timeout=120) as r:
                        self.blocks[(path, i)] = r.read()
                    break
                except Exception:  # noqa: BLE001
                    if k == 3:
                        raise
                    time.sleep(3 * (k + 1))
        return self.blocks[(path, i)]

    def cat_file(self, path, start=None, end=None, **_):
        start = start or 0
        end = self.size(path) if end is None else end
        first, last = start // self.BLOCK, (end - 1) // self.BLOCK
        data = b''.join(self._block(path, i) for i in range(first, last + 1))
        return data[start - first * self.BLOCK:end - first * self.BLOCK]


def om_read(url, names):
    """Bir .om dosyasından istenen değişkenler → bölge noktaları (NPTS uzunlukta, EC9 dışı NaN)"""
    import omfiles   # yalnız bu model için gerekli
    g = o1280()
    rd = omfiles.OmFileReader.from_fsspec(RangeFS(), url)
    out = {}
    for name in names:
        band = np.asarray(rd.get_child_by_name(name)[0:1, g['a']:g['b']], dtype=float).ravel()
        a = np.full(NPTS, np.nan)
        a[g['sel']] = (band[g['idx']] * g['w']).sum(axis=0)
        out[name] = a
    return out


def ecmwf9(steps_filter=None):
    def ready(run, st):
        return exists(om_url(run, st))

    for run in candidates('ecmwf9', ready):
        ecmwf9_run(run, ready, steps_filter)


def ecmwf9_run(run, ready, steps_filter):
    rid = f'{run:%Y%m%d%H}'
    hours = om_hours(run)
    want = steps_filter or standard_steps(hours[-1])
    f = {k: {} for k in ('mslp', 't2m', 'gust', 'sdep', 'ptype', 'pwat', 'u10m', 'v10m', 'tcc')}
    ip, isn = {}, {}   # adımın aralığındaki (önceki adımdan bu yana) yağış ve kar

    def fetch(st):
        prev = max([x for x in want if x < st], default=-1)
        between = [h for h in hours if prev < h <= st]

        def dl():
            snap = om_read(om_url(run, st), ['temperature_2m', 'pressure_msl', 'wind_u_component_10m', 'wind_v_component_10m',
                                             'cloud_cover', 'total_column_integrated_water_vapour', 'snow_depth'])
            pr, sn, gu = np.zeros(NPTS), np.zeros(NPTS), np.full(NPTS, np.nan)
            for h in between:
                if h == 0:
                    continue
                x = om_read(om_url(run, h), ['precipitation', 'snowfall_water_equivalent', 'wind_gusts_10m'])
                pr += np.nan_to_num(x['precipitation'])
                sn += np.nan_to_num(x['snowfall_water_equivalent'])
                gu = np.fmax(gu, x['wind_gusts_10m'])
            buf = io.BytesIO()
            np.savez_compressed(buf, pr=pr.astype(np.float32), sn=sn.astype(np.float32), gu=gu.astype(np.float32),
                                **{k: v.astype(np.float32) for k, v in snap.items()})
            return buf.getvalue()
        z = np.load(io.BytesIO(cached(f'ecmwf9/{rid}/{st}', dl)))
        mask = np.isnan(z['temperature_2m'])
        f['t2m'][st] = z['temperature_2m'].astype(float)
        f['mslp'][st] = z['pressure_msl'] / 100
        f['u10m'][st], f['v10m'][st] = z['wind_u_component_10m'].astype(float), z['wind_v_component_10m'].astype(float)
        f['tcc'][st] = z['cloud_cover'].astype(float)
        f['pwat'][st] = z['total_column_integrated_water_vapour'].astype(float)
        f['sdep'][st] = z['snow_depth'] * 100                      # m → cm
        if st:
            f['gust'][st] = z['gu'] * 3.6                          # aralıktaki en güçlü hamle, m/s → km/sa
            pr, sn = z['pr'].astype(float), z['sn'].astype(float)
            ip[st], isn[st] = np.where(mask, np.nan, pr), np.where(mask, np.nan, sn)
            # Yağış türü: aralıktaki kar payından (su eşdeğeri; 0,1 mm/sa altı yağış yok sayılır)
            frac = np.where(pr > 0, sn / np.maximum(pr, 1e-6), 0)
            code = np.where(frac >= 0.8, 2, np.where(frac >= 0.2, 5, 1)).astype(float)
            code[pr < 0.1 * (st - prev)] = 0
            code[mask] = np.nan
            f['ptype'][st] = code

    res = progressive('ecmwf9', run, want, ready, fetch, steps_filter)
    if not res:
        return
    steps, complete = res
    tp, sf = {0: np.zeros(NPTS)}, {0: np.zeros(NPTS)}
    acc_p, acc_s = np.zeros(NPTS), np.zeros(NPTS)
    for st in steps:
        if st:
            acc_p, acc_s = acc_p + np.nan_to_num(ip[st]), acc_s + np.nan_to_num(isn[st])   # sn: mm su ≈ cm kar (10:1)
            tp[st], sf[st] = acc_p.copy(), acc_s.copy()
    accumulations(f, steps, tp, sf)
    write('ecmwf9', run, steps, f, {'name': 'ECMWF 9 km', 'source': 'ECMWF IFS gerçek zamanlı 9 km, Open-Meteo açık verisi (CC BY 4.0)',
                                    'resolution': '9 km', 'complete': complete, 'expected': want[-1]}, regions=EC9_REGIONS)


# ---------------------------------------------------------------- ECMWF ENS (açık veri, hazır ürünler)
# "ep" dosyaları: 51 üyenin ortalaması (em), standart sapması (es) ve olasılıklar (ep). İki parça: 0–240 ve 246–360 saat.

ENS_PICK = {
    ('em', 'gh', '500'): 'z500', ('es', 'gh', '500'): 'z500s', ('em', 'gh', '1000'): 'z1000',
    ('em', 't', '850'): 't850', ('es', 't', '850'): 't850s', ('em', 'msl', None): 'mslp', ('es', 'msl', None): 'mslps',
    ('ep', 'tpg1', None): 'p_tp1', ('ep', 'tpg10', None): 'p_tp10', ('ep', 'tpg25', None): 'p_tp25',
    ('ep', 'ptsa_lt_1stdev', '850'): 'p_c1', ('ep', 'ptsa_lt_2stdev', '850'): 'p_c2', ('ep', 'ptsa_gt_1stdev', '850'): 'p_w1',
}


def ens_file(run, st):
    part = 240 if st <= 240 else 360
    return f'{ECMWF_BASE}/{run:%Y%m%d}/{run:%H}z/ifs/0p25/enfo/{run:%Y%m%d%H}0000-{part}h-enfo-ep'


def ens(steps_filter=None):
    def ready(run, st):
        return exists(f'{ens_file(run, st)}.index')

    for run in candidates('ens', ready, hours=(0, 12)):   # 15 günlük ensemble yalnızca 00Z ve 12Z
        ens_run(run, ready, steps_filter)


def ens_run(run, ready, steps_filter):
    rid = f'{run:%Y%m%d%H}'
    want = steps_filter or list(range(0, 361, 6))
    f = {k: {} for k in set(ENS_PICK.values()) | {'thk'}}
    indexes = {}

    def records(st):
        url = ens_file(run, st)
        if url not in indexes:
            indexes[url] = [json.loads(x) for x in get(f'{url}.index').decode().splitlines() if x.strip()]
        out = []
        for r in indexes[url]:
            key = (r['type'], r['param'], r.get('levelist'))
            if key not in ENS_PICK:
                continue
            # Ortalama/sapma ve sıcaklık olasılığı o saatte; yağış olasılığı o saatte biten 24 saat için
            if r['step'] == str(st) or r['step'] == f'{st - 24}-{st}':
                out.append((key, r))
        return url, out

    def fetch(st):
        url, recs = records(st)
        for key, r in recs:
            def dl(r=r):
                req = urllib.request.Request(f'{url}.grib2', headers={**UA, 'Range': f"bytes={r['_offset']}-{r['_offset'] + r['_length'] - 1}"})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    return resp.read()
            data = cached(f'ens/{rid}/{st}/{"_".join(x or "" for x in key)}', dl)
            for _n, _lt, _lv, vals, lats, lons, _s in messages(data):
                a = subset(vals, lats, lons)
                name = ENS_PICK[key]
                f[name][st] = a / 100 if name in ('mslp', 'mslps') else a - 273.15 if name == 't850' else a
        derived(f, st)

    res = progressive('ens', run, want, ready, fetch, steps_filter)
    if not res:
        return
    steps, complete = res
    write('ens', run, steps, f, {'name': 'ECMWF ENS', 'source': 'ECMWF açık veri, 51 üyeli ensemble (CC BY 4.0)', 'resolution': '0,25°',
                                 'complete': complete, 'expected': want[-1]})


# ---------------------------------------------------------------- ICON-EU (DWD)

ICON_BASE = 'https://opendata.dwd.de/weather/nwp/icon-eu/grib'
# Her adımda indirilen değişkenler: (değişken, seviye); 0. saatte birikimli olanlar yok
ICON_VARS = [('t', 850), ('t', 500), ('fi', 850), ('fi', 500), ('pmsl', None), ('t_2m', None), ('h_snow', None), ('hzerocl', None),
             ('fi', 1000), ('u', 250), ('v', 250), ('relhum', 700), ('tqv', None), ('ww', None), ('u_10m', None), ('v_10m', None), ('clct', None)]
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
    f = {k: {} for k in ('t850', 'z850', 'z500', 't500', 'mslp', 't2m', 'gust', 'sdep', 'frz',
                         'z1000', 'thk', 'ptype', 'u250', 'v250', 'ws250', 'rh700', 'pwat', 'u10m', 'v10m', 'tcc')}
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
            'z1000': grab('fi', st, 1000) / g, 'u250': grab('u', st, 250), 'v250': grab('v', st, 250),
            'rh700': grab('relhum', st, 700), 'pwat': grab('tqv', st),
            'u10m': grab('u_10m', st), 'v10m': grab('v_10m', st), 'tcc': grab('clct', st),
        }
        if st > 0:
            row['ptype'] = icon_ptype(grab('ww', st))
        if st > 0:
            row['gust'] = grab('vmax_10m', st) * 3.6
            prec[st] = grab('tot_prec', st)
            snow[st] = grab('snow_gsp', st) + grab('snow_con', st)
        for k, v in row.items():
            f[k][st] = v
        derived(f, st)

    res = progressive('icon', run, want, ready, fetch, steps_filter)
    if not res:
        return
    steps, complete = res
    prec[0] = np.zeros(NPTS)
    snow[0] = np.zeros(NPTS)
    accumulations(f, steps, prec, snow)
    write('icon', run, steps, f, {'name': 'ICON', 'source': 'DWD açık veri', 'resolution': '0,0625° → 0,25°', 'coverage': '29,5–70,5°K, 23,5°B–62,5°D',
                                  'complete': complete, 'expected': want[-1]}, regions=('tr', 'eu'))  # ICON-EU dünyayı kapsamaz


# ---------------------------------------------------------------- GEM (Kanada, ECCC GDPS 15 km)

GEM_BASE = 'https://dd.weather.gc.ca/{d}/WXO-DD/model_gdps/15km/{h}/{st:03d}/{d}T{h}Z_MSC_GDPS_{var}_LatLon0.15_PT{st:03d}H.grib2'
GEM_VARS = ['AirTemp_IsbL-0850', 'AirTemp_IsbL-0500', 'GeopotentialHeight_IsbL-0850', 'GeopotentialHeight_IsbL-0500',
            'GeopotentialHeight_IsbL-1000', 'Pressure_MSL', 'AirTemp_AGL-2m', 'SnowDepth_Sfc', 'WindU_IsbL-0250', 'WindV_IsbL-0250',
            'RelativeHumidity_IsbL-0700', 'WindU_AGL-10m', 'WindV_AGL-10m',
            'WindU_IsbL-0010', 'AirTemp_IsbL-0010', 'GeopotentialHeight_IsbL-0010',   # 10 hPa: kutup girdabı
            'TotalCloudCover_Sfc']   # toplam bulut (%)
GEM_ACC = ['WindGust-Max_AGL-10m', 'Precip-Accum_Sfc', 'Snow-Accum_Sfc']


def gem_url(run, var, st):
    return GEM_BASE.format(d=f'{run:%Y%m%d}', h=f'{run:%H}', st=st, var=var)


def subset_interp(vals, lats, lons):
    """0,15°'lik GEM ızgarası bizim ızgaralarla hizalı değil: bölge noktalarına çift doğrusal ara değer → NPTS dizi"""
    lons = ((lons + 180) % 360) - 180
    ox, oy = np.argsort(lons), np.argsort(lats)
    lons, lats, vals = lons[ox], lats[oy], vals[np.ix_(oy, ox)]
    # Boylamda sarma: son sütundan sonra ilk sütun (+360)
    lons = np.append(lons, lons[0] + 360)
    vals = np.concatenate([vals, vals[:, :1]], axis=1)
    parts = []
    for r in REGIONS.values():
        fy = np.interp(r['lats'], lats, np.arange(len(lats)))
        fx = np.interp(((r['lons'] + 180) % 360) - 180, lons, np.arange(len(lons)))
        y0 = np.clip(np.floor(fy).astype(int), 0, len(lats) - 2)
        x0 = np.clip(np.floor(fx).astype(int), 0, len(lons) - 2)
        ty, tx = (fy - y0)[:, None], (fx - x0)[None, :]
        a = (vals[np.ix_(y0, x0)] * (1 - tx) + vals[np.ix_(y0, x0 + 1)] * tx) * (1 - ty) \
            + (vals[np.ix_(y0 + 1, x0)] * (1 - tx) + vals[np.ix_(y0 + 1, x0 + 1)] * tx) * ty
        parts.append(a.ravel())
    return np.concatenate(parts)


def gem(steps_filter=None):
    def ready(run, st):
        return all(exists(gem_url(run, v, st)) for v in GEM_VARS + (GEM_ACC if st else []))

    for run in candidates('gem', ready, hours=(0, 12)):
        gem_run(run, ready, steps_filter)


def gem_run(run, ready, steps_filter):
    rid = f'{run:%Y%m%d%H}'
    want = steps_filter or list(range(0, 241, 6))
    f = {k: {} for k in ('t850', 'z850', 'z500', 't500', 'mslp', 't2m', 'gust', 'sdep', 'z1000', 'thk', 'ptype',
                         'u250', 'v250', 'ws250', 'rh700', 'u10m', 'v10m', 'u10', 't10', 'z10', 'tcc')}
    prec, snow = {}, {}

    def grab(var, st):
        data = cached(f'gem/{rid}/{st}/{var}', lambda: get(gem_url(run, var, st)))
        for _, _, _, vals, lats, lons, _ in messages(data):
            return subset_interp(vals, lats, lons)
        return None

    def fetch(st):
        row = {
            't850': grab('AirTemp_IsbL-0850', st) - 273.15, 't500': grab('AirTemp_IsbL-0500', st) - 273.15,
            'z850': grab('GeopotentialHeight_IsbL-0850', st), 'z500': grab('GeopotentialHeight_IsbL-0500', st),
            'z1000': grab('GeopotentialHeight_IsbL-1000', st), 'mslp': grab('Pressure_MSL', st) / 100,
            't2m': grab('AirTemp_AGL-2m', st) - 273.15, 'sdep': grab('SnowDepth_Sfc', st) * 100,
            'u250': grab('WindU_IsbL-0250', st), 'v250': grab('WindV_IsbL-0250', st),
            'rh700': grab('RelativeHumidity_IsbL-0700', st), 'u10m': grab('WindU_AGL-10m', st), 'v10m': grab('WindV_AGL-10m', st),
            'u10': grab('WindU_IsbL-0010', st), 't10': grab('AirTemp_IsbL-0010', st) - 273.15, 'z10': grab('GeopotentialHeight_IsbL-0010', st),
            'tcc': grab('TotalCloudCover_Sfc', st),
        }
        if st > 0:
            row['gust'] = grab('WindGust-Max_AGL-10m', st) * 3.6
            prec[st] = grab('Precip-Accum_Sfc', st)            # kg/m² = mm
            snow[st] = grab('Snow-Accum_Sfc', st)              # mm su eşdeğeri ≈ cm kar (10:1)
        for k, v in row.items():
            f[k][st] = v
        derived(f, st)

    res = progressive('gem', run, want, ready, fetch, steps_filter)
    if not res:
        return
    steps, complete = res
    prec[0] = np.zeros(NPTS)
    snow[0] = np.zeros(NPTS)
    accumulations(f, steps, prec, snow)
    # Yağış türü: son 6 saatin yağış ve kar miktarlarının oranından (ayrı tür dosyası indirmeden)
    for s in steps:
        p6, s6 = f['pr6'].get(s), f['sn6'].get(s)
        if p6 is None or s6 is None:
            continue
        frac = np.where(p6 > 0.2, s6 / np.maximum(p6, 1e-6), 0)
        code = np.where(p6 > 0.2, 1, 0)
        code = np.where((p6 > 0.2) & (frac >= 0.8), 2, code)
        code = np.where((p6 > 0.2) & (frac > 0.2) & (frac < 0.8), 5, code)
        f['ptype'][s] = code.astype(float)
    write('gem', run, steps, f, {'name': 'GEM', 'source': 'ECCC (Kanada) açık veri', 'resolution': '0,15° → 0,25°',
                                 'complete': complete, 'expected': want[-1]})


# ---------------------------------------------------------------- UKMO (Met Office küresel 10 km, AWS açık veri)
# NetCDF4 dosyaları (adım ve değişken başına bir dosya). Basınç seviyesi dosyaları 120–220 MB ama seviye başına
# 128×128 parçalı: yalnız gereken seviyeler HTTP aralık istekleriyle okunur (h5py + RangeFile).

UKMO_BASE = 'https://met-office-atmospheric-model-data.s3.eu-west-2.amazonaws.com/global-deterministic-10km/'


class RangeFile(io.RawIOBase):
    """Uzak dosyayı 1 MB'lık parçalar halinde, istendikçe okuyan dosya nesnesi (h5py bunu yerel dosya gibi açar)"""

    def __init__(self, url, block=1 << 20):
        super().__init__()
        self.url, self.pos, self.block, self.parts = url, 0, block, {}
        with urllib.request.urlopen(urllib.request.Request(url, method='HEAD', headers=UA), timeout=60) as r:
            self.size = int(r.headers['Content-Length'])

    def readable(self):
        return True

    def seekable(self):
        return True

    def tell(self):
        return self.pos

    def seek(self, off, whence=0):
        self.pos = off if whence == 0 else self.pos + off if whence == 1 else self.size + off
        return self.pos

    def _part(self, i):
        if i not in self.parts:
            a = i * self.block
            b = min(self.size, a + self.block) - 1
            for k in range(4):
                try:
                    req = urllib.request.Request(self.url, headers={**UA, 'Range': f'bytes={a}-{b}'})
                    with urllib.request.urlopen(req, timeout=120) as r:
                        self.parts[i] = r.read()
                    break
                except Exception:  # noqa: BLE001
                    if k == 3:
                        raise
                    time.sleep(3 * (k + 1))
        return self.parts[i]

    def readinto(self, buf):
        n = min(len(buf), self.size - self.pos)
        if n <= 0:
            return 0
        out, p = bytearray(), self.pos
        while len(out) < n:
            i = p // self.block
            c = self._part(i)[p - i * self.block: p - i * self.block + n - len(out)]
            out += c
            p += len(c)
        buf[:n] = out
        self.pos += n
        return n


def ukmo_url(run, st, var):
    return f'{UKMO_BASE}{run:%Y%m%dT%H%MZ}/{run + dt.timedelta(hours=st):%Y%m%dT%H%MZ}-PT{st:04d}H00M-{var}.nc'


def ukmo_read(url, levels=None):
    """Dosyanın ana değişkeni → bölge noktaları (NPTS); levels (hPa) verilirse {seviye: dizi}"""
    import h5py   # yalnız UKMO için gerekli
    f = RangeFile(url)
    h = h5py.File(f, 'r')
    try:
        name = [k for k in h if isinstance(h[k], h5py.Dataset) and h[k].ndim >= 2 and k != 'flag' and not k.endswith('bnds')][0]
        d = h[name]
        lat, lon = h['latitude'][:].astype(float), h['longitude'][:].astype(float)
        if levels is None:
            return subset_interp(np.array(d[...], dtype=float), lat, lon)
        p = [int(round(x)) for x in h['pressure'][:]]
        return {lev: subset_interp(np.array(d[p.index(lev * 100)], dtype=float), lat, lon) for lev in levels}
    finally:
        h.close()
        f.close()


def ukmo_acc(st):
    """(st−6, st] aralığını kaplayan birikim dosyaları: 54. saate dek saatlik, 144'e dek 3 saatlik, sonra 6 saatlik"""
    if st <= 54:
        return [(k, 'PT01H') for k in range(st - 5, st + 1)]
    if st <= 144:
        return [(st - 3, 'PT03H'), (st, 'PT03H')]
    return [(st, 'PT06H')]


def ukmo(steps_filter=None):
    def ready(run, st):
        need = [ukmo_url(run, st, 'pressure_at_mean_sea_level'), ukmo_url(run, st, 'temperature_on_pressure_levels')]
        if st:
            k, per = ukmo_acc(st)[-1]
            need.append(ukmo_url(run, k, f'precipitation_accumulation-{per}'))
        return all(exists(u) for u in need)

    for run in candidates('ukmo', ready):
        ukmo_run(run, ready, steps_filter)


def ukmo_run(run, ready, steps_filter):
    from concurrent.futures import ThreadPoolExecutor
    rid = f'{run:%Y%m%d%H}'
    # 00Z/12Z 7 gün, 06Z/18Z kısa koşu (69 saate dek)
    want = steps_filter or list(range(0, 169 if run.hour in (0, 12) else 67, 6))
    f = {k: {} for k in ('t850', 'z850', 'z500', 't500', 'mslp', 't2m', 'gust', 'sdep', 'z1000', 'thk', 'ptype',
                         'u250', 'v250', 'ws250', 'rh700', 'u10m', 'v10m', 'tcc')}
    pr6, rn6 = {}, {}

    def grab(key, st, fetch):
        data = cached(f'ukmo/{rid}/{st}/{key}', lambda: fetch().astype(np.float32).tobytes())
        return np.frombuffer(data, dtype=np.float32).astype(float)

    def level(var, st, lev):
        # Bir dosyadan birden çok seviye: hepsi tek açılışta okunup ayrı ayrı önbelleğe yazılır
        key = lambda lv: f'{var}{lv}'   # noqa: E731
        levs = {'geopotential_height_on_pressure_levels': (500, 850, 1000), 'temperature_on_pressure_levels': (850, 500)}.get(var, (lev,))
        if not all(is_cached(f'ukmo/{rid}/{st}/{key(lv)}') for lv in levs):
            got = ukmo_read(ukmo_url(run, st, var), levs)
            for lv, a in got.items():
                cached(f'ukmo/{rid}/{st}/{key(lv)}', lambda a=a: a.astype(np.float32).tobytes())
        return grab(key(lev), st, lambda: None)

    def surf(var, st):
        return grab(var, st, lambda: ukmo_read(ukmo_url(run, st, var)))

    def fetch(st):
        jobs = {
            'z500': lambda: level('geopotential_height_on_pressure_levels', st, 500),
            't850': lambda: level('temperature_on_pressure_levels', st, 850),
            'ws250': lambda: level('wind_speed_on_pressure_levels', st, 250),
            'wd250': lambda: level('wind_direction_on_pressure_levels', st, 250),
            'rh700': lambda: level('relative_humidity_on_pressure_levels', st, 700),
            'mslp': lambda: surf('pressure_at_mean_sea_level', st), 't2m': lambda: surf('temperature_at_screen_level', st),
            'swe': lambda: surf('snow_depth_water_equivalent', st), 'ws10': lambda: surf('wind_speed_at_10m', st),
            'wd10': lambda: surf('wind_direction_at_10m', st), 'tcc': lambda: surf('cloud_amount_of_total_cloud', st),
        }
        if st:
            jobs['gust'] = lambda: surf('wind_gust_at_10m', st)
            for k, per in ukmo_acc(st):
                jobs[f'p{k}'] = lambda k=k, per=per: grab(f'pr{per}', k, lambda: ukmo_read(ukmo_url(run, k, f'precipitation_accumulation-{per}')))
                jobs[f'r{k}'] = lambda k=k, per=per: grab(f'rn{per}', k, lambda: ukmo_read(ukmo_url(run, k, f'rainfall_accumulation-{per}')))
        with ThreadPoolExecutor(6) as ex:
            res = dict(zip(jobs, ex.map(lambda fn: fn(), jobs.values())))
        z = {lv: level('geopotential_height_on_pressure_levels', st, lv) for lv in (850, 1000)}
        t500 = level('temperature_on_pressure_levels', st, 500)
        rad = np.radians
        row = {
            't850': res['t850'] - 273.15, 't500': t500 - 273.15, 'z500': res['z500'], 'z850': z[850], 'z1000': z[1000],
            'mslp': res['mslp'] / 100, 't2m': res['t2m'] - 273.15, 'sdep': res['swe'] * 1000 / 3,   # su eşdeğeri (m) → kar (cm), yoğunluk ~0,3
            'u250': -res['ws250'] * np.sin(rad(res['wd250'])), 'v250': -res['ws250'] * np.cos(rad(res['wd250'])),
            'rh700': res['rh700'] * 100, 'u10m': -res['ws10'] * np.sin(rad(res['wd10'])), 'v10m': -res['ws10'] * np.cos(rad(res['wd10'])),
            'tcc': res['tcc'] * (100 if np.nanmax(res['tcc']) <= 1.5 else 1),   # oran (0–1) → %
        }
        if st:
            row['gust'] = res['gust'] * 3.6
            pr6[st] = sum(res[f'p{k}'] for k, _ in ukmo_acc(st)) * 1000     # m → mm
            rn6[st] = sum(res[f'r{k}'] for k, _ in ukmo_acc(st)) * 1000
        for k, v in row.items():
            f[k][st] = v
        derived(f, st)

    res = progressive('ukmo', run, want, ready, fetch, steps_filter, budget=8 * 60)
    if not res:
        return
    steps, complete = res
    # 6 saatlik toplamlardan koşu başından birikim; kar = yağış − yağmur (mm su ≈ cm kar, 10:1)
    prec, snow = {0: np.zeros(NPTS)}, {0: np.zeros(NPTS)}
    for a, b in zip(steps, steps[1:]):
        prec[b] = prec[a] + pr6[b]
        snow[b] = snow[a] + np.maximum(pr6[b] - rn6[b], 0)
    accumulations(f, steps, prec, snow)
    for s in steps:
        p6, s6 = f['pr6'].get(s), f['sn6'].get(s)
        if p6 is None or s6 is None:
            continue
        frac = np.where(p6 > 0.2, s6 / np.maximum(p6, 1e-6), 0)
        code = np.where(p6 > 0.2, 1, 0)
        code = np.where((p6 > 0.2) & (frac >= 0.8), 2, code)
        code = np.where((p6 > 0.2) & (frac > 0.2) & (frac < 0.8), 5, code)
        f['ptype'][s] = code.astype(float)
    write('ukmo', run, steps, f, {'name': 'UKMO', 'source': 'Met Office açık veri (AWS)', 'resolution': '10 km → 0,25°',
                                  'complete': complete, 'expected': want[-1]})


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
    with yazim_kilidi():
        _write_index()


def _write_index():
    models_meta = {}
    for m in ('gfs', 'ecmwf', 'ecmwf9', 'icon', 'ens', 'aifs', 'gem', 'ukmo', 'gefs', 'gefsk'):
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
    models = args or ['gfs', 'ecmwf', 'icon', 'ens', 'aifs', 'gem', 'ukmo']
    if not steps and 'gfs' in models:   # göstergeler yalnız GFS döngüsünde (paralel döngüler aynı dosyaya yazmasın)
        try:
            endeksler(OUT, get, log)   # AO, NAO, PNA, AAO, MJO, QBO, ENSO
        except Exception as e:  # noqa: BLE001
            log('göstergeler', 'HATA', repr(e))
    failed = []
    for m in models:
        try:
            {'gfs': gfs, 'ecmwf': ecmwf, 'ecmwf9': ecmwf9, 'icon': icon, 'ens': ens, 'aifs': aifs, 'gem': gem, 'ukmo': ukmo,
             'gefs': gefs_model('gefs'), 'gefsk': gefs_model('gefsk')}[m](steps)
        except Exception as e:  # noqa: BLE001 — bir model hata verirse diğerleri yine üretilsin
            log(m, 'HATA', repr(e))
            failed.append(m)
        write_index()  # her modelden sonra: biten modeller hemen görünsün
    if not steps and 'ecmwf' in models:   # geçmiş analiz arşivi ECMWF verisinden: yalnız ECMWF'den sonra
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
