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
