"""MGM istasyonlarının anlık ölçümleri (canlı gözlem haritası).

Kaynak: MGM'nin web sitesinin kullandığı servis (servis.mgm.gov.tr, "Son Durumlar"); il başına bir istek.
Ölçümler kalite kontrolsüzdür (MGM'nin sitesindeki gibi). 81 il sırayla, istekler arasında kısa beklemeyle çekilir.
- out/gozlem/mgm-istasyon.json  istasyon bilgisi (ad, il, ilçe, konum, yükseklik); günde bir yenilenir
- out/gozlem/mgm.json           son ölçümler: her istasyon için
    [no, sıcaklık, hissedilen, nem, rüzgâr km/sa, yön, deniz seviyesi basıncı, kar cm, yağış 10dk, 1sa, 6sa, 24sa, hadise, dakika önce]
- out/gozlem/mgm-24s.json       son 24 saat, saat başı: sıcaklık, rüzgâr, 1 saatlik yağış (oynatma ve istasyon grafiği)
- out/gozlem/gecmis/<plaka>.json  il başına son 48 saat, her ölçüm turu: sıcaklık, hissedilen, nem, rüzgâr, yön, basınç, kar, yağış
- out/gozlem/gecmis/index.json    istasyon → il plakası
- out/gozlem/gunluk-mgm/<YYYY-MM>/<plaka>.json  istasyon başına günlük özet (TSİ günü): en düşük, en yüksek, ortalama, rüzgâr, yağış
En sık 10 dakikada bir (dosya yaşına bakar).
"""
import datetime as dt
import json
import os
import time
import urllib.parse
import urllib.request

OUT = os.path.join('out', 'gozlem')
API = 'https://servis.mgm.gov.tr/web/'
HDR = {'User-Agent': 'havaxpro-veri/1.0', 'Origin': 'https://www.mgm.gov.tr', 'Referer': 'https://www.mgm.gov.tr/'}
NA = -9999


def log(*a):
    print(dt.datetime.utcnow().strftime('%H:%M:%S'), 'mgm', *a, flush=True)


def get(path, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(API + path, headers=HDR), timeout=40) as r:
                return json.loads(r.read())
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


def tr_title(s):
    """Türkçe baş harf büyütme: 'ÇAMLIHEMŞİN/PALOVİT YAYLASI' → 'Çamlıhemşin/Palovit Yaylası'"""
    low = s.replace('I', 'ı').replace('İ', 'i').lower()
    out, up = [], True
    for ch in low:
        out.append(({'i': 'İ', 'ı': 'I'}.get(ch) or ch.upper()) if up else ch)
        up = ch in ' /-(.'
    return ''.join(out)


def age_min(path):
    return (time.time() - os.path.getmtime(path)) / 60 if os.path.exists(path) else 1e9


def stations():
    path = os.path.join(OUT, 'mgm-istasyon.json')
    if age_min(path) < 24 * 60:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    iller = sorted({(m['ilPlaka'], m['il']) for m in get('merkezler/iller')})
    out = {}
    for plaka, il in iller:
        try:
            for s in get('istasyonlar/ilAdDetay?il=' + urllib.parse.quote(il)):
                if s.get('enlem') and s.get('boylam'):
                    out[str(s['istNo'])] = [tr_title(s['istAd']), il, s.get('ilce') or '', round(s['enlem'], 4), round(s['boylam'], 4), s.get('yukseklik')]
        except Exception as e:  # noqa: BLE001
            log(il, 'istasyonlar alınamadı', repr(e))
        time.sleep(0.4)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(out, fh, ensure_ascii=False, separators=(',', ':'))
    log(len(out), 'istasyon bilgisi')
    return out


def val(x, d=1):
    return None if x is None or x <= NA + 1 else round(x, d)


