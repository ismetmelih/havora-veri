"""HAVORA model haritaları için veri üretici.

GFS (NOAA), ECMWF (açık veri) ve ICON-EU (DWD) modellerinin ham GRIB2 çıktılarını indirir,
Türkiye ve çevresini (30–46°K, 18–50°D, 0,25°) keser ve sitenin okuyacağı küçük dosyalara çevirir.

Çıktı: out/<model>/meta.json ve out/<model>/<alan>.bin.gz
  .bin.gz: gzip'li, adım adım art arda dizilmiş int16 (little-endian) ızgaralar; değer = ham * scale
  eksik değer: -32768

Kullanım: python uret.py [gfs ecmwf icon] [--steps 0,3,6]  (adım verilmezse tamamı)
Yalnızca herkese açık veriler kullanılır; hiçbir anahtar ya da gizli bilgi gerekmez.
"""
import bz2
import datetime as dt
import gzip
import json
import os
import re
import sys
import tempfile
import time
import urllib.request

import eccodes
import numpy as np

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'out')

# Hedef ızgara: kuzeyden güneye, batıdan doğuya
LAT_N, LAT_S, LON_W, LON_E, STEP = 46.0, 30.0, 18.0, 50.0, 0.25
LATS = np.round(np.arange(LAT_N, LAT_S - 1e-6, -STEP), 2)
LONS = np.round(np.arange(LON_W, LON_E + 1e-6, STEP), 2)
NY, NX = len(LATS), len(LONS)

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
}

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
    """GRIB2 baytlarından (kısa ad, seviye türü, seviye, 2B dizi, enlemler, boylamlar) üret."""
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
                    lats = np.round(eccodes.codes_get_array(h, 'distinctLatitudes'), 4)
                    lons = np.round(eccodes.codes_get_array(h, 'distinctLongitudes'), 4)
                    # dağıtılan sıra ile değer sırası aynı olsun
                    if eccodes.codes_get(h, 'jScansPositively') == 0:
                        lats = lats[::-1] if lats[0] < lats[-1] else lats
                    elif lats[0] > lats[-1]:
                        lats = lats[::-1]
                    missing = eccodes.codes_get(h, 'missingValue')
                    vals = np.where(vals == missing, np.nan, vals)
                    yield (eccodes.codes_get(h, 'shortName'), eccodes.codes_get(h, 'typeOfLevel'),
                           eccodes.codes_get(h, 'level'), vals, lats, lons)
                finally:
                    eccodes.codes_release(h)
    finally:
        os.unlink(path)


def subset(vals, lats, lons):
    """Kaynak ızgaradan hedef ızgarayı (LATS x LONS) seç."""
    lons = np.where(lons > 180, lons - 360, lons)
    yi = np.array([np.argmin(np.abs(lats - la)) for la in LATS])
    xi = np.array([np.argmin(np.abs(lons - lo)) for lo in LONS])
    if np.abs(lats[yi] - LATS).max() > 0.01 or np.abs(lons[xi] - LONS).max() > 0.01:
        raise ValueError('kaynak ızgara hedef ızgarayla hizalı değil')
    return vals[np.ix_(yi, xi)]


def cumulative_to_6h(steps, cum):
    """Başlangıçtan beri birikmiş değerlerden 'son 6 saatte' toplamı."""
    out = {}
    for s in steps:
        if s not in cum:
            continue
        prev = cum.get(s - 6, np.zeros_like(cum[s]) if s < 6 else None)
        out[s] = None if prev is None else np.maximum(cum[s] - prev, 0)
    return out


