"""Türkiye SYNOP gözlemleri (MGM istasyonları, WMO blok 17): son ölçüm, bugünün en yüksek ve en düşük sıcaklığı,
kar kalınlığı, 24 saatlik yağış, hamle.

Kaynak: OGIMET (ogimet.com, WMO GTS üzerinden yayılan SYNOP raporları). Saatte bir son 50 saat alınır (dünün en yüksek/en düşük sıcaklığı için).
İstasyon adı ve konumu: OGIMET istasyon listesi (ayda bir).
Çıktı: out/gozlem/synop.json
"""
import datetime as dt
import json
import math
import os
import re
import time
import urllib.request

OUT = os.path.join('out', 'gozlem', 'synop.json')
META = os.path.join('out', 'gozlem', 'synop-istasyon.json')
UA = {'User-Agent': 'havaxpro-veri/1.0'}
SYN = 'https://www.ogimet.com/cgi-bin/getsynop?block=17&begin={b}&end={e}'
LIST = 'https://www.ogimet.com/display_stations.php?lang=en&tipo=AND&isyn=&oaci=&nombre=&estado=Turkey&Send=Send'
TSI = dt.timedelta(hours=3)
VIS9 = {90: 0.05, 91: 0.05, 92: 0.2, 93: 0.5, 94: 1, 95: 2, 96: 4, 97: 10, 98: 20, 99: 50}   # görüş kodu 90–99 (km)
PERIOD = {'1': 6, '2': 12, '3': 18, '4': 24, '5': 1, '6': 2, '7': 3, '8': 9, '9': 15}   # yağış grubunun süresi (saat)


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120) as r:
        return r.read().decode('utf-8', 'replace')


def dms(s):
    """'41-05-22N' → 41.0894"""
    m = re.match(r'(\d+)-(\d+)(?:-(\d+))?([NSEW])', s)
    if not m:
        return None
    v = int(m.group(1)) + int(m.group(2)) / 60 + int(m.group(3) or 0) / 3600
    return round(-v if m.group(4) in 'SW' else v, 4)


def station_list():
    if os.path.exists(META) and time.time() - os.path.getmtime(META) < 30 * 86400:
        with open(META, encoding='utf-8') as f:
            return json.load(f)
    html = get(LIST)
    out = {}
    for row in html.split('<tr')[1:]:
        cells = [re.sub(r'<[^>]*>', '', c).split('>')[-1].strip() for c in re.findall(r'<td[^>]*>(.*?)</td>', row, re.S)]
        idx = next((i for i, c in enumerate(cells) if re.fullmatch(r'17\d{3}', c)), None)
        if idx is None or len(cells) < idx + 7:
            continue
        name, lat, lon, elev = cells[idx + 2], dms(cells[idx + 4]), dms(cells[idx + 5]), cells[idx + 6]
        if lat is None or lon is None:
            continue
        out[cells[idx]] = {'name': name, 'lat': lat, 'lon': lon, 'elev': int(elev) if elev.lstrip('-').isdigit() else None}
    if out:
        os.makedirs(os.path.dirname(META), exist_ok=True)
        with open(META, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, separators=(',', ':'))
    return out


def temp(g):
    """'1sTTT' → °C (s: 0 artı, 1 eksi)"""
    if '/' in g[2:] or g[1] not in '01':
        return None
    v = int(g[2:]) / 10
    return -v if g[1] == '1' else v


def parse(msg, iw):
    """Bir SYNOP raporu (AAXX başlığından sonraki gruplar) → sözlük"""
    g = msg.replace('==', '').split()
    out = {}
    if len(g) < 3:
        return out
    # g[0] istasyon, g[1] iRiXhVV, g[2] Nddff
    vv = g[1][3:5]
    if vv.isdigit():
        v = int(vv)
        if v <= 50:
            out['vis'] = v / 10
        elif 56 <= v <= 80:
            out['vis'] = v - 50
        elif 81 <= v <= 89:
            out['vis'] = 30 + (v - 80) * 5
        elif v >= 90:
            out['vis'] = VIS9[v]
    n = g[2]
    if n[0].isdigit():
        out['n'] = int(n[0])
    k = 3
    if n[1:3].isdigit():
        out['wdir'] = int(n[1:3]) * 10
    ff = n[3:5]
    if ff.isdigit():
        f = int(ff)
        if f == 99 and k < len(g) and g[k].startswith('00'):
            f = int(g[k][2:]) if g[k][2:].isdigit() else f
            k += 1
        out['wspd'] = round(f * (1.852 if iw in '34' else 3.6))
    sec, rad = 1, False
    for x in g[k:]:
        if x == '333':
            sec = 3
            continue
        if x in ('555', '444', '222'):
            sec = 5
            continue
        if len(x) != 5:
            continue
        if sec == 1:
            if x[0] == '1':
                out['t'] = temp(x)
            elif x[0] == '2' and x[1] in '01':
                out['td'] = temp(x)
            elif x[0] == '4' and x[1:].isdigit():
                p = int(x[1:]) / 10
                out['p'] = round(p + 1000 if p < 100 else p, 1)
            elif x[0] == '6' and x[1:4].isdigit() and x[4].isdigit():
                r = int(x[1:4])
                out['rr'] = [(r - 990) / 10 if r >= 990 else r, PERIOD.get(x[4], 6)]
            elif x[0] == '7' and x[1:3].isdigit():
                out['ww'] = int(x[1:3])
        elif sec == 3:
            # 55SSS (güneşlenme) grubunun ardından gelen j FFFF ışınım grupları (j 0–6) sıcaklık ya da kar sanılmasın
            if x.startswith('55'):
                rad = True
                continue
            if rad and x[0] in '0123456':
                continue
            rad = False
            if x[0] == '1' and x[1] in '01':
                out['tx'] = temp(x)
            elif x[0] == '2' and x[1] in '01':
                out['tn'] = temp(x)
            elif x[0] == '4' and x[2:].isdigit():
                s = int(x[2:])
                if 1 <= s <= 996:
                    out['snow'] = s
                elif s == 997:
                    out['snow'] = 0.5
            elif x[0] == '7' and x[1:].isdigit():
                out['p24'] = int(x[1:]) / 10
            elif x[:3] in ('910', '911') and x[3:].isdigit():
                out['gust'] = max(out.get('gust', 0), round(int(x[3:]) * (1.852 if iw in '34' else 3.6)))
    return out


