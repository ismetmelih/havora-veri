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
}

KEEP = 4         # model başına saklanan koşu sayısı
KEEP_WORLD = 2   # dünya bölgesi (en büyük dosyalar) bu kadar yeni koşuda tutulur
CLIMATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'klima', 'z500_era5_1990_2019.npz')

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
    """Birikmiş yağış ve kardan 6 ve 24 saatlik toplamlar"""
    f['pr6'], f['sn6'] = cumulative_to(steps, rain, 6), cumulative_to(steps, snow, 6)
    f['pr24'], f['sn24'] = cumulative_to(steps, rain, 24), cumulative_to(steps, snow, 24)


class Climate:
    """500 hPa uzun yıllar ortalaması (ERA5 1990–2019, WeatherBench2): her 5 günde bir, 1,5°.
    Bölge noktalarına çift doğrusal, günler arasında doğrusal ara değer."""

    def __init__(self):
        d = np.load(CLIMATE)
        self.z, self.doy = d['z500'].astype(float), d['doy']
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
    """500 hPa anomali: model yüksekliği eksi o günün uzun yıllar ortalaması (m)"""
    clim = Climate()
    f['z500a'] = {s: z - clim.at(run + dt.timedelta(hours=s)) for s, z in f['z500'].items()}


def write(model, run, steps, fields, extra=None, regions=tuple(REGIONS)):
    add_anomaly(fields, run)
    d = os.path.join(OUT, model, f'{run:%Y%m%d%H}')
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
            if name not in fields:
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
    prune(model)


def runs_of(model):
    """Saklanan koşu klasörleri, en yeni önce"""
    d = os.path.join(OUT, model)
    if not os.path.isdir(d):
        return []
    return sorted((x for x in os.listdir(d) if re.fullmatch(r'\d{10}', x) and os.path.exists(os.path.join(d, x, 'meta.json'))), reverse=True)


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


# ---------------------------------------------------------------- GFS (NOAA)

def gfs(steps_filter=None):
    base = 'https://nomads.ncep.noaa.gov'
    run = None
    for t in recent_runs((0, 6, 12, 18)):
        d = f'gfs.{t:%Y%m%d}/{t:%H}/atmos'
        if exists(f'{base}/pub/data/nccf/com/gfs/prod/{d}/gfs.t{t:%H}z.pgrb2.0p25.f384'):
            run = t
            break
    if run is None:
        return log('GFS: tamamlanmış koşu bulunamadı')
    if published('gfs', run) and not steps_filter:
        return log('GFS: yeni koşu yok', run)
    steps = steps_filter or standard_steps(384)
    log('GFS', run, len(steps), 'adım indiriliyor')
    f = {k: {} for k in ('t850', 'z850', 'z500', 't500', 'mslp', 't2m', 'gust')}
    apcp, csnow = {}, {}
    for s in steps:
        q = (f'dir=%2Fgfs.{run:%Y%m%d}%2F{run:%H}%2Fatmos&file=gfs.t{run:%H}z.pgrb2.0p25.f{s:03d}'
             '&var_TMP=on&var_HGT=on&var_PRMSL=on&var_APCP=on&var_CSNOW=on&var_GUST=on'
             '&lev_850_mb=on&lev_500_mb=on&lev_mean_sea_level=on&lev_surface=on&lev_2_m_above_ground=on'
             f'&subregion=&toplat={BOX_N}&leftlon={BOX_W}&rightlon={BOX_E}&bottomlat={BOX_S}')
        data = get(f'{base}/cgi-bin/filter_gfs_0p25.pl?{q}')
        for name, ltype, lev, vals, lats, lons, start in messages(data):
            a = subset(vals, lats, lons)
            if name == 't' and lev == 850 and ltype == 'isobaricInhPa':
                f['t850'][s] = a - 273.15
            elif name == 't' and lev == 500 and ltype == 'isobaricInhPa':
                f['t500'][s] = a - 273.15
            elif name == 'gh' and lev == 850:
                f['z850'][s] = a
            elif name == 'gh' and lev == 500:
                f['z500'][s] = a
            elif name == 'prmsl':
                f['mslp'][s] = a / 100
            elif name == '2t':
                f['t2m'][s] = a - 273.15
            elif name == 'gust':
                f['gust'][s] = a * 3.6
            elif name == 'tp' and start == 0:
                apcp[s] = a  # koşu başından beri toplam (dosyada bir de 6 saatlik kova var, o atlanır)
            elif name == 'csnow':
                csnow[s] = a
        time.sleep(0.6)  # NOMADS'a nazik ol (dakikada en fazla ~120 istek)
    zero = np.zeros(NPTS)
    cum, cum_snow, prev = {0: zero}, {0: zero}, 0
    for s in sorted(steps)[1:]:
        if s not in apcp or prev not in cum:
            continue
        cum[s] = apcp[s]
        # Kar: önceki adımdan bu yana yağışı, adım anındaki "kar yağıyor" bayrağıyla say (yaklaşık)
        cum_snow[s] = cum_snow[prev] + np.maximum(apcp[s] - cum[prev], 0) * csnow.get(s, 0)
        prev = s
    accumulations(f, steps, cum, cum_snow)
    write('gfs', run, steps, f, {'name': 'GFS', 'source': 'NOAA NCEP', 'resolution': '0,25°'})


