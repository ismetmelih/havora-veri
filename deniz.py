"""Deniz suyu sıcaklığı: NOAA OISST v2.1 günlük (0,25°, uydu + şamandıra + gemi), sıcaklık ve uzun yıllar ortalamasına göre fark.

Çıktı (out/deniz/):
  son.json   son gün: Türkiye çevresi ızgarası (0,25°), dünya ızgarası (1°), denizlerin ortalaması, kıyı şehirleri
  seri.json  son 120 gün: denizlerin günlük ortalaması (sıcaklık, fark)
Günde bir kez yeni gün var mı bakılır; ilk çalışmada seri geriye doğru (turda en çok 30 gün) doldurulur.
"""
import base64
import datetime as dt
import json
import os
import re
import tempfile
import time
import urllib.request
import warnings

import h5py
import numpy as np

BASE = 'https://www.ncei.noaa.gov/data/sea-surface-temperature-optimum-interpolation/v2.1/access/avhrr/'
OUT = os.path.join('out', 'deniz')
UA = {'User-Agent': 'havaxpro-veri/1.0'}
DAYS = 120
warnings.simplefilter('ignore', RuntimeWarning)   # karadaki boş 1° hücrelerde ortalama uyarısı
BOX = (30, 48, 18, 46)   # Türkiye çevresi: güney, kuzey, batı, doğu
# Denizler: (ad, güney, kuzey, batı, doğu, hariç tutulan kutular)
SEAS = {
    'karadeniz': ('Karadeniz', 41.0, 47.0, 27.4, 42.0, [(45.3, 48, 34.6, 42)]),   # Azak Denizi hariç
    'marmara': ('Marmara', 40.2, 41.1, 26.9, 30.0, []),
    'ege': ('Ege', 35.2, 40.9, 22.5, 28.3, [(40.2, 41.1, 26.9, 30.0)]),
    'akdeniz': ('Akdeniz', 33.0, 37.2, 28.3, 36.5, []),
}
CITIES = [
    ('İğneada', 41.88, 28.03, 'karadeniz'), ('Şile', 41.18, 29.61, 'karadeniz'), ('Zonguldak', 41.46, 31.79, 'karadeniz'),
    ('Amasra', 41.75, 32.39, 'karadeniz'), ('Sinop', 42.03, 35.15, 'karadeniz'), ('Samsun', 41.29, 36.33, 'karadeniz'),
    ('Ordu', 40.99, 37.88, 'karadeniz'), ('Trabzon', 41.00, 39.72, 'karadeniz'), ('Rize', 41.03, 40.52, 'karadeniz'),
    ('İstanbul', 40.98, 28.98, 'marmara'), ('Tekirdağ', 40.97, 27.51, 'marmara'), ('Mudanya', 40.38, 28.88, 'marmara'),
    ('Çanakkale', 40.15, 26.40, 'ege'), ('Ayvalık', 39.31, 26.69, 'ege'), ('Çeşme', 38.32, 26.30, 'ege'), ('İzmir', 38.42, 27.13, 'ege'),
    ('Kuşadası', 37.86, 27.26, 'ege'), ('Bodrum', 37.03, 27.43, 'ege'), ('Marmaris', 36.85, 28.27, 'akdeniz'),
    ('Fethiye', 36.62, 29.10, 'akdeniz'), ('Kaş', 36.20, 29.64, 'akdeniz'), ('Antalya', 36.88, 30.70, 'akdeniz'),
    ('Alanya', 36.54, 32.00, 'akdeniz'), ('Anamur', 36.08, 32.84, 'akdeniz'), ('Mersin', 36.80, 34.63, 'akdeniz'),
    ('İskenderun', 36.59, 36.17, 'akdeniz'),
]


def get(url):
    for i in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180) as r:
                return r.read()
        except Exception:  # noqa: BLE001
            if i == 2:
                raise
            time.sleep(5 * (i + 1))


def files_of(month):
    """Ayın günlük dosyaları: {tarih: dosya adı}; aynı gün için kesin (final) sürüm ön sürüme tercih edilir"""
    names = re.findall(r'href="(oisst-avhrr-v02r01\.(\d{8})(_preliminary)?\.nc)"', get(f'{BASE}{month}/').decode('latin-1'))
    out = {}
    for name, d, pre in sorted(names, key=lambda x: (x[1], x[2] == '')):
        out[d] = name
    return out


def read(day, name):
    with tempfile.NamedTemporaryFile(suffix='.nc') as tmp:
        tmp.write(get(f'{BASE}{day[:6]}/{name}'))
        tmp.flush()
        with h5py.File(tmp.name, 'r') as h:
            def field(k):
                a = h[k][0, 0].astype(float)
                return np.where(a == -999, np.nan, a * 0.01)
            return field('sst'), field('anom'), h['lat'][:].astype(float), h['lon'][:].astype(float)


def sea_masks(lat, lon):
    la, lo = lat[:, None], lon[None, :]
    out = {}
    for k, (_, s, n, w, e, excl) in SEAS.items():
        m = (la >= s) & (la <= n) & (lo >= w) & (lo <= e)
        for (s2, n2, w2, e2) in excl:
            m &= ~((la >= s2) & (la <= n2) & (lo >= w2) & (lo <= e2))
        out[k] = m
    return out


