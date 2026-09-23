# Minerva IMS: Turhost → Hetzner taşıma planı (sıfıra yakın kesinti)

> **Görev dağılımı (2026-09-23).** Hesap patron adına; teknik yürütme eleman;
> karar, hesaplar, hukuk ve lab iletişimi Doğukan. Gün gün takip ve tikler
> paylaşılan görev panosunda (Artifact). Özet:

| Kim | Ne yapar | Neyi yapmaz |
|---|---|---|
| Patron | Hetzner hesabını kendi adına açar (kimlik + selfie, şirket kartı), DPA'yı akdeder, Doğukan'ı ve elemanı projeye üye olarak davet eder | Teknik iş |
| Doğukan | Go/no-go ve onaylar, GitHub erişimi, Turhost DNS paneli, avukat + mali müşavir, lab/patron iletişimi, iş akışı testleri, cutover koordinasyonu | Sunucu komutları |
| Eleman | Sunucuyu oluşturur ve kurar, prova, cutover komutları, sonrası izleme ve uzak yedek | `./deploy.sh` dışında deploy, go/no-go kararı |
| Claude | Script'ler, teşhis, adım adım eşlik | Onaysız canlı müdahale |

Erişim hijyeni: eleman kendi SSH anahtarını kullanır, Hetzner'e üye daveti ile girer
(şifre paylaşımı yok), GitHub'da collaborator olur. Script'ler `hetzner-migration`
branch'inde; cutover sonrası ilk normal deploy'la main'e birleşir.

**Takvim — SIKIŞTIRILDI (23.09 gece):** Turhost VPS'i bu hafta kapanıyor (alan adı, DNS
ve e-posta Turhost'ta kalıyor). İş en geç Cuma bitecek; 7–14 günlük köprü süresi YOK,
köprü yalnız VPS kapanana kadar yaşar.
Per 24 Eyl sabah: TTL 300 (Doğukan, ilk iş), Hetzner hesabı (patron), KVKK maili →
Per gün içi: sunucu + kurulum + veri kopyası + prova + iş akışı testi →
**Per 24 Eyl 20:00 cutover** (yedek: **Cum 25 Eyl 12:50**, lab öğle arası) → sonrası izleme,
uzak yedek. Tam arşiv 23.09 23:44'te Doğukan'ın Mac'ine alındı (`~/Minerva-Turhost-Arsiv/`).
Python: zaman baskısı yüzünden yeni sunucuda canlıyla aynı **3.10** (uv ile, `/opt/uv-python`);
3.12'ye geçiş taşımadan sonra, sakin bir zamanda.

**Cutover (Per 24 Eyl akşamı; yedek Cuma 12:50 — saatler aynı sırayla kayar):**

| Saat | Kim | Ne |
|---|---|---|
| 19:30 | Doğukan | Go/no-go |
| 19:45 | Doğukan | Lab grubuna "20:00–20:15 kayıt girmeyin" |
| 20:00 | Eleman | Eski sunucuda scheduler kapat + yazma dondurma |
| 20:02 | Eleman | Son DB dökümü + dosya farkı, sayım doğrulama |
| 20:08 | Eleman | Yeni uygulama scheduler'lı başlar, yedek timer'ları açılır |
| 20:10 | Eleman | Eski nginx köprüye geçer, eski uygulama durur (yazma kesintisi biter) |
| 20:12 | Doğukan | 5 dakikalık iş akışı testi |
| 20:20 | Doğukan | Turhost panelinde üç A kaydı yeni IP'ye |
| 20:30 | Eleman | Doğrudan erişim + Shopify/Kommo webhook kontrolü |
| 20:45 | Doğukan | Lab grubuna "tamam" |

Geri dönüş kararı Doğukan'da, uygulaması elemanda. Ertesi sabah 07:45 eleman ofiste PDKS testi.

---

## Context

Minerva 108 IMS/ERP (FastAPI + Jinja2 + PostgreSQL) Turhost VPS'te canlı; lab ekibi
07:00–19:00 arası sürekli kullanıyor (üretim/lot pipeline'ı, PDKS giriş-çıkış, CRM,
sipariş portalı, Shopify/Paraşüt entegrasyonları). Sunucu Hetzner Cloud'a taşınacak.
Hedef: **lab fark etmesin** (3-5 dk yalnız yazma dondurma, okuma kesintisiz), **sıfır
veri kaybı**, her adımda geri dönüş. Sadece IMS VPS'i taşınıyor; alan adı, DNS zone'u
ve e-posta Turhost'ta kalıyor. Ek aciliyet: **PostgreSQL 14'ün desteği 2026-11-12'de
bitiyor** (7 hafta) — taşıma aynı zamanda PG 17'ye geçiş.

