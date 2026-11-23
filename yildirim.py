"""Gerçek yıldırım: EUMETSAT MTG Yıldırım Görüntüleyici (LI-2-LFL, "Lightning Flashes"), her çakmanın konumu, saati ve parlaklığı.

Kaynak: EUMETSAT Data Store (koleksiyon EO:EUM:DAT:0691), 10 dakikalık paketler, paket bitiminden ~1–2 dakika sonra.
Erişim: EUMETSAT API anahtarı (GitHub Secrets: EUMETSAT_KEY, EUMETSAT_SECRET); anahtar hiçbir yere yazılmaz.
Çıktı: out/yildirim/son.json  { generated, kaynak, son_paket, flashes: [[boylam, enlem, unix sn, parlaklık], …] }  son 2 saat, Türkiye ve çevresi.
`python yildirim.py --dene`: son paketin dosya yapısını yazar (geliştirme için).
"""
import base64
import datetime as dt
import json
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import numpy as np

OUT = os.path.join('out', 'yildirim', 'son.json')
COLL = 'EO:EUM:DAT:0691'
API = 'https://api.eumetsat.int'
BOX = (15.0, 52.0, 28.0, 50.0)          # batı, doğu, güney, kuzey
KEEP = 2 * 3600                          # son 2 saat
UA = {'User-Agent': 'havaxpro-veri/1.0'}


def log(*a):
    print(dt.datetime.now(dt.timezone.utc).strftime('%H:%M:%S'), 'yildirim', *a, flush=True)


def http(url, data=None, headers=None, timeout=120):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def token():
    key, sec = os.environ.get('EUMETSAT_KEY', ''), os.environ.get('EUMETSAT_SECRET', '')
    if not key or not sec:
        return None
    auth = base64.b64encode(f'{key}:{sec}'.encode()).decode()
    j = json.loads(http(f'{API}/token', data=b'grant_type=client_credentials', headers={'Authorization': f'Basic {auth}', 'Content-Type': 'application/x-www-form-urlencoded'}))
    return j['access_token']


def products(n=14):
    """Son paketler (en yenisi önce): [(kimlik, gövde .nc adı, bitiş zamanı)]"""
    q = urllib.parse.urlencode({'format': 'json', 'pi': COLL, 'si': 0, 'c': n, 'sort': 'start,time,0'})
    j = json.loads(http(f'{API}/data/search-products/1.0.0/os?{q}'))
    out = []
    for f in j.get('features', []):
        p = f['properties']
        body = [x['title'] for x in p.get('links', {}).get('sip-entries', []) if 'CHK-BODY' in x.get('title', '') and x['title'].endswith('.nc')]
        if body:
            out.append((f['id'], body[0], p['date'].split('/')[-1]))
    return out


def fetch(tok, pid, name):
    u = f"{API}/data/download/1.0.0/collections/{urllib.parse.quote(COLL, safe='')}/products/{urllib.parse.quote(pid, safe='')}/entry?{urllib.parse.urlencode({'name': name})}"
    for k in range(3):
        try:
            return http(u, headers={'Authorization': f'Bearer {tok}'}, timeout=300)
        except Exception:  # noqa: BLE001
            if k == 2:
                raise
            time.sleep(5 * (k + 1))


def read_var(ds):
    """netCDF değişkeni (HDF5): ölçek, kaydırma ve boş değer uygulanmış dizi"""
    a = ds[()].astype('float64')
    fill = ds.attrs.get('_FillValue')
    if fill is not None:
        a[ds[()] == np.asarray(fill).ravel()[0]] = np.nan
    sf, ao = ds.attrs.get('scale_factor'), ds.attrs.get('add_offset')
    if sf is not None:
        a = a * float(np.asarray(sf).ravel()[0])
    if ao is not None:
        a = a + float(np.asarray(ao).ravel()[0])
    return a


