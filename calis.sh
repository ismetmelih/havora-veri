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
    if [ -d .git ] && [ -z "$(git status --porcelain -uall -- '*/meta.json' 'gozlem/*' 'rekor/*' 'iklim/*' 'analiz/*' 'strat/*' 'deniz/*')" ]; then
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
  "python strat.py" "python nmme.py" "python deniz.py" "python yagis.py" "python kis.py" &
# NOAA GFS ve DWD ICON: koşudan ~3,5 saat sonra saat saat gelir, 5 dakikada bir bakılır
dongu hizli 300 25 "python uret.py gfs" "python uret.py icon" &
# ECMWF, AIFS ve ENS
dongu ecmwf 300 35 "python uret.py ecmwf" "python uret.py aifs" "python uret.py ens" &
# GEM ve UKMO: büyük dosyalar, yavaş
dongu yavas 600 50 "python uret.py gem" "python uret.py ukmo" &
# Yayıncı
( while [ "$(date +%s)" -lt "$son" ]; do yayinla; sleep 120; done ) &

wait
yayinla
devam