Kullanıcı kararları (2026-09-22): Hetzner hesabı yok, yeni açılacak; lokasyon FSN1;
cutover hafta içi akşam 20:00–23:00 TR; KVKK: teknik hazırlık devam, **canlı veri
taşıma avukat onayına bağlı**; sertleştirme tam paket (non-root servis, Cloud Firewall,
ufw, fail2ban, SSH key-only).

## Mevcut durum envanteri (canlı sunucudan salt-okunur doğrulandı, 2026-09-22)

**Sunucu:** Turhost VPS `136.144.251.26`, SSH root :23422 (`PermitRootLogin yes`),
Ubuntu 22.04.5, 4 vCPU / 7.7 GB / 194 GB (5.2 GB dolu), swap yok, ufw/fail2ban yok,
TZ UTC, locale C.UTF-8, uptime 145 gün.

**Servisler:** `minerva.service` (uvicorn tek süreç, **User=root**, 127.0.0.1:8000,
`--proxy-headers --forwarded-allow-ips=127.0.0.1`, `EnvironmentFile=/var/www/minerva/.env`,
PATH yalnız `venv/bin`, `Restart=` yok), nginx 1.18 (`sites-enabled/minerva`: 3 server
bloğu; `conf.d/minerva-rate-limits.conf`: `limit_req_zone pubimg 60r/s`, `review 10r/m`;
`client_max_body_size 520M`, `/yorum/` 12m; `/public/product-images/` regex'i
`alias /var/www/minerva/product_images/` ile diskten servis; HSTS/CSP/Permissions-Policy
header'ları), PostgreSQL 14.24 (localhost, scram-sha-256, `shared_buffers` 128 MB,
gerisi default; DB owner `postgres`, app rolü `minerva_user`), certbot 1.21 apt
(`authenticator=nginx`), `certbot.timer`, `unattended-upgrades`.

