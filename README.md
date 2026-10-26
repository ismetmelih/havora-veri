# HAVORA model verileri

[HAVORA](https://main.d1k15nsfcl8igw.amplifyapp.com) sitesindeki **Model Haritaları** sayfasının verileri.

Bu depoda yalnızca herkese açık model verileri ve onları hazırlayan program vardır; anahtar, şifre ya da kişisel bilgi yoktur.

## Kaynaklar

| Model | Kaynak | Çözünürlük | Süre |
|---|---|---|---|
| GFS | NOAA NOMADS | 0,25° | 240 saat |
| ECMWF IFS | ECMWF açık veri (CC-BY-4.0) | 0,25° | 240 saat |
| ICON-EU | DWD açık veri | 0,0625° → 0,25° | 120 saat |

## Nasıl çalışır

`uret.py` her saat GitHub Actions'ta çalışır. Yeni bir model koşusu yayımlandıysa ham GRIB2 dosyalarını indirir, Türkiye ve çevresini (30–46°K, 18–50°D) keser ve `gh-pages` dalına yazar. Site bu dosyaları GitHub Pages'ten okur.

Alanlar: 850 hPa sıcaklık ve yükseklik, 500 hPa yükseklik, deniz seviyesi basıncı, 2 m sıcaklık, rüzgar hamlesi, 6 saatlik yağış ve kar.

Dosya biçimi: `<model>/<alan>.bin.gz`, gzip'li ve adım adım art arda dizilmiş int16 (little-endian) ızgaralardır. Gerçek değer = ham × `scale`. Eksik değer -32768'dir. Izgara, adımlar ve ölçekler `<model>/meta.json` içindedir.

```
pip install -r requirements.txt
python uret.py              # hepsi
python uret.py gfs --steps 0,6,12
```
