# Minerva108 — geliştirme komutları
# Kullanım: `make <hedef>` (örn `make test`).

.PHONY: help install test test-fast lint dev migrate stamp shell deploy clean

help:
	@echo "Minerva108 — geliştirme komutları"
	@echo ""
	@echo "  make install    Bağımlılıkları kur (.venv'e)"
	@echo "  make dev        Uvicorn'u --reload ile başlat (port 8000)"
	@echo "  make test       Tüm pytest suite'i (auth + RBAC + backup)"
	@echo "  make test-fast  Sadece backup-olmayan testler (subprocess'siz)"
	@echo "  make migrate    Alembic ile bekleyen migration'ları uygula"
	@echo "  make stamp      Mevcut DB'yi en son revision'da işle (ilk kurulum)"
	@echo "  make deploy     Test gate → push → prod restart → HTTP doğrulama"
	@echo "  make shell      Python REPL — DB session'lu (debug için)"
	@echo "  make clean      __pycache__ ve .pyc dosyalarını sil"

install:
	.venv/bin/pip install -r requirements.txt

dev:
	.venv/bin/uvicorn api_main:app --reload --port 8000 --env-file .env

test:
	.venv/bin/pytest tests/ -v

test-fast:
	.venv/bin/pytest tests/ -v --ignore=tests/test_backup.py

migrate:
	@set -a && . ./.env && set +a && \
		.venv/bin/alembic upgrade head

stamp:
	@set -a && . ./.env && set +a && \
		.venv/bin/alembic stamp head

deploy:
	./deploy.sh

shell:
	@set -a && . ./.env && set +a && \
		.venv/bin/python -c "from database import SessionLocal, Item, User; db=SessionLocal(); import code; code.interact(local=locals())"

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type f -name '*.pyc' -delete 2>/dev/null || true
	@echo "✓ pycache temizlendi"
