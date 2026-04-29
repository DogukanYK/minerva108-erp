#!/bin/bash

echo "🚀 Minerva108 Canlı Sunucusuna (Turhost) deployment başlatılıyor..."

# 1. Tüm değişiklikleri Git'e ekle
git add .

# 2. Commit mesajını sor (Boş bırakılırsa varsayılan mesajı kullanır)
read -p "Commit mesajini girin (Bos birakirsaniz 'feat: auto-deploy update' olacaktir): " commit_msg
if [ -z "$commit_msg" ]; then
  commit_msg="feat: auto-deploy update"
fi

# 3. Commit ve Push işlemleri
git commit -m "$commit_msg"
git push origin main

echo "🌍 Turhost sunucusuna bağlanılıyor (136.144.251.26)..."

# 4. SSH ile bağlan, klasöre git, güncellemeyi çek ve servisi yeniden başlat
ssh root@136.144.251.26 "cd /var/www/minerva && git pull origin main && systemctl restart minerva"

echo "✅ Fırlatma Başarılı! Fabrika şu an en güncel haliyle canlıda."