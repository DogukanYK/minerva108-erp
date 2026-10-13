"""Minerva Asistan — RBAC kategorisi, yetki backfill'i, şema ve migration zinciri.

`assistant.use` tüm iç rollerde açık, Distributor'da kapalı.  Override'lı
kullanıcıya `_backfill_perm_assistant` rol varsayılanını YALNIZ override'da bir
okuma izni (items/inventory/recipes/production/reports .view ya da
pdks.view_own) varsa yazar — yalnız `pdks.kiosk` taşıyan kiosk tableti False
alır; var olan anahtar ezilmez; sentinel'le bir kez çalışır; her değişen
kullanıcı için `permissions.backfill` audit satırı bırakır.  Üç tablo
create_all ile gelir; Alembic migration'ı mevcut TEK head'e zincirlenir.
"""
import json
import os

import bcrypt
from sqlalchemy import inspect

from core.permissions import PERMISSION_CATEGORIES, _DEFAULT_PERMISSIONS, _has_permission
from database import (AdminAuditLog, AppSetting, AssistantConversation, AssistantMessage,
                      AssistantUsage, User, _backfill_perm_assistant, engine)

SENTINEL = "backfill.perm.assistant.v1"
READ_GATE = "items.view|inventory.view|recipes.view|production.view|reports.view|pdks.view_own"
REVISION, PARENT = "b4d6f8a0c2e4", "a6c8e0b2d4f7"


def _user(db, username, role, permissions=None):
    u = User(username=username, full_name=username.title(), role=role, is_active=True,
             password_hash=bcrypt.hashpw(b"minerva123", bcrypt.gensalt()).decode(),
             permissions=json.dumps(permissions) if permissions is not None else None)
    db.add(u)
    db.commit()
    return u


def _assistant(db, u):
    db.expire_all()
    return json.loads(db.get(User, u.id).permissions).get("assistant")


def _reset_sentinel(db):
    db.query(AppSetting).filter(AppSetting.key == SENTINEL).delete()
    db.commit()


# ─── Kategori + rol varsayılanları ───────────────────────────────────────────

def test_category_defaults_internal_roles_on_distributor_off():
    assert PERMISSION_CATEGORIES["assistant"] == ["use"]
    for role in ("Manager", "LabLead", "LabTech", "Staff"):
        assert _DEFAULT_PERMISSIONS[role]["assistant"] == {"use": True}, role
    assert _DEFAULT_PERMISSIONS["Distributor"]["assistant"] == {"use": False}
    # Her rolde anahtar seti kategoriyle birebir (matris + backfill kalıbı buna güvenir)
    for role, cats in _DEFAULT_PERMISSIONS.items():
        assert set(cats["assistant"]) == set(PERMISSION_CATEGORIES["assistant"]), role


def test_effective_permission_by_role(db_session):
    db = db_session
    assert _has_permission(db.query(User).filter_by(username="dogukan").one(), "assistant", "use")  # SuperAdmin
    assert _has_permission(_user(db, "asst_staff", "Staff"), "assistant", "use")
    assert _has_permission(_user(db, "asst_lab", "LabTech"), "assistant", "use")
    assert not _has_permission(_user(db, "asst_dist", "Distributor"), "assistant", "use")
    # TUZAK: override rol varsayılanının YERİNE geçer → anahtar yoksa False.  Backfill bu yüzden var.
    assert not _has_permission(_user(db, "asst_ovr", "Staff", {"items": {"view": True}}), "assistant", "use")


# ─── Backfill ─────────────────────────────────────────────────────────────────

