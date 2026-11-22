"""GEFS senaryo haritaları: 31 senaryonun (kontrol + 30) her biri için Avrupa–Türkiye sinoptik alanları.

Diyagram sayfasında bir senaryo çizgisine dokununca o senaryonun haritası açılır.
Kaynak: NOAA GEFS 0,5° (AWS açık veri), gerekli mesajlar .idx ile bayt aralığı olarak indirilir.
Alanlar (1° ızgara, 20°B–55°D, 28°K–68°K, 0–240 saat, 6 saatte bir):
  z500  500 hPa yükseklik (dam)          bayt = dam − 470
  t850  850 hPa sıcaklık (°C)            bayt = (°C + 50) × 2
  mslp  deniz seviyesi basıncı (hPa)     bayt = (hPa − 940) × 2
  pr6   6 saatlik yağış (mm) + kar       bayt = q (yağmur) ya da 128 + q (kar, GEFS CSNOW); q = √mm × 12 (en çok 127)
Her senaryo × alan bir PNG (gri, 8 bit): genişlik = boylam, yükseklik = adım × enlem (adımlar alt alta).
Çıktı: out/gefs-uye/meta.json ve out/gefs-uye/{c00,p01..p30}-{alan}.png (yalnız son koşu tutulur).
"""
import datetime as dt
import json
import os
import struct
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zlib
from concurrent.futures import ThreadPoolExecutor

import eccodes
import numpy as np

OUT = os.path.join('out', 'gefs-uye')
S3 = 'https://noaa-gefs-pds.s3.amazonaws.com'
UA = {'User-Agent': 'havaxpro-veri/1.0'}
STEPS = list(range(0, 241, 6))
MEMBERS = ['c00'] + [f'p{i:02d}' for i in range(1, 31)]
if os.environ.get('UYE_TEST'):   # yerel deneme: birkaç senaryo
    MEMBERS = MEMBERS[: int(os.environ['UYE_TEST'])]
LON = np.arange(-20, 56, 1.0)
LAT = np.arange(68, 27, -1.0)
WANT = {('HGT', '500 mb'): 'z500', ('TMP', '850 mb'): 't850', ('PRMSL', 'mean sea level'): 'mslp', ('APCP', 'surface'): 'apcp', ('CSNOW', 'surface'): 'csnow'}
_GRIB = threading.Lock()


def log(*a):
    print(dt.datetime.utcnow().strftime('%H:%M:%S'), 'uyeler', *a, flush=True)


def get(url, headers=None, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={**UA, **(headers or {})}), timeout=60) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404 or i == tries - 1:
                raise
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                raise
        time.sleep(3 * (i + 1))


def path(run, mem, fh):
    pre = 'gec00' if mem == 'c00' else f'gep{mem[1:]}'
    return f'{S3}/gefs.{run:%Y%m%d}/{run:%H}/atmos/pgrb2ap5/{pre}.t{run:%H}z.pgrb2a.0p50.f{fh:03d}'


def latest_run():
    now = dt.datetime.utcnow()
    for h in range(0, 36, 6):
        t = (now - dt.timedelta(hours=h)).replace(minute=0, second=0, microsecond=0)
        t = t.replace(hour=t.hour - t.hour % 6)
        try:
            get(path(t, 'p30', 240) + '.idx')
            return t
        except Exception:  # noqa: BLE001
            continue
    return None


def sample(vals, lat0, lon0, dlat, dlon):
    """0,5° dünya ızgarasından bölgeyi 1° örnekle (boylam 0–360 sarmalı); dlat, dlon artı"""
    j = np.rint((lat0 - LAT) / dlat if lat0 > 0 else (LAT - lat0) / dlat).astype(int)
    i = np.rint(((LON - lon0) % 360) / dlon).astype(int) % vals.shape[1]
    return vals[np.ix_(j, i)]


def one(run, mem, fh):
    """Bir senaryonun bir adımı: alan adı → (enlem × boylam) dizisi"""
    url = path(run, mem, fh)
    rows = [ln.split(':') for ln in get(url + '.idx').decode().splitlines()]
    chunks = []
    for k, r in enumerate(rows):
        if (r[3], r[4]) in WANT:
            a = int(r[1])
            b = int(rows[k + 1][1]) - 1 if k + 1 < len(rows) else ''
            chunks.append(get(url, {'Range': f'bytes={a}-{b}'}))
    out = {}
    with _GRIB, tempfile.NamedTemporaryFile(suffix='.grb2') as tmp:
        tmp.write(b''.join(chunks))
        tmp.flush()
        with open(tmp.name, 'rb') as f:
            while True:
                h = eccodes.codes_grib_new_from_file(f)
                if h is None:
                    break
                key = (eccodes.codes_get(h, 'shortName'), eccodes.codes_get(h, 'typeOfLevel'))
                name = {('gh', 'isobaricInhPa'): 'z500', ('t', 'isobaricInhPa'): 't850', ('prmsl', 'meanSea'): 'mslp',
                        ('tp', 'surface'): 'apcp', ('csnow', 'surface'): 'csnow'}.get(key)
                if name:
                    nj, ni = eccodes.codes_get(h, 'Nj'), eccodes.codes_get(h, 'Ni')
                    v = eccodes.codes_get_values(h).reshape(nj, ni)
                    lat0 = eccodes.codes_get(h, 'latitudeOfFirstGridPointInDegrees')
                    lon0 = eccodes.codes_get(h, 'longitudeOfFirstGridPointInDegrees')
                    dlat = eccodes.codes_get(h, 'jDirectionIncrementInDegrees')
                    dlon = eccodes.codes_get(h, 'iDirectionIncrementInDegrees')
                    out[name] = sample(v, lat0, lon0, dlat, dlon)
                eccodes.codes_release(h)
    return out