def write(model, run, steps, fields, extra=None):
    d = os.path.join(OUT, model)
    os.makedirs(d, exist_ok=True)
    meta = {
        'model': model, 'run': run.strftime('%Y-%m-%dT%H:00Z'), 'generated': dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%MZ'),
        'grid': {'latN': LAT_N, 'lonW': LON_W, 'step': STEP, 'ny': NY, 'nx': NX},
        'steps': steps, 'fields': {}, **(extra or {}),
    }
    for name, (unit, scale) in FIELDS.items():
        if name not in fields:
            continue
        buf = np.full((len(steps), NY, NX), -32768, dtype='<i2')
        have = 0
        for i, s in enumerate(steps):
            a = fields[name].get(s)
            if a is None:
                continue
            q = np.round(a / scale)
            q = np.where(np.isfinite(q), np.clip(q, -32767, 32767), -32768)
            buf[i] = q.astype('<i2')
            have += 1
        if not have:
            continue
        with gzip.open(os.path.join(d, f'{name}.bin.gz'), 'wb', compresslevel=9) as f:
            f.write(buf.tobytes())
        meta['fields'][name] = {'unit': unit, 'scale': scale}
    with open(os.path.join(d, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False)
    log(model, run, len(steps), 'adım,', ', '.join(meta['fields']), 'yazıldı')


def published_run(model):
    try:
        with open(os.path.join(OUT, model, 'meta.json'), encoding='utf-8') as f:
            return json.load(f)['run']
    except Exception:  # noqa: BLE001
        return None


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
        if exists(f'{base}/pub/data/nccf/com/gfs/prod/{d}/gfs.t{t:%H}z.pgrb2.0p25.f240'):
            run = t
            break
    if run is None:
        return log('GFS: tamamlanmış koşu bulunamadı')
    if published_run('gfs') == run.strftime('%Y-%m-%dT%H:00Z') and not steps_filter:
        return log('GFS: yeni koşu yok', run)
    steps = steps_filter or standard_steps(240)
    log('GFS', run, len(steps), 'adım indiriliyor')
    f = {k: {} for k in ('t850', 'z850', 'z500', 'mslp', 't2m', 'gust')}
    apcp, csnow = {}, {}
    for s in steps:
        q = (f'dir=%2Fgfs.{run:%Y%m%d}%2F{run:%H}%2Fatmos&file=gfs.t{run:%H}z.pgrb2.0p25.f{s:03d}'
             '&var_TMP=on&var_HGT=on&var_PRMSL=on&var_APCP=on&var_CSNOW=on&var_GUST=on'
             '&lev_850_mb=on&lev_500_mb=on&lev_mean_sea_level=on&lev_surface=on&lev_2_m_above_ground=on'
             f'&subregion=&toplat={LAT_N}&leftlon={LON_W}&rightlon={LON_E}&bottomlat={LAT_S}')
        data = get(f'{base}/cgi-bin/filter_gfs_0p25.pl?{q}')
        for name, ltype, lev, vals, lats, lons in messages(data):
            a = subset(vals, lats, lons)
            if name == 't' and lev == 850 and ltype == 'isobaricInhPa':
                f['t850'][s] = a - 273.15
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
            elif name == 'tp':
                apcp[s] = a
            elif name == 'csnow':
                csnow[s] = a
        time.sleep(0.6)  # NOMADS'a nazik ol (dakikada en fazla ~120 istek)
    # GFS yağışı 6 saatlik kovalarda birikir ve her 6 saatte sıfırlanır:
    #   f%6==3 → kova başından 3 saatlik toplam; f%6==0 → 6 saatlik kovanın tamamı.
    # Adım adım "önceki adımdan bu yana" yağışı bul, başlangıçtan beri birikmişe çevir.
    zero = np.zeros((NY, NX))
    cum, cum_snow, prev = {0: zero}, {0: zero}, 0
    for s in sorted(steps)[1:]:
        if s not in apcp or prev not in cum:
            continue
        if s % 6 == 0 and s - prev == 3:
            part = apcp[s] - apcp[prev]   # 6 saatlik kova eksi ilk 3 saati
        else:
            part = apcp[s]                # kova başından bu yana (3 ya da 6 saat)
        part = np.maximum(part, 0)
        cum[s] = cum[prev] + part
        # Kar: o aralıktaki yağışı, adım anındaki "kar yağıyor" bayrağıyla say (yaklaşık)
        cum_snow[s] = cum_snow[prev] + part * csnow.get(s, 0)
        prev = s
    f['pr6'] = cumulative_to_6h(steps, cum)
    f['sn6'] = cumulative_to_6h(steps, cum_snow)
    write('gfs', run, steps, f, {'name': 'GFS', 'source': 'NOAA NCEP', 'resolution': '0,25°'})


# ---------------------------------------------------------------- ECMWF (açık veri)

def ecmwf(steps_filter=None):
    base = 'https://data.ecmwf.int/forecasts'
    run = None
    for t in recent_runs((0, 12)):
        if exists(f'{base}/{t:%Y%m%d}/{t:%H}z/ifs/0p25/oper/{t:%Y%m%d%H}0000-240h-oper-fc.index'):
            run = t
            break
    if run is None:
        return log('ECMWF: tamamlanmış koşu bulunamadı')
    if published_run('ecmwf') == run.strftime('%Y-%m-%dT%H:00Z') and not steps_filter:
        return log('ECMWF: yeni koşu yok', run)
    steps = steps_filter or standard_steps(240)
    log('ECMWF', run, len(steps), 'adım indiriliyor')
    want = {('t', '850'), ('gh', '850'), ('gh', '500'), ('msl', None), ('2t', None), ('10fg', None), ('tp', None), ('sf', None)}
    f = {k: {} for k in ('t850', 'z850', 'z500', 'mslp', 't2m', 'gust')}
    tp, sf = {}, {}
    for s in steps:
        stem = f'{base}/{run:%Y%m%d}/{run:%H}z/ifs/0p25/oper/{run:%Y%m%d%H}0000-{s}h-oper-fc'
        index = [json.loads(line) for line in get(f'{stem}.index').decode().splitlines() if line.strip()]
        parts = [r for r in index if (r['param'], r.get('levelist')) in want]
        chunks = []
        for r in parts:  # yalnızca gereken mesajları bayt aralığıyla indir
            req = urllib.request.Request(f'{stem}.grib2', headers={**UA, 'Range': f"bytes={r['_offset']}-{r['_offset'] + r['_length'] - 1}"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                chunks.append(resp.read())
        for name, ltype, lev, vals, lats, lons in messages(b''.join(chunks)):
            a = subset(vals, lats, lons)
            if name == 't' and lev == 850:
                f['t850'][s] = a - 273.15
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
    tp.setdefault(0, np.zeros((NY, NX)))
    sf.setdefault(0, np.zeros((NY, NX)))
    f['pr6'] = cumulative_to_6h(steps, tp)
    f['sn6'] = cumulative_to_6h(steps, sf)
    write('ecmwf', run, steps, f, {'name': 'ECMWF', 'source': 'ECMWF açık veri (CC BY 4.0)', 'resolution': '0,25°'})


# ---------------------------------------------------------------- ICON-EU (DWD)

def icon(steps_filter=None):
    base = 'https://opendata.dwd.de/weather/nwp/icon-eu/grib'
    run = None
    for t in recent_runs((0, 12)):
        name = f'icon-eu_europe_regular-lat-lon_single-level_{t:%Y%m%d%H}_120_T_2M.grib2.bz2'
        if exists(f'{base}/{t:%H}/t_2m/{name}'):
            run = t
            break
    if run is None:
        return log('ICON: tamamlanmış koşu bulunamadı')
    if published_run('icon') == run.strftime('%Y-%m-%dT%H:00Z') and not steps_filter:
        return log('ICON: yeni koşu yok', run)
    steps = steps_filter or list(range(0, 121, 3))
    log('ICON', run, len(steps), 'adım indiriliyor')
    r = f'{run:%Y%m%d%H}'

    def grab(var, s, level=None):
        kind = 'pressure-level' if level else 'single-level'
        lv = f'_{level}' if level else ''
        url = f'{base}/{run:%H}/{var.lower()}/icon-eu_europe_regular-lat-lon_{kind}_{r}_{s:03d}{lv}_{var.upper()}.grib2.bz2'
        for _, _, _, vals, lats, lons in messages(bz2.decompress(get(url))):
            return subset(vals, lats, lons)
        return None

    g = 9.80665
    f = {k: {} for k in ('t850', 'z850', 'z500', 'mslp', 't2m', 'gust')}
    prec, snow = {}, {}
    for s in steps:
        f['t850'][s] = grab('t', s, 850) - 273.15
        f['z850'][s] = grab('fi', s, 850) / g
        f['z500'][s] = grab('fi', s, 500) / g
        f['mslp'][s] = grab('pmsl', s) / 100
        f['t2m'][s] = grab('t_2m', s) - 273.15
        if s > 0:
            f['gust'][s] = grab('vmax_10m', s) * 3.6
            prec[s] = grab('tot_prec', s)
            snow[s] = grab('snow_gsp', s) + grab('snow_con', s)
    prec[0] = np.zeros((NY, NX))
    snow[0] = np.zeros((NY, NX))
    f['pr6'] = cumulative_to_6h(steps, prec)
    f['sn6'] = cumulative_to_6h(steps, snow)
    write('icon', run, steps, f, {'name': 'ICON', 'source': 'DWD açık veri', 'resolution': '0,0625° → 0,25°'})


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
    with open(os.path.join(OUT, 'index.json'), 'w', encoding='utf-8') as fh:
        models_meta = {}
        for m in ('gfs', 'ecmwf', 'icon'):
            try:
                with open(os.path.join(OUT, m, 'meta.json'), encoding='utf-8') as mf:
                    mm = json.load(mf)
                models_meta[m] = {'run': mm['run'], 'generated': mm['generated'], 'steps': len(mm['steps'])}
            except Exception:  # noqa: BLE001
                pass
        json.dump({'updated': dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%MZ'), 'models': models_meta}, fh, ensure_ascii=False)
    if failed and len(failed) == len(models):
        sys.exit(1)


if __name__ == '__main__':
    os.makedirs(OUT, exist_ok=True)
    main()
