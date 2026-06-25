"""
Minerva Drive — dosya paylaşım testleri.

  • yükleme + listeleme (login'li)
  • public paylaşım sayfası + indirme (auth YOK)
  • şifre koruması: kilitli → yanlış → doğru → indirme
  • süre dolması → 410
  • yönetim uçları auth ister
  • dosya silince koleksiyondan da düşer
"""
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from api_main import app
from database import DriveCollection

_H = {"Origin": "http://testserver"}


def _upload(ac, name="t.txt", content=b"hello-minerva"):
    r = ac.post("/api/drive/upload", files={"file": (name, content, "text/plain")}, headers=_H)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _collection(ac, file_ids, name="A Link", password=None, expires_days=None):
    body = {"name": name, "file_ids": file_ids}
    if password:
        body["password"] = password
    if expires_days:
        body["expires_days"] = expires_days
    r = ac.post("/api/drive/collections", json=body, headers=_H)
    assert r.status_code == 201, r.text
    return r.json()


def test_upload_and_list(authed_client: TestClient):
    fid = _upload(authed_client)
    files = authed_client.get("/api/drive/files").json()
    assert any(f["id"] == fid for f in files)


def test_folder_upload_creates_tree_downloads_basename(authed_client: TestClient):
    """Klasör yüklemesi GERÇEK klasör ağacı kurar; dosya adı basename, indirme de basename."""
    r = authed_client.post(
        "/api/drive/upload",
        files={"file": ("rapor.pdf", b"PDFDATA", "application/pdf")},
        data={"rel_path": "Belgeler/2026/rapor.pdf"},
        headers=_H,
    )
    assert r.status_code == 201, r.text
    fid = r.json()["id"]
    assert r.json()["name"] == "rapor.pdf"                   # basename (yol ağaçta)
    # Belgeler/2026 klasör ağacı kuruldu, dosya 2026 içinde
    root = authed_client.get("/api/drive/list").json()
    belg = next(f for f in root["folders"] if f["name"] == "Belgeler")
    yr = authed_client.get(f"/api/drive/list?folder_id={belg['id']}").json()
    y26 = next(f for f in yr["folders"] if f["name"] == "2026")
    leaf = authed_client.get(f"/api/drive/list?folder_id={y26['id']}").json()
    assert any(x["id"] == fid and x["name"] == "rapor.pdf" for x in leaf["files"])

    c = _collection(authed_client, [fid], name="Klasör Linki")
    pub = TestClient(app)
    dl = pub.get(f"/s/{c['token']}/f/{fid}")
    assert dl.status_code == 200 and dl.content == b"PDFDATA"
    assert "rapor.pdf" in dl.headers.get("content-disposition", "")


def test_folder_upload_strips_path_traversal(authed_client: TestClient):
    """'..'/kök kaçışı göreli yoldan ayıklanır — kök altında 'etc' klasörü oluşur, kaçış yok."""
    r = authed_client.post(
        "/api/drive/upload",
        files={"file": ("x.txt", b"x", "text/plain")},
        data={"rel_path": "../../etc/gizli.txt"},
        headers=_H,
    )
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "gizli.txt"                   # basename, '..' ayıklandı
    root = authed_client.get("/api/drive/list").json()
    assert any(f["name"] == "etc" for f in root["folders"])


def test_share_open_and_download(authed_client: TestClient):
    fid = _upload(authed_client, content=b"DATA123")
    c = _collection(authed_client, [fid], name="Geven Belgeleri")
    token = c["token"]
    assert c["share_path"] == f"/s/{token}" and c["file_count"] == 1
    pub = TestClient(app)                      # auth YOK
    page = pub.get(f"/s/{token}")
    assert page.status_code == 200 and "Geven Belgeleri" in page.text
    dl = pub.get(f"/s/{token}/f/{fid}")
    assert dl.status_code == 200 and dl.content == b"DATA123"
    assert "attachment" in dl.headers.get("content-disposition", "")


def test_password_protected_flow(authed_client: TestClient):
    fid = _upload(authed_client, content=b"SECRET")
    c = _collection(authed_client, [fid], password="gizli")
    token = c["token"]
    assert c["protected"] is True
    pub = TestClient(app)
    # şifre olmadan indirme engelli
    assert pub.get(f"/s/{token}/f/{fid}").status_code == 403
    # yanlış şifre → hâlâ engelli
    pub.post(f"/s/{token}/unlock", data={"password": "yanlis"}, headers=_H)
    assert pub.get(f"/s/{token}/f/{fid}").status_code == 403
    # doğru şifre → kilit-açma çerezi → indirme açılır
    pub.post(f"/s/{token}/unlock", data={"password": "gizli"}, headers=_H)
    ok = pub.get(f"/s/{token}/f/{fid}")
    assert ok.status_code == 200 and ok.content == b"SECRET"