def test_backfill_gates_on_existing_read_permission_and_audits(db_session):
    db = db_session
    staff = _user(db, "bf_staff", "Staff", {"items": {"view": True, "create": False}})
    pdks_only = _user(db, "bf_pdks", "Staff", {"pdks": {"check": True, "view_own": True}})
    kiosk = _user(db, "bf_kiosk", "Staff", {"pdks": {"kiosk": True}})
    noread = _user(db, "bf_noread", "LabTech", {"items": {"view": False}, "qc": {"view": True}})
    dist = _user(db, "bf_dist", "Distributor", {"portal": {"view": True, "order": True}})
    manual = _user(db, "bf_manual", "Manager", {"items": {"view": True}, "assistant": {"use": False}})
    plain = _user(db, "bf_plain", "Staff")                       # override yok → rol varsayılanı
    broken = _user(db, "bf_broken", "Staff")
    broken.permissions = "{bozuk json"
    db.commit()
    _reset_sentinel(db)

    _backfill_perm_assistant()

    assert _assistant(db, staff) == {"use": True}
    assert _assistant(db, pdks_only) == {"use": True}            # pdks.view_own tek başına yeter
    assert _assistant(db, kiosk) == {"use": False}               # yalnız pdks.kiosk → kapalı
    assert _assistant(db, noread) == {"use": False}              # okuma izni yok → kapalı
    assert _assistant(db, dist) == {"use": False}                # rol varsayılanı zaten False
    assert _assistant(db, manual) == {"use": False}              # var olan anahtar ezilmez
    assert db.get(User, plain.id).permissions is None            # override'sız kullanıcıya dokunulmaz
    assert db.get(User, broken.id).permissions == "{bozuk json"  # bozuk JSON atlanır, çökmez
    assert _has_permission(db.get(User, staff.id), "assistant", "use") is True
    assert _has_permission(db.get(User, kiosk.id), "assistant", "use") is False
    assert _has_permission(db.get(User, plain.id), "assistant", "use") is True

    assert db.query(AppSetting).filter_by(key=SENTINEL).one().value == "5"
    audits = [a for a in db.query(AdminAuditLog).filter_by(action="permissions.backfill")
              if json.loads(a.details).get("sentinel") == SENTINEL]
    by_user = {a.target_id: json.loads(a.details) for a in audits}
    assert set(by_user) == {staff.id, pdks_only.id, kiosk.id, noread.id, dist.id}
    assert all(a.actor_name == "sistem" and a.target_type == "user" for a in audits)
    assert by_user[staff.id] == {"keys": {"assistant.use": True}, "gated": {}, "role": "Staff",
                                 "sentinel": SENTINEL}
    assert by_user[kiosk.id]["keys"] == {"assistant.use": False}
    assert by_user[kiosk.id]["gated"] == {"assistant.use": READ_GATE}
    assert by_user[noread.id]["gated"] == {"assistant.use": READ_GATE}
    assert by_user[dist.id]["gated"] == {}                       # kapı değil rol varsayılanı kapattı


def test_backfill_is_idempotent_and_respects_admin_changes(db_session):
    db = db_session
    u = _user(db, "bf_idem", "Staff", {"items": {"view": True}})
    _reset_sentinel(db)
    _backfill_perm_assistant()
    assert _assistant(db, u) == {"use": True}
    assert db.query(AppSetting).filter_by(key=SENTINEL).one().value == "1"

    # Yönetici sonradan kapatır; ikinci çağrı sentinel yüzünden hiç dokunmaz
    row = db.get(User, u.id)
    p = json.loads(row.permissions)
    p["assistant"]["use"] = False
    row.permissions = json.dumps(p)
    db.commit()
    late = _user(db, "bf_late", "Staff", {"items": {"view": True}})
    before = db.query(AdminAuditLog).filter_by(action="permissions.backfill").count()
    _backfill_perm_assistant()
    assert _assistant(db, u) == {"use": False}
    assert _assistant(db, late) is None
    assert db.query(AdminAuditLog).filter_by(action="permissions.backfill").count() == before

    # Sentinel silinip yeniden koşsa bile var olan anahtar ezilmez, yalnız eksik olan yazılır
    _reset_sentinel(db)
    _backfill_perm_assistant()
    assert _assistant(db, u) == {"use": False}
    assert _assistant(db, late) == {"use": True}


