"""İstasyon rekorları: NOAA NCEI GSOD (Global Summary of the Day) arşivinden Türkiye istasyonları.

Her yıl için istasyon başına aylık uç değerler out/rekor/yil/<yıl>.json olarak saklanır. Arşiv kademeli dolar:
her çalışmada içinde bulunulan yıl yenilenir (günde en çok bir kez) ve geçmişe doğru en çok YIL_TUR yıl eklenir.
Sonunda out/rekor/ozet.json: Bu ay, Bu yıl, Son 5 yıl ve Tüm kayıt için her büyüklükte ilk 10.
Birimler: °C, km/sa, hPa, mm. Resmi MGM rekoru değildir.
"""
import csv
import io
import json
import os
import re
import tarfile
import tempfile
import time
import urllib.request

HIST = 'https://www.ncei.noaa.gov/pub/data/noaa/isd-history.csv'
# Yılın bütün istasyonları tek arşivde: tek indirme, içinden yalnız Türkiye istasyonları okunur
ARCHIVE = 'https://www.ncei.noaa.gov/data/global-summary-of-the-day/archive/{y}.tar.gz'
OUT = os.path.join('out', 'rekor')
ILK_YIL = 1973          # GSOD'da Türkiye verisinin düzenli başladığı yıl
YIL_TUR = 3             # her çalışmada geçmişe doğru en çok bu kadar yıl
SURE_SN = 300           # bir çalışma en çok bu kadar sürer
UA = {'User-Agent': 'havaxpro-veri/1.0'}

# Büyüklükler: (anahtar, GSOD sütunu, yön, dönüştürme, eksik değer)
F2C = lambda f: round((f - 32) * 5 / 9, 1)
KT2KMH = lambda k: round(k * 1.852)
VARS = [
    ('tmax', 'MAX', 'max', F2C, 9999.9),
    ('tmin', 'MIN', 'min', F2C, 9999.9),
    ('gust', 'GUST', 'max', KT2KMH, 999.9),
    ('wind', 'MXSPD', 'max', KT2KMH, 999.9),
    ('pmin', 'SLP', 'min', lambda p: round(p, 1), 9999.9),
    ('pmax', 'SLP', 'max', lambda p: round(p, 1), 9999.9),
    ('prcp', 'PRCP', 'max', lambda i: round(i * 25.4, 1), 99.99),
]


def get(url, timeout=60):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode('utf-8', 'replace')


def stations():
    """Türkiye istasyonları (isd-history): kimlik, ad, enlem, boylam, ilk ve son yıl"""
    path = os.path.join(OUT, 'istasyonlar.json')
    if os.path.exists(path) and time.time() - os.path.getmtime(path) < 30 * 86400:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    out = []
    for r in csv.DictReader(io.StringIO(get(HIST, 120))):
        if r['CTRY'] != 'TU' or not r['LAT'] or not r['BEGIN']:
            continue
        name = r['STATION NAME'].title().replace(' Arpt', '').replace(' Intl', '').strip()
        out.append({'id': r['USAF'] + r['WBAN'], 'name': name, 'lat': float(r['LAT']), 'lon': float(r['LON']),
                    'y0': int(r['BEGIN'][:4]), 'y1': int(r['END'][:4])})
    os.makedirs(OUT, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False)
    return out


def parse(text):
    """Bir istasyonun bir yılı (CSV metni): { ay: { büyüklük: [değer, gün] } }"""
    months = {}
    for r in csv.DictReader(io.StringIO(text)):
        day = r['DATE']
        m = months.setdefault(day[5:7], {})
        for key, col, way, conv, miss in VARS:
            try:
                raw = float(r[col])
            except (ValueError, KeyError):
                continue
            if raw >= miss - 0.01:
                continue
            v = conv(raw)
            cur = m.get(key)
            if cur is None or (way == 'max' and v > cur[0]) or (way == 'min' and v < cur[0]):
                m[key] = [v, day]
    return months or None


def build_year(sts, year):
    ids = {s['id'] for s in sts}
    data = {}
    with tempfile.NamedTemporaryFile(suffix='.tar.gz') as tmp:
        req = urllib.request.Request(ARCHIVE.format(y=year), headers=UA)
        try:
            r = urllib.request.urlopen(req, timeout=300)
        except urllib.error.HTTPError as e:
            if e.code == 404:   # NOAA bu yılın arşivini henüz yayınlamadı: canlı gözlemler kullanılır
                print(f'Rekor: {year} arşivi yok')
                return False
            raise
        with r:
            while chunk := r.read(1 << 20):
                tmp.write(chunk)
        tmp.flush()
        with tarfile.open(tmp.name, 'r:gz') as tar:
            for mem in tar:
                sid = os.path.basename(mem.name)[:-4]
                if not mem.isfile() or sid not in ids:
                    continue
                months = parse(tar.extractfile(mem).read().decode('utf-8', 'replace'))
                if months:
                    data[sid] = months
    os.makedirs(os.path.join(OUT, 'yil'), exist_ok=True)
    with open(os.path.join(OUT, 'yil', f'{year}.json'), 'w', encoding='utf-8') as f:
        json.dump({'year': year, 'built': time.strftime('%Y-%m-%d'), 'stations': data}, f, separators=(',', ':'))
    print(f'Rekor: {year} yılı, {len(data)} istasyon')
    return True