def main():
    if os.path.exists(OUT) and time.time() - os.path.getmtime(OUT) < 50 * 60:
        print('SYNOP: güncel')
        return
    meta = station_list()
    now = dt.datetime.utcnow()
    # 50 saat: dün (TSİ gece yarısından gece yarısına) günün her saatinde tam kapsansın
    text = get(SYN.format(b=f'{now - dt.timedelta(hours=50):%Y%m%d%H}00', e=f'{now:%Y%m%d%H}00'))
    reports = {}
    for line in text.splitlines():
        p = line.split(',', 6)
        if len(p) < 7 or 'AAXX' not in p[6]:
            continue
        t = dt.datetime(int(p[1]), int(p[2]), int(p[3]), int(p[4]), int(p[5]))
        body = p[6].split('AAXX', 1)[1].split()
        if len(body) < 2:
            continue
        iw = body[0][4] if len(body[0]) == 5 else '1'
        reports.setdefault(p[0], []).append((t, parse(' '.join(body[1:]), iw)))
    today = (now + TSI).date()
    day0 = dt.datetime.combine(today, dt.time()) - TSI   # bugün 00:00 TSİ (UTC)
    stations = []
    for wmo, obs in reports.items():
        m = meta.get(wmo)
        if not m or m['lat'] < 35.8:   # blok 17'deki Kıbrıs istasyonları dışarıda
            continue
        obs.sort(key=lambda x: x[0])
        t_last, last = obs[-1]
        row = {'id': wmo, **m, 'time': t_last.strftime('%Y-%m-%dT%H:%MZ')}
        for k in ('t', 'td', 'p', 'wdir', 'wspd', 'vis', 'n', 'ww'):
            if last.get(k) is not None:
                row[k] = last[k]
        if row.get('t') is not None and row.get('td') is not None:
            a, b = 17.625, 243.04
            row['rh'] = round(100 * math.exp(a * row['td'] / (b + row['td']) - a * row['t'] / (b + row['t'])))
        # Bugün (TSİ): saatlik sıcaklıklar ve Tx/Tn grupları
        tday = [o['t'] for t, o in obs if t >= day0 and o.get('t') is not None]
        tx = [o['tx'] for t, o in obs if t >= day0 and o.get('tx') is not None]
        tn = [o['tn'] for t, o in obs if t >= day0 and o.get('tn') is not None]
        if tday or tx:
            row['tmax'] = max(tday + tx)
        if tday or tn:
            row['tmin'] = min(tday + tn)
        # Dün (TSİ)
        y0 = day0 - dt.timedelta(days=1)
        yt = [o['t'] for t, o in obs if y0 <= t < day0 and o.get('t') is not None]
        yx = [o['tx'] for t, o in obs if y0 <= t < day0 and o.get('tx') is not None]
        yn = [o['tn'] for t, o in obs if y0 <= t < day0 and o.get('tn') is not None]
        if len(yt) >= 7:   # günün çoğu varsa (3 saatte bir rapor verenler dahil)
            row['ymax'], row['ymin'] = max(yt + yx), min(yt + yn)
        # Son 24 saatte en sert hamle
        g24 = [o['gust'] for t, o in obs if t >= now - dt.timedelta(hours=24) and o.get('gust')]
        if g24:
            row['gust'] = max(g24)
        # Kar kalınlığı: son 24 saatteki en yeni ölçüm
        sn = [(t, o['snow']) for t, o in obs if t >= now - dt.timedelta(hours=24) and o.get('snow') is not None]
        if sn:
            row['snow'] = sn[-1][1]
        # 24 saatlik yağış: 06 UTC raporundaki 24 saat grubu; yoksa 6 saatlik toplamlar
        p24 = [(t, o['p24']) for t, o in obs if t >= now - dt.timedelta(hours=26) and o.get('p24') is not None]
        if p24:
            row['p24'] = p24[-1][1]
            row['p24t'] = p24[-1][0].strftime('%Y-%m-%dT%H:%MZ')
        else:
            six = [o['rr'][0] for t, o in obs if t > now - dt.timedelta(hours=24) and o.get('rr') and o['rr'][1] == 6 and t.hour % 6 == 0]
            if six:
                row['p24'] = round(sum(six), 1)
        stations.append(row)
    stations.sort(key=lambda s: s['id'])
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as f:
        json.dump({'generated': time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime()), 'day': f'{today}', 'stations': stations,
                   'source': 'OGIMET (WMO SYNOP, MGM istasyonları)'}, f, ensure_ascii=False, separators=(',', ':'))
    print(f'SYNOP: {len(stations)} istasyon')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # noqa: BLE001  (bu iş aksarsa model işi sürsün)
        print('SYNOP alınamadı:', e)
