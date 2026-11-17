"""
Göstergeler: AO, NAO, PNA, AAO (gözlem + GEFS ve GFS tahmini), MJO (RMM), QBO, ENSO.
Kaynaklar NOAA CPC ve Avustralya Meteoroloji Bürosu (BoM); hepsi herkese açık.
Çıktı out/endeks/meta.json: yayın adımı */meta.json değişince çalıştığı için bu adla yazılır.
"""
import csv
import datetime as dt
import io
import email.utils
import json
import math
import urllib.request
import os
import re

CPC = 'https://ftp.cpc.ncep.noaa.gov/cwlinks/'
TELE = {   # ad → (CPC dosya adı parçası, seviye)
    'ao': ('ao', 'z1000'),
    'nao': ('nao', 'z500'),
    'pna': ('pna', 'z500'),
    'aao': ('aao', 'z700'),
}
OBS_DAYS = 150
# MJO: NOAA PSL gerçek zamanlı OMI (ROMI). BoM'un RMM dosyası betiklerle indirilmeye kapalı.
MJO_URL = 'https://psl.noaa.gov/mjo/mjoindex/romi.cpcolr.1x.txt'
QBO_URL = 'https://www.cpc.ncep.noaa.gov/data/indices/qbo.{}.index'
ONI_URL = 'https://www.cpc.ncep.noaa.gov/data/indices/oni.ascii.txt'
NINO_URL = 'https://www.cpc.ncep.noaa.gov/data/indices/wksst9120.for'
NAMES = ('ao', 'nao', 'pna', 'aao', 'mjo', 'qbo', 'enso')
FORMAT = 2   # çıktı biçimi değişince artır: eski dosya beklemeden yeniden üretilir
EVERY_H = 1   # NOAA günde bir kez (~14:00 UTC) günceller; saatte bir bakınca en geç 1 saatte gelir


def last_modified(url):
    """Kaynak dosyanın sunucudaki son güncellenme zamanı (ISO, UTC); bilinmiyorsa None"""
    try:
        req = urllib.request.Request(url, method='HEAD', headers={'User-Agent': 'havora-veri/1.0 (+https://github.com/ismetmelih/havora-veri)'})
        with urllib.request.urlopen(req, timeout=30) as r:
            lm = r.headers.get('Last-Modified')
        return email.utils.parsedate_to_datetime(lm).strftime('%Y-%m-%dT%H:%MZ') if lm else None
    except Exception:  # noqa: BLE001
        return None


def r2(v):
    return None if v is None else round(float(v), 2)


def rows(text):
    return list(csv.reader(io.StringIO(text)))


def teleconnection(get, name):
    key, lev = TELE[name]
    start = '19790101' if name == 'aao' else '19500101'
    obs_url = f'{CPC}norm.daily.{key}.cdas.{lev}.{start}_current.csv'
    obs = rows(get(obs_url).decode())[1:]
    obs = [(f'{int(y):04d}-{int(m):02d}-{int(d):02d}', r2(v)) for y, m, d, v in obs[-OBS_DAYS:] if v not in ('', 'NaN')]
    out = {'obs': obs, 'source_time': last_modified(obs_url)}
    # GEFS: 31 üye, 0–15 gün; en yeni başlangıç tarihi alınır
    ens = rows(get(f'{CPC}norm.daily.{key}.gefs.{lev}.120days.csv').decode())[1:]
    last = max(r[2] for r in ens)
    members = {}
    for lead, mem, init, val, valid in ens:
        if init == last and val not in ('', 'NaN'):
            members.setdefault(int(mem), []).append((int(lead), valid, float(val)))
    days = sorted({(lead, valid) for m in members.values() for lead, valid, _ in m})
    # NOAA'nın grafiği gibi: CDAS gözlemi ~2 gün geriden gelir; sonraki günler GEFS'in o günkü başlangıç
    # analizinden (0. saat, 31 üyenin ortalaması) eklenir. Böylece gözlem bugüne kadar uzanır.
    lead0 = {}
    for lead, mem, init, val, valid in ens:
        if lead == '0' and val not in ('', 'NaN'):
            lead0.setdefault(init, []).append(float(val))
    cdas_last = obs[-1][0] if obs else ''
    out['obs_cdas_until'] = cdas_last
    for init in sorted(lead0):
        if init > cdas_last:
            obs.append((init, r2(sum(lead0[init]) / len(lead0[init]))))
    out['gefs'] = {
        'init': last,
        'valid': [v for _, v in days],
        'members': [[r2(next((x for ld, _, x in members[m] if ld == lead), None)) for lead, _ in days] for m in sorted(members)],
    }
    det = rows(get(f'{CPC}norm.daily.{key}.gfs.{lev}.120days.csv').decode())[1:]
    lastd = max(r[1] for r in det)
    out['gfs'] = {'init': lastd, 'points': [(valid, r2(val)) for lead, init, val, valid in det if init == lastd and val not in ('', 'NaN')]}
    return out


