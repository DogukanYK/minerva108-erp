import os
import urllib.request
import glob

# 1. Kütüphaneyi yerel static klasörüne indir
lib_url = "https://unpkg.com/html5-qrcode"
local_path = "static/html5-qrcode.min.js"

print("Barkod kütüphanesi indiriliyor...")
req = urllib.request.Request(lib_url, headers={'User-Agent': 'Mozilla/5.0'})
with urllib.request.urlopen(req) as response, open(local_path, 'wb') as out_file:
    out_file.write(response.read())
print(f"Başarılı: {local_path} kaydedildi.")

# 2. 12 Template içindeki CDN linkini Yerel link ile değiştir
templates = glob.glob("templates/*.html")
cdn_strings = [
    "https://unpkg.com/html5-qrcode",
    "https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js", # Olası versiyonlu hali
    "https://cdnjs.cloudflare.com/ajax/libs/html5-qrcode/2.3.8/html5-qrcode.min.js"
]

for tmpl in templates:
    with open(tmpl, 'r', encoding='utf-8') as f:
        content = f.read()
    
    modified = False
    for cdn in cdn_strings:
        if cdn in content:
            # Sadece CDN URL'sini kendi lokal URL'miz ile değiştir
            content = content.replace(cdn, "/static/html5-qrcode.min.js")
            modified = True
            
    if modified:
        with open(tmpl, 'w', encoding='utf-8') as f:
            f.write(content)
        print(f"Güncellendi: {tmpl}")

print("Tüm şablonlar yerel kütüphaneye bağlandı. Operasyon Tamam!")
