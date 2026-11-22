"""Canlı kameralar: YouTube'daki halka açık, yerleştirmeye izin verilen Türkiye kameraları.

Liste elle seçilir (kameralar.json). İki tür:
- tur "yt": YouTube yayını (kanal, başlık kalıbı, video)
- tur "hls": belediyenin herkese açık HLS yayını (ör. İBB İstanbul'u Seyret); yalnız adres yanıt veriyor mu denetlenir
Kişilerin yüzünün ya da araç plakalarının seçilebildiği yakın çekim meydan/sokak kameraları listeye alınmaz
(KVKK, "Belediyelerin Turistik Tanıtım Amacıyla Yaptığı Canlı Yayınlar Hakkında Kamuoyu Duyurusu", 2026).
YouTube yayınları için Yayınlar yeniden başlayınca
video kimliği değişebildiği için saatte bir:
- her kanalın resmi RSS akışında (feeds/videos.xml) başlık kalıbına uyan yayınlar aday olarak eklenir
- her aday YouTube oEmbed ile denetlenir: 200 = var ve yerleştirilebilir; 401/404 = yerleştirme kapalı ya da silinmiş
Çıktı: out/kamera/kameralar.json  (her kamera: v = önce denenecek video, alt = sıradaki adaylar)
Sayfa önce v'yi açar; yayın canlı değilse sıradaki adaya geçer.
"""
import datetime as dt
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

OUT = os.path.join('out', 'kamera', 'kameralar.json')
UA = {'User-Agent': 'havaxpro-veri/1.0'}


def log(*a):
    print(dt.datetime.utcnow().strftime('%H:%M:%S'), 'kamera', *a, flush=True)


def get(url, timeout=25):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return r.read().decode('utf-8', 'ignore')


def embeddable(v):
    """True: yerleştirilebilir; False: yerleştirme kapalı ya da video yok; None: denetlenemedi"""
    u = 'https://www.youtube.com/oembed?format=json&url=' + urllib.parse.quote(f'https://www.youtube.com/watch?v={v}')
    try:
        get(u, 15)
        return True
    except urllib.error.HTTPError as e:
        return False if e.code in (401, 403, 404) else None
    except Exception:  # noqa: BLE001
        return None


def main():
    if os.path.exists(OUT) and time.time() - os.path.getmtime(OUT) < 55 * 60:
        log('güncel')
        return
    with open('kameralar.json', encoding='utf-8') as fh:
        cams = json.load(fh)
    feeds = {}
    for c in {x['c'] for x in cams if x.get('tur', 'yt') == 'yt'}:
        try:
            xml = get(f'https://www.youtube.com/feeds/videos.xml?channel_id={c}')
            feeds[c] = [(v, html.unescape(t)) for v, t in re.findall(r'<yt:videoId>([^<]+)</yt:videoId>.*?<title>([^<]+)</title>', xml, re.S)]
        except Exception as e:  # noqa: BLE001
            log(c, 'akış alınamadı', repr(e))
            feeds[c] = []
        time.sleep(0.3)
    checked, out, gone = {}, [], 0
    for x in cams:
        if x.get('tur') == 'hls':
            try:
                get(x['u'], 15)
                ok = True
            except urllib.error.HTTPError:
                ok = False
            except Exception:  # noqa: BLE001
                ok = True      # geçici ağ hatası: kamerayı düşürme
            gone += not ok
            out.append({**{k: v for k, v in x.items() if k != 't'}, 'ok': ok})
            continue
        cand = [x['v']] + [v for v, t in feeds.get(x['c'], []) if re.search(x['t'], t) and v != x['v']]
        ok = []
        for v in cand[:5]:
            if v not in checked:
                checked[v] = embeddable(v)
                time.sleep(0.2)
            if checked[v] is not False:
                ok.append(v)
        gone += not ok
        out.append({k: x[k] for k in ('k', 'n', 'il', 'ilce', 'la', 'lo', 'kat', 'src', 'c', 'tur')} | {'v': ok[0] if ok else x['v'], 'alt': ok[1:], 'ok': bool(ok)})
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, 'w', encoding='utf-8') as fh:
        json.dump({'generated': dt.datetime.utcnow().strftime('%Y-%m-%dT%H:%MZ'), 'cams': out}, fh, ensure_ascii=False, separators=(',', ':'))
    log(len(out), 'kamera yazıldı,', gone, 'tanesinin yayını yok')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:  # noqa: BLE001
        log('HATA', repr(e))