def test_expired_share(authed_client: TestClient, db_session):
    fid = _upload(authed_client)
    c = _collection(authed_client, [fid])
    token = c["token"]
    col = db_session.query(DriveCollection).filter(DriveCollection.share_token == token).first()
    col.expires_at = datetime.utcnow() - timedelta(days=1)
    db_session.commit()
    pub = TestClient(app)
    page = pub.get(f"/s/{token}")
    assert page.status_code == 200 and "doldu" in page.text.lower()
    assert pub.get(f"/s/{token}/f/{fid}").status_code == 410


def test_management_requires_auth():
    pub = TestClient(app)
    assert pub.get("/api/drive/files").status_code in (401, 403)
    assert pub.post("/api/drive/upload",
                    files={"file": ("x.txt", b"x", "text/plain")}, headers=_H).status_code in (401, 403)


def test_unknown_share_404():
    pub = TestClient(app)
    assert pub.get("/s/yok-boyle-bir-link").status_code == 404


def test_custom_slug(authed_client: TestClient):
    fid = _upload(authed_client)
    r = authed_client.post("/api/drive/collections",
                           json={"name": "Geven", "file_ids": [fid], "custom_slug": "Geven Belgeleri"},
                           headers=_H)
    assert r.status_code == 201, r.text
    assert r.json()["token"] == "geven-belgeleri"        # slugify
    pub = TestClient(app)
    assert pub.get("/s/geven-belgeleri").status_code == 200


def test_custom_slug_collision_and_too_short(authed_client: TestClient):
    fid = _upload(authed_client)
    a = authed_client.post("/api/drive/collections",
                           json={"name": "A", "file_ids": [fid], "custom_slug": "rapor-2026"}, headers=_H)
    assert a.status_code == 201
    # aynı slug → çakışma
    b = authed_client.post("/api/drive/collections",
                           json={"name": "B", "file_ids": [fid], "custom_slug": "rapor-2026"}, headers=_H)
    assert b.status_code == 400
    # çok kısa → 400
    c = authed_client.post("/api/drive/collections",
                           json={"name": "C", "file_ids": [fid], "custom_slug": "ab"}, headers=_H)
    assert c.status_code == 400


def test_delete_file_removes_from_collection(authed_client: TestClient):
    fid = _upload(authed_client)
    c = _collection(authed_client, [fid])
    assert c["file_count"] == 1
    assert authed_client.delete(f"/api/drive/files/{fid}", headers=_H).status_code == 200
    colls = authed_client.get("/api/drive/collections").json()
    this = [x for x in colls if x["token"] == c["token"]][0]
    assert this["file_count"] == 0


# ─── Klasör ağacı (Faz 1) ────────────────────────────────────────────────────

def _folder(ac, name, parent_id=None):
    r = ac.post("/api/drive/folders", json={"name": name, "parent_id": parent_id}, headers=_H)
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_folder_crud_and_breadcrumb(authed_client: TestClient):
    fid = _folder(authed_client, "Serenida")
    sub = _folder(authed_client, "MSDS", fid)
    root = authed_client.get("/api/drive/list").json()
    assert any(f["id"] == fid for f in root["folders"])
    inside = authed_client.get(f"/api/drive/list?folder_id={fid}").json()
    assert any(f["id"] == sub for f in inside["folders"])
    assert [p["name"] for p in inside["breadcrumb"]] == ["Serenida"]


def test_folder_upload_builds_tree(authed_client: TestClient):
    r = authed_client.post("/api/drive/upload",
                           files={"file": ("a.pdf", b"X", "application/pdf")},
                           data={"rel_path": "Serenida/MSDS/a.pdf"}, headers=_H)
    assert r.status_code == 201, r.text
    root = authed_client.get("/api/drive/list").json()
    ser = next(f for f in root["folders"] if f["name"] == "Serenida")
    inside = authed_client.get(f"/api/drive/list?folder_id={ser['id']}").json()
    msds = next(f for f in inside["folders"] if f["name"] == "MSDS")
    files = authed_client.get(f"/api/drive/list?folder_id={msds['id']}").json()
    assert any(x["name"] == "a.pdf" for x in files["files"])


