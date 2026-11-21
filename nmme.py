"""ENSO tahmini: NMME (Kuzey Amerika çoklu model topluluğu) modellerinin 9 aylık deniz yüzeyi sıcaklık farkı tahmininden
Niño 1+2, 3, 3.4 ve 4 bölgelerinin aylık ortalaması.

Kaynak: NOAA CPC NMME realtime_anom (her modelin topluluk ortalaması, 1°, kendi 1991–2020 iklimine göre fark).
Ayda bir (her ayın ~8'inde) yeni tahmin gelir; her turda yalnız klasör listesine bakılır.
Çıktı: out/analiz/nmme.json
"""
import json
import os
import re
import tempfile
import time
import urllib.request

import numpy as np
from scipy.io import netcdf_file

BASE = 'https://ftp.cpc.ncep.noaa.gov/NMME/realtime_anom/ENSMEAN/'
OUT = os.path.join('out', 'analiz', 'nmme.json')
UA = {'User-Agent': 'havaxpro-veri/1.0'}
# Bölgeler: (güney, kuzey, batı, doğu) boylam 0–360
REGIONS = {'nino12': (-10, 0, 270, 280), 'nino3': (-5, 5, 210, 270), 'nino34': (-5, 5, 190, 240), 'nino4': (-5, 5, 160, 210)}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180) as r:
        return r.read()


def region_means(path):
    with netcdf_file(path, mmap=False) as ds:
        a = ds.variables['fcst'].data.astype(float)
        lat = ds.variables['lat'].data.astype(float).copy()
        lon = ds.variables['lon'].data.astype(float).copy()
        target = ds.variables['target'].data.astype(float).copy()
    a = np.where(np.abs(a) > 100, np.nan, a)   # eksik değer
    out = {}
    for k, (s, n, w, e) in REGIONS.items():
        my = (lat >= s) & (lat <= n)
        mx = (lon >= w) & (lon <= e)
        wts = np.cos(np.radians(lat[my]))[:, None] * np.ones((1, mx.sum()))
        vals = []
        for t in range(a.shape[0]):
            box = a[t][np.ix_(my, mx)]
            ok = np.isfinite(box)
            vals.append(round(float(np.sum(box[ok] * wts[ok]) / np.sum(wts[ok])), 2) if ok.any() else None)
        out[k] = vals
    # hedef ay: 1960-01'den bu yana ay sayısı
    months = [f'{1960 + int(m) // 12}-{int(m) % 12 + 1:02d}' for m in target]
    return months, out


def main():
    dirs = sorted(re.findall(r'href="(\d{10})/"', get(BASE).decode('latin-1')))
    if not dirs:
        print('NMME: klasör yok')
        return
    init = dirs[-1]
    try:
        with open(OUT, encoding='utf-8') as f:
            if json.load(f).get('dir') == init:
                print('NMME: güncel')
                return
    except (OSError, ValueError):
        pass
    files = sorted(set(re.findall(r'href="([\w.]+\.tmpsfc\.\d{6}\.ENSMEAN\.anom\.nc)"', get(BASE + init + '/').decode('latin-1'))))
    models, months = {}, None
    with tempfile.TemporaryDirectory() as tmp:
        for fn in files:
            name = fn.split('.tmpsfc.')[0]
            p = os.path.join(tmp, 'f.nc')
            try:
                with open(p, 'wb') as f:
                    f.write(get(BASE + init + '/' + fn))
                m, vals = region_means(p)
            except Exception as e:  # noqa: BLE001
                print('NMME', name, 'okunamadı:', e)
                continue
            if months is None or len(m) < len(months):
                months = m
            models[name] = vals
    if not models:
        return
    # NMME ortalama dosyası 12 ay içerir, son aylar boş (0): bütün modeller ortak aylara kesilir
    models = {k: {r: v[:len(months)] for r, v in vals.items()} for k, vals in models.items()}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump({'generated': time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime()), 'dir': init, 'init': f'{init[:4]}-{init[4:6]}',
                   'months': months, 'models': models,
                   'source': 'NOAA CPC NMME (topluluk ortalamaları, 1991–2020 iklimine göre fark)'}, f, ensure_ascii=False, separators=(',', ':'))
    print(f'NMME: {init[:6]} başlangıçlı {len(models)} model yazıldı')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # noqa: BLE001  (bu iş aksarsa model işi sürsün)
        print('NMME alınamadı:', e)
