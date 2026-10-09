"""Dry-run by default; --commit grants only Işık's fason view/approval."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--commit", action="store_true", help="Yetki değişikliğini denetim kaydıyla kaydet")
    args = parser.parse_args()
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    from database import SessionLocal
    from core.outsourcing_access import grant_isik_approval
    with SessionLocal() as db:
        print(json.dumps(grant_isik_approval(db, commit=args.commit), ensure_ascii=False))


if __name__ == "__main__":
    main()
