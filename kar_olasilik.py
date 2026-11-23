"""Kar olasılığı: 81 il merkezinde topluluk (ensemble) tahminlerinin her senaryosu için günlük kar toplamı.

Kaynak: Open-Meteo topluluk API'si (ECMWF ENS 51, ECMWF AIFS ENS, NOAA GEFS 31, Kanada GEPS); saatlik kar (cm).
Çıktı: out/kar/olasilik.json
    { generated, days: [YYYY-MM-DD ×15], models: { key: { name, members, run } },
      il: { key: [ [ [gün1, gün2, …] her senaryo ] her il (plaka sırası) ] } }   değerler: cm × 10 (tamsayı)
Sayfa (kar-olasilik.html) eşik ve süreyi kendisi seçer: olasılık = eşiği aşan senaryoların payı.
Open-Meteo bu istekleri ağır sayar (dakikalık sınır): modeller arasında 70 sn beklenir; 4 saatte bir yenilenir.
"""
import datetime as dt
import json
import os
import time
import urllib.parse
import urllib.request

from kis import PROV

OUT = os.path.join('out', 'kar', 'olasilik.json')
API = 'https://ensemble-api.open-meteo.com/v1/ensemble'
META = 'https://ensemble-api.open-meteo.com/data/{}/static/meta.json'
MODELS = [('ens', 'ecmwf_ifs025', 'ecmwf_ifs025_ensemble', 'ECMWF ENS'), ('aifs', 'ecmwf_aifs025', 'ecmwf_aifs025_ensemble', 'ECMWF AIFS ENS'),
          ('gefs', 'gfs05', 'ncep_gefs05', 'NOAA GEFS'), ('geps', 'gem_global', 'cmc_gem_geps', 'Kanada GEPS')]
DAYS = 15
UA = {'User-Agent': 'havaxpro-veri/1.0'}


def log(*a):
    print(dt.datetime.utcnow().strftime('%H:%M:%S'), 'kar-olasilik', *a, flush=True)


def get(url, timeout=180):
    for k in range(3):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and k < 2:
                time.sleep(75)
                continue
            raise


def main():
    if os.path.exists(OUT) and time.time() - os.path.getmtime(OUT) < 4 * 3600:
        log('güncel')
        return
    now = dt.datetime.utcnow() + dt.timedelta(hours=3)   # TSİ
    days = [(now + dt.timedelta(days=k)).strftime('%Y-%m-%d') for k in range(DAYS)]
    hour0 = now.strftime('%Y-%m-%dT%H:00')
    out = {'generated': dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%MZ'), 'days': days, 'il_adlari': [p[0] for p in PROV], 'models': {}, 'il': {}}
    old = {}
    if os.path.exists(OUT):
        try:
            with open(OUT, encoding='utf-8') as fh:
                old = json.load(fh)
        except (OSError, ValueError):
            old = {}
    for n, (key, mid, meta_id, name) in enumerate(MODELS):
        if n:
            time.sleep(70)
        try:
            q = urllib.parse.urlencode({'latitude': ','.join(str(p[1]) for p in PROV), 'longitude': ','.join(str(p[2]) for p in PROV),
                                        'hourly': 'snowfall', 'models': mid, 'forecast_days': DAYS + 1, 'timezone': 'Europe/Istanbul'})
            d = get(f'{API}?{q}')
            per_il = []
            for loc in d:
                h = loc['hourly']
                keys = [k for k in h if k.startswith('snowfall')]
                mem = []
                for k in keys:
                    s = {}
                    for t, v in zip(h['time'], h[k]):
                        if v is None or t < hour0 or t[:10] not in days:
                            continue
                        s[t[:10]] = s.get(t[:10], 0) + v
                    if s:
                        mem.append([round(s.get(day, 0) * 10) for day in days])
                per_il.append(mem)
            if not any(per_il):
                raise ValueError('boş yanıt')
            run = None
            try:
                run = dt.datetime.utcfromtimestamp(get(META.format(meta_id), 30)['last_run_initialisation_time']).strftime('%Y-%m-%dT%H:%MZ')
            except Exception:  # noqa: BLE001
                pass
            out['models'][key] = {'name': name, 'members': max(len(m) for m in per_il), 'run': run}
            out['il'][key] = per_il
            log(name, len(per_il), 'il', out['models'][key]['members'], 'senaryo')
        except Exception as e:  # noqa: BLE001 — alınamayan modelin eski verisi (aynı günlerse) korunur
            log(name, 'alınamadı', repr(e))
            if old.get('il', {}).get(key) and old.get('days') == days:
                out['models'][key], out['il'][key] = old['models'][key], old['il'][key]
    if not out['il']:
        log('hiç model yok, yazılmadı')
        return
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(',', ':'))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log('HATA', repr(e))
