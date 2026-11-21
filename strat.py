"""Kutup girdabı geçmişi: 60°K'de 10 hPa kuşak ortalaması batı rüzgarı (u60).

- 1979/80–2024/25 kışları: NCEP/NCAR Reanalysis 1 günlük ortalamaları (NOAA PSL, OPeNDAP ile yalnız 60°K satırı).
  Biten yıllar bir kez indirilir (out/strat/arsiv/<yıl>.json), sonra değişmez.
- Bu sezon (ve PSL'nin güncellemeyi bıraktığı 17 Mart 2026 sonrası): GFS 00Z analizleri (NOAA, AWS açık veri),
  her gün için yalnız 10 hPa rüzgar kaydı byte aralığıyla indirilir.
Çıktı: out/strat/iklim.json
  clim      1991–2020 günlük %10, ortalama, %90, en düşük, en yüksek (1 Temmuz'dan başlayan 365 gün)
  obs       bu sezonun gözlemi (aynı gün sırası)
  ssw       her kışın ani stratosfer ısınması tarihleri (Charlton ve Polvani 2007 ölçütü)
  analogs   bu sezona (1 Ekim'den bugüne) en çok benzeyen 5 kış: u60 eğrisi, SSW tarihleri, Türkiye kış sıcaklık farkı
Günde bir kez hesaplanır.
"""
import datetime as dt
import json
import os
import struct
import tempfile
import time
import urllib.request

import eccodes
import numpy as np

OUT = os.path.join('out', 'strat')
ARS = os.path.join(OUT, 'arsiv')
UA = {'User-Agent': 'havaxpro-veri/1.0'}
PSL = 'https://psl.noaa.gov/thredds/dodsC/Datasets/ncep.reanalysis/Dailies/pressure/uwnd.{y}.nc'
GFS = 'https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.{d}/00/atmos/gfs.t00z.pgrb2.1p00.f000'
FIRST, LAST_R1 = 1979, 2025        # NCEP R1'den alınan tam yıllar
R1_END = dt.date(2026, 3, 17)      # PSL'de R1'in son günü; sonrası GFS analizi
BASE = (1991, 2020)                # klimatoloji kışları (1991/92 … 2020/21)
GFS_PER_RUN = 70                   # bir turda en çok bu kadar günlük GFS analizi indirilir


def get(url, headers=None, tries=3):
    for i in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={**UA, **(headers or {})}), timeout=120) as r:
                return r.read()
        except Exception:  # noqa: BLE001
            if i == tries - 1:
                raise
            time.sleep(5 * (i + 1))


def r1_year(y):
    """NCEP R1 günlük 10 hPa (seviye 16), 60°K (enlem 12) rüzgarı → günlük kuşak ortalaması listesi"""
    n = 366 if y % 4 == 0 else 365
    if y > LAST_R1:   # yarım kalan yıl: gün sayısı sunucudan
        dds = get(PSL.format(y=y) + '.dds').decode()
        n = int(dds.split('time[time = ')[1].split(']')[0])
    raw = get(PSL.format(y=y) + f'.dods?uwnd[0:1:{n - 1}][16:1:16][12:1:12][0:1:143]')
    k = raw.index(b'\nData:\n') + 7
    count = struct.unpack('>I', raw[k:k + 4])[0]
    vals = np.array(struct.unpack(f'>{count}f', raw[k + 8:k + 8 + 4 * count])).reshape(-1, 144)
    return [round(float(v), 2) for v in vals.mean(axis=1)]


def gfs_day(d):
    """GFS 00Z analizi: 10 hPa rüzgarın 60°K kuşak ortalaması (1° ızgara)"""
    url = GFS.format(d=f'{d:%Y%m%d}')
    idx = get(url + '.idx').decode().splitlines()
    for i, line in enumerate(idx):
        if ':UGRD:10 mb:' in line:
            a, b = int(line.split(':')[1]), int(idx[i + 1].split(':')[1]) - 1
            data = get(url, {'Range': f'bytes={a}-{b}'})
            break
    else:
        return None
    with tempfile.NamedTemporaryFile(suffix='.grb2') as tmp:
        tmp.write(data)
        tmp.flush()
        with open(tmp.name, 'rb') as f:
            h = eccodes.codes_grib_new_from_file(f)
            try:
                ni, nj = eccodes.codes_get(h, 'Ni'), eccodes.codes_get(h, 'Nj')
                vals = eccodes.codes_get_values(h).reshape(nj, ni)
                lats = eccodes.codes_get_array(h, 'latitudes').reshape(nj, ni)[:, 0]
            finally:
                eccodes.codes_release(h)
    return round(float(vals[int(np.argmin(np.abs(lats - 60)))].mean()), 2)


