#!/usr/bin/env python3
# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Minerva Asistan — önbellek sondası.  GERÇEK API, GERÇEK HARCAMA (≈ 0,15 $,
Opus 5.5; yalnız kullanıcı onayıyla çalıştırılır — `--yes` verilmezse sorar).

Ne doğrular (prompt-caching.md "verify after every change" kuralı):
  A) Canlı istek yapısıyla (`core.assistant.runner.build_params` — tools →
     system[1h breakpoint] → messages, `tool_choice` auto, Opus 5.5 beta
     alanları) BİR soru gönderir; usage alanlarını basar.  Beklenen:
     `cache_creation` (1h) > 0 = statik önek (araçlar + kurallar + SSS).
  B) Byte-aynı ikinci istek.  Beklenen: `cache_read_input_tokens` > 0.
     0 ise sessiz bir önbellek bozucu var (tarih/isim/sırasız JSON…) →
     çıkış kodu 1.  Bu, canlıdaki her istek için fazladan ~0,05 $ demektir.
  C) B'nin asistan turunu (thinking blokları + imzaları, SDK `to_dict()`
     ile BİREBİR — history.append_rows'un kalıcılaştırdığı biçim) geçmişe
     ekleyip ikinci bir soru sorar: preserved-thinking replay'i 400 vermiyor
     ve önbellek okuması sürüyor mu.  `--conversation-id N` verilirse geçmiş
     bu betiğin turu yerine DB'deki kayıtlı sohbetten (`history.api_messages`,
     `DATABASE_URL` gerekir) alınır — canlı bir sohbetin replay'ini sınar.

Kullanım (önce `.env` kaynaklanır — `core.assistant.tools` `database`'i import
eder, o da SECRET_KEY/DATABASE_URL'i import anında ister; Makefile kalıbı):
  set -a && . ./.env && set +a && \
  ANTHROPIC_API_KEY=sk-… .venv/bin/python scripts/assistant_cache_probe.py [--yes]
      [--model claude-opus-5-5] [--effort low] [--ttl 1h] [--safe-mode]
      [--conversation-id N] [--save sonda.json]

Çıkış kodları: 0 ok · 1 ikinci istekte önbellek okuması YOK · 2 anahtar/argüman/
onay yok · 3 API hatası (400 metni basılır; beta alanları reddedildiyse
`--safe-mode` ile yinele — runner da aynı 400'de tek seferlik safe-mode'a geçer).

Testlerde ÇALIŞTIRILMAZ (ağ + harcama); DB'ye yazmaz (assistant_usage'a kayıt
yok — bu harcama /admin/usage raporunda GÖRÜNMEZ, bilinçli).
"""
import argparse
import asyncio
import datetime as _dt
import json
import os
import sys
import time
from typing import List

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROBE_QUESTION_1 = "Merhaba. Araç çağırmadan, üç kısa maddeyle neler yapabildiğini söyle."
PROBE_QUESTION_2 = "Teşekkürler. Şimdi tek cümleyle: hangi konularda yardım edemezsin?"
EST_COST_USD = 0.15


def _ctx():
    """DB'siz, sabit oturum bağlamı (izin yok → araç listesi deterministik)."""
    from core.assistant.tools import ToolContext
    return ToolContext(user_id=0, username="sonda", full_name="Önbellek Sondası", role="Staff",
                       perms={}, domain="cosmetics", can_finance=False, role_label="Personel")


def _fmt_usage(u) -> str:
    from core.assistant.budget import usage_fields
    f = usage_fields(u)
    return ("input={input_tokens} cache_write={cache_creation_input_tokens} "
            "(1h={cache_creation_1h_tokens} 5m={cache_creation_5m_tokens}) "
            "cache_read={cache_read_input_tokens} output={output_tokens}").format(**f)


