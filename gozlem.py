"""İstasyon gözlemleri (METAR): Türkiye ve çevresindeki havalimanlarının son ölçümleri.

Kaynak: NOAA Aviation Weather Center (aviationweather.gov, kamuya açık). Tarayıcıdan doğrudan okunamadığı için
(CORS yok) burada çekilip out/gozlem/metar.json olarak yayınlanır. Her turda (15 dk) yenilenir.
"""
import json
import os
import time
import urllib.request

URL = 'https://aviationweather.gov/api/data/metar?bbox=34,25,43.5,45.5&format=json&hours=2'
OUT = os.path.join('out', 'gozlem', 'metar.json')
KEEP = ('icaoId', 'name', 'lat', 'lon', 'elev', 'temp', 'dewp', 'wdir', 'wspd', 'wgst', 'wxString', 'visib', 'altim', 'cover', 'clouds', 'reportTime', 'rawOb')


def gunluk(data, latest):
    """Günlük uç değerler (rekorlar için): out/gozlem/gunluk-<yıl>.json → { istasyon: { gün: {tmax, tmin, gust, wind, pmin, pmax} } }"""
    year = time.strftime('%Y', time.gmtime())
    path = os.path.join('out', 'gozlem', f'gunluk-{year}.json')
    try:
        with open(path, encoding='utf-8') as f:
            g = json.load(f)
    except (OSError, ValueError):
        g = {'names': {}, 'days': {}}
    for k, ob in latest.items():
        g['names'][k] = ob.get('name', k)
    for ob in data:
        k, t = ob.get('icaoId'), ob.get('reportTime', '')
        if not k or not t.startswith(year):
            continue
        d = g['days'].setdefault(k, {}).setdefault(t[:10], {})
        def upd(key, v, way):
            if v is None:
                return
            if key not in d or (way == 'max' and v > d[key]) or (way == 'min' and v < d[key]):
                d[key] = v
        upd('tmax', ob.get('temp'), 'max')
        upd('tmin', ob.get('temp'), 'min')
        upd('gust', round(ob['wgst'] * 1.852) if ob.get('wgst') else None, 'max')
        upd('wind', round(ob['wspd'] * 1.852) if ob.get('wspd') else None, 'max')
        upd('pmin', ob.get('altim'), 'min')
        upd('pmax', ob.get('altim'), 'max')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(g, f, ensure_ascii=False, separators=(',', ':'))


def main():
    req = urllib.request.Request(URL, headers={'User-Agent': 'havaxpro-veri/1.0'})
    with urllib.request.urlopen(req, timeout=40) as r:
        data = json.load(r)
    # İstasyon başına en yeni rapor
    latest = {}
    for ob in data:
        k = ob.get('icaoId')
        if not k or ob.get('lat') is None:
            continue
        if k not in latest or ob.get('reportTime', '') > latest[k].get('reportTime', ''):
            latest[k] = ob
    stations = [{k: ob.get(k) for k in KEEP if ob.get(k) is not None} for ob in latest.values()]
    gunluk(data, latest)
    stations.sort(key=lambda s: s['icaoId'])
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    new = {'source': 'NOAA Aviation Weather Center (METAR)', 'stations': stations}
    # Değişmediyse dosyaya dokunma (yayınla boşuna tetiklenmesin)
    try:
        with open(OUT, encoding='utf-8') as f:
            if json.load(f).get('stations') == stations:
                print('Gözlem: değişiklik yok')
                return
    except (OSError, ValueError):
        pass
    new['generated'] = time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime())
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(new, f, ensure_ascii=False, separators=(',', ':'))
    print(f'Gözlem: {len(stations)} istasyon yazıldı')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # gözlem alınamazsa model işi sürsün
        print('Gözlem alınamadı:', e)
