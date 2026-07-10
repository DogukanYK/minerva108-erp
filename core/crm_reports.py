# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
CRM raporlama — satış hunisi, kazan/kaybet, tahmin, aktivite liderliği.

Ay gruplaması Python'da yapılır (DB-bağımsız; PG + sqlite testlerinde aynı çalışır).
Tüm tutarlar fırsatın `value` alanından; para birimi karışıksa ham toplam verilir
(çoklu para birimi normalizasyonu kapsam dışı — pratikte TRY ağırlıklı).
"""
from datetime import datetime
from collections import defaultdict

from sqlalchemy import func
from sqlalchemy.orm import Session

from database import CrmDeal, CrmStage, CrmActivity, CrmTask, User
from core.permissions import _has_permission


def _month_key(dt) -> str:
    return dt.strftime("%Y-%m") if dt else "—"


def _last_months(n: int):
    now = datetime.utcnow()
    out = []
    y, m = now.year, now.month
    for _ in range(n):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12; y -= 1
    return list(reversed(out))


def report_summary(db: Session, months: int = 6) -> dict:
    stages = (db.query(CrmStage).filter(CrmStage.is_active == True)  # noqa: E712
              .order_by(CrmStage.sort_order, CrmStage.id).all())
    open_deals = db.query(CrmDeal).filter(CrmDeal.status == "open", CrmDeal.is_active == True).all()  # noqa: E712
    won_deals = db.query(CrmDeal).filter(CrmDeal.status == "won", CrmDeal.is_active == True).all()  # noqa: E712
    lost_deals = db.query(CrmDeal).filter(CrmDeal.status == "lost", CrmDeal.is_active == True).all()  # noqa: E712

    # ── Huni: açık fırsatların aşama dağılımı (won/lost hariç) ──
    stage_val = defaultdict(float)
    stage_cnt = defaultdict(int)
    for d in open_deals:
        stage_val[d.stage_id] += (d.value or 0.0)
        stage_cnt[d.stage_id] += 1
    funnel = [{"stage": s.name, "count": stage_cnt.get(s.id, 0), "value": round(stage_val.get(s.id, 0.0), 2)}
              for s in stages if not (s.is_won or s.is_lost)]

    # ── Kazan/kaybet oranı ──
    total_closed = len(won_deals) + len(lost_deals)
    win_rate = round(100 * len(won_deals) / total_closed, 1) if total_closed else 0.0

    # ── Aylık kazan/kaybet + kazanılan değer ──
    keys = _last_months(months)
    won_by_m = defaultdict(lambda: {"count": 0, "value": 0.0})
    lost_by_m = defaultdict(int)
    for d in won_deals:
        k = _month_key(d.won_at or d.closed_at or d.created_at)
        won_by_m[k]["count"] += 1; won_by_m[k]["value"] += (d.value or 0.0)
    for d in lost_deals:
        lost_by_m[_month_key(d.closed_at or d.created_at)] += 1
    monthly = [{"month": k,
                "won_count": won_by_m[k]["count"], "won_value": round(won_by_m[k]["value"], 2),
                "lost_count": lost_by_m.get(k, 0)} for k in keys]

    # ── Tahmin: açık fırsat × olasılık, beklenen kapanış ayına göre ──
    fc = defaultdict(float)
    for d in open_deals:
        k = _month_key(d.expected_close_at) if d.expected_close_at else "Tarihsiz"
        fc[k] += (d.value or 0.0) * ((d.probability or 0) / 100.0)
    forecast = [{"month": k, "weighted_value": round(v, 2)} for k, v in sorted(fc.items())]

    # ── Aktivite liderliği (CRM kullanıcıları) ──
    users = [u for u in db.query(User).filter(User.is_active == True).all()  # noqa: E712
             if _has_permission(u, "crm", "view")]
    act_cnt = dict(db.query(CrmActivity.author_user_id, func.count(CrmActivity.id))
                   .group_by(CrmActivity.author_user_id).all())
    won_cnt = dict(db.query(CrmDeal.owner_user_id, func.count(CrmDeal.id))
                   .filter(CrmDeal.status == "won", CrmDeal.is_active == True)  # noqa: E712
                   .group_by(CrmDeal.owner_user_id).all())
    won_value = dict(db.query(CrmDeal.owner_user_id, func.coalesce(func.sum(CrmDeal.value), 0.0))
                     .filter(CrmDeal.status == "won", CrmDeal.is_active == True)  # noqa: E712
                     .group_by(CrmDeal.owner_user_id).all())
    open_cnt = dict(db.query(CrmDeal.owner_user_id, func.count(CrmDeal.id))
                    .filter(CrmDeal.status == "open", CrmDeal.is_active == True)  # noqa: E712
                    .group_by(CrmDeal.owner_user_id).all())
    leaderboard = sorted(
        [{"name": u.full_name, "activities": int(act_cnt.get(u.id, 0)),
          "won": int(won_cnt.get(u.id, 0)), "won_value": round(float(won_value.get(u.id, 0.0)), 2),
          "open": int(open_cnt.get(u.id, 0))} for u in users],
        key=lambda x: (-x["won_value"], -x["won"], -x["activities"]))

    return {
        "totals": {
            "open_count": len(open_deals),
            "open_value": round(sum((d.value or 0.0) for d in open_deals), 2),
            "won_count": len(won_deals),
            "won_value": round(sum((d.value or 0.0) for d in won_deals), 2),
            "lost_count": len(lost_deals),
            "win_rate": win_rate,
        },
        "funnel": funnel,
        "monthly": monthly,
        "forecast": forecast,
        "leaderboard": leaderboard,
    }