async def _call(client, settings, messages: List[dict], label: str, safe_mode: bool) -> dict:
    """Runner ile aynı yol: `beta.messages.stream(**build_params(...))`,
    request-id `stream.request_id`'den, mesaj `get_final_message()`."""
    from core.assistant import runner
    from core.assistant.budget import compute_cost

    params = runner.build_params(settings, messages, safe_mode=safe_mode)
    t0 = time.perf_counter()
    text_parts: List[str] = []
    async with client.beta.messages.stream(**params) as stream:
        async for ev in stream:
            if getattr(ev, "type", None) == "text" and getattr(ev, "text", ""):
                text_parts.append(ev.text)
        msg = await stream.get_final_message()
        rid = getattr(stream, "request_id", None)
    ms = int((time.perf_counter() - t0) * 1000)
    cost = compute_cost(settings.model, msg.usage)
    kinds = [getattr(b, "type", "?") for b in msg.content]
    print(f"[{label}] {ms} ms · request_id={rid} · stop={msg.stop_reason} · model={msg.model}")
    print(f"[{label}] usage: {_fmt_usage(msg.usage)} · maliyet ≈ ${cost:.4f}")
    print(f"[{label}] bloklar: {kinds}")
    print(f"[{label}] metin: {''.join(text_parts).strip()[:300]!r}")
    return {"label": label, "request_id": rid, "latency_ms": ms, "stop_reason": msg.stop_reason,
            "cost_usd": cost, "usage": msg.usage.to_dict() if hasattr(msg.usage, "to_dict") else {},
            "content": [b.to_dict() for b in msg.content]}


def _db_history(conv_id: int) -> List[dict]:
    from database import AssistantConversation, SessionLocal
    from core.assistant import history
    db = SessionLocal()
    try:
        conv = db.get(AssistantConversation, conv_id)
        if conv is None:
            raise SystemExit(f"Sohbet bulunamadı: {conv_id}")
        rows = history.api_messages(db, conv)
    finally:
        db.close()
    if not any(m["role"] == "assistant" for m in rows):
        raise SystemExit(f"Sohbet {conv_id} içinde asistan turu yok.")
    return rows


