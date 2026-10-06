"""
"Aynı malzeme" gruplarının lab kararlarından tohumlanması — 06.10.2026.

`_backfill_material_groups_from_kept()` yalnız LAB'ın verdiği "ayrı kalsın"
(kept) kararlarını gruba çevirir; `pending_clusters`'ın tek aktif kart kalınca
kendisi kapattığı kümeler (decided_by='sistem'), bekleyen ve birleştirilmiş
kümeler, 2'den az aktif kartlı kümeler ve kartı zaten gruplu kümeler atlanır.
Sentinel'li: ikinci çağrı hiçbir şey yapmaz.
"""
import json

from sqlalchemy.orm import Session

from database import (AppSetting, DuplicateItemDecision, Item, MaterialGroup,
                      _backfill_material_groups_from_kept)

SENT = "backfill.material_groups_from_kept.v1"


def _card(db, name, active=True, domain="cosmetics", group_id=None):
    it = Item(name=name, category="Hammadde", unit="g", current_stock=0, domain=domain,
              is_active=active, material_group_id=group_id)
    db.add(it); db.flush()
    return it.id


def _decision(db, key, title, ids, status="kept", by="Songül Arslan"):
    d = DuplicateItemDecision(cluster_key=key, title=title, item_ids=json.dumps(ids),
                              status=status, decided_by=by)
    db.add(d); db.flush()
    return d.id


def test_seeds_only_lab_kept_clusters_once(db_session: Session):
    # init_db (conftest) boş tabloda sentinel'i zaten yazdı → eski DB'yi taklit et
    db_session.query(AppSetting).filter(AppSetting.key == SENT).delete()
    stearil = [_card(db_session, n) for n in ("SETİL STEARİL ALKOL", "CETYL STEARYL ALCOHOL",
                                              "CETEARYL ALCOHOL")]
    kept = _decision(db_session, "k1", "Setil stearil alkol", stearil)
    # pending_clusters'ın otomatik kapattığı küme lab kararı değildir — 2 aktif
    # kartı olsa bile (kart sonradan geri açılmış olabilir) tohumlanmaz
    sys_ids = [_card(db_session, "Sistem 1"), _card(db_session, "Sistem 2")]
    _decision(db_session, "k2", "Sistem kapattı", sys_ids, by="sistem")
    _decision(db_session, "k3", "Bekleyen", [_card(db_session, "B1"), _card(db_session, "B2")],
              status="pending", by=None)
    _decision(db_session, "k4", "Birleşti", [_card(db_session, "M1"), _card(db_session, "M2")],
              status="merged")
    _decision(db_session, "k5", "Tek aktif", [_card(db_session, "T1"),
                                              _card(db_session, "T2", active=False)])
    manual = MaterialGroup(name="Elle kurulmuş", domain="cosmetics")
    db_session.add(manual); db_session.flush()
    _decision(db_session, "k6", "Zaten gruplu", [_card(db_session, "G1", group_id=manual.id),
                                                 _card(db_session, "G2")])
    db_session.commit()

    _backfill_material_groups_from_kept()
    db_session.expire_all()

    seeded = db_session.query(MaterialGroup).filter(MaterialGroup.source == "dup_kept").all()
    assert len(seeded) == 1
    g = seeded[0]
    assert (g.name, g.source_ref, g.domain, g.is_active) == ("Setil stearil alkol", kept,
                                                             "cosmetics", True)
    members = {i.id for i in db_session.query(Item).filter(Item.material_group_id == g.id)}
    assert members == set(stearil)
    assert db_session.query(AppSetting).filter(AppSetting.key == SENT).one().value == "1"

    # İkinci çağrı no-op — lab grubu sonradan dağıtsa bile geri gelmez
    for it in db_session.query(Item).filter(Item.material_group_id == g.id):
        it.material_group_id = None
    db_session.commit()
    _backfill_material_groups_from_kept()
    db_session.expire_all()
    assert db_session.query(MaterialGroup).count() == 2       # elle kurulan + tohumlanan
    assert db_session.query(Item).filter(Item.material_group_id == g.id).count() == 0


def test_seed_name_clash_gets_suffix(db_session: Session):
    db_session.query(AppSetting).filter(AppSetting.key == SENT).delete()
    db_session.add(MaterialGroup(name="JOJOBA YAĞI", domain="cosmetics"))
    _decision(db_session, "j1", "Jojoba yagi", [_card(db_session, "J1"), _card(db_session, "J2")])
    db_session.commit()
    _backfill_material_groups_from_kept()
    db_session.expire_all()
    g = db_session.query(MaterialGroup).filter(MaterialGroup.source == "dup_kept").one()
    assert g.name == "Jojoba yagi (2)"