def means(sst, anom, masks, lat):
    w = np.cos(np.radians(lat))[:, None] * np.ones_like(sst)
    res = {}
    for k, m in masks.items():
        ok = m & np.isfinite(sst)
        if ok.any():
            res[k] = [round(float(np.sum(sst[ok] * w[ok]) / np.sum(w[ok])), 2), round(float(np.sum(anom[ok] * w[ok]) / np.sum(w[ok])), 2)]
    return res


def b64(a):
    return base64.b64encode(np.ascontiguousarray(a).tobytes()).decode()


def main():
    os.makedirs(OUT, exist_ok=True)
    today = dt.datetime.utcnow().date()
    avail = {}
    for k in range(5):   # bu ay ve önceki aylar (seri için)
        m = (today.replace(day=1) - dt.timedelta(days=31 * k)).strftime('%Y%m')
        try:
            avail.update(files_of(m))
        except Exception:  # noqa: BLE001
            pass
    if not avail:
        print('Deniz: dosya listesi alınamadı')
        return
    last = max(avail)
    sp = os.path.join(OUT, 'seri.json')
    try:
        with open(sp, encoding='utf-8') as f:
            seri = json.load(f)
    except (OSError, ValueError):
        seri = {'days': {}}
    try:
        with open(os.path.join(OUT, 'son.json'), encoding='utf-8') as f:
            done = json.load(f).get('date') == f'{last[:4]}-{last[4:6]}-{last[6:]}'
    except (OSError, ValueError):
        done = False
    want = sorted((d for d in avail if d >= (today - dt.timedelta(days=DAYS)).strftime('%Y%m%d')), reverse=True)
    missing = [d for d in want if f'{d[:4]}-{d[4:6]}-{d[6:]}' not in seri['days']]
    if done and not missing:
        print('Deniz: güncel')
        return
    t0, masks = time.time(), None
    for d in [last] + [x for x in missing if x != last][:30]:
        if time.time() - t0 > 300:
            break
        try:
            sst, anom, lat, lon = read(d, avail[d])
        except Exception as e:  # noqa: BLE001
            print('Deniz', d, 'okunamadı:', e)
            continue
        masks = masks or sea_masks(lat, lon)
        iso = f'{d[:4]}-{d[4:6]}-{d[6:]}'
        seri['days'][iso] = means(sst, anom, masks, lat)
        if d == last and not done:
            # Türkiye çevresi (0,25°, 0,1 °C, int16; kara −32768)
            yi = np.where((lat >= BOX[0]) & (lat <= BOX[1]))[0]
            xi = np.where((lon >= BOX[2]) & (lon <= BOX[3]))[0]
            q = lambda a: np.where(np.isfinite(a), np.round(a * 10), -32768).astype('<i2')   # noqa: E731
            box_sst, box_anom = sst[np.ix_(yi, xi)], anom[np.ix_(yi, xi)]
            # Dünya (1°): 4×4 hücre ortalaması; sıcaklık (°C−15)×5, fark ×10, int8; kara −128
            with np.errstate(all='ignore'):
                wsst = np.nanmean(sst.reshape(180, 4, 360, 4), axis=(1, 3))
                wanom = np.nanmean(anom.reshape(180, 4, 360, 4), axis=(1, 3))
            i8 = lambda a, k, off: np.where(np.isfinite(a), np.clip(np.round((a - off) * k), -127, 127), -128).astype(np.int8)   # noqa: E731
            cities = []
            for name, cla, clo, sea in CITIES:
                best, bd = None, 9
                for j in range(len(yi)):
                    for i in range(len(xi)):
                        v = box_sst[j, i]
                        if not np.isfinite(v):
                            continue
                        dd = np.hypot(lat[yi[j]] - cla, (lon[xi[i]] - clo) * np.cos(np.radians(cla)))
                        if dd < bd:
                            bd, best = dd, (round(float(v), 1), round(float(box_anom[j, i]), 1))
                if best and bd <= 0.8:
                    cities.append({'name': name, 'lat': cla, 'lon': clo, 'sea': sea, 'sst': best[0], 'anom': best[1]})
            with open(os.path.join(OUT, 'son.json'), 'w', encoding='utf-8') as f:
                json.dump({'date': iso, 'generated': time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime()), 'preliminary': 'preliminary' in avail[d],
                           'seas': {k: {'name': SEAS[k][0], 'sst': v[0], 'anom': v[1]} for k, v in seri['days'][iso].items()}, 'cities': cities,
                           'box': {'lat0': float(lat[yi[0]]), 'lon0': float(lon[xi[0]]), 'step': 0.25, 'ny': len(yi), 'nx': len(xi),
                                   'sst': b64(q(box_sst)), 'anom': b64(q(box_anom))},
                           'world': {'lat0': -89.5, 'lon0': 0.5, 'step': 1, 'ny': 180, 'nx': 360,
                                     'sst': b64(i8(wsst, 5, 15)), 'anom': b64(i8(wanom, 10, 0))},
                           'source': 'NOAA OISST v2.1 (günlük, 0,25°; uydu, şamandıra ve gemi ölçümleri)'}, f, ensure_ascii=False, separators=(',', ':'))
    keep = sorted(seri['days'])[-DAYS:]
    seri['days'] = {k: seri['days'][k] for k in keep}
    seri['names'] = {k: v[0] for k, v in SEAS.items()}
    with open(sp, 'w', encoding='utf-8') as f:
        json.dump(seri, f, ensure_ascii=False, separators=(',', ':'))
    print(f'Deniz: son gün {last}, seride {len(seri["days"])} gün')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # noqa: BLE001  (bu iş aksarsa model işi sürsün)
        print('Deniz suyu alınamadı:', e)