def test_move_file_into_folder(authed_client: TestClient):
    fid = _folder(authed_client, "Hedef")
    file_id = _upload(authed_client, name="x.txt")
    r = authed_client.post("/api/drive/move", headers=_H,
                           json={"file_ids": [file_id], "folder_ids": [], "target_folder_id": fid})
    assert r.status_code == 200 and r.json()["moved_files"] == 1
    inside = authed_client.get(f"/api/drive/list?folder_id={fid}").json()
    assert any(x["id"] == file_id for x in inside["files"])
    root = authed_client.get("/api/drive/list").json()
    assert not any(x["id"] == file_id for x in root["files"])


def test_move_folder_cycle_blocked(authed_client: TestClient):
    a = _folder(authed_client, "A")
    b = _folder(authed_client, "B", a)            # B, A'nın altında
    r = authed_client.post("/api/drive/move", headers=_H,
                           json={"file_ids": [], "folder_ids": [a], "target_folder_id": b})
    assert r.status_code == 400                    # A'yı kendi altına taşıyamaz


def test_delete_folder_recursive(authed_client: TestClient, db_session):
    from database import DriveFolder
    fid = _folder(authed_client, "Sil")
    sub = _folder(authed_client, "Alt", fid)
    authed_client.post("/api/drive/upload", files={"file": ("y.txt", b"Y", "text/plain")},
                       data={"folder_id": str(sub)}, headers=_H)
    assert authed_client.delete(f"/api/drive/folders/{fid}", headers=_H).status_code == 200
    assert db_session.query(DriveFolder).filter(DriveFolder.id.in_([fid, sub])).count() == 0


def test_search_files_and_folders(authed_client: TestClient):
    fid = _folder(authed_client, "AramaKlasor")
    authed_client.post("/api/drive/upload", files={"file": ("benzersizdosya.txt", b"Z", "text/plain")},
                       data={"folder_id": str(fid)}, headers=_H)
    assert any(x["name"] == "benzersizdosya.txt"
               for x in authed_client.get("/api/drive/search?q=benzersiz").json()["files"])
    assert any(x["name"] == "AramaKlasor"
               for x in authed_client.get("/api/drive/search?q=AramaKlas").json()["folders"])


def test_backfill_paths_to_folders(db_session):
    from database import DriveFile, DriveFolder, _backfill_drive_folders
    db_session.add(DriveFile(original_name="Eski/Yol/dosya.pdf", stored_name="bf" + "a" * 12, size_bytes=1))
    db_session.commit()
    _backfill_drive_folders()
    db_session.expire_all()
    rec = db_session.query(DriveFile).filter(DriveFile.stored_name == "bf" + "a" * 12).first()
    assert rec.original_name == "dosya.pdf" and rec.folder_id is not None
    assert db_session.query(DriveFolder).filter(DriveFolder.name == "Eski").first() is not None


# ─── Klasör paylaşımı + ZIP (Faz 2) ──────────────────────────────────────────

def test_share_folder_recursive_browse_download_zip(authed_client: TestClient):
    authed_client.post("/api/drive/upload", files={"file": ("a.pdf", b"PDFDATA", "application/pdf")},
                       data={"rel_path": "Paylas/Alt/a.pdf"}, headers=_H)
    pid = next(f["id"] for f in authed_client.get("/api/drive/list").json()["folders"] if f["name"] == "Paylas")
    c = authed_client.post("/api/drive/collections", headers=_H,
                           json={"name": "Paylasim", "file_ids": [], "folder_ids": [pid]})
    assert c.status_code == 201 and c.json()["folder_count"] == 1
    tok = c.json()["token"]
    pub = TestClient(app)
    root = pub.get(f"/s/{tok}/browse").json()
    assert any(f["name"] == "Paylas" for f in root["folders"])
    inside = pub.get(f"/s/{tok}/browse?folder={pid}").json()
    alt = next(f for f in inside["folders"] if f["name"] == "Alt")
    leaf = pub.get(f"/s/{tok}/browse?folder={alt['id']}").json()
    fid = leaf["files"][0]["id"]
    dl = pub.get(f"/s/{tok}/f/{fid}")
    assert dl.status_code == 200 and dl.content == b"PDFDATA"
    z = pub.get(f"/s/{tok}/zip")
    assert z.status_code == 200 and z.content[:2] == b"PK"


def test_share_download_outside_share_blocked(authed_client: TestClient):
    hidden = _upload(authed_client, name="gizli.txt")     # paylaşımsız
    shown = _upload(authed_client, name="acik.txt")
    c = authed_client.post("/api/drive/collections", headers=_H,
                           json={"name": "L", "file_ids": [shown]}).json()
    pub = TestClient(app)
    assert pub.get(f"/s/{c['token']}/f/{hidden}").status_code == 404
    assert pub.get(f"/s/{c['token']}/f/{shown}").status_code == 200
