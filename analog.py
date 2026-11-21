"""Benzer kışlar: bu sonbaharın (ekim) ENSO, QBO, AO ve NAO durumuna en çok benzeyen geçmiş sonbaharlar ve o kışların
(aralık–şubat) sonuçları: Türkiye sıcaklık farkı (NASA GISTEMP, 1991–2020'ye göre), AO, NAO ve ENSO.

Kaynak: NOAA CPC (ONI, QBO 30 hPa, AO, NAO aylık). Günde bir kez hesaplanır. Çıktı: out/analiz/benzer.json
"""
import json
import os
import re
import time
import urllib.request

OUT = os.path.join('out', 'analiz', 'benzer.json')
UA = {'User-Agent': 'havaxpro-veri/1.0'}
CPC = 'https://www.cpc.ncep.noaa.gov'
URLS = {
    'oni': f'{CPC}/data/indices/oni.ascii.txt',
    'qbo': f'{CPC}/data/indices/qbo.u30.index',
    'ao': f'{CPC}/products/precip/CWlink/daily_ao_index/monthly.ao.index.b50.current.ascii',
    'nao': f'{CPC}/products/precip/CWlink/pna/norm.nao.monthly.b5001.current.ascii',
}
SEAS = ['DJF', 'JFM', 'FMA', 'MAM', 'AMJ', 'MJJ', 'JJA', 'JAS', 'ASO', 'SON', 'OND', 'NDJ']
# Benzerlikte ağırlık ve ölçek (bir "birim" fark)
FEAT = {'oni': (1.5, 0.7), 'qbo': (1.0, 12.0), 'ao': (0.6, 1.0), 'nao': (0.6, 1.0)}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return r.read().decode('utf-8', 'replace')


def monthly(text):
    """'YYYY  M  değer' satırları → {(yıl, ay): değer}"""
    out = {}
    for line in text.splitlines():
        p = line.split()
        if len(p) == 3:
            try:
                out[(int(p[0]), int(p[1]))] = float(p[2])
            except ValueError:
                pass
    return out


def oni(text):
    """ONI üç aylık: ortadaki aya yazılır (ASO → eylül)"""
    out = {}
    for line in text.splitlines()[1:]:
        p = line.split()
        if len(p) == 4 and p[0] in SEAS:
            m = SEAS.index(p[0]) + 1     # DJF'nin ortası ocak
            out[(int(p[1]), m)] = float(p[3])
    return out


def qbo(text):
    """İlk bölüm (ORIGINAL DATA): yıl ve 12 ay; -999.9 eksik"""
    out, started = {}, False
    for line in text.splitlines():
        if line.startswith('YEAR'):
            if started:
                break
            started = True
            continue
        if started and line[:4].isdigit():
            y = int(line[:4])
            vals = re.findall(r'-?\d+\.\d+', line[4:])   # bitişik değerler de ayrılır ("15.91-999.90")
            for m, v in enumerate(vals, 1):
                try:
                    x = float(v)
                except ValueError:
                    continue
                if x > -999:
                    out[(y, m)] = x
    return out


def mean(d, keys):
    v = [d[k] for k in keys if k in d]
    return round(sum(v) / len(v), 2) if v else None


def main():
    if os.path.exists(OUT) and time.time() - os.path.getmtime(OUT) < 20 * 3600:
        print('Benzer kışlar: güncel')
        return
    s = {'oni': oni(get(URLS['oni'])), 'qbo': qbo(get(URLS['qbo'])), 'ao': monthly(get(URLS['ao'])), 'nao': monthly(get(URLS['nao']))}
    try:
        with open(os.path.join('out', 'iklim', 'tr-aylik.json'), encoding='utf-8') as f:
            trm = {(int(k[:4]), int(k[5:])): v for k, v in json.load(f)}
    except (OSError, ValueError):
        trm = {}
    # Bu sonbaharın son ayı: dört seride de değer olan en yeni ay (eylül–kasım)
    now = time.gmtime()
    y = now.tm_year if now.tm_mon >= 9 else now.tm_year - 1
    month = next((m for m in (11, 10, 9) if all((y, m) in s[k] or (k == 'oni' and (y, m - 1) in s[k]) for k in s)), 10)
    def feat(yy):
        return {'oni': s['oni'].get((yy, month), s['oni'].get((yy, month - 1))), 'qbo': s['qbo'].get((yy, month)),
                'ao': s['ao'].get((yy, month)), 'nao': s['nao'].get((yy, month))}
    cur = feat(y)
    rows = []
    for yy in range(1979, y):
        f = feat(yy)
        if any(f[k] is None for k in FEAT) or any(cur[k] is None for k in FEAT):
            continue
        dist = sum(w * ((f[k] - cur[k]) / sc) ** 2 for k, (w, sc) in FEAT.items()) ** 0.5
        win = [(yy, 12), (yy + 1, 1), (yy + 1, 2)]
        rows.append({'winter': f'{yy}/{str(yy + 1)[2:]}', 'year': yy, 'dist': round(dist, 3), 'feat': f,
                     'djf': {'tr': mean(trm, win), 'ao': mean(s['ao'], win), 'nao': mean(s['nao'], win), 'oni': s['oni'].get((yy + 1, 1))}})
    rows.sort(key=lambda r: r['dist'])
    best = rows[:6]
    for r in best:
        r['sim'] = round(100 / (1 + r['dist']), 0)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as fh:
        json.dump({'generated': time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime()), 'season': f'{y}/{str(y + 1)[2:]}',
                   'month': month, 'current': cur, 'analogs': best, 'all': rows,
                   'source': 'NOAA CPC (ONI, QBO 30 hPa, AO, NAO) · Türkiye: NASA GISTEMP v4, 1991–2020'},
                  fh, ensure_ascii=False, separators=(',', ':'))
    print(f'Benzer kışlar: {len(rows)} kış karşılaştırıldı')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # analiz aksarsa model işi sürsün
        print('Benzer kışlar alınamadı:', e)
