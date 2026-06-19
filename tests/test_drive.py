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


def test_delete_file_removes_from_collection(authed_client: TestClient):
    fid = _upload(authed_client)
    c = _collection(authed_client, [fid])
    assert c["file_count"] == 1
    assert authed_client.delete(f"/api/drive/files/{fid}", headers=_H).status_code == 200
    colls = authed_client.get("/api/drive/collections").json()
    this = [x for x in colls if x["token"] == c["token"]][0]
    assert this["file_count"] == 0