def load(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def dump(path, obj):
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, separators=(',', ':'))


def season_days(y):
    """y kışının günleri: 1 Temmuz y → 30 Haziran y+1, 29 Şubat hariç (365 gün)"""
    d, out = dt.date(y, 7, 1), []
    while d < dt.date(y + 1, 7, 1):
        if not (d.month == 2 and d.day == 29):
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def ssw_dates(days, u):
    """Charlton ve Polvani (2007): kasım–mart arasında u60 doğuya döner; iki olay arasında en az 20 gün batı rüzgarı;
    son ısınma sayılmaz (30 Nisan'dan önce en az 10 gün art arda batı rüzgarına dönmeli)."""
    out, last_end, k = [], None, 0
    n = len(days)
    while k < n:
        d, v = days[k], u[k]
        if v is None or v >= 0 or d.month not in (11, 12, 1, 2, 3):
            k += 1
            continue
        # Olayın sonu: rüzgar yeniden batıya döndüğü gün
        e = k
        while e < n and (u[e] is None or u[e] < 0):
            e += 1
        if last_end is not None and (k - last_end) < 20:
            k = e
            continue
        # 30 Nisan'a kadar en az 10 gün art arda batı rüzgarı
        run, ok = 0, False
        for j in range(e, n):
            if days[j] > dt.date(days[j].year, 4, 30) and days[j].month < 7:
                break
            run = run + 1 if (u[j] is not None and u[j] > 0) else 0
            if run >= 10:
                ok = True
                break
        if ok:
            out.append(f'{d:%Y-%m-%d}')
            last_end = e
        k = e
    return out


