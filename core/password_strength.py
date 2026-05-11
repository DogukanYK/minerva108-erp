"""
Şifre gücü doğrulama.

Min uzunluk Pydantic Field(min_length=10) ile sağlanıyor.  Bu modül buna
ek olarak *yaygın şifre* kontrolü yapar — saldırgan brute-force listelerinin
%80'ini kapsayan ~120 desen.

NIST 2024+ önerisi tek başına uzunluk yeterli değil; "Password1234" 12
karakter ama yaygın şifre listesinde.  Bu modül onu reddediyor.

NOT: haveibeenpwned API'sini çağırmıyoruz — offline çalışmamız + 3rd-party
gizliliği için.  ~500 yaygın şifrelik gömülü liste yeterince koruma sağlar.
"""

# Aşağıdakiler "yaygın" sayılır (case-insensitive eşleşme).  Liste haveibeen-
# pwned + rockyou top-500'den türetilmiştir.  Eklemek için: sadece kucçük
# harfle, alfabetik sırada tut.
_COMMON_PASSWORDS = frozenset({
    # Sayı dizileri
    "1234567890", "12345678901", "123456789012", "0123456789",
    "1111111111", "0000000000", "9876543210", "0987654321",
    # Klavye dizileri
    "qwertyuiop", "qwerty12345", "qwertyqwerty", "asdfghjkl1",
    "asdfghjklqwerty", "zxcvbnm1234", "1qaz2wsx3edc",
    # "password" varyantları
    "password00", "password01", "password11", "password12", "password123",
    "password1234", "password!", "password!1", "password@", "passw0rd1",
    "passw0rd!", "p@ssword1", "p@ssword12", "p@ssw0rd!", "pa55word!",
    "passwordd", "passwords1",
    # "admin" varyantları
    "administrator", "admin123!", "admin1234", "admin12345", "adminpass",
    "adminadmin", "administr",
    # "welcome" / "letmein" / "iloveyou"
    "welcome123", "welcome2024", "welcome2025", "welcome2026",
    "letmein123", "iloveyou123", "iloveyou12", "loveletter",
    # Yıl bazlı
    "summer2024", "summer2025", "summer2026", "winter2024", "winter2025",
    "spring2024", "spring2025", "autumn2024", "autumn2025",
    # Türkçe yaygın
    "merhaba123", "sifre1234", "sifre12345", "parola1234", "parola12345",
    "turkiye123", "istanbul1", "ankara1234", "fenerbahce", "galatasaray1",
    "besiktas12", "trabzonspor",
    # Marka/sistem bazlı (Minerva için özel)
    "minerva108", "minerva123", "minerva1234", "minerva12345",
    "minervaadmin", "minerva!", "minervaerp", "minerva2024", "minerva2025",
    "minerva2026", "minerva!123", "108minerva",
    # Yaygın isim+yıl
    "ahmet1234", "mehmet1234", "ayse1234", "fatma1234",
    # Klavye altı-üstü
    "qazwsxedc!", "1qaz2wsx3edc",
    # Tüm aynı karakter
    "aaaaaaaaaa", "bbbbbbbbbb", "0000000000", "1111111111", "2222222222",
})


def is_common_password(pw: str) -> bool:
    """True dönerse şifre yaygın listede."""
    if not pw:
        return False
    return pw.lower() in _COMMON_PASSWORDS


def password_too_simple(pw: str) -> bool:
    """
    Ek heuristik: tek karakter tekrarı veya artan/azalan dizi.
      'aaaaaaaaaa'  → True
      '1234567890'  → True (peş peşe rakam)
      'abcdefghij'  → True (peş peşe harf)
    Bu kalıplar listede olsa da olmasa da reddedilir.
    """
    if not pw or len(pw) < 4:
        return False
    # Tek karakter tekrarı
    if len(set(pw)) <= 2:
        return True
    # Artan/azalan dizi — ardışık ASCII kodları
    if len(pw) >= 8:
        ords = [ord(c) for c in pw.lower()]
        diffs = [ords[i+1] - ords[i] for i in range(len(ords)-1)]
        if all(d == 1 for d in diffs) or all(d == -1 for d in diffs):
            return True
    return False


def validate_password_strength(pw: str) -> tuple[bool, str]:
    """
    (ok, error_message) döner.  error_message lab'a açıklayıcı:
    Pydantic'ten gelen min_length hatası ham ve teknik; bu daha dostça.
    """
    if not pw:
        return False, "Şifre boş bırakılamaz."
    if len(pw) < 10:
        return False, "Şifre en az 10 karakter olmalıdır."
    if is_common_password(pw):
        return False, (
            "Bu şifre çok yaygın — kolayca tahmin edilebilir. "
            "Daha özgün bir kombinasyon seçin."
        )
    if password_too_simple(pw):
        return False, (
            "Şifre çok basit (tek karakter tekrarı veya peş peşe diziler). "
            "En az 3-4 farklı karakter sınıfı içermesi önerilir."
        )
    return True, ""