**Alan adları (hepsi bu IP'ye A kaydı, TTL 14400):** `ims.`, `crm.`, `siparis.minerva108.com`.
Üçü de ayrı Let's Encrypt sertifikası (bitiş 20/26/28 Kas 2026). DNS zone Turhost cPanel
NS'lerinde (`cpns1/cpns2.turhost.com`). Kök `minerva108.com` → Shopify + Turhost;
**MX = minerva108.com → e-posta Turhost'ta, dokunulmayacak.** Uygulama **her üç host'ta
HSTS (max-age 1 yıl, includeSubDomains)** gönderiyor (`api_main.py:96,109,140` + nginx) →
yeni sunucu DNS değişmeden ÖNCE geçerli sertifikayla ayakta olmalı.

**Veri (taşınacak state):**
| Ne | Boyut | Not |
|---|---|---|
| PostgreSQL `minerva_db` | **21 MB**, 79 tablo (hepsinde PK), 77 sequence, sadece `plpgsql`, large object yok, Alembic head `8954ef5ca750`, collation C.UTF-8 | dump/restore saniyeler |
| `drive_files/` | 484 MB | Minerva Drive yüklemeleri, DB'de yok |
| `backups/` | 248 MB | saatlik `minerva_auto_*.dump` + günlük tarball'lar, 30 gün |
| `product_images/` | 29 MB | Amazon/Walmart public (nginx alias) |
| `_sozlesmeler/` | 17 MB | untracked |
| `data/*.json` | 64 KB | `barcode_aliases`, `gs1_master`, `inci_synonyms`, `sahit_sayim_*` — git'te yok |
| `system_reports/`, `review_audio/` | 584 KB / boş | |
| `/var/backups/minerva/` | 376 KB | Mayıs `.sql.gz` arşivi |
| `minerva108.db` | untracked | legacy sqlite, güvenlik için kopyala |
| `.env` (+ `.env.bak*`) | | tüm secret'lar; **`SECRET_KEY` aynı kalırsa oturumlar kopmaz**; `COOKIE_DOMAIN` ve VAPID prod'da yok |
| `~/.ssh/id_ed25519` | | sunucunun GitHub deploy key'i (`DogukanYK/minerva108-erp`) |
| `/etc/letsencrypt/` | | accounts + live + archive + renewal — bütünüyle kopyalanacak |
| nginx conf'ları, `/root/minerva-nginx.bak.*` | | |
| systemd: `minerva-snapshot.{service,timer}` (07–19 saatlik, repo `ops/snapshot/`), `minerva-backup.{service,timer}` + `/usr/local/bin/minerva-backup.sh` (03:00 UTC, **repoda YOK**, sadece sunucuda) + aynı script için root crontab satırı | | ikisi de `backups/` dizinine yazar |

**Zamanlanmış işler (APScheduler, `AsyncIOScheduler()` → sunucu saati = UTC):** her 5 dk
IMS→Shopify stok push, her 15 dk Shopify delta (**DB'ye yazar**), her 10 dk Paraşüt retry
(**DB'ye yazar**), 08:00 CRM tarama, 09:00 SKT tarama, 03:00 tam uzlaştırma, ayın 1'i
00:30 stok snapshot + 01:00 aylık rapor. → Yeni sunucu da **UTC** olmalı; cutover ayın
1'inde yapılmamalı; **yeni sunucudaki uygulama canlıya alınana kadar `DISABLE_SCHEDULER=true`
ile çalışmalı** (yoksa prova DB'siyle Shopify'a stok basar / Paraşüt'e fatura atar).

**Gelen entegrasyonlar — hostname bazlı, IP bağımlılığı yok:** Kommo webhook
(`crm.minerva108.com/api/kommo/webhook/{secret}`), Shopify order webhook'ları
(`ims.minerva108.com/api/shopify/webhook/orders/{store}`, kayıt URL'i `x-forwarded-host`'tan
üretilir), Amazon/Walmart `product_images` URL'leri. PDKS gerçek IP'yi loopback peer'da
`X-Real-IP`'den okur (`routers/pdks.py:253-261`) → köprü (bridge) döneminde nginx realip
ayarı şart (aşağıda). Giden: Shopify Admin API, Paraşüt, Kommo — IP allowlist yok.

**Runtime:** Python 3.10.12 (lokal 3.9.6), 77 paket, pinned `requirements.txt`;
reportlab fontları `/usr/share/fonts/truetype/dejavu/` (`fonts-dejavu-core`); uygulama
`pg_dump`/`pg_restore` (`routers/backup.py:_find_pg_binary`) ve `git`
(`routers/system.py`) binary'lerini shell'den çağırır. Tests: `make test` (~398 test,
`minerva_test` DB gerekir).

**Repo'da eski IP'ye referans veren dosyalar:** `deploy.sh`, `.claude/hooks/block-adhoc-deploy.sh`,
`.codex/hooks/block-adhoc-deploy.sh`, `CLAUDE.md`, `AGENTS.md`, `HANDOFF.md`, `ops/siparis-subdomain.md`.

## Araştırma sonuçları (2026-09-22; ★ = resmi kaynaktan bizzat doğrulandı)

**Hetzner**
- ★ Fiyatlar (DE/FI, EUR/ay, KDV hariç, 15 Haz 2026 sonrası): CX33 4 vCPU/8 GB/80 GB **8.49**,
  CX43 15.99, CAX21 (Arm) 10.49, CPX32 35.49; Primary IPv4 0.50; Backups sunucu fiyatının
  %20'si. Rescale yeni fiyata geçirir → doğru tipi bir kez seç. [docs.hetzner.com price-adjustment, 2026-07-08]
- CX serisi Eylül 2026'da stok sıkıntılı (dakika bazında değişiyor). Sıra: **CX33 FSN1** →
  CX43 → CAX21 (Arm; x86 tercihine aykırı) → CPX32. Hesap onaylanınca sunucuyu hemen oluştur.
- Lokasyon: FSN1/NBG1 İstanbul'dan ~26-30 ms, HEL1 ~42 ms. Türkiye DC yok.
- Hetzner Backups: günde 1, 7 slot, zaman seçilemez, tutarlılık garantisi yok → PostgreSQL
  için tek başına yetmez; saatlik pg_dump + offsite düzeni korunur. Snapshots manuel/kalıcı.
- Cloud Firewall ücretsiz (kural yoksa inbound kapalı, NAT yok → gerçek istemci IP korunur).
- Hesap: Mart 2026'dan beri iDenfy KYC (kimlik + selfie) veya kartla ön ödeme; red nedenleri
  eksik bilgi / pahalı ilk sipariş. EUR/USD açılışta seçilir, değiştirilemez. Fatura AB dışına
  Alman KDV'siz kesilir; Türkiye'de **KDV-2 (%20 sorumlu sıfatıyla) beyanı** zorunlu, stopaj
  için mali müşavir görüşü. İtiraz: cda-review@hetzner.com.
- Offsite yedek: Storage Box BX11 (1 TB, ≈3.20 €/ay, FSN1; SSH:23 rsync/borg/restic).
- ★ **Hetzner bireysel DPA imzalamıyor**; DPA yalnız `accounts.hetzner.com/account/dpa`
  standart metniyle akdediliyor; sayfada Türkiye/KVKK yok. [docs.hetzner.com data-privacy-faq]

**KVKK / Türkiye erişimi**
- KVKK m.9 (7499 sayılı Kanun, yürürlük 2024-06-01): yurt dışına aktarım için yeterlilik
  kararı / uygun güvence (standart sözleşme, Kurul kararı 2024/959; modül: veri
  sorumlusu→veri işleyen) / istisnai hal. Açık rıza artık yalnız **arızi** aktarımlar için →
  sürekli ERP akışına dayanak olamaz. Standart sözleşme imzadan itibaren **5 iş günü** içinde
  Kurum'a bildirilir; bildirmemenin ayrı cezası var. En son teyit (Kas 2025): **hiçbir
  ülkeye yeterlilik kararı yok**. (kvkk.gov.tr bugün fetch edilemedi → canlıya geçmeden
  tekrar bak.) Hetzner standart sözleşmeyi imzalamadığından **hukuki yol avukatla
  netleşmeli** — cutover go/no-go maddesi.
- VERBİS kaydı + personel (PDKS/konum) ve müşteri (CRM) aydınlatma metinleri "Almanya /
  Hetzner" alıcı bilgisiyle güncellenmeli.
- BTK'nın Hetzner IP bloklarını engellediğine dair **belgelenmiş kayıt yok**; ancak ani
  engelleme emsalleri (VPN 2023, eSIM 2025) ve TTNET'te öğleden sonra yavaşlama anekdotları
  var → cutover öncesi 1 hafta ofis hattından latency/traceroute ölçümü; köprü (bridge)
  zaten 7-14 gün geri dönüş imkânı verir.

**Cutover tekniği**
- ★ Cloudflare partial (CNAME) setup yalnız Business/Enterprise → Free planda tüm zone
  (MX dahil) taşınmalı → **Cloudflare kullanılmayacak**, DNS Turhost'ta kalır.
- Düşük TTL tek başına yetmez (bazı resolver'lar yok sayar) → asıl mekanizma **eski
  sunucuda nginx reverse-proxy köprüsü**: eski IP'ye gelen her istek yeni sunucuya
  iletilir; eski sunucu 7-14 gün köprü olarak yaşar. TTL'i 300'e indirmek ikincil önlem
  (Turhost cPanel Zone Editor'da editable olup olmadığı panelde denenecek).
- Sertifika: `/etc/letsencrypt` bütünüyle kopyalanır → yeni nginx ilk andan geçerli
  sertifikayla çalışır (HSTS güvenli). Yenileme DNS geçtikten sonra snap certbot ile
  (`renew --dry-run`). Sertifikalar Kas 20-28'e kadar geçerli; yenileme penceresi ~Ekim
  21-29'da açılır → cutover **Ekim 20'den önce** olursa tek kopya yeter, sonra olursa eski
  sunucu yenilemiş olabilir → tekrar kopyala.
- ★ Ubuntu 24.04 nginx **1.24.0** → `http2 on;` YOK (1.25.1+), `listen 443 ssl http2;`
  sözdizimi korunur.
- Köprü + PDKS: yeni nginx'te `set_real_ip_from 136.144.251.26; real_ip_header
  X-Forwarded-For; real_ip_recursive on;` → `$remote_addr` gerçek istemci olur, `X-Real-IP`
  doğru dolar, rate-limit zone'ları da doğru IP'yi sayar.
- Yazma dondurma: nginx'te `map $request_method` ile POST/PUT/PATCH/DELETE → 503 +
  `Retry-After`, GET açık. Shopify/Kommo webhook'ları 503 alınca yeniden dener.

**PostgreSQL**
- ★ PG14 EOL **2026-11-12**; PG16 2028-11-09; PG17 2029-11-08; PG18 (18.6) 2030-11-14.
  Ubuntu 24.04 deposu PG16; PGDG (`apt.postgresql.org.sh`) ile 17/18. **Karar: PG17**
  (2 yıl olgun, 3 yıl destek).
- ★ Yöntem: **dump/restore + kısa yazma dondurma** (logical replication 21 MB için
  değmez: eski PG'de `wal_level=logical` restart ister, sequence/DDL taşımaz, ek hata
  yüzeyi). Yeni sunucudaki **pg_dump 17** SSH tüneliyle eski PG14'e bağlanır (yeni client
  eski sunucudan dump alabilir, tersi olmaz). `-Fc --no-owner --no-acl`; rol elle
  (`CREATE ROLE minerva_user`); DB `LOCALE 'C.UTF-8' TEMPLATE template0` (glibc
  2.35→2.39 collation değişimi dump/restore'da index'ler yeniden kurulduğu için sorun
  değil; C.UTF-8 zaten en az etkilenen locale); restore sonrası `ANALYZE`.
- Uygulamanın backup endpoint'i için `postgresql-client-17` binary'si `/usr/bin/pg_dump`
  (pg_wrapper) üzerinden bulunur — `_find_pg_binary` değişmeden çalışır.
- postgresql.conf (8 GB): `shared_buffers=2GB`, `effective_cache_size=6GB`,
  `work_mem=16MB`, `maintenance_work_mem=512MB`, `wal_compression=on`.

**Runtime**
- Tüm pin'lerin Python 3.12 wheel'i var (psycopg2-binary 2.9.9 cp312, pandas 2.2.3,
  pydantic 2.10.3, bcrypt abi3, uvicorn 0.32.1) → **Python 3.12 venv** (PEP 668: sistem
  pip yok). Test suite 3.12'de kırmızıysa fallback `uv python install 3.10`.
- ★ python-jose 3.3.0'da CVE-2024-33663/33664 (JWT); güncel sürüm 3.5.0 (Mayıs 2025).
  **Taşıma kapsamı dışında, ayrı PR** (aynı restart'a bindirmeyelim).
- certbot snap (apt 1.21 EFF tarafından güncellenmiyor); `fonts-dejavu-core` aynı yol;
  `needrestart` 24.04'te servisleri otomatik restart edebilir → `$nrconf{restart}='l'`;
  unattended-upgrades reboot kapalı kalır, kernel güncellemeleri mesai dışı elle.

**Doğrulama durumu:** ★ işaretliler resmi kaynaktan teyit edildi. Teyit edilemeyenler:
KVKK yeterlilik kararı canlı durumu (site erişilemedi), CX33 anlık stok, Storage Box
güncel fiyatı, Turhost panelde TTL düzenlenebilirliği, python-jose fix'in tam sürümü.

## Önerilen yaklaşım (özet)

1. Yeni sunucuyu (CX33 FSN1, Ubuntu 24.04, PG17, Python 3.12, sertleştirilmiş) **tam
   olarak** kur, sertifikaları ve `.env`'i kopyala, prova verisiyle çalıştır, test suite'i
   ve smoke testleri geçir. Bu aşamada kişisel veri kalıcı taşınmaz (prova DB'si silinir).
2. Cutover gecesi (20:00-23:00 TR): eski sunucuda scheduler'ı kapat + nginx yazma
   dondurma → son `pg_dump` + `rsync --delete` → yeni sunucuda restore + sayım doğrulama →
   yeni uygulamayı scheduler'lı başlat → eski nginx'i **köprü** moduna al (dondurma biter,
   toplam ~3-5 dk yazma kesintisi, okuma hiç kesilmez) → eski app/timer'ları durdur → DNS A
   kayıtlarını değiştir.
3. Köprü 7-14 gün açık; eski access log'da trafik sıfırlanınca Turhost'u (snapshot alıp)
   kapat. Offsite yedek (Storage Box + borg) ve repo/doküman güncellemeleri.

Geri dönüş: DNS değişmeden önce her adım → eski nginx normal conf'a (symlink swap +
reload), eski app'i normal başlat; eski DB'ye dondurma süresince yazılmadığı için veri kaybı
yok. DNS sonrası → A kayıtlarını geri al + köprüyü kaldır + yeni→eski ters dump/restore
(aynı script, tersine).

## Runbook

### Faz 0 — Ön hazırlık (T-14 gün → T-7)
- [ ] **Hetzner hesabı** (patron/şirket adına): şirket unvanı + adres + VKN eksiksiz, **EUR**,
      iDenfy için kimlik + selfie hazır, 3D Secure kurumsal kart; ilk sipariş tek CX33.
      Onaylanınca `accounts.hetzner.com/account/dpa`'dan DPA'yı akdet. Mali müşavire
      KDV-2 + stopaj notu.
- [ ] **KVKK paralel iş** (avukat): Hetzner'in standart sözleşme imzalamadığı bilgisiyle
      uygun güvence yolu; aydınlatma metinleri + VERBİS; kvkk.gov.tr yeterlilik listesi
      canlı kontrol. Sonuç "go" olmadan **Faz 3 başlamaz**.
- [ ] **Turhost DNS paneli**: ims/crm/siparis A kayıtlarında TTL alanı düzenlenebilir mi →
      dene; değilse Turhost destek bileti (köprü zaten TTL'den bağımsız çalışır).
- [ ] **Ağ ölçümü**: ofis hattından 1 hafta `ping/mtr fsn.icmp.hetzner.com` (BTK/ISS riski).
- [ ] **Repo değişiklikleri** (bu makine, ayrı branch, prod'a etkisiz):
      - `ops/hetzner/` klasörü: `cloud-init.yaml`, `setup.sh` (paketler, PGDG PG17, kullanıcı,
        dizinler, swap, fail2ban, needrestart), `minerva.service` (sertleştirilmiş, aşağıda),
        `nginx/minerva` (mevcut site conf + realip snippet include), `nginx/minerva-rate-limits.conf`
        (kopya), `nginx/realip-bridge.conf`, `old-server/freeze-on.conf` + `freeze-off.conf`
        (write-freeze snippet), `old-server/minerva-bridge` (köprü site conf),
        `migrate/dump_restore.sh`, `migrate/rsync_files.sh`, `migrate/verify_counts.sh`,
        `RUNBOOK.md` (bu runbook).
      - `ops/backup/`: sunucudaki `minerva-backup.sh` + `.service/.timer`'ı repoya al;
        `ops/snapshot/minerva-snapshot.service` ve backup service → `User=minerva`
        (backups/ dosyaları app kullanıcısınca okunabilsin).
      - `deploy.sh`: `SERVER_IP`, `SSH_KEY` yeni değerler; remote blokta git/pip komutları
        `sudo -u minerva -H` ile (repo `minerva:minerva` sahipli); geri kalan akış aynı.
      - `.claude/hooks/block-adhoc-deploy.sh` + `.codex/hooks/…`: regex'e `hetzner|<YENİ_IP>` ekle.
      - `CLAUDE.md`, `AGENTS.md`, `HANDOFF.md`, `ops/siparis-subdomain.md`: Deploy bölümü yeni host.
      - `~/.ssh/config`: `Host hetzner` alias (repo dışı).
      Sertleştirilmiş unit özü: `User=minerva Group=minerva`, `EnvironmentFile=/var/www/minerva/.env`,
      `Environment=PATH=/var/www/minerva/venv/bin:/usr/bin:/bin`, aynı `ExecStart`
      (`--forwarded-allow-ips=127.0.0.1` aynen), `Restart=always`, `NoNewPrivileges=true`,
      `PrivateTmp=true`, `ProtectSystem=strict`, `ProtectHome=true`, `ReadWritePaths=/var/www/minerva`.

### Faz 1 — Yeni sunucu kurulumu (T-7)
- [ ] `hcloud` (brew) veya Console: Firewall (in: 80/443 any, 23422 ofis IP + evin IP'si,
      ICMP), Primary IPv4, **CX33 fsn1 ubuntu-24.04 `--enable-backup`** `--user-data-from-file
      ops/hetzner/cloud-init.yaml` (kullanıcı `minerva`, SSH port 23422, `PermitRootLogin
      prohibit-password`, ufw, fail2ban, unattended-upgrades, TZ UTC). Stok yoksa fallback sırası.
- [ ] `setup.sh`: nginx, PGDG → `postgresql-17` + `postgresql-client-17`, `python3.12-venv`,
      git, `fonts-dejavu-core`, snap certbot; 2 GB swapfile; `needrestart` list-only.
- [ ] PostgreSQL: cluster locale `C.UTF-8` doğrula (`SHOW lc_collate`), `postgresql.conf`
      değerleri, `CREATE ROLE minerva_user LOGIN PASSWORD '<.env'deki>'`,
      `CREATE DATABASE minerva_db OWNER minerva_user LOCALE 'C.UTF-8' TEMPLATE template0`,
      ayrıca `minerva_test` (test suite için).
- [ ] Uygulama: `minerva` kullanıcısı için ed25519 anahtar → GitHub **deploy key** (read-only);
      `/var/www/minerva` clone (main), Python 3.10 venv (uv, `/opt/uv-python`, `--seed`), `pip install -r
      requirements.txt`; eski sunucudan `pip freeze` çıktısıyla fark karşılaştır.
- [ ] Eski sunucudan rsync (ilk tam kopya; salt-okunur kaynak): `.env` (+ `.env.bak*`),
      `drive_files/ backups/ product_images/ _sozlesmeler/ data/ system_reports/
      review_audio/`, `minerva108.db`, `/etc/letsencrypt/`, nginx conf yedekleri,
      `/var/backups/minerva/`. `chown -R minerva:minerva /var/www/minerva`.
- [ ] Yeni `.env`'e **`DISABLE_SCHEDULER=true` ekle** (cutover'a kadar kalacak).
- [ ] nginx: site conf + rate-limits + realip snippet; `nginx -t`; sertifikalar kopyadan
      geliyor → 443 hemen çalışır. systemd: `minerva.service` + snapshot/backup timer'ları
      (timer'lar prova sırasında **disabled**, cutover'da enable).
- [ ] Test paketi sunucuda ÇALIŞTIRILMAZ: `tests/conftest.py` sabit bir yerel test şifresiyle
      (`minerva_user`) bağlanır, prod rolünün şifresi farklıdır. Python 3.12 uyumu elemanın
      kendi makinesinde (Gün 1) tam test paketiyle doğrulanır; sunucuda smoke test yapılır.
      Kırmızı test çıkarsa fallback `uv python install 3.10`.

### Faz 2 — Prova (T-3)
- [ ] `migrate/dump_restore.sh` prova: yeni sunucudan `ssh -L 15432:127.0.0.1:5432 turhost` +
      `pg_dump -Fc --no-owner --no-acl` (PG17 client) → `pg_restore -U minerva_user --no-owner
      --no-acl -d minerva_db` → `verify_counts.sh` (79 tabloda `count(*)` eski vs yeni,
      `alembic_version`, sequence `last_value`'lar) → `ANALYZE`. **Süreyi ölç** (hedef <60 sn).
- [ ] `migrate/rsync_files.sh` delta prova (`-aHAX --numeric-ids --delete -n` sonra gerçek).
- [ ] Mac'te `/etc/hosts` ile üç host'u yeni IP'ye yönlendirip smoke test: login (aynı
      SECRET_KEY → mevcut cookie geçerli), ana sayfalar, PDF rapor (fontlar), Excel export,
      Drive indirme, `product_images` public URL, `/api/system/health` (pg_dump bulunuyor,
      scheduler **kapalı** görünmeli), backup endpoint'ten manuel dump, CRM ve siparis host
      yönlendirmeleri, PDKS sayfası.
- [ ] Köprü (realip) provası. Yeni sunucuda realip conf'u AÇ ve Turhost iptaline kadar açık
      bırak (yalnız eski sunucu IP'sine güvenir, zararsız):
      `mv /etc/nginx/conf.d/minerva-realip-bridge.conf.DISABLED /etc/nginx/conf.d/minerva-realip-bridge.conf && nginx -t && systemctl reload nginx`.
      Eski sunucudan (salt-okunur istek, eski nginx'e dokunmaz):
      `curl -sk -o /dev/null -w '%{http_code}\n' --resolve ims.minerva108.com:443:<YENİ_IP> -H "X-Forwarded-For: 203.0.113.7" https://ims.minerva108.com/login`
      → yeni sunucuda `tail -1 /var/log/nginx/access.log` satırı `203.0.113.7` ile başlamalı
      (nginx gerçek istemci IP'sini zincirden aldı; PDKS'nin okuduğu `X-Real-IP` bu değerdir).
      Aynı isteği Mac'ten (eski sunucu dışından) atınca satır Mac'in IP'siyle başlamalı —
      dışarıdan gelen sahte başlığa güvenilmez.
- [ ] Turhost panelde TTL 300 (T-48 saat). Rollback provası: eski sunucuda `freeze-on` →
      `freeze-off` symlink swap + `nginx -t && nginx -s reload` kuru çalıştırma (mesai dışı, saniyelik).
- [ ] Prova DB'sini sıfırla (`DROP/CREATE DATABASE`) — kişisel veri kalıcı kalmasın (KVKK).

### Faz 3 — Cutover (T-0, hafta içi 20:00-23:00 TR; ayın 1'i değil)
Go/no-go: KVKK "go" ✔, testler yeşil ✔, prova <60 sn ✔, TTL 300 ≥24 saat ✔, yeni nginx
sertifikaları geçerli ✔, son saatlik snapshot alınmış ✔, Hetzner snapshot alındı ✔.
İki sunucudaki komutları **eleman kendi terminalinden** çalıştırır (proje hook'u yalnız
Claude'un turhost'ta `systemctl restart` koşmasını engeller). Önkoşul (prova haftası):
`render.sh <YENİ_IP>` çıktıları ve `minerva-freeze-map.conf` eski sunucuda yerinde,
`nginx -t` temiz; realip conf yeni sunucuda açık.
1. **Eski sunucu, 20:00** (interaktif editör yok, geçici drop-in; reboot'ta kaybolur):
   `mkdir -p /run/systemd/system/minerva.service.d && printf '[Service]\nEnvironment=DISABLE_SCHEDULER=true\n' > /run/systemd/system/minerva.service.d/cutover.conf && systemctl daemon-reload && systemctl restart minerva`
   (~3 sn; scheduler yazmaları biter; eski `.env`'e dokunulmaz) →
   `ln -sfn /etc/nginx/sites-available/minerva-freeze /etc/nginx/sites-enabled/minerva && nginx -t && nginx -s reload`
   (yazma dondurma başlar; GET çalışır).
2. **Yeni sunucu**: `systemctl stop minerva` → `dump_restore.sh cutover` (DROP/CREATE +
   restore + ANALYZE) → `verify_counts.sh minerva_db` (fark = DUR) →
   `rsync_files.sh final` (drive_files, product_images, _sozlesmeler, data, system_reports,
   backups, review_audio, .env; eski `.env`'de `DISABLE_SCHEDULER` yok, yeni `.env` onunla
   ezilir → scheduler açık gelir; `grep DISABLE_SCHEDULER /var/www/minerva/.env` boş olmalı)
   → `systemctl start minerva` →
   `curl -sk -o /dev/null -w '%{http_code}\n' --resolve ims.minerva108.com:443:127.0.0.1 https://ims.minerva108.com/login`
   200 → `systemctl enable --now minerva-snapshot.timer minerva-backup.timer`.
3. **Eski sunucu**: `sites-enabled/minerva` → `minerva-bridge` symlink, `nginx -t`,
   `nginx -s reload` (dondurma biter, trafik yeni sunucuya akar) → `systemctl stop minerva
   minerva-snapshot.timer minerva-backup.timer` + crontab satırını yorumla. **Toplam yazma
   kesintisi: adım 1 başı → adım 3 sonu, hedef 3-5 dk.**
4. Doğrulama (köprü üzerinden): login, kayıt oluştur/sil, ofisten PDKS `ip_allowed` doğru
   (realip zinciri), Shopify `webhooks/setup` çağrısı "zaten kurulu" demeli, Kommo test
   webhook'u, `/api/system/health` scheduler job'ları listeli.
5. **DNS**: Turhost panelde ims/crm/siparis A → yeni IP. `dig +short` ile doğrula.
6. Doğrudan (köprüsüz) erişim testi: Mac `/etc/hosts` ile; mobil veriden.

### Faz 4 — Sonrası (T+1 → T+14)
- [ ] T+1: `certbot renew --dry-run` (DNS geçti, HTTP-01 çalışır); eski access log'da
      köprü trafiğini izle; `journalctl -u minerva` hata taraması; Shopify 5 dk sync'in
      yeni sunucudan çalıştığını IMS'ten doğrula.
- [ ] Storage Box BX11 + borg append-only (saatlik dump + drive_files + _sozlesmeler),
      cron/timer olarak; `ops/hetzner/offsite/`.
- [ ] TTL 14400'e geri; köprü trafiği 0 olunca (≥7 gün): Turhost'ta son snapshot/tar al,
      yeni sunucu nginx'inden realip-bridge satırlarını kaldır, Turhost VPS'i iptal et.
- [ ] `deploy.sh` ile ilk gerçek deploy (test gate + HTTP doğrulama) — repo değişiklikleri
      main'e bu deploy ile gider. Memory/doküman güncellemesi.
- [ ] Ayrı PR: python-jose ≥3.5.0.

## Kritik tuzaklar (özet)
- Yeni sunucudaki uygulama **cutover'a kadar `DISABLE_SCHEDULER=true`** — aksi hâlde prova
  verisiyle Shopify stok push / Paraşüt fatura atar.
- Cutover'da eski uygulama scheduler'ı **önce** kapatılmalı; dondurma nginx'te olduğu için
  APScheduler yazmaları (15 dk delta, 10 dk Paraşüt) nginx'i atlar.
- Köprü açıkken eski uygulama **durmalı** (stale DB ile Shopify'a stok basmasın).
- `http2 on;` yazma (nginx 1.24). `{n,m}` içeren regex çift tırnakta kalsın (mevcut TUZAK).
- Sertifika kopyası `accounts/` dahil; `renew --dry-run` DNS'ten **sonra**.
- `ProtectSystem=strict` → `ReadWritePaths=/var/www/minerva` olmadan uploads yazamaz.
- backups/ ve timer'lar `minerva` kullanıcısı olarak çalışmalı (root:600 dosyaları app okuyamaz).
- Prova DB'si cutover'dan önce silinmeli (KVKK).

## Doğrulama
- Yeni sunucu canlıyla aynı Python 3.10'u kullanır; test paketi zaten bu sürümle deploy.sh kapısında geçiyor (sunucuda test koşulmaz, bkz. Faz 1).
- `verify_counts.sh`: 79 tablo sayımı eski == yeni, `alembic_version` = `8954ef5ca750`,
  sequence'ler tablo max(id) ≥.
- `/etc/hosts` smoke listesi (Faz 2) + köprü üzerinden aynı liste (Faz 3.4) + DNS sonrası
  doğrudan (Faz 3.6).
- PDKS: ofis IP'sinden check-in "ip_allowed" hem köprü hem doğrudan yolda doğru.
- `curl -sI https://ims.minerva108.com/login | grep -iE "strict|permissions|server"` → HSTS,
  Permissions-Policy `camera=(self)`, nginx.
- `/api/system/health`: pg_dump bulundu, scheduler job listesi, son backup zamanı ilerliyor.
- `certbot renew --dry-run` OK; `systemctl list-timers` snapshot/backup/certbot aktif.
- `./deploy.sh` uçtan uca (test gate → push → sudo -u minerva sync → restart → HTTP 200).