async def _main(args) -> int:
    import anthropic
    from core.assistant import client as client_mod
    from core.assistant.settings import Settings
    from core.assistant import runner

    try:
        client = client_mod.get_client()
    except client_mod.NotConfigured:
        print("ANTHROPIC_API_KEY tanımlı değil.", file=sys.stderr)
        return 2
    settings = Settings(model=args.model, effort=args.effort, cache_ttl=args.ttl)
    ctx = _ctx()
    today = _dt.datetime(2026, 1, 5, 9, 0, 0)       # sabit: iki istek byte-aynı kalsın
    ctx_msg = runner.context_message(ctx, settings, today)
    q1 = {"role": "user", "content": [{"type": "text", "text": PROBE_QUESTION_1}]}
    q2 = {"role": "user", "content": [{"type": "text", "text": PROBE_QUESTION_2}]}
    report = {"model": settings.model, "effort": settings.effort, "ttl": settings.cache_ttl, "calls": []}
    rc = 0
    try:
        a = await _call(client, settings, [q1, ctx_msg], "A ilk istek", args.safe_mode)
        report["calls"].append(a)
        ua = a["usage"]
        one_h = ((ua.get("cache_creation") or {}).get("ephemeral_1h_input_tokens")
                 if isinstance(ua.get("cache_creation"), dict) else None)
        static_prefix = one_h if one_h else ua.get("cache_creation_input_tokens", 0)
        print(f"[A] statik önek (önbelleğe yazılan) ≈ {static_prefix} token")
        if not ua.get("cache_creation_input_tokens") and not ua.get("cache_read_input_tokens"):
            print("[A] UYARI: ne yazma ne okuma — önek 512 token'ın altında mı, yoksa "
                  "önbellek bu platformda kapalı mı?", file=sys.stderr)

        b = await _call(client, settings, [q1, ctx_msg], "B özdeş istek", args.safe_mode)
        report["calls"].append(b)
        read_b = int(b["usage"].get("cache_read_input_tokens") or 0)
        if read_b <= 0:
            print("[B] HATA: cache_read_input_tokens == 0 — önek her istekte değişiyor "
                  "(sessiz önbellek bozucu).  İki isteğin tools/system baytlarını karşılaştır.",
                  file=sys.stderr)
            rc = 1
        elif static_prefix and read_b < static_prefix:
            print(f"[B] UYARI: okuma ({read_b}) statik önekten ({static_prefix}) küçük — "
                  "kısmi isabet.", file=sys.stderr)
        else:
            print(f"[B] OK: {read_b} token önbellekten okundu.")

        if args.conversation_id:
            hist = _db_history(int(args.conversation_id))
            print(f"[C] DB sohbeti {args.conversation_id}: {len(hist)} mesaj replay ediliyor")
        else:
            hist = [q1, ctx_msg, {"role": "assistant", "content": b["content"]}]
        n_thinking = sum(1 for m in hist if m["role"] == "assistant"
                         for blk in (m["content"] if isinstance(m["content"], list) else [])
                         if isinstance(blk, dict) and blk.get("type") == "thinking")
        print(f"[C] replay edilen thinking bloğu: {n_thinking}"
              + ("" if n_thinking else " (model bu turda thinking döndürmedi — imza testi boş)"))
        c = await _call(client, settings, hist + [q2, ctx_msg], "C replay + 2. soru", args.safe_mode)
        report["calls"].append(c)
        read_c = int(c["usage"].get("cache_read_input_tokens") or 0)
        if read_c <= 0:
            print("[C] UYARI: replay'de önbellek okuması yok.", file=sys.stderr)
        else:
            print(f"[C] OK: replay 400 vermedi, {read_c} token önbellekten okundu.")
    except anthropic.BadRequestError as e:
        print(f"API 400: {e}", file=sys.stderr)
        if not args.safe_mode and any(m in str(e).lower() for m in runner._SAFE_MODE_MARKERS):
            print("İpucu: beta alanları reddedildi → `--safe-mode` ile yinele.", file=sys.stderr)
        return 3
    except anthropic.AuthenticationError:
        print("API anahtarı geçersiz.", file=sys.stderr)
        return 3
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        print(f"API hatası: {e}", file=sys.stderr)
        return 3
    finally:
        total = sum(c["cost_usd"] for c in report["calls"])
        print(f"Toplam harcama ≈ ${total:.4f} ({len(report['calls'])} çağrı)")
        if args.save:
            with open(args.save, "w", encoding="utf-8") as f:
                json.dump(report, f, ensure_ascii=False, indent=1, default=str)
            print(f"Rapor: {args.save}")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser(description="Minerva Asistan önbellek sondası (gerçek API, ≈ $0.15)")
    ap.add_argument("--yes", action="store_true", help="onay sormadan çalıştır")
    ap.add_argument("--model", default="claude-opus-5-5")
    ap.add_argument("--effort", default="low", choices=("low", "medium", "high"))
    ap.add_argument("--ttl", default="1h", choices=("5m", "1h"))
    ap.add_argument("--safe-mode", action="store_true", help="beta alanları olmadan (runner safe mode)")
    ap.add_argument("--conversation-id", type=int, default=None,
                    help="C adımında DB'deki bu sohbeti replay et (DATABASE_URL gerekir)")
    ap.add_argument("--save", default=None, help="çağrı/usage raporunu JSON olarak yaz")
    args = ap.parse_args()
    if not args.yes:
        if not sys.stdin.isatty():
            print("Onay gerekli: etkileşimli değil, `--yes` ver.", file=sys.stderr)
            return 2
        ans = input(f"Gerçek API'ye 3 istek gidecek (≈ ${EST_COST_USD:.2f}, {args.model}). Devam? [e/H] ")
        if ans.strip().lower() not in ("e", "evet", "y", "yes"):
            print("İptal.")
            return 2
    return asyncio.run(_main(args))


if __name__ == "__main__":
    sys.exit(main())
