"""Dünya sıcaklık anomalisi: NASA GISTEMP v4 (GHCN v4 + ERSST v5, 2°, 1200 km yumuşatma), 1991–2020 normaline göre.

Kaynak aylık güncellenir; her turda yalnız başlığa (Last-Modified) bakılır, yeni sürüm varsa yeniden hesaplanır.
Çıktı (out/iklim/):
  ozet.json        yıl yıl küresel ve Türkiye ortalaması
  yil/<yıl>.json   o yılın 2°'lik dünya haritası (0,1 °C, int8, base64; -128 = veri yok)
"""
import base64
import gzip
import json
import os
import shutil
import tempfile
import time
import urllib.request
import warnings

import numpy as np
from scipy.io import netcdf_file

URL = 'https://data.giss.nasa.gov/pub/gistemp/gistemp1200_GHCNv4_ERSSTv5.nc.gz'
OUT = os.path.join('out', 'iklim')
UA = {'User-Agent': 'havaxpro-veri/1.0'}
BASE = (1991, 2020)
warnings.simplefilter('ignore', RuntimeWarning)   # boş hücrelerde ortalama uyarısı
TR = {'lat': (36, 42), 'lon': (26, 45)}   # Türkiye kutusu (hücre merkezleri)


def last_modified():
    req = urllib.request.Request(URL, method='HEAD', headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.headers.get('Last-Modified', '')


def main():
    os.makedirs(os.path.join(OUT, 'yil'), exist_ok=True)
    stamp = os.path.join(OUT, 'kaynak.txt')
    local = os.environ.get('IKLIM_NC')   # yerel deneme: önceden indirilmiş .nc
    lm = 'yerel' if local else last_modified()
    if os.path.exists(stamp) and open(stamp, encoding='utf-8').read() == lm and os.path.exists(os.path.join(OUT, 'ozet.json')):
        print('İklim: değişiklik yok')
        return
    with tempfile.TemporaryDirectory() as tmp:
        gz, nc = os.path.join(tmp, 'g.nc.gz'), os.path.join(tmp, 'g.nc')
        if local:
            nc = local
        else:
            with urllib.request.urlopen(urllib.request.Request(URL, headers=UA), timeout=300) as r, open(gz, 'wb') as f:
                shutil.copyfileobj(r, f)
            with gzip.open(gz) as src, open(nc, 'wb') as dst:
                shutil.copyfileobj(src, dst)
        with netcdf_file(nc, mmap=False) as ds:
            v = ds.variables['tempanomaly']
            raw = v.data.astype(np.float32)
            fill = getattr(v, '_FillValue', 32767)
            scale = float(getattr(v, 'scale_factor', 1.0))
            offs = float(getattr(v, 'add_offset', 0.0))
            ta = np.where(v.data == fill, np.nan, raw * scale + offs)
            lat = ds.variables['lat'].data.astype(float).copy()
            lon = ds.variables['lon'].data.astype(float).copy()
            days = ds.variables['time'].data.astype(float).copy()
    # Ay sırası: 1800-01-01'den gün → yıl, ay
    t0 = np.datetime64('1800-01-01')
    dates = (t0 + days.astype('timedelta64[D]')).astype('datetime64[M]')
    years = dates.astype(int) // 12 + 1970
    months = dates.astype(int) % 12
    # 1991–2020 normali: her hücre ve takvim ayı için ortalama
    norm = np.full((12,) + ta.shape[1:], np.nan, np.float32)
    for m in range(12):
        sel = (months == m) & (years >= BASE[0]) & (years <= BASE[1])
        with np.errstate(all='ignore'):
            norm[m] = np.nanmean(ta[sel], axis=0)
    anom = ta - norm[months]
    w = np.cos(np.radians(lat))[:, None] * np.ones((1, len(lon)))
    tr_mask = (lat[:, None] >= TR['lat'][0]) & (lat[:, None] <= TR['lat'][1]) & (lon[None, :] >= TR['lon'][0]) & (lon[None, :] <= TR['lon'][1])
    # Türkiye aylık sıcaklık farkı serisi (benzer kışlar için kış ortalamaları buradan)
    trm = []
    for k in range(len(years)):
        g = anom[k][tr_mask]
        g = g[np.isfinite(g)]
        if g.size:
            trm.append([f'{int(years[k])}-{int(months[k]) + 1:02d}', round(float(g.mean()), 2)])
    with open(os.path.join(OUT, 'tr-aylik.json'), 'w', encoding='utf-8') as f:
        json.dump(trm, f, separators=(',', ':'))
    summary = []
    for y in range(int(years.min()), int(years.max()) + 1):
        sel = years == y
        n = int(sel.sum())
        if n == 0:
            continue
        with np.errstate(all='ignore'):
            grid = np.nanmean(anom[sel], axis=0)
        ok = np.isfinite(grid)
        glob = float(np.nansum(grid * w) / np.sum(w[ok])) if ok.any() else None
        trv = grid[tr_mask & ok]
        tr = float(trv.mean()) if trv.size else None
        q = np.where(ok, np.clip(np.round(grid * 10), -127, 127), -128).astype(np.int8)
        with open(os.path.join(OUT, 'yil', f'{y}.json'), 'w', encoding='utf-8') as f:
            json.dump({'y': y, 'months': n, 'glob': round(glob, 2) if glob is not None else None, 'tr': round(tr, 2) if tr is not None else None,
                       'lat0': float(lat[0]), 'lon0': float(lon[0]), 'step': float(lat[1] - lat[0]), 'ny': len(lat), 'nx': len(lon),
                       'grid': base64.b64encode(q.tobytes()).decode()}, f, separators=(',', ':'))
        summary.append({'y': y, 'months': n, 'glob': round(glob, 2) if glob is not None else None, 'tr': round(tr, 2) if tr is not None else None})
    with open(os.path.join(OUT, 'ozet.json'), 'w', encoding='utf-8') as f:
        json.dump({'source': 'NASA GISTEMP v4 (GHCN v4 + ERSST v5, 2°)', 'base': f'{BASE[0]}–{BASE[1]}', 'modified': lm,
                   'generated': time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime()), 'years': summary}, f, ensure_ascii=False, separators=(',', ':'))
    with open(stamp, 'w', encoding='utf-8') as f:
        f.write(lm)
    print(f'İklim: {len(summary)} yıl yazıldı')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # iklim işi aksarsa model işi sürsün
        print('İklim alınamadı:', e)