def mjo(get):
    # OMI'yi alışılmış RMM faz diyagramına çevirme (PSL): x = PC2, y = −PC1; faz 1–8 açıya göre
    pts = []
    for line in get(MJO_URL).decode('latin-1').splitlines():
        p = line.split()
        if len(p) < 7 or not p[0].isdigit():
            continue
        y, m, d, _, pc1, pc2, amp = p[:7]
        x, yy = float(pc2), -float(pc1)
        if abs(x) > 100:
            continue
        ph = int((math.degrees(math.atan2(yy, x)) + 180) // 45) % 8 + 1
        pts.append((f'{int(y):04d}-{int(m):02d}-{int(d):02d}', r2(x), r2(yy), ph, r2(amp)))
    return {'points': pts[-120:], 'source_time': last_modified(MJO_URL)}


def qbo(get):
    out = {}
    for lev in ('u30', 'u50'):
        text = get(QBO_URL.format(lev)).decode('latin-1').splitlines()
        vals = []
        for line in text:
            if 'ANOMALY' in line:   # yalnızca ilk bölüm (gerçek rüzgar, m/s)
                break
            # Eksik aylar bitişik yazılıyor (15.91-999.90): sayılar tek tek ayıklanır
            if not line[:4].isdigit():
                continue
            nums = re.findall(r'-?\d+\.\d+', line[4:])
            if len(nums) == 12:
                y = int(line[:4])
                for mth, v in enumerate(nums, 1):
                    v = float(v)
                    if v > -999:
                        vals.append((f'{y:04d}-{mth:02d}-15', round(v, 1)))
        out[lev] = vals[-96:]   # son 8 yıl
    out['source_time'] = last_modified(QBO_URL.format('u30'))
    return out


def enso(get):
    oni = []
    seas = {'DJF': 1, 'JFM': 2, 'FMA': 3, 'MAM': 4, 'AMJ': 5, 'MJJ': 6, 'JJA': 7, 'JAS': 8, 'ASO': 9, 'SON': 10, 'OND': 11, 'NDJ': 12}
    for line in get(ONI_URL).decode('latin-1').splitlines()[1:]:
        p = line.split()
        if len(p) == 4 and p[0] in seas:
            oni.append((f'{int(p[1]):04d}-{seas[p[0]]:02d}-15', float(p[3])))
    weekly = []
    for line in get(NINO_URL).decode('latin-1').splitlines():
        line = line.rstrip()
        if len(line) < 60 or not line[1:3].isdigit():
            continue
        d = dt.datetime.strptime(line[1:10], '%d%b%Y').date()
        # Sayılar bitişik yazılabiliyor (20.6-0.1): sırayla Niño1+2, 3, 3.4, 4 için sıcaklık ve anomali
        nums = re.findall(r'-?\d+\.\d', line[10:])
        if len(nums) == 8:
            weekly.append((d.isoformat(), float(nums[5])))
    return {'oni': oni[-96:], 'nino34': weekly[-104:], 'source_time': last_modified(NINO_URL)}


def endeksler(out_dir, get, log):
    path = os.path.join(out_dir, 'endeks', 'meta.json')
    old = {}
    if os.path.exists(path):
        with open(path, encoding='utf-8') as fh:
            old = json.load(fh)
        try:
            age = dt.datetime.utcnow() - dt.datetime.strptime(old['generated'], '%Y-%m-%dT%H:%MZ')
            # Bir gösterge eksikse (kaynak o an yanıt vermediyse) beklemeden bir sonraki turda tekrar dene
            if age < dt.timedelta(hours=EVERY_H) and all(k in old for k in NAMES) and old.get('format') == FORMAT:
                log('göstergeler güncel')
                return
        except (KeyError, ValueError):
            pass
    data = {'generated': dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%MZ'), 'format': FORMAT}
    # Her gösterge ayrı: biri hata verirse eski değeri kalır
    for name, fn in [('ao', lambda: teleconnection(get, 'ao')), ('nao', lambda: teleconnection(get, 'nao')),
                     ('pna', lambda: teleconnection(get, 'pna')), ('aao', lambda: teleconnection(get, 'aao')),
                     ('mjo', lambda: mjo(get)), ('qbo', lambda: qbo(get)), ('enso', lambda: enso(get))]:
        try:
            data[name] = fn()
        except Exception as e:  # noqa: BLE001
            log('gösterge', name, 'HATA', repr(e))
            if name in old:
                data[name] = old[name]
    # Değer değişmese de yazılır: "generated" son kontrol saatidir, sitede verinin güncel olduğu buradan görülür
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(',', ':'))
    log('göstergeler yazıldı', os.path.getsize(path) // 1024, 'KB')
