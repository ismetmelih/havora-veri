"""Senaryo kar haritaları: ECMWF ENS ve AIFS ENS'in her senaryosu için 3, 7 ve 15 günlük toplam kar (Türkiye, 0,25°).

Kaynak: ECMWF açık verisi (CC BY 4.0), "sf" (kar yağışı, koşu başından beri birikmiş, m su eşdeğeri).
Birikmiş olduğu için yalnız 72, 168 ve 360. saatler indirilir; .index ile yalnız sf mesajları (bayt aralığı).
Çıktı:
  out/kar/senaryo.json            kutu, pencereler, modeller (koşu, senaryo numaraları)
  out/kar/senaryo-<model>.bin.gz  [senaryo][pencere][enlem][boylam] uint8, q = √cm × 12 (en çok 255 ≈ 450 cm)
10:1 oranıyla 1 mm su ≈ 1 cm kar. 6 saatte bir yenilenir (yalnız 00Z/12Z koşuları 15 gün).
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
BASE = 'https://data.ecmwf.int/forecasts'
UA = {'User-Agent': 'havaxpro-veri/1.0'}
STEPS = [72, 168, 360]
BOX = {'w': 25.0, 'e': 45.5, 'n': 43.0, 's': 35.5, 'step': 0.25}
NX = int(round((BOX['e'] - BOX['w']) / BOX['step'])) + 1
NY = int(round((BOX['n'] - BOX['s']) / BOX['step'])) + 1
MODELS = [('ens', 'ifs', ['ef'], 'ECMWF ENS'), ('aifs', 'aifs-ens', ['cf', 'pf'], 'ECMWF AIFS ENS')]
_GRIB = threading.Lock()


def log(*a):
    print(dt.datetime.utcnow().strftime('%H:%M:%S'), 'senaryo-kar', *a, flush=True)


def get(url, rng=None, tries=4):
    for k in range(tries):
        try:
            h = dict(UA)
            if rng:
                h['Range'] = f'bytes={rng[0]}-{rng[1]}'
            with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=120) as r:
                return r.read()
        except Exception:  # noqa: BLE001
            if k == tries - 1:
                raise
            time.sleep(4 * (k + 1))


def stem(run, product, step, kind):
    return f'{BASE}/{run:%Y%m%d}/{run:%H}z/{product}/0p25/enfo/{run:%Y%m%d%H}0000-{step}h-enfo-{kind}'


def latest(product, kinds):
    now = dt.datetime.utcnow().replace(minute=0, second=0, microsecond=0)
    for h in range(0, 72, 12):
        t = now - dt.timedelta(hours=h)
        run = t.replace(hour=0 if t.hour < 12 else 12)
        try:
            get(f'{stem(run, product, 360, kinds[-1])}.index')
            return run
        except Exception:  # noqa: BLE001
            continue
    return None


def crop(msg):
    with _GRIB:
        g = eccodes.codes_new_from_message(msg)
        try:
            ni, nj = eccodes.codes_get(g, 'Ni'), eccodes.codes_get(g, 'Nj')
            la1, lo1 = eccodes.codes_get(g, 'latitudeOfFirstGridPointInDegrees'), eccodes.codes_get(g, 'longitudeOfFirstGridPointInDegrees')
            di, dj = eccodes.codes_get(g, 'iDirectionIncrementInDegrees'), eccodes.codes_get(g, 'jDirectionIncrementInDegrees')
            num = int(eccodes.codes_get(g, 'number'))
            # IFS: m su eşdeğeri, AIFS: kg/m² (= mm) → cm kar (10:1, 1 mm su ≈ 1 cm)
            k = 1.0 if str(eccodes.codes_get(g, 'units')).startswith('kg') else 1000.0
            vals = eccodes.codes_get_values(g).reshape(nj, ni)
        finally:
            eccodes.codes_release(g)
    lats = la1 - np.arange(nj) * dj
    lons = (lo1 + np.arange(ni) * di + 180) % 360 - 180
    yi = [int(np.argmin(np.abs(lats - (BOX['n'] - k * BOX['step'])))) for k in range(NY)]
    xi = [int(np.argmin(np.abs(lons - (BOX['w'] + k * BOX['step'])))) for k in range(NX)]
    return num, vals[np.ix_(yi, xi)] * k


def model(key, product, kinds, name):
    run = latest(product, kinds)
    if not run:
        log(name, 'koşu bulunamadı')
        return None
    rid = f'{run:%Y%m%d%H}'
    meta_path = os.path.join(OUT, 'senaryo.json')
    if os.path.exists(meta_path):
        with open(meta_path, encoding='utf-8') as fh:
            old = json.load(fh)
        if old.get('models', {}).get(key, {}).get('run') == rid and os.path.exists(os.path.join(OUT, f'senaryo-{key}.bin.gz')):
            log(name, rid, 'zaten var')
            return old['models'][key]
    by = {}   # senaryo no → [pencere] dizi
    for wi, step in enumerate(STEPS):
        jobs = []
        for kind in kinds:
            s = stem(run, product, step, kind)
            for line in get(f'{s}.index').decode().splitlines():
                if not line.strip():
                    continue
                r = json.loads(line)
                if r.get('param') == 'sf':
                    jobs.append((f'{s}.grib2', r['_offset'], r['_offset'] + r['_length'] - 1))
        with ThreadPoolExecutor(8) as ex:
            for num, a in ex.map(lambda j: crop(get(j[0], (j[1], j[2]))), jobs):
                by.setdefault(num, [None] * len(STEPS))[wi] = a
        log(name, rid, f'+{step} sa', len(jobs), 'senaryo')
    nums = sorted(n for n, w in by.items() if all(x is not None for x in w))
    if not nums:
        return None
    arr = np.zeros((len(nums), len(STEPS), NY, NX), dtype=np.uint8)
    for i, n in enumerate(nums):
        for wi in range(len(STEPS)):
            cm = np.clip(by[n][wi], 0, None)    # cm kar (crop birimi çevirdi)
            arr[i, wi] = np.clip(np.round(np.sqrt(cm) * 12), 0, 255).astype(np.uint8)
    with gzip.open(os.path.join(OUT, f'senaryo-{key}.bin.gz'), 'wb', compresslevel=9) as fh:
        fh.write(arr.tobytes())
    return {'name': name, 'run': rid, 'members': nums}


def main():
    path = os.path.join(OUT, 'senaryo.json')
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 3 * 3600:
        log('güncel')
        return
    os.makedirs(OUT, exist_ok=True)
    meta = {'generated': dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%MZ'), 'box': {**BOX, 'nx': NX, 'ny': NY}, 'steps': STEPS,
            'encoding': 'uint8 q = sqrt(cm) * 12; [senaryo][pencere][enlem kuzeyden][boylam batıdan]', 'models': {}}
    for key, product, kinds, name in MODELS:
        try:
            m = model(key, product, kinds, name)
            if m:
                meta['models'][key] = m
        except Exception as e:  # noqa: BLE001
            log(name, 'HATA', repr(e))
    if meta['models']:
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(meta, fh, ensure_ascii=False)


if __name__ == '__main__':
    main()