def test_backfill_runs_in_init_db(db_session):
    """conftest her testte init_db çağırır → sentinel yerinde olmalı."""
    assert db_session.query(AppSetting).filter_by(key=SENTINEL).first() is not None


# ─── Şema ─────────────────────────────────────────────────────────────────────

def test_assistant_tables_created_with_cascades(db_session):
    db = db_session
    names = set(inspect(engine).get_table_names())
    assert {"assistant_conversations", "assistant_messages", "assistant_usage"} <= names
    idx = {i["name"] for i in inspect(engine).get_indexes("assistant_usage")}
    assert {"ix_assistant_usage_created", "ix_assistant_usage_user_created"} <= idx
    idx = {i["name"] for i in inspect(engine).get_indexes("assistant_conversations")}
    assert {"ix_assistant_conversations_user_id", "ix_assistant_conv_user_updated"} <= idx

    u = _user(db, "schema_u", "Staff")
    conv = AssistantConversation(user_id=u.id, domain="cosmetics", model="claude-opus-5-5",
                                 prompt_hash="abc123def456", title="Stearil alkol nerede?")
    db.add(conv)
    db.flush()
    child = AssistantConversation(user_id=u.id, model="claude-opus-5-5",
                                  prompt_hash="abc123def456", parent_id=conv.id)
    db.add(child)
    db.flush()
    assert (child.domain, child.turns, child.last_prompt_tokens, child.is_active) == ("cosmetics", 0, 0, True)

    blocks = [{"type": "thinking", "thinking": "…", "signature": "sig=="},
              {"type": "text", "text": "Stearil alkol A rafında."}]
    q = AssistantMessage(conversation_id=conv.id, role="user", kind="question",
                         content_json=json.dumps([{"type": "text", "text": "soru"}]), display_text="soru")
    a = AssistantMessage(conversation_id=conv.id, role="assistant", kind="assistant",
                         content_json=json.dumps(blocks), display_text="Stearil alkol A rafında.",
                         tools_json=json.dumps([{"name": "find_item", "summary": "1 kart"}]))
    db.add_all([q, a])
    db.flush()
    use = AssistantUsage(user_id=u.id, conversation_id=conv.id, message_id=a.id, domain="cosmetics",
                         model="claude-opus-5-5", input_tokens=212, cache_read_input_tokens=11034,
                         cache_creation_input_tokens=0, cache_creation_1h_tokens=0, output_tokens=512,
                         cost_usd=0.0231, stop_reason="end_turn", request_id="req_x",
                         question_preview="soru", latency_ms=1800)
    db.add(use)
    db.commit()
    assert use.round_no == 1
    assert [m.kind for m in conv.messages] == ["question", "assistant"]
    assert json.loads(a.content_json) == blocks                 # birebir replay

    # Sohbet silinince mesajlar CASCADE; usage satırı kalır (SET NULL); çocuk sohbetin parent'ı NULL
    db.delete(conv)
    db.commit()
    db.expire_all()
    assert db.query(AssistantMessage).count() == 0
    assert db.get(AssistantConversation, child.id).parent_id is None
    kept = db.get(AssistantUsage, use.id)
    assert kept is not None and kept.conversation_id is None and kept.message_id is None
    assert kept.cost_usd == 0.0231

    # Kullanıcı silinince sohbet + usage CASCADE
    db.delete(db.get(User, u.id))
    db.commit()
    assert db.query(AssistantConversation).count() == 0
    assert db.query(AssistantUsage).count() == 0


# ─── Alembic ──────────────────────────────────────────────────────────────────

def test_alembic_migration_chains_to_single_head():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = ScriptDirectory.from_config(Config(os.path.join(root, "alembic.ini")))
    rev = script.get_revision(REVISION)
    assert rev.down_revision == PARENT
    assert script.get_heads() == [REVISION], "yeni migration mevcut head'e zincirlenmeli (çoklu head yok)"
    assert "deploy.sh" in (rev.longdoc or "") and "alembic" in (rev.longdoc or "")   # docstring notu
