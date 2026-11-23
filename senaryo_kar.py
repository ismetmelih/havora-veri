"""Senaryo haritaları: dört topluluk modelinin her senaryosu için 3, 7 ve 15 günlük toplam kar ve toplam yağış (Türkiye ve çevresi).

Kaynaklar (hepsi açık veri):
  ECMWF ENS ve AIFS ENS  data.ecmwf.int (CC BY 4.0), 0,25°: "sf" kar, "tp" yağış, koşu başından birikmiş → yalnız 72/168/360. saat
  NOAA GEFS              noaa-gefs-pds (AWS), 0,5°: 6 saatlik APCP ve CSNOW (kar payı); kar = Σ APCP × CSNOW (10:1)
  Kanada GEPS            dd.weather.gc.ca (MSC), 0,5°: APCP ve ASNOW koşu başından birikmiş, bütün senaryolar tek dosyada
Hepsi aynı 0,25° kutuya (çift doğrusal) taşınır. 10:1 oranıyla 1 mm su ≈ 1 cm kar.
Çıktı:
  out/kar/senaryo.json                 kutu, pencereler, kodlama, modeller (koşu, senaryo numaraları, çözünürlük)
  out/kar/senaryo-<model>.bin.gz        kar (cm)   [senaryo][pencere][enlem kuzeyden][boylam batıdan] uint8, q = √değer × QK
  out/kar/senaryo-<model>-yagis.bin.gz  yağış (mm) aynı biçim
Her çalışmada yalnız son koşular denetlenir (birkaç istek); yeni koşu varsa o model yeniden hesaplanır.
"""
import datetime as dt
import gzip
import json
import os
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

import eccodes
import numpy as np

OUT = os.path.join('out', 'kar')
UA = {'User-Agent': 'havaxpro-veri/1.0'}
STEPS = [72, 168, 360]
BOX = {'w': 22.0, 'e': 47.0, 'n': 46.5, 's': 33.5, 'step': 0.25}
NX = int(round((BOX['e'] - BOX['w']) / BOX['step'])) + 1
NY = int(round((BOX['n'] - BOX['s']) / BOX['step'])) + 1
LATS = BOX['n'] - np.arange(NY) * BOX['step']
LONS = BOX['w'] + np.arange(NX) * BOX['step']
QK = 11            # q = √değer × 11 → en çok 255 ≈ 537 (cm ya da mm)
ECMWF = 'https://data.ecmwf.int/forecasts'
GEFS = 'https://noaa-gefs-pds.s3.amazonaws.com'
GEPS = 'https://dd.weather.gc.ca'
_GRIB = threading.Lock()


def log(*a):
    print(dt.datetime.utcnow().strftime('%H:%M:%S'), 'senaryo', *a, flush=True)


def get(url, rng=None, tries=4, timeout=120):
    for k in range(tries):
        try:
            h = dict(UA)
            if rng:
                h['Range'] = f'bytes={rng[0]}-{rng[1]}'
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404 or k == tries - 1:
                raise
            time.sleep(4 * (k + 1))
        except Exception:  # noqa: BLE001
            if k == tries - 1:
                raise
            time.sleep(4 * (k + 1))


def exists(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA, method='HEAD'), timeout=30) as r:
            return r.status == 200
    except Exception:  # noqa: BLE001
        return False


def split_grib(buf):
    """Art arda GRIB2 iletileri (bir bayt aralığında ya da dosyada birden çok alan)."""
    out, i = [], 0
    while True:
        i = buf.find(b'GRIB', i)
        if i < 0 or i + 16 > len(buf):
            return out
        n = int.from_bytes(buf[i + 8:i + 16], 'big')
        out.append(buf[i:i + n])
        i += n


def to_box(msg):
    """GRIB iletisi → (kısa ad, senaryo no, birim, kutu dizisi). Enlem yönü ve 0–360 boylam dikkate alınır; çift doğrusal."""
    with _GRIB:
        g = eccodes.codes_new_from_message(msg)
        try:
            ni, nj = eccodes.codes_get(g, 'Ni'), eccodes.codes_get(g, 'Nj')
            la1, lo1 = eccodes.codes_get(g, 'latitudeOfFirstGridPointInDegrees'), eccodes.codes_get(g, 'longitudeOfFirstGridPointInDegrees')
            di, dj = eccodes.codes_get(g, 'iDirectionIncrementInDegrees'), eccodes.codes_get(g, 'jDirectionIncrementInDegrees')
            south_first = eccodes.codes_get(g, 'jScansPositively') == 1
            num = int(eccodes.codes_get(g, 'number'))
            par = str(eccodes.codes_get(g, 'shortName'))
            units = str(eccodes.codes_get(g, 'units'))
            vals = eccodes.codes_get_values(g).reshape(nj, ni)
        finally:
            eccodes.codes_release(g)
    fy = ((LATS - la1) / dj) if south_first else ((la1 - LATS) / dj)
    fx = ((LONS - lo1) % 360) / di
    y0 = np.clip(np.floor(fy).astype(int), 0, nj - 2)
    x0 = np.floor(fx).astype(int) % ni
    x1 = (x0 + 1) % ni
    wy = (fy - y0)[:, None]
    wx = (fx - np.floor(fx))[None, :]
    a = vals[np.ix_(y0, x0)] * (1 - wx) + vals[np.ix_(y0, x1)] * wx
    b = vals[np.ix_(y0 + 1, x0)] * (1 - wx) + vals[np.ix_(y0 + 1, x1)] * wx
    return par, num, units, a * (1 - wy) + b * wy


