# HAVORA model verileri

[HAVORA](https://main.d1k15nsfcl8igw.amplifyapp.com) sitesindeki **Model Haritaları** sayfasının verileri.

Bu depoda yalnızca herkese açık model verileri ve onları hazırlayan program vardır; anahtar, şifre ya da kişisel bilgi yoktur.

## Kaynaklar

| Model | Kaynak | Çözünürlük | Süre |
|---|---|---|---|
| GFS | NOAA NOMADS | 0,25° | 384 saat |
| ECMWF IFS | ECMWF açık veri (CC-BY-4.0) | 0,25° | 360 saat |
| ICON-EU | DWD açık veri | 0,0625° → 0,25° | 120 saat |

Model başına son 4 koşu saklanır (sabah 00Z, öğle 06Z, akşam 12Z, gece 18Z). ECMWF'nin 00Z ve 12Z koşuları 15 gün, 06Z ve 18Z koşuları 6 gün; GFS 16 gün.

## Nasıl çalışır

`uret.py` her saat GitHub Actions'ta çalışır. Yeni bir model koşusu yayımlandıysa ham GRIB2 dosyalarını indirir, Türkiye ve çevresi ile Avrupa–Atlantik bölgesini keser ve `gh-pages` dalına yazar. Site bu dosyaları GitHub Pages'ten okur.

Alanlar: 850 hPa sıcaklık ve yükseklik, 500 hPa yükseklik ve sıcaklık, 500 hPa anomali, deniz seviyesi basıncı, 2 m sıcaklık, rüzgar hamlesi, 6 ve 24 saatlik yağış ve kar.

500 hPa anomali, `klima/z500_era5_1990_2019.npz` dosyasındaki 1990–2019 ortalamasına göre hesaplanır. Bu dosya [WeatherBench2](https://weatherbench2.readthedocs.io) ERA5 iklim ortalamasından (1,5°, 5 günde bir) üretildi. Copernicus İklim Değişikliği Servisi bilgisinden değiştirilerek elde edilmiştir.

Dosya biçimi: `<model>/<koşu YYYYMMDDHH>/<alan>.<bölge>.bin.gz`; koşuların listesi `index.json` içinde. Bölgeler: `tr` (Türkiye, 0,25°, 3 saatte bir) ve `eu` (Avrupa, Atlantik ve Kuzey Afrika, 0,5°, 6 saatte bir). İçerik gzip'li int16 ızgaralardır. Her satır soldan farklarla (16 bit taşmalı) yazılır, sonra önce bütün düşük baytlar, ardından bütün yüksek baytlar gelir. Gerçek değer = ham × `scale`, eksik değer -32768. Izgaralar, adımlar ve ölçekler `<model>/meta.json` içindedir.

```
pip install -r requirements.txt
python uret.py              # hepsi
python uret.py gfs --steps 0,6,12
```