def parse(raw):
    import h5py
    with tempfile.NamedTemporaryFile(suffix='.nc') as fh:
        fh.write(raw); fh.flush()
        with h5py.File(fh.name, 'r') as h:
            if '--dene' in sys.argv:
                def show(name, obj):
                    if isinstance(obj, h5py.Dataset):
                        log('  değişken', name, obj.shape, obj.dtype, {k: (v.decode() if isinstance(v, bytes) else (v.tolist() if hasattr(v, 'tolist') else v)) for k, v in obj.attrs.items() if k in ('units', 'scale_factor', 'add_offset', '_FillValue', 'long_name')})
                h.visititems(show)
            g = h['data'] if 'data' in h else h        # LFL: değişkenler kökte
            lat, lon = read_var(g['latitude']), read_var(g['longitude'])
            tv = g['flash_time']
            t = read_var(tv)
            units = tv.attrs.get('units', b'')
            units = units.decode() if isinstance(units, bytes) else str(units)
            base = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
            if 'since' in units:
                ref = units.split('since', 1)[1].strip().replace('T', ' ').replace('Z', '')[:19]
                try:
                    base = dt.datetime.strptime(ref, '%Y-%m-%d %H:%M:%S').replace(tzinfo=dt.timezone.utc)
                except ValueError:
                    base = dt.datetime.strptime(ref[:10], '%Y-%m-%d').replace(tzinfo=dt.timezone.utc)
            mult = 86400.0 if units.startswith('day') else 1.0
            ts = base.timestamp() + t * mult
            rad = read_var(g['radiance']) if 'radiance' in g else np.zeros_like(lat)
    return lon, lat, ts, rad


def main():
    tok = token()
    if not tok:
        log('EUMETSAT anahtarı yok (EUMETSAT_KEY / EUMETSAT_SECRET), atlandı')
        return
    old = {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding='utf-8') as fh:
                old = json.load(fh)
        except (OSError, ValueError):
            old = {}
    done = set(old.get('paketler', []))
    flashes = [f for f in old.get('flashes', []) if f[2] > time.time() - KEEP]
    plist = products(3 if '--dene' in sys.argv else 14)
    new = 0
    for pid, name, end in reversed(plist):                      # eskiden yeniye
        if pid in done and '--dene' not in sys.argv:
            continue
        try:
            lon, lat, ts, rad = parse(fetch(tok, pid, name))
        except Exception as e:  # noqa: BLE001
            log('paket okunamadı', end, repr(e)[:200])
            continue
        m = (lon >= BOX[0]) & (lon <= BOX[1]) & (lat >= BOX[2]) & (lat <= BOX[3]) & np.isfinite(ts)
        if '--dene' in sys.argv:
            log('paket', end, 'toplam çakma', len(lon), 'bölgede', int(m.sum()), 'zaman aralığı', dt.datetime.fromtimestamp(np.nanmin(ts), dt.timezone.utc).isoformat() if len(ts) else '-', dt.datetime.fromtimestamp(np.nanmax(ts), dt.timezone.utc).isoformat() if len(ts) else '-')
            continue
        flashes += [[round(float(a), 3), round(float(b), 3), int(c), round(float(d), 1) if np.isfinite(d) else 0] for a, b, c, d in zip(lon[m], lat[m], ts[m], rad[m])]
        done.add(pid); new += 1
        log('paket', end, int(m.sum()), 'çakma (bölgede)')
    if '--dene' in sys.argv:
        return
    flashes = sorted((f for f in flashes if f[2] > time.time() - KEEP), key=lambda f: f[2])
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    out = {'generated': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'), 'kaynak': 'EUMETSAT MTG LI-2-LFL (Lightning Flashes)',
           'son_paket': plist[0][2] if plist else None, 'paketler': [p for p, _, _ in plist if p in done], 'flashes': flashes}
    if new or not os.path.exists(OUT):
        with open(OUT, 'w', encoding='utf-8') as fh:
            json.dump(out, fh, ensure_ascii=False, separators=(',', ':'))
    log(new, 'yeni paket,', len(flashes), 'çakma (son 2 saat)')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log('HATA', repr(e)[:300])