# ---------------------------------------------------------------- ECMWF (açık veri)

def ecmwf(steps_filter=None):
    base = 'https://data.ecmwf.int/forecasts'
    run = None
    for t in recent_runs((0, 6, 12, 18)):
        maxh = 360 if t.hour in (0, 12) else 144  # 06/18 koşuları 6 gün
        stream = 'oper'
        if exists(f'{base}/{t:%Y%m%d}/{t:%H}z/ifs/0p25/{stream}/{t:%Y%m%d%H}0000-{maxh}h-{stream}-fc.index'):
            run = t
            break
    if run is None:
        return log('ECMWF: tamamlanmış koşu bulunamadı')
    if published('ecmwf', run) and not steps_filter:
        return log('ECMWF: yeni koşu yok', run)
    steps = steps_filter or standard_steps(maxh)
    log('ECMWF', run, len(steps), 'adım indiriliyor')
    want = {('t', '850'), ('t', '500'), ('gh', '850'), ('gh', '500'), ('msl', None), ('2t', None), ('10fg', None), ('tp', None), ('sf', None)}
    f = {k: {} for k in ('t850', 'z850', 'z500', 't500', 'mslp', 't2m', 'gust')}
    tp, sf = {}, {}
    for s in steps:
        stem = f'{base}/{run:%Y%m%d}/{run:%H}z/ifs/0p25/{stream}/{run:%Y%m%d%H}0000-{s}h-{stream}-fc'
        index = [json.loads(line) for line in get(f'{stem}.index').decode().splitlines() if line.strip()]
        parts = [r for r in index if (r['param'], r.get('levelist')) in want]
        chunks = []
        for r in parts:  # yalnızca gereken mesajları bayt aralığıyla indir
            req = urllib.request.Request(f'{stem}.grib2', headers={**UA, 'Range': f"bytes={r['_offset']}-{r['_offset'] + r['_length'] - 1}"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                chunks.append(resp.read())
        for name, ltype, lev, vals, lats, lons, _ in messages(b''.join(chunks)):
            a = subset(vals, lats, lons)
            if name == 't' and lev == 850:
                f['t850'][s] = a - 273.15
            elif name == 't' and lev == 500:
                f['t500'][s] = a - 273.15
            elif name == 'gh' and lev == 850:
                f['z850'][s] = a
            elif name == 'gh' and lev == 500:
                f['z500'][s] = a
            elif name == 'msl':
                f['mslp'][s] = a / 100
            elif name == '2t':
                f['t2m'][s] = a - 273.15
            elif name == '10fg':
                f['gust'][s] = a * 3.6
            elif name == 'tp':
                tp[s] = a * 1000          # m → mm
            elif name == 'sf':
                sf[s] = a * 1000          # m su eşdeğeri → mm ≈ cm kar (10:1)
    tp.setdefault(0, np.zeros(NPTS))
    sf.setdefault(0, np.zeros(NPTS))
    accumulations(f, steps, tp, sf)
    write('ecmwf', run, steps, f, {'name': 'ECMWF', 'source': 'ECMWF açık veri (CC BY 4.0)', 'resolution': '0,25°'})


# ---------------------------------------------------------------- ICON-EU (DWD)

def icon(steps_filter=None):
    base = 'https://opendata.dwd.de/weather/nwp/icon-eu/grib'
    run = None
    for t in recent_runs((0, 6, 12, 18)):
        name = f'icon-eu_europe_regular-lat-lon_single-level_{t:%Y%m%d%H}_120_T_2M.grib2.bz2'
        if exists(f'{base}/{t:%H}/t_2m/{name}'):
            run = t
            break
    if run is None:
        return log('ICON: tamamlanmış koşu bulunamadı')
    if published('icon', run) and not steps_filter:
        return log('ICON: yeni koşu yok', run)
    steps = steps_filter or list(range(0, 121, 3))
    log('ICON', run, len(steps), 'adım indiriliyor')
    r = f'{run:%Y%m%d%H}'

    def grab(var, s, level=None):
        kind = 'pressure-level' if level else 'single-level'
        lv = f'_{level}' if level else ''
        url = f'{base}/{run:%H}/{var.lower()}/icon-eu_europe_regular-lat-lon_{kind}_{r}_{s:03d}{lv}_{var.upper()}.grib2.bz2'
        for _, _, _, vals, lats, lons, _ in messages(bz2.decompress(get(url))):
            return subset(vals, lats, lons)
        return None

    g = 9.80665
    f = {k: {} for k in ('t850', 'z850', 'z500', 't500', 'mslp', 't2m', 'gust')}
    prec, snow = {}, {}
    for s in steps:
        f['t850'][s] = grab('t', s, 850) - 273.15
        f['t500'][s] = grab('t', s, 500) - 273.15
        f['z850'][s] = grab('fi', s, 850) / g
        f['z500'][s] = grab('fi', s, 500) / g
        f['mslp'][s] = grab('pmsl', s) / 100
        f['t2m'][s] = grab('t_2m', s) - 273.15
        if s > 0:
            f['gust'][s] = grab('vmax_10m', s) * 3.6
            prec[s] = grab('tot_prec', s)
            snow[s] = grab('snow_gsp', s) + grab('snow_con', s)
    prec[0] = np.zeros(NPTS)
    snow[0] = np.zeros(NPTS)
    accumulations(f, steps, prec, snow)
    write('icon', run, steps, f, {'name': 'ICON', 'source': 'DWD açık veri', 'resolution': '0,0625° → 0,25°', 'coverage': '29,5–70,5°K, 23,5°B–62,5°D'}, regions=('tr', 'eu'))  # ICON-EU dünyayı kapsamaz


def write_index():
    """index.json: her model için saklanan koşular, en yeni önce"""
    models_meta = {}
    for m in ('gfs', 'ecmwf', 'icon'):
        runs = []
        for r in runs_of(m):
            with open(os.path.join(OUT, m, r, 'meta.json'), encoding='utf-8') as mf:
                mm = json.load(mf)
            runs.append({'id': r, 'run': mm['run'], 'generated': mm['generated'],
                         'hours': mm['regions']['tr']['steps'][-1], 'regions': list(mm['regions'])})
        if runs:
            models_meta[m] = {'runs': runs}
    with open(os.path.join(OUT, 'index.json'), 'w', encoding='utf-8') as fh:
        json.dump({'version': 2, 'updated': dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%MZ'), 'models': models_meta}, fh, ensure_ascii=False)


def main():
    args = sys.argv[1:]
    steps = None
    if '--steps' in args:
        i = args.index('--steps')
        steps = [int(x) for x in args[i + 1].split(',')]
        args = args[:i] + args[i + 2:]
    models = args or ['gfs', 'ecmwf', 'icon']
    failed = []
    for m in models:
        try:
            {'gfs': gfs, 'ecmwf': ecmwf, 'icon': icon}[m](steps)
        except Exception as e:  # noqa: BLE001 — bir model hata verirse diğerleri yine üretilsin
            log(m, 'HATA', repr(e))
            failed.append(m)
        write_index()  # her modelden sonra: biten modeller hemen görünsün
    if failed and len(failed) == len(models):
        sys.exit(1)


if __name__ == '__main__':
    os.makedirs(OUT, exist_ok=True)
    main()
