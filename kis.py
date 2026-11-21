"""Kış arşivi: il merkezlerinde 1991'den bugüne aylık ortalama sıcaklık, toplam kar yağışı ve karlı gün sayısı.

Kaynak: Open-Meteo geçmiş hava API'si (ERA5 / ERA5-Land yeniden analizi, ücretsiz kullanım, CC BY 4.0).
Ücretsiz sınır (dakikada 600, saatte 5.000, günde 10.000 birim) aşılmasın diye arşiv yavaş dolar: saatte en çok
BATCH il (il başına ~190 birim). Arşiv tamamlanınca günde bir son 40 gün bütün iller için tek istekte yenilenir.
Çıktı: out/iklim/kis-il.json → { months: [...], prov: { il: { t: [...], sn: [...], sd: [...] } } }
  t: aylık ortalama sıcaklık (°C), sn: aylık toplam kar (cm), sd: karlı gün (≥1 cm kar yağan gün)
"""
import datetime as dt
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

OUT = os.path.join('out', 'iklim', 'kis-il.json')
API = 'https://archive-api.open-meteo.com/v1/archive'
UA = {'User-Agent': 'havaxpro-veri/1.0'}
START = '1991-01-01'
BATCH = int(os.environ.get('KIS_BATCH', 12))
# 81 il merkezi (plaka sırası; adlar il sınırı dosyasındakiyle aynı)
PROV = [('Adana', 36.99, 35.33), ('Adıyaman', 37.76, 38.28), ('Afyon', 38.76, 30.54), ('Ağrı', 39.72, 43.05), ('Amasya', 40.65, 35.83),
        ('Ankara', 39.93, 32.86), ('Antalya', 36.89, 30.71), ('Artvin', 41.18, 41.82), ('Aydın', 37.85, 27.85), ('Balıkesir', 39.65, 27.88),
        ('Bilecik', 40.14, 29.98), ('Bingöl', 38.88, 40.5), ('Bitlis', 38.4, 42.11), ('Bolu', 40.74, 31.61), ('Burdur', 37.72, 30.29),
        ('Bursa', 40.19, 29.06), ('Çanakkale', 40.15, 26.41), ('Çankırı', 40.6, 33.62), ('Çorum', 40.55, 34.95), ('Denizli', 37.78, 29.09),
        ('Diyarbakır', 37.91, 40.24), ('Edirne', 41.68, 26.56), ('Elazığ', 38.68, 39.22), ('Erzincan', 39.75, 39.49), ('Erzurum', 39.9, 41.27),
        ('Eskişehir', 39.78, 30.52), ('Gaziantep', 37.07, 37.38), ('Giresun', 40.91, 38.39), ('Gümüşhane', 40.46, 39.48), ('Hakkari', 37.58, 43.74),
        ('Hatay', 36.2, 36.16), ('Isparta', 37.76, 30.55), ('Mersin', 36.81, 34.64), ('İstanbul', 41.01, 28.98), ('İzmir', 38.42, 27.14),
        ('Kars', 40.6, 43.1), ('Kastamonu', 41.39, 33.78), ('Kayseri', 38.72, 35.49), ('Kırklareli', 41.74, 27.22), ('Kırşehir', 39.15, 34.16),
        ('Kocaeli', 40.77, 29.94), ('Konya', 37.87, 32.48), ('Kütahya', 39.42, 29.98), ('Malatya', 38.35, 38.31), ('Manisa', 38.61, 27.43),
        ('Kahramanmaraş', 37.58, 36.94), ('Mardin', 37.31, 40.74), ('Muğla', 37.22, 28.36), ('Muş', 38.74, 41.49), ('Nevşehir', 38.62, 34.71),
        ('Niğde', 37.97, 34.68), ('Ordu', 40.98, 37.88), ('Rize', 41.03, 40.52), ('Sakarya', 40.69, 30.44), ('Samsun', 41.29, 36.33),
        ('Siirt', 37.93, 41.94), ('Sinop', 42.03, 35.15), ('Sivas', 39.75, 37.02), ('Tekirdağ', 40.98, 27.51), ('Tokat', 40.31, 36.55),
        ('Trabzon', 41.0, 39.72), ('Tunceli', 39.11, 39.55), ('Şanlıurfa', 37.17, 38.79), ('Uşak', 38.68, 29.41), ('Van', 38.5, 43.38),
        ('Yozgat', 39.82, 34.81), ('Zonguldak', 41.46, 31.79), ('Aksaray', 38.37, 34.03), ('Bayburt', 40.26, 40.23), ('Karaman', 37.18, 33.22),
        ('Kırıkkale', 39.85, 33.51), ('Batman', 37.89, 41.13), ('Şırnak', 37.52, 42.46), ('Bartın', 41.64, 32.34), ('Ardahan', 41.11, 42.7),
        ('Iğdır', 39.92, 44.04), ('Yalova', 40.65, 29.27), ('Karabük', 41.2, 32.62), ('Kilis', 36.72, 37.12), ('Osmaniye', 37.07, 36.25),
        ('Düzce', 40.84, 31.16)]