def encode(name, a):
    if name == 'z500':
        return np.clip(np.rint(a / 10 - 470), 0, 255)
    if name == 't850':
        return np.clip(np.rint((a - 273.15 + 50) * 2), 0, 255)
    if name == 'mslp':
        return np.clip(np.rint((a / 100 - 940) * 2), 0, 255)
    raise ValueError(name)


def png(path_, img):
    """8 bit gri PNG (satır süzgeci: Sub; düzgün alanlar iyi sıkışır)"""
    h, w = img.shape
    raw = bytearray()
    for row in img.astype(np.uint8):
        raw.append(1)
        raw += bytes(np.concatenate([[row[0]], (row[1:].astype(np.int16) - row[:-1]) % 256]).astype(np.uint8))
    def chunk(t, d):
        return struct.pack('>I', len(d)) + t + d + struct.pack('>I', zlib.crc32(t + d) & 0xFFFFFFFF)
    with open(path_, 'wb') as fh:
        fh.write(b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', w, h, 8, 0, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(bytes(raw), 9)) + chunk(b'IEND', b''))


def main():
    run = latest_run()
    if run is None:
        log('240 saati hazır GEFS koşusu yok')
        return
    rid = f'{run:%Y%m%d%H}'
    mpath = os.path.join(OUT, 'meta.json')
    if os.path.exists(mpath):
        with open(mpath, encoding='utf-8') as fh:
            if json.load(fh).get('run_id') == rid:
                log('güncel', rid)
                return
    t0 = time.time()
    ny, nx, ns = len(LAT), len(LON), len(STEPS)
    cube = {m: {f: np.zeros((ns, ny, nx), np.uint8) for f in ('z500', 't850', 'mslp', 'pr6')} for m in MEMBERS}
    jobs = [(m, k, fh) for m in MEMBERS for k, fh in enumerate(STEPS)]
    bad = []

    def work(job):
        m, k, fh = job
        try:
            d = one(run, m, fh)
        except Exception as e:  # noqa: BLE001
            return job, None, repr(e)
        return job, d, None

    with ThreadPoolExecutor(10) as ex:
        for (m, k, fh), d, err in ex.map(work, jobs):
            if err or not d or 'z500' not in d:
                bad.append(f'{m}+{fh}')
                continue
            for f in ('z500', 't850', 'mslp'):
                if f in d:
                    cube[m][f][k] = encode(f, d[f])
            if 'apcp' in d:
                q = np.clip(np.rint(np.sqrt(np.maximum(d['apcp'], 0)) * 12), 0, 127)
                snow = (d.get('csnow', np.zeros_like(q)) >= 0.5) & (q > 0)
                cube[m]['pr6'][k] = np.where(snow, q + 128, q)
    if len(bad) > len(jobs) * 0.1:
        log('çok eksik adım, yazılmadı', len(bad))
        return
    os.makedirs(OUT, exist_ok=True)
    for m in MEMBERS:
        for f, a in cube[m].items():
            png(os.path.join(OUT, f'{m}-{f}.png'), a.reshape(ns * ny, nx))
    meta = {'run_id': rid, 'run': run.strftime('%Y-%m-%dT%H:00Z'), 'generated': dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%MZ'),
            'source': 'NOAA GEFS 0,5° (31 senaryo)', 'steps': STEPS, 'members': MEMBERS, 'missing': bad,
            'grid': {'lon0': float(LON[0]), 'lat0': float(LAT[0]), 'step': 1.0, 'nx': nx, 'ny': ny},
            'enc': {'z500': 'dam = b + 470', 't850': '°C = b / 2 − 50', 'mslp': 'hPa = b / 2 + 940', 'pr6': 'b ≥ 128 kar; q = b % 128; mm = (q / 12)²'}}
    with open(mpath, 'w', encoding='utf-8') as fh:
        json.dump(meta, fh, ensure_ascii=False, separators=(',', ':'))
    log('yazıldı', rid, f'{time.time() - t0:.0f} sn', len(bad), 'eksik adım')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log('HATA', repr(e))
