from database import SessionLocal, init_db, User
from core.auth import hash_password


def seed():
    init_db()
    db = SessionLocal()
    try:
        if db.query(User).count() == 0:
            admin = User(
                username="admin",
                password_hash=hash_password("minerva108"),
                full_name="Süper Yönetici",
                role="admin",
                is_active=True,
            )
            db.add(admin)
            db.commit()
            print("✅ Admin kullanıcısı oluşturuldu. Kullanıcı: admin / Şifre: minerva108")
        else:
            print("ℹ️  Veritabanında zaten kullanıcı mevcut. Seed atlandı.")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
