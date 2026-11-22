#!/usr/bin/env bash
# GitHub'ın zamanlanmış işleri saatlik değil 4–8 saatte bir çalışabiliyor. Bu yüzden iş bir kez başlayınca
# yaklaşık 5,5 saat açık kalır. Modeller paralel döngülerde çalışır: hızlı yayınlananlar (GFS, ICON) yavaşları
# (GEM, UKMO) beklemez. Yayıncı 2 dakikada bir değişiklik varsa yayınlar. Bir sonraki iş sıraya girer ve bu bitince devralır.
set -u
SURE_DK=${SURE_DK:-330}      # iş en çok bu kadar dakika sürer (GitHub sınırı 360)
LOCK=.yazim.lock             # out/ klasörüne yazma (uret.py) ve yayınlama aynı anda olmasın
son=$(( $(date +%s) + SURE_DK * 60 ))

git clone -q --depth 1 --branch gh-pages "https://github.com/${GITHUB_REPOSITORY}" out || mkdir -p out

# Bir sonraki işi kendimiz başlat: GitHub'ın zamanlanmış tetiklemesi saatlerce gecikebiliyor.
# Yeni iş sıraya girer (aynı concurrency grubu) ve bu bitince hemen başlar; böylece veri akışında boşluk kalmaz.
devam() {
  curl -s -o /dev/null -w "Sonraki iş istendi: HTTP %{http_code}\n" -X POST \
    -H "Authorization: Bearer ${GH_TOKEN}" -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/${GITHUB_REPOSITORY}/dispatches" -d '{"event_type":"devam"}'
}

yayinla() {
  (
    flock 9
    cd out || exit 0
    # -uall: yeni klasördeki dosyalar da (ör. ilk kez yazılan endeks/meta.json) tek tek görünsün
    if [ -d .git ] && [ -z "$(git status --porcelain -uall -- '*/meta.json' 'gozlem/*' 'rekor/*' 'iklim/*' 'analiz/*' 'strat/*' 'deniz/*' 'kamera/*')" ]; then
      exit 0
    fi
    rm -rf .git
    touch .nojekyll
    git init -q -b gh-pages
    git config user.name "github-actions[bot]"
    git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
    git add -A
    git commit -qm "Model verileri $(date -u +%Y-%m-%dT%H:%MZ)"
    git push -qf "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}" gh-pages && echo "[yayın] Yayınlandı $(date -u +%H:%MZ)"
  ) 9>"$LOCK"
}

# Döngü: komutları sırayla çalıştırır, bekler; kalan süre bir turdan azsa yeni tur başlatmaz.
# dongu <ad> <bekleme sn> <bir turun en uzun süresi dk> <komut>...
dongu() {
  local ad=$1 bekle=$2 tur=$3
  shift 3
  while [ $(( $(date +%s) + tur * 60 )) -lt "$son" ]; do
    for k in "$@"; do
      eval "$k" 2>&1 | sed -u "s/^/[$ad] /"
    done
    sleep "$bekle"
  done
  echo "[$ad] süre doldu"
}

# Yan işler (gözlemler, arşivler): her biri kendi sıklığını dosya yaşına göre denetler
dongu yan 900 25 "python gozlem.py" "python synop.py" "python rekor.py" "python iklim.py" "python analog.py" \
  "python strat.py" "python nmme.py" "python deniz.py" "python yagis.py" "python kis.py" "python normal.py" "python rossby.py" "python aam.py" &
# Canlı kameralar: saatte bir yayın adresleri denetlenir (kamera.py kendi sıklığını dosya yaşına göre tutar)
dongu kamera 600 5 "python kamera.py" &
# MGM istasyonları (canlı gözlem haritası): 5 dakikada bir bakılır, ölçümler ~10 dakikada bir yazılır
dongu gozlem 300 10 "python mgm.py" &
# NOAA GFS ve DWD ICON: ayrı döngüler (biri işlenirken öteki beklemesin), 2 dakikada bir bakılır
dongu gfs 120 25 "python uret.py gfs" &
dongu icon 120 25 "python uret.py icon" &
# ECMWF ayrı, AIFS ve ENS birlikte: 2,5 dakikada bir
dongu ecmwf 150 35 "python uret.py ecmwf" &
# ECMWF 9 km (Open-Meteo açık verisi): 0,25° açık veriden ~2 saat önce, adım adım; yalnız yer alanları
dongu ecmwf9 120 35 "python uret.py ecmwf9" &
dongu aifs 150 35 "python uret.py aifs" "python uret.py ens" &
# GEM ve UKMO: ayrı döngüler; UKMO yavaş okunur, 8 dakikada bir eldekiler yazılır
dongu gem 150 40 "python uret.py gem" &
dongu ukmo 120 50 "python uret.py ukmo" &
# GEFS ortalaması ve kontrol senaryosu (canlı haritada GFS senaryo seçimi); 31 senaryonun haritaları (diyagram sayfası)
dongu gefs 150 40 "python uret.py gefs" &
dongu gefsk 150 40 "python uret.py gefsk" "python uyeler.py" &
# Yayıncı
( while [ "$(date +%s)" -lt "$son" ]; do yayinla; sleep 120; done ) &

wait
yayinla
devam
