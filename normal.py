"""Topluluk diyagramlarındaki "normal" çizgisi için 850 hPa sıcaklık ve 500 hPa yükseklik normalleri.

Kaynak: klima/ klasöründeki ERA5 1990–2019 günlük normalleri (1,5°, yılın 5 günde bir günü). Türkiye ve çevresi
(33–45°K, 24–48°D) kesilip tek dosya yazılır; değişmediği için yalnız dosya yoksa üretilir.
Çıktı: out/iklim/normal-850-500.json → { lat, lon, doy, t850: [doy][lat][lon] (°C, 0,1), z500: [doy][lat][lon] (m) }
"""
import json
import os

import numpy as np

KLIMA = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'klima')
OUT = os.path.join('out', 'iklim', 'normal-850-500.json')
LAT, LON = (33, 45), (24, 48)


def main():
    if os.path.exists(OUT):
        print('Normaller: var')
        return
    out = {'source': 'ERA5 1990–2019 günlük normalleri', 'unit': {'t850': '°C', 'z500': 'm'}}
    for var in ('t850', 'z500'):
        d = np.load(os.path.join(KLIMA, f'{var}_era5_1990_2019.npz'))
        lat, lon = d['lat'], d['lon']
        yi = np.where((lat >= LAT[0]) & (lat <= LAT[1]))[0]
        xi = np.where((lon >= LON[0]) & (lon <= LON[1]))[0]
        a = d[var][:, yi][:, :, xi].astype(float) * (0.1 if var == 't850' else 1)
        out[var] = np.round(a, 1 if var == 't850' else 0).tolist()
        out['lat'], out['lon'], out['doy'] = lat[yi].tolist(), lon[xi].tolist(), d['doy'].tolist()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    print('Normaller yazıldı', os.path.getsize(OUT) // 1024, 'KB')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print('Normaller yazılamadı:', e)