def main():
    os.makedirs(OUT, exist_ok=True)
    last = os.path.join(OUT, 'mgm.json')
    if age_min(last) < 9.5:
        log('güncel')
        return
    meta = stations()
    now = dt.datetime.utcnow()
    rows, plaka_of = [], {}
    for plaka in range(1, 82):
        try:
            for o in get(f'sondurumlar/ilTumSondurum?ilPlaka={plaka}'):
                no = str(o['istNo'])
                if no not in meta or o.get('veriZamani') is None:
                    continue
                t = dt.datetime.strptime(o['veriZamani'][:19], '%Y-%m-%dT%H:%M:%S')
                ago = int((now - t).total_seconds() // 60)
                if ago > 180:      # 3 saatten eski ölçüm haritaya konmaz
                    continue
                ws = val(o.get('ruzgarHiz'))
                rows.append([int(no), val(o.get('sicaklik')), val(o.get('hissedilenSicaklik')), val(o.get('nem'), 0),
                             None if ws is None else round(ws, 0), val(o.get('ruzgarYon'), 0), val(o.get('denizeIndirgenmisBasinc')),
                             val(o.get('karYukseklik'), 0), val(o.get('yagis10Dk')), val(o.get('yagis1Saat')), val(o.get('yagis6Saat')),
                             val(o.get('yagis24Saat')), o.get('hadiseKodu') if o.get('hadiseKodu') not in (None, '-9999') else None, max(0, ago)])
                plaka_of[int(no)] = plaka
        except Exception as e:  # noqa: BLE001
            log(plaka, 'alınamadı', repr(e))
        time.sleep(0.3)
    if len(rows) < 100:
        log('çok az ölçüm, yazılmadı', len(rows))
        return
    res = {'generated': now.strftime('%Y-%m-%dT%H:%MZ'), 'source': 'MGM (Son Durumlar; kalite kontrolsüz)',
           'columns': ['no', 'sicaklik', 'hissedilen', 'nem', 'ruzgar', 'yon', 'basinc', 'kar', 'yagis10dk', 'yagis1sa', 'yagis6sa', 'yagis24sa', 'hadise', 'dakika'],
           'rows': rows}
    with open(last, 'w', encoding='utf-8') as fh:
        json.dump(res, fh, ensure_ascii=False, separators=(',', ':'))
    # Son 24 saat (saat başı): her saat bir kez eklenir
    hpath = os.path.join(OUT, 'mgm-24s.json')
    hist = {'times': [], 't': {}, 'w': {}, 'r': {}}
    if os.path.exists(hpath):
        with open(hpath, encoding='utf-8') as fh:
            hist = json.load(fh)
    hour = now.replace(minute=0, second=0, microsecond=0).strftime('%Y-%m-%dT%H:00Z')
    if not hist['times'] or hist['times'][-1] != hour:
        hist['times'].append(hour)
        for r in rows:
            for key, v in (('t', r[1]), ('w', r[4]), ('r', r[9])):
                hist[key].setdefault(str(r[0]), [])
        n = len(hist['times'])
        for key, col in (('t', 1), ('w', 4), ('r', 9)):
            have = {str(r[0]): r[col] for r in rows}
            for no, arr in hist[key].items():
                arr += [None] * (n - 1 - len(arr))        # eksik saatler
                arr.append(have.get(no))
        if n > 24:   # yalnız son 24 saat
            hist['times'] = hist['times'][-24:]
            for key in ('t', 'w', 'r'):
                hist[key] = {no: a[-24:] for no, a in hist[key].items() if any(v is not None for v in a[-24:])}
        with open(hpath, 'w', encoding='utf-8') as fh:
            json.dump(hist, fh, separators=(',', ':'))
    try:
        detail_history(rows, plaka_of, now)
        daily_summary(rows, plaka_of, now)
    except Exception as e:  # noqa: BLE001 — ayrıntılı geçmiş aksarsa anlık harita yine yayınlansın
        log('geçmiş', 'HATA', repr(e))
    log(len(rows), 'ölçüm yazıldı')


# Ayrıntılı geçmişte tutulan büyüklükler: (anahtar, satır sütunu, ölçek) — değerler tamsayı (değer × ölçek)
HKEYS = (('t', 1, 10), ('f', 2, 10), ('rh', 3, 1), ('ws', 4, 1), ('wd', 5, 1), ('p', 6, 10), ('sn', 7, 1), ('r10', 8, 10), ('r1', 9, 10), ('r24', 11, 10))


def detail_history(rows, plaka_of, now):
    """İl başına son 48 saat: her turda bir zaman adımı; 20 dakikadan eski ölçüm o adıma yazılmaz"""
    gdir = os.path.join(OUT, 'gecmis')
    os.makedirs(gdir, exist_ok=True)
    stamp, cutoff = now.strftime('%Y-%m-%dT%H:%MZ'), now - dt.timedelta(hours=48)
    by = {}
    for r in rows:
        by.setdefault(plaka_of.get(r[0]), []).append(r)
    for plaka, rs in by.items():
        if plaka is None:
            continue
        path = os.path.join(gdir, f'{plaka}.json')
        h = {'times': [], 'keys': [k for k, _, _ in HKEYS], 'scale': [sc for _, _, sc in HKEYS], 'st': {}}
        if os.path.exists(path):
            try:
                with open(path, encoding='utf-8') as fh:
                    old = json.load(fh)
                if old.get('keys') == h['keys']:
                    h = old
            except (OSError, ValueError):
                pass
        if h['times'] and h['times'][-1] == stamp:
            continue
        keep = [i for i, t in enumerate(h['times']) if dt.datetime.strptime(t, '%Y-%m-%dT%H:%MZ') >= cutoff]
        h['times'] = [h['times'][i] for i in keep] + [stamp]
        n = len(h['times'])
        cur = {r[0]: r for r in rs}
        for no in list(h['st']):
            h['st'][no] = [[a[i] if i < len(a) else None for i in keep] for a in h['st'][no]]
        for no in cur:
            h['st'].setdefault(str(no), [[None] * (n - 1) for _ in HKEYS])
        for no, arrs in list(h['st'].items()):
            r = cur.get(int(no))
            fresh = r is not None and r[13] <= 20
            for k, (_, col, sc) in enumerate(HKEYS):
                v = r[col] if fresh else None
                arrs[k].append(None if v is None else round(v * sc))
            if not any(v is not None for a in arrs for v in a):
                del h['st'][no]
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(h, fh, separators=(',', ':'))
    with open(os.path.join(gdir, 'index.json'), 'w', encoding='utf-8') as fh:
        json.dump({str(k): v for k, v in plaka_of.items()}, fh, separators=(',', ':'))


def daily_summary(rows, plaka_of, now):
    """Günlük özet (TSİ günü): [en düşük, en yüksek, sıcaklık toplamı ×10, ölçüm sayısı, en sert rüzgâr, rüzgâr toplamı, rüzgâr sayısı, son 24 sa yağış ×10]"""
    local = now + dt.timedelta(hours=3)
    day, month = local.strftime('%d'), local.strftime('%Y-%m')
    mdir = os.path.join(OUT, 'gunluk-mgm', month)
    os.makedirs(mdir, exist_ok=True)
    by = {}
    for r in rows:
        if r[13] <= 20:
            by.setdefault(plaka_of.get(r[0]), []).append(r)
    for plaka, rs in by.items():
        if plaka is None:
            continue
        path = os.path.join(mdir, f'{plaka}.json')
        d = {}
        if os.path.exists(path):
            try:
                with open(path, encoding='utf-8') as fh:
                    d = json.load(fh)
            except (OSError, ValueError):
                d = {}
        for r in rs:
            e = d.setdefault(str(r[0]), {}).setdefault(day, [None, None, 0, 0, None, 0, 0, None])
            t, w, r24 = r[1], r[4], r[11]
            if t is not None:
                e[0] = t if e[0] is None else min(e[0], t)
                e[1] = t if e[1] is None else max(e[1], t)
                e[2] += round(t * 10); e[3] += 1
            if w is not None:
                e[4] = w if e[4] is None else max(e[4], w)
                e[5] += round(w); e[6] += 1
            if r24 is not None:
                e[7] = round(r24 * 10)
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(d, fh, separators=(',', ':'))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log('HATA', repr(e))
