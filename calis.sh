#!/usr/bin/env bash
# GitHub'ın zamanlanmış işleri saatlik değil 4–8 saatte bir çalışabiliyor. Bu yüzden iş bir kez başlayınca
# yaklaşık 5,5 saat açık kalır: 15 dakikada bir yeni model koşusu ve gösterge var mı bakar, varsa hemen yayınlar.
# Bir sonraki zamanlanmış iş sıraya girer ve bu bitince devralır.
set -u
SURE_DK=${SURE_DK:-330}      # iş en çok bu kadar dakika sürer (GitHub sınırı 360)
BEKLE_SN=${BEKLE_SN:-900}    # turlar arası bekleme
TUR_DK=50                    # bir tur (bütün modeller yeni olursa) en çok bu kadar sürebilir
son=$(( $(date +%s) + SURE_DK * 60 ))

git clone -q --depth 1 --branch gh-pages "https://github.com/${GITHUB_REPOSITORY}" out || mkdir -p out

yayinla() {
  (
    cd out || exit 0
    if [ -d .git ] && [ -z "$(git status --porcelain -- '*/meta.json')" ]; then
      echo "Yeni veri yok"; exit 0
    fi
    rm -rf .git
    touch .nojekyll
    git init -q -b gh-pages
    git config user.name "github-actions[bot]"
    git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
    git add -A
    git commit -qm "Model verileri $(date -u +%Y-%m-%dT%H:%MZ)"
    git push -qf "https://x-access-token:${GH_TOKEN}@github.com/${GITHUB_REPOSITORY}" gh-pages && echo "Yayınlandı"
  )
}

while :; do
  echo "::group::Tur $(date -u +%H:%MZ)"
  python uret.py || echo "uret.py hata verdi; sonraki turda tekrar denenecek"
  yayinla
  echo "::endgroup::"
  simdi=$(date +%s)
  if [ $(( simdi + BEKLE_SN + TUR_DK * 60 )) -gt "$son" ]; then break; fi
  sleep "$BEKLE_SN"
done