# ---------------- ECMWF ENS / AIFS ENS ----------------
def ec_stem(run, product, step, kind):
    return f'{ECMWF}/{run:%Y%m%d}/{run:%H}z/{product}/0p25/enfo/{run:%Y%m%d%H}0000-{step}h-enfo-{kind}'


def ecmwf(product, kinds):
    now = dt.datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    run = None
    for h in range(0, 72, 12):
        t = now - dt.timedelta(hours=h)
        r = t.replace(hour=0 if t.hour < 12 else 12)
        if exists(f'{ec_stem(r, product, 360, kinds[-1])}.index'):
            run = r
            break
    if not run:
        return None, None

    def fetch():
        data = {'kar': {}, 'yagis': {}}
        for wi, step in enumerate(STEPS):
            jobs = []
            for kind in kinds:
                s = ec_stem(run, product, step, kind)
                for line in get(f'{s}.index').decode().splitlines():
                    if line.strip():
                        r = json.loads(line)
                        if r.get('param') in ('sf', 'tp'):
                            jobs.append((f'{s}.grib2', r['_offset'], r['_offset'] + r['_length'] - 1))
            with ThreadPoolExecutor(8) as ex:
                for par, num, units, a in ex.map(lambda j: to_box(get(j[0], (j[1], j[2]))), jobs):
                    k = 1.0 if units.startswith('kg') else 1000.0      # IFS m su, AIFS kg/m² (= mm)
                    data['kar' if par == 'sf' else 'yagis'].setdefault(num, [None] * len(STEPS))[wi] = a * k
            log(product, f'{run:%Y%m%d%H}', f'+{step} sa', len(jobs), 'alan')
        return data
    return run, fetch


