# siparis.minerva108.com — Distribütör Sipariş Portalı alt-alanı

> **2026-09-22:** IMS'in kendisi Turhost'tan Hetzner'e taşınıyor (bkz.
> `ops/hetzner/RUNBOOK.md`). Bu doküman DNS/nginx/certbot adımlarının Turhost'ta
> nasıl yapıldığını anlatır — aynı desen (`ops/hetzner/setup.sh` içindeki nginx
> site conf'u zaten üçünü de içerir) yeni sunucuda da geçerli; ayrı bir kurulum
> gerekmiyor, sadece cutover'da diğer iki host'la birlikte taşınıyor.


Uygulama **host-agnostik**: `siparis.*` host'undan gelen istekleri `_is_distributor_host`
algılar, kök (`/`) → `/portal`'a yönlenir, `/login` "Sipariş Portalı" markasıyla gelir.
Aşağıdaki adımlar `deploy.sh` DIŞINDA, `turhost`'ta bir kez yapılır (crm.minerva108.com ile
aynı yöntem).

## 1. DNS (alan adı sağlayıcısı)
`siparis.minerva108.com` için **A kaydı** → `136.144.251.26` (ims/crm ile aynı IP).

## 2. Nginx server block (turhost: `/etc/nginx/sites-enabled/minerva`)
crm bloğunun birebir kopyası, `server_name` = `siparis.minerva108.com`:

```nginx
server {
    listen 80;
    server_name siparis.minerva108.com;
    return 301 https://$host$request_uri;
}
server {
    listen 443 ssl http2;
    server_name siparis.minerva108.com;

    # certbot adımından sonra dolacak:
    ssl_certificate     /etc/letsencrypt/live/siparis.minerva108.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/siparis.minerva108.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;   # rate limiter için gerçek IP
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

## 3. TLS (certbot)
```bash
certbot --nginx -d siparis.minerva108.com
nginx -t && systemctl reload nginx
```
`certbot.timer` zaten aktif → otomatik yeniler.

## 4. SSO (opsiyonel, zaten crm için ayarlıysa)
Prod `.env`'de `COOKIE_DOMAIN=.minerva108.com` ise distribütör cookie'si tüm subdomain'leri
kapsar (sorun değil; distribütör yalnız `portal` yetkisine sahip, ERP sayfalarına girerse
redirect). Ayarlı DEĞİLse gerek yok — distribütör doğrudan siparis'te giriş yapar.

## 5. Doğrulama
```bash
curl -s -o /dev/null -w "%{http_code}\n" https://siparis.minerva108.com/login   # 200
```
Personel `/distributors` sayfasından bir distribütör oluşturur + fiyat girer → distribütör
siparis.minerva108.com'da giriş yapıp sipariş verir → ims `/quotations`'ta "Onay Bekliyor"
görünür → Onayla/Reddet.
