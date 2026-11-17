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

GitHub Actions işi (`calis.sh`) 5,5 saat açık kalır ve 15 dakikada bir `uret.py`'yi her model için ayrı çalıştırıp hemen yayınlar. Koşular kademeli yayınlanır: ilk 12 saati hazır olan yeni koşu `"complete": false` olarak yazılır, merkez yeni saatleri koydukça uzar (indirilen adımlar `onbellek/`te tutulur, tekrar indirilmez). `uret.py` ham GRIB2 dosyalarını indirir, Türkiye ve çevresi ile Avrupa–Atlantik bölgesini keser ve `gh-pages` dalına yazar. Site bu dosyaları GitHub Pages'ten okur.

Alanlar: 850 hPa sıcaklık ve yükseklik, 500 hPa yükseklik ve sıcaklık, 500 ve 850 hPa anomali, deniz seviyesi basıncı, 2 m sıcaklık, rüzgar hamlesi, 6 ve 24 saatlik yağış ve kar, toplam kar, kar kalınlığı, donma seviyesi (GFS, ICON), 50 ve 10 hPa yükseklik ve sıcaklık (GFS, ECMWF; yalnızca dünya bölgesi). Stratosfer serileri (`meta.json` → `series`): 60°K'de 10 hPa kuşak ortalaması batı rüzgarı ve 60–90°K ortalama 10 hPa sıcaklığı.

Geçmiş analiz: `arsiv/<YYYYMMDDHH>/` klasörlerinde ECMWF 00Z ve 12Z koşularının başlangıç haritaları (500/850 hPa, basınç, 2 m sıcaklık ve anomaliler), 60 gün saklanır.

Göstergeler (`endeks.py` → `endeks/meta.json`, 3 saatte bir yenilenir): AO, NAO, PNA ve AAO için son 150 günün gözlemi, GEFS'in 31 üyeli ve GFS'in 15 günlük tahmini (NOAA CPC); MJO için NOAA PSL'nin gerçek zamanlı OMI endeksi, RMM faz düzlemine çevrilmiş (x = PC2, y = −PC1); QBO için 30 ve 50 hPa aylık ekvator rüzgarı; ENSO için haftalık Niño 3.4 anomalisi ve ONI (NOAA CPC).

Anomaliler, `klima/z500_era5_1990_2019.npz` ve `klima/t850_era5_1990_2019.npz` dosyalarındaki 1990–2019 ortalamasına göre hesaplanır. Bu dosya [WeatherBench2](https://weatherbench2.readthedocs.io) ERA5 iklim ortalamasından (1,5°, 5 günde bir) üretildi. Copernicus İklim Değişikliği Servisi bilgisinden değiştirilerek elde edilmiştir.

Dosya biçimi: `<model>/<koşu YYYYMMDDHH>/<alan>.<bölge>.bin.gz`; koşuların listesi `index.json` içinde. Bölgeler: `tr` (Türkiye, 0,25°, 3 saatte bir) ve `eu` (Avrupa, Atlantik ve Kuzey Afrika, 0,5°, 6 saatte bir). İçerik gzip'li int16 ızgaralardır. Her satır soldan farklarla (16 bit taşmalı) yazılır, sonra önce bütün düşük baytlar, ardından bütün yüksek baytlar gelir. Gerçek değer = ham × `scale`, eksik değer -32768. Izgaralar, adımlar ve ölçekler `<model>/meta.json` içindedir.

```
pip install -r requirements.txt
python uret.py              # hepsi
python uret.py gfs --steps 0,6,12
```