# ---------------- NOAA GEFS ----------------
def gefs():
    now = dt.datetime.utcnow()
    run = None
    for h in range(0, 48, 6):
        t = now - dt.timedelta(hours=h)
        r = t.replace(hour=t.hour // 6 * 6, minute=0, second=0, microsecond=0)
        if exists(f'{GEFS}/gefs.{r:%Y%m%d}/{r:%H}/atmos/pgrb2ap5/gep30.t{r:%H}z.pgrb2a.0p50.f360.idx'):
            run = r
            break
    if not run:
        return None, None
    members = ['gec00'] + [f'gep{k:02d}' for k in range(1, 31)]
    hours = list(range(6, 361, 6))

    def job(mh):
        m, h = mh
        base = f'{GEFS}/gefs.{run:%Y%m%d}/{run:%H}/atmos/pgrb2ap5/{m}.t{run:%H}z.pgrb2a.0p50.f{h:03d}'
        lines = get(f'{base}.idx').decode().splitlines()
        rng = []
        for i, ln in enumerate(lines):
            p = ln.split(':')
            if p[3] in ('APCP', 'CSNOW') and p[4] == 'surface':
                rng += [int(p[1]), int(lines[i + 1].split(':')[1]) - 1]
        got = {}
        for msg in split_grib(get(base, (min(rng), max(rng)))):   # APCP ve CSNOW yan yana: tek istek
            par, _, _, arr = to_box(msg)
            got[par] = arr
        tp = np.clip(got['tp'], 0, None)                          # APCP (kg/m² = mm), son 6 saat
        cs = np.clip(got.get('csnow', np.zeros_like(tp)), 0, 1)   # bu 6 saatte kar payı (ortalama)
        return int(m[3:]), h, tp, tp * cs

    def fetch():
        acc = {n: {'yagis': np.zeros((NY, NX)), 'kar': np.zeros((NY, NX))} for n in range(len(members))}
        data = {'kar': {}, 'yagis': {}}
        jobs = [(m, h) for h in hours for m in members]
        with ThreadPoolExecutor(16) as ex:
            for k, (num, h, tp, sn) in enumerate(ex.map(job, jobs), 1):   # sıra korunur: saatler artan
                acc[num]['yagis'] += tp
                acc[num]['kar'] += sn
                if h in STEPS:
                    for f in ('kar', 'yagis'):
                        data[f].setdefault(num, [None] * len(STEPS))[STEPS.index(h)] = acc[num][f].copy()
                if k % 372 == 0:
                    log('gefs', f'{run:%Y%m%d%H}', f'{k}/{len(jobs)}')
        return data
    return run, fetch


# ---------------- Kanada GEPS ----------------
def geps_url(run, step, var, today):
    root = f'{GEPS}/today' if today else f'{GEPS}/{run:%Y%m%d}/WXO-DD'
    return f'{root}/ensemble/geps/grib2/raw/{run:%H}/{step:03d}/CMC_geps-raw_{var}_SFC_0_latlon0p5x0p5_{run:%Y%m%d%H}_P{step:03d}_allmbrs.grib2'


def geps():
    now = dt.datetime.utcnow()
    run = today = None
    for h in range(0, 48, 12):
        t = now - dt.timedelta(hours=h)
        r = t.replace(hour=0 if t.hour < 12 else 12, minute=0, second=0, microsecond=0)
        for td in ((True, False) if r.date() == now.date() else (False,)):
            if exists(geps_url(r, 360, 'ASNOW', td)):
                run, today = r, td
                break
        if run:
            break
    if not run:
        return None, None

    def fetch():
        data = {'kar': {}, 'yagis': {}}
        for wi, step in enumerate(STEPS):
            for var, field in (('ASNOW', 'kar'), ('APCP', 'yagis')):
                for msg in split_grib(get(geps_url(run, step, var, today), timeout=300)):
                    _, num, _, a = to_box(msg)               # ikisi de kg/m² (kar 10:1 → cm)
                    data[field].setdefault(num, [None] * len(STEPS))[wi] = a
            log('geps', f'{run:%Y%m%d%H}', f'+{step} sa')
        return data
    return run, fetch


MODELS = [('ens', 'ECMWF ENS', '0,25°', lambda: ecmwf('ifs', ['ef'])),
          ('aifs', 'ECMWF AIFS ENS', '0,25°', lambda: ecmwf('aifs-ens', ['cf', 'pf'])),
          ('gefs', 'NOAA GEFS', '0,5°', gefs),
          ('geps', 'Kanada GEPS', '0,5°', geps)]


def write(key, data):
    nums = sorted(n for n in data['kar'] if all(x is not None for x in data['kar'][n]) and all(x is not None for x in data['yagis'].get(n, [None])))
    if not nums:
        return None
    for field, suffix in (('kar', ''), ('yagis', '-yagis')):
        arr = np.zeros((len(nums), len(STEPS), NY, NX), dtype=np.uint8)
        for i, n in enumerate(nums):
            for wi in range(len(STEPS)):
                v = np.clip(np.nan_to_num(data[field][n][wi]), 0, None)
                arr[i, wi] = np.clip(np.round(np.sqrt(v) * QK), 0, 255).astype(np.uint8)
        with gzip.open(os.path.join(OUT, f'senaryo-{key}{suffix}.bin.gz'), 'wb', compresslevel=9) as fh:
            fh.write(arr.tobytes())
    return nums


def main():
    path = os.path.join(OUT, 'senaryo.json')
    old = {}
    if os.path.exists(path):
        with open(path, encoding='utf-8') as fh:
            old = json.load(fh)
    box = {**BOX, 'nx': NX, 'ny': NY}
    same_grid = old.get('box') == box and old.get('qk') == QK     # kutu ya da kodlama değiştiyse hepsi yeniden
    os.makedirs(OUT, exist_ok=True)
    meta = {'generated': dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%MZ'), 'box': box, 'steps': STEPS, 'qk': QK,
            'encoding': f'uint8 q = sqrt(değer) * {QK}; [senaryo][pencere][enlem kuzeyden][boylam batıdan]; kar cm, yağış mm', 'models': {}}
    changed = False
    for key, name, res, src in MODELS:
        prev = old.get('models', {}).get(key) if same_grid else None
        try:
            run, fetch = src()
            if not run:
                log(name, 'koşu bulunamadı')
                if prev:
                    meta['models'][key] = prev
                continue
            rid = f'{run:%Y%m%d%H}'
            if prev and prev.get('run') == rid and os.path.exists(os.path.join(OUT, f'senaryo-{key}-yagis.bin.gz')):
                meta['models'][key] = prev
                continue
            t0 = time.time()
            nums = write(key, fetch())
            if nums:
                meta['models'][key] = {'name': name, 'run': rid, 'members': nums, 'fields': ['kar', 'yagis'], 'res': res}
                changed = True
                log(name, rid, len(nums), 'senaryo', f'{time.time() - t0:.0f} sn')
                with open(path, 'w', encoding='utf-8') as fh:      # her modelden sonra yaz: biten model hemen yayımlanabilsin
                    json.dump({**meta, 'models': {**(old.get('models', {}) if same_grid else {}), **meta['models']}}, fh, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            log(name, 'HATA', repr(e))
            if prev:
                meta['models'][key] = prev
    if changed and meta['models']:
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(meta, fh, ensure_ascii=False)
    else:
        log('yeni koşu yok')


if __name__ == '__main__':
    main()