def main():
    os.makedirs(ARS, exist_ok=True)
    target = os.path.join(OUT, 'iklim.json')
    gp = os.path.join(OUT, 'gfs-analiz.json')
    gfs = load(gp, {})
    # GFS analizi geçmişi eksikse (ilk günler) her turda doldurmaya devam et; tamamsa günde bir
    yday = dt.datetime.utcnow().date() - dt.timedelta(days=1)
    full = all(f'{R1_END + dt.timedelta(days=k):%Y-%m-%d}' in gfs for k in range(1, (yday - R1_END).days + 1))
    if full and os.path.exists(target) and time.time() - os.path.getmtime(target) < 20 * 3600:
        print('Stratosfer geçmişi: güncel')
        return
    t0 = time.time()
    # 1) NCEP R1 arşivi (yalnız eksik yıllar)
    for y in range(FIRST, LAST_R1 + 2):
        p = os.path.join(ARS, f'{y}.json')
        if os.path.exists(p) or time.time() - t0 > 240:
            continue
        try:
            dump(p, {'y': y, 'u': r1_year(y)})
        except Exception as e:  # noqa: BLE001
            print('NCEP R1', y, 'alınamadı:', e)
    daily = {}
    for y in range(FIRST, LAST_R1 + 2):
        a = load(os.path.join(ARS, f'{y}.json'), None)
        if not a:
            continue
        d0 = dt.date(y, 1, 1)
        for i, v in enumerate(a['u']):
            daily[d0 + dt.timedelta(days=i)] = v
    # 2) GFS analizleri: R1'in bittiği günden bugüne (yeniden eskiye doğru, turda en çok GFS_PER_RUN gün)
    today = dt.datetime.utcnow().date()
    d, got = today, 0
    while d > R1_END and got < GFS_PER_RUN and time.time() - t0 < 420:
        key = f'{d:%Y-%m-%d}'
        if key not in gfs:
            try:
                v = gfs_day(d)
                if v is not None:
                    gfs[key] = v
                    got += 1
            except Exception:  # noqa: BLE001  (bugünün 00Z'si henüz yoksa)
                pass
        d -= dt.timedelta(days=1)
    dump(gp, gfs)
    for k, v in gfs.items():
        daily.setdefault(dt.date.fromisoformat(k), v)
    if len(daily) < 365 * 10:
        print('Stratosfer geçmişi: arşiv henüz dolmadı', len(daily))
        return
    # 3) Kış kış seriler
    cur = today.year if today.month >= 7 else today.year - 1
    seasons = {}
    for y in range(FIRST, cur + 1):
        days = season_days(y)
        seasons[y] = (days, [daily.get(x) for x in days])
    clim_rows = np.array([[np.nan if v is None else v for v in seasons[y][1]] for y in range(BASE[0], BASE[1] + 1)], float)
    def smooth(a):
        k = np.ones(5) / 5
        return np.convolve(np.pad(a, 2, mode='edge'), k, mode='valid')
    with np.errstate(all='ignore'):
        clim = {name: [round(float(x), 1) for x in smooth(fn(clim_rows))] for name, fn in {
            'p10': lambda a: np.nanpercentile(a, 10, axis=0), 'mean': lambda a: np.nanmean(a, axis=0),
            'p90': lambda a: np.nanpercentile(a, 90, axis=0), 'min': lambda a: np.nanmin(a, axis=0), 'max': lambda a: np.nanmax(a, axis=0)}.items()}
    ssw = {f'{y}/{str(y + 1)[2:]}': ssw_dates(*seasons[y]) for y in range(FIRST, cur + 1)}
    # 4) Benzerlik: 1 Ekim'den son gözleme kadar, yakın günlere daha çok ağırlık
    days, obs = seasons[cur]
    have = [i for i, v in enumerate(obs) if v is not None]
    start = days.index(dt.date(cur, 10, 1))
    if not have or have[-1] - start < 10:
        start = days.index(dt.date(cur, 9, 1))
    idx = [i for i in have if i >= start]
    trm = {k: v for k, v in load(os.path.join('out', 'iklim', 'tr-aylik.json'), [])}
    rows = []
    for y in range(FIRST, cur):
        u = seasons[y][1]
        pairs = [(i, u[i] - obs[i]) for i in idx if u[i] is not None]
        if len(pairs) < len(idx) * 0.9 or not pairs:
            continue
        w = np.linspace(1, 3, len(pairs))
        dist = float(np.sqrt(np.sum(w * np.array([p[1] for p in pairs]) ** 2) / w.sum()))
        mon = {m: trm.get(f'{y + (m < 7)}-{m:02d}') for m in (12, 1, 2)}
        vals = [v for v in mon.values() if v is not None]
        rows.append({'season': f'{y}/{str(y + 1)[2:]}', 'y': y, 'dist': round(dist, 1), 'sim': round(100 / (1 + dist / 5)),
                     'ssw': ssw[f'{y}/{str(y + 1)[2:]}'], 'tr': {'dec': mon[12], 'jan': mon[1], 'feb': mon[2],
                                                                     'djf': round(sum(vals) / len(vals), 2) if len(vals) == 3 else None},
                     'u': [None if v is None else round(v, 1) for v in u]})
    rows.sort(key=lambda r: r['dist'])
    last = days[have[-1]] if have else None
    dump(target, {
        'generated': time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime()), 'season': f'{cur}/{str(cur + 1)[2:]}', 'start': f'{days[0]:%Y-%m-%d}',
        'base': f'{BASE[0]}/{str(BASE[0] + 1)[2:]}–{BASE[1]}/{str(BASE[1] + 1)[2:]}', 'compareFrom': f'{days[start]:%Y-%m-%d}',
        'obsEnd': f'{last:%Y-%m-%d}' if last else None, 'clim': clim,
        'obs': [None if v is None else round(v, 1) for v in obs], 'ssw': ssw, 'analogs': rows[:5],
        'source': 'NCEP/NCAR Reanalysis 1 (NOAA PSL, 1979–Mart 2026) · GFS analizi (NOAA, AWS açık veri) · Türkiye: NASA GISTEMP v4',
    })
    print(f'Stratosfer geçmişi: {len(ssw)} kış, {sum(len(v) for v in ssw.values())} ani ısınma, son gözlem {last}')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # noqa: BLE001  (bu iş aksarsa model işi sürsün)
        print('Stratosfer geçmişi alınamadı:', e)