def get(params):
    url = f'{API}?{urllib.parse.urlencode(params)}'
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=180) as r:
        return json.load(r)


def monthly(daily):
    """Günlük → {ay: [ortalama sıcaklık, toplam kar, karlı gün]}"""
    acc = {}
    for d, t, s in zip(daily['time'], daily['temperature_2m_mean'], daily['snowfall_sum']):
        a = acc.setdefault(d[:7], [0.0, 0, 0.0, 0])
        if t is not None:
            a[0] += t
            a[1] += 1
        if s is not None:
            a[2] += s
            a[3] += s >= 1
    return {m: [round(a[0] / a[1], 1) if a[1] else None, round(a[2], 1), a[3]] for m, a in acc.items()}


def merge(store, name, mon):
    p = store['prov'].setdefault(name, {})
    p.update(mon)


def main():
    try:
        with open(OUT, encoding='utf-8') as f:
            raw = json.load(f)
        store = {'prov': {k: dict(zip(raw['months'], zip(v['t'], v['sn'], v['sd']))) for k, v in raw['prov'].items()},
                 'stamp': raw.get('stamp', {})}
        store['prov'] = {k: {m: list(x) for m, x in v.items()} for k, v in store['prov'].items()}
    except (OSError, ValueError, KeyError):
        store = {'prov': {}, 'stamp': {}}
    end = (dt.datetime.utcnow().date() - dt.timedelta(days=6)).isoformat()   # ERA5 ~5 gün geriden
    todo = [p for p in PROV if p[0] not in store['prov']]
    now = time.time()
    if todo:
        if now - store['stamp'].get('batch', 0) < 55 * 60:
            print(f'Kış arşivi: {len(todo)} il bekliyor (saatte {BATCH})')
            return
        for i, (name, la, lo) in enumerate(todo[:BATCH]):
            if i:
                time.sleep(21)   # dakikalık sınır
            try:
                d = get({'latitude': la, 'longitude': lo, 'start_date': START, 'end_date': end, 'daily': 'snowfall_sum,temperature_2m_mean', 'timezone': 'Europe/Istanbul'})
            except urllib.error.HTTPError as e:
                print('Kış arşivi: Open-Meteo sınırı', e.code)
                break
            merge(store, name, monthly(d['daily']))
        store['stamp']['batch'] = now
    elif now - store['stamp'].get('daily', 0) > 20 * 3600:
        # Son 40 gün, bütün iller tek istekte
        start = (dt.datetime.utcnow().date() - dt.timedelta(days=46)).replace(day=1).isoformat()
        d = get({'latitude': ','.join(str(p[1]) for p in PROV), 'longitude': ','.join(str(p[2]) for p in PROV), 'start_date': start,
                 'end_date': end, 'daily': 'snowfall_sum,temperature_2m_mean', 'timezone': 'Europe/Istanbul'})
        for (name, _, _), loc in zip(PROV, d if isinstance(d, list) else [d]):
            merge(store, name, monthly(loc['daily']))
        store['stamp']['daily'] = now
    else:
        print('Kış arşivi: güncel')
        return
    months = sorted({m for v in store['prov'].values() for m in v})
    out = {'generated': time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime()), 'months': months, 'last': end, 'stamp': store['stamp'],
           'complete': len(store['prov']) == len(PROV),
           'source': 'Open-Meteo geçmiş hava (ERA5 / ERA5-Land), il merkezleri',
           'prov': {k: {'t': [v.get(m, [None, None, None])[0] for m in months], 'sn': [v.get(m, [None, None, None])[1] for m in months],
                        'sd': [v.get(m, [None, None, None])[2] for m in months]} for k, v in store['prov'].items()}}
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    print(f'Kış arşivi: {len(store["prov"])}/{len(PROV)} il, {len(months)} ay')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # noqa: BLE001  (bu iş aksarsa model işi sürsün)
        print('Kış arşivi alınamadı:', e)