def summarize(sts, years):
    names = {s['id']: s for s in sts}
    now = time.gmtime()
    cy, cm = now.tm_year, f'{now.tm_mon:02d}'
    periods = {'ay': lambda y, m: y == cy and m == cm, 'yil': lambda y, m: y == cy,
               'bes': lambda y, m: y >= cy - 4, 'tum': lambda y, m: True}
    best = {p: {k: {} for k, *_ in VARS} for p in periods}
    for y in years:
        try:
            with open(os.path.join(OUT, 'yil', f'{y}.json'), encoding='utf-8') as f:
                data = json.load(f)['stations']
        except (OSError, ValueError):
            continue
        merge(best, periods, VARS, y, data)
    # Canlı gözlemlerden günlük uç değerler (havalimanı ölçümleri; NOAA arşivinin bittiği yerden sonrası)
    for y in range(cy - 4, cy + 1):
        try:
            with open(os.path.join('out', 'gozlem', f'gunluk-{y}.json'), encoding='utf-8') as f:
                live = json.load(f)
        except (OSError, ValueError):
            continue
        months = {}
        for sid, days in live['days'].items():
            if not sid.startswith('LT'):   # yalnız Türkiye havalimanları
                continue
            raw = live['names'].get(sid, sid).split(',')[0]
            clean = re.sub(r'\b(Arpt|Airport|Intl|International|Ab|Mil)\b', '', raw.title()).replace('/', ' / ').split()
            names.setdefault(sid, {'name': f"{' '.join(clean)} Havalimanı"})
            m = months.setdefault(sid, {})
            for day, vals in days.items():
                mm = m.setdefault(day[5:7], {})
                for key, _, way, *_ in VARS:
                    v = vals.get(key)
                    if v is None:
                        continue
                    cur = mm.get(key)
                    if cur is None or (way == 'max' and v > cur[0]) or (way == 'min' and v < cur[0]):
                        mm[key] = [v, day]
        merge(best, periods, VARS, y, months)
    out = {'generated': time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime()), 'years': [min(years) if years else cy, cy], 'periods': {}}
    out['archive_end'] = max((y for y in years), default=None)
    _write(out, best, periods, names)


def merge(best, periods, VARS_, y, data):
    for sid, months in data.items():
        if True:
            for m, vals in months.items():
                for key, _, way, *_ in VARS:
                    if key not in vals:
                        continue
                    v, day = vals[key]
                    for p, ok in periods.items():
                        if not ok(y, m):
                            continue
                        cur = best[p][key].get(sid)
                        if cur is None or (way == 'max' and v > cur[0]) or (way == 'min' and v < cur[0]):
                            best[p][key][sid] = [v, day]


def _write(out, best, periods, names):
    for p in periods:
        out['periods'][p] = {}
        for key, _, way, *_ in VARS:
            rows = sorted(best[p][key].items(), key=lambda kv: kv[1][0], reverse=(way == 'max'))[:10]
            out['periods'][p][key] = [{'id': sid, 'name': names.get(sid, {}).get('name', sid), 'v': v, 'd': d} for sid, (v, d) in rows]
    with open(os.path.join(OUT, 'ozet.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    print('Rekor: özet yazıldı')


def main():
    t0 = time.time()
    sts = stations()
    cy = time.gmtime().tm_year
    ydir = os.path.join(OUT, 'yil')
    have = {int(n[:4]) for n in os.listdir(ydir)} if os.path.isdir(ydir) else set()
    changed = False
    cur = os.path.join(ydir, f'{cy}.json')
    # Bu yıl ve geçen yıl (NOAA yılı gecikmeli yayınlar): günde bir yenile
    for y in (cy, cy - 1):
        path = os.path.join(ydir, f'{y}.json')
        if not os.path.exists(path) or time.time() - os.path.getmtime(path) > 20 * 3600:
            if build_year(sts, y):
                have.add(y)
                changed = True
    n = 0
    for y in range(cy - 1, ILK_YIL - 1, -1):
        if y in have or y == cy:
            continue
        if n >= YIL_TUR or time.time() - t0 > SURE_SN:
            break
        build_year(sts, y)
        have.add(y)
        n += 1
        changed = True
    oz = os.path.join(OUT, 'ozet.json')
    # Özet: arşiv değiştiyse ya da saatte bir (canlı gözlemler eklenir)
    if changed or not os.path.exists(oz) or time.time() - os.path.getmtime(oz) > 3600:
        summarize(sts, sorted(have))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # rekor işi aksarsa model işi sürsün
        print('Rekor alınamadı:', e)
