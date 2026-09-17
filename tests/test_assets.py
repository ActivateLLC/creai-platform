"""Uploads: validation, byte sniffing, quotas, isolation, serving, agent context, sites."""

import json
import os
import secrets

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

os.environ.setdefault("ENV", "development")

from app.core import db                                   # noqa: E402
from app.core.config import settings                      # noqa: E402
from app.main import app                                  # noqa: E402
from app.services import agent, assets, models            # noqa: E402
from app.services import site as site_spec                # noqa: E402

from tests.test_agent import FakeModel, text              # noqa: E402
from tests.test_isolation import auth, sign_in            # noqa: E402

pytestmark = pytest.mark.asyncio

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 40
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
MP4 = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 40
PDF = b"%PDF-1.7\n" + b"x" * 40


class FakeBody:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data


class FakeS3:
    def __init__(self):
        self.objects, self.cors, self.posts = {}, None, []

    def generate_presigned_post(self, bucket, key, Fields, Conditions, ExpiresIn):
        self.posts.append({"key": key, "fields": Fields, "conditions": Conditions})
        return {"url": f"https://{bucket}.t3.storageapi.dev/", "fields": {**Fields, "key": key, "policy": "p"}}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise KeyError(Key)
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket, Key, Range=None):
        data = self.objects[Key]
        if Range:
            a, b = Range.removeprefix("bytes=").split("-")
            data = data[int(a):int(b) + 1]
        return {"Body": FakeBody(data)}

    def delete_object(self, Bucket, Key):
        self.objects.pop(Key, None)

    def generate_presigned_url(self, op, Params, ExpiresIn):
        return f"https://signed.example/{Params['Key']}?type={Params['ResponseContentType']}"

    def put_bucket_cors(self, Bucket, CORSConfiguration):
        self.cors = CORSConfiguration


@pytest_asyncio.fixture
async def api(monkeypatch):
    for k, v in (("assets_bucket", "b"), ("assets_key_id", "k"), ("assets_secret", "s"),
                 ("assets_endpoint", "https://t3.storageapi.dev"), ("anthropic_key", "test")):
        object.__setattr__(settings, k, v)
    fake = FakeS3()
    monkeypatch.setattr(assets, "_s3", lambda: fake)
    from app.api import agent as agent_routes
    agent_routes._hits.clear()
    await db.connect()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        c.s3 = fake
        yield c
    await db.disconnect()
    for k in ("assets_bucket", "assets_key_id", "assets_secret", "assets_endpoint"):
        object.__setattr__(settings, k, "")


async def workspace(api):
    tok = await sign_in(api, f"as{secrets.token_hex(3)}@files-{secrets.token_hex(2)}.io")
    org = (await api.get("/v1/auth/me", headers=auth(tok))).json()["active_org"]
    pid = (await api.post("/v1/projects", headers=auth(tok), json={"name": "Shop"})).json()["id"]
    return tok, org, pid


async def upload(api, tok, name, mime, data, **extra):
    r = await api.post("/v1/assets", headers=auth(tok), json={"name": name, "mime": mime, "size": len(data), **extra})
    assert r.status_code == 200, r.text
    out = r.json()
    api.s3.objects[out["upload"]["fields"]["key"]] = data
    done = await api.post(f"/v1/assets/{out['id']}/complete", headers=auth(tok))
    return out, done


async def test_upload_rules(api):
    tok, org, pid = await workspace(api)
    for mime in ("image/svg+xml", "text/html", "application/javascript", "image/heic"):
        r = await api.post("/v1/assets", headers=auth(tok), json={"name": "x", "mime": mime, "size": 10})
        assert r.status_code == 400, mime
    big = await api.post("/v1/assets", headers=auth(tok), json={"name": "x.jpg", "mime": "image/jpeg", "size": 16 * 1024 * 1024})
    assert big.status_code == 413 and "15 MB" in big.json()["detail"]
    r = await api.post("/v1/assets", headers=auth(tok), json={"name": "../../etc/pass<wd>.jpg", "mime": "image/jpeg",
                                                               "size": 100, "project_id": pid})
    post = api.s3.posts[-1]
    assert post["key"].startswith(f"org/{org}/") and post["key"].endswith("/etcpasswd.jpg") and ".." not in post["key"]
    assert {"Content-Type": "image/jpeg"} in post["conditions"]
    assert ["content-length-range", 1, 15 * 1024 * 1024] in post["conditions"]
    assert r.json()["upload"]["fields"]["Content-Type"] == "image/jpeg"
    other, _, other_pid = await workspace(api)
    assert (await api.post("/v1/assets", headers=auth(tok), json={"name": "x.jpg", "mime": "image/jpeg", "size": 10,
                                                                  "project_id": other_pid})).status_code == 404
    # quota
    async with db.conn() as c:
        await c.execute("""INSERT INTO assets (org_id, kind, mime, name, size, key, token, status)
                           VALUES ($1,'video','video/mp4','big.mp4',$2,$3,$4,'ready')""",
                        org, assets.QUOTA["free"], f"k-{secrets.token_hex(6)}", secrets.token_urlsafe(18))
    full = await api.post("/v1/assets", headers=auth(tok), json={"name": "x.jpg", "mime": "image/jpeg", "size": 10})
    assert full.status_code == 413 and "storage" in full.json()["detail"]


async def test_sniffing_isolation_listing_and_serving(api):
    tok, org, pid = await workspace(api)
    fake, done = await upload(api, tok, "photo.jpg", "image/jpeg", b"<html><script>alert(1)</script>")
    assert done.status_code == 400 and fake["upload"]["fields"]["key"] not in api.s3.objects
    for name, mime, data in (("a.jpg", "image/jpeg", JPEG), ("b.png", "image/png", PNG), ("m.pdf", "application/pdf", PDF)):
        _, d = await upload(api, tok, name, mime, data, project_id=pid)
        assert d.status_code == 200 and d.json()["status"] == "ready"
    vid, d = await upload(api, tok, "tour.mov", "video/quicktime", MP4, project_id=pid, duration=32.5, width=1920, height=1080)
    assert d.status_code == 200
    still, d2 = await upload(api, tok, "tour-1.jpg", "image/jpeg", JPEG, parent_id=vid["id"])
    assert d2.status_code == 200

    other, _, _ = await workspace(api)
    assert (await api.post(f"/v1/assets/{vid['id']}/complete", headers=auth(other))).status_code == 404
    assert (await api.post("/v1/assets", headers=auth(other), json={"name": "s.jpg", "mime": "image/jpeg", "size": 5,
                                                                    "parent_id": vid["id"]})).status_code == 404
    assert (await api.get("/v1/assets", headers=auth(other))).json()["assets"] == []

    lib = (await api.get(f"/v1/assets?project_id={pid}", headers=auth(tok))).json()
    names = [a["name"] for a in lib["assets"]]
    assert names == ["tour.mov", "m.pdf", "b.png", "a.jpg"]                    # stills are nested, not listed
    video = lib["assets"][0]
    assert len(video["frames"]) == 1 and lib["used"] > 0 and lib["quota"] == assets.QUOTA["free"]

    token = video["url"].rsplit("/", 1)[1]
    r = await api.get(f"/f/{token}", follow_redirects=False)
    assert r.status_code == 302 and "type=video/quicktime" in r.headers["location"]
    assert (await api.get("/f/nope-not-a-real-token-xx", follow_redirects=False)).status_code == 404
    assert (await api.get("/f/../../etc", follow_redirects=False)).status_code == 404

    assert (await api.delete(f"/v1/assets/{vid['id']}", headers=auth(other))).status_code == 404
    assert (await api.delete(f"/v1/assets/{vid['id']}", headers=auth(tok))).status_code == 200
    assert (await api.get(f"/f/{token}", follow_redirects=False)).status_code == 404
    assert still["upload"]["fields"]["key"] not in api.s3.objects             # stills go with the video


async def test_agent_sees_files_but_never_stores_bytes(api, monkeypatch):
    tok, org, pid = await workspace(api)
    photo, _ = await upload(api, tok, "storefront.jpg", "image/jpeg", JPEG, project_id=pid, width=1600, height=1200)
    menu, _ = await upload(api, tok, "menu.pdf", "application/pdf", PDF, project_id=pid)
    vid, _ = await upload(api, tok, "tour.mp4", "video/mp4", MP4, project_id=pid, duration=75)
    await upload(api, tok, "f1.jpg", "image/jpeg", JPEG, parent_id=vid["id"])
    other, _, _ = await workspace(api)
    theirs, _ = await upload(api, other, "secret.jpg", "image/jpeg", PNG)

    model = FakeModel([text("Nice storefront.")])
    monkeypatch.setattr(agent, "_call", model)
    r = await api.post(f"/v1/agent/projects/{pid}", headers=auth(tok), json={
        "message": "Use these", "asset_ids": [photo["id"], menu["id"], vid["id"], theirs["id"]]})
    assert r.status_code == 200, r.text
    first = model.calls[0]["messages"][-1]["content"]
    kinds = [b["type"] for b in first]
    assert kinds == ["image", "document", "image", "text"]
    note = first[-1]["text"]
    assert "storefront.jpg" in note and "1600×1200" in note and "1:15" in note and "hero_video" in note
    assert "secret.jpg" not in note                                             # someone else's file is ignored
    assert note.rstrip().endswith("Use these")

    thread = (await api.get(f"/v1/agent/projects/{pid}", headers=auth(tok))).json()["messages"]
    mine = next(m for m in thread if m["role"] == "user")
    assert mine["text"] == "Use these" and [a["name"] for a in mine["assets"]] == ["storefront.jpg", "menu.pdf", "tour.mp4"]
    assert mine["assets"][2]["thumb"]
    async with db.conn() as c:
        stored = json.dumps(await c.fetchval("SELECT answers FROM projects WHERE id=$1", pid))
    assert "base64" not in stored and len(stored) < 5000

    assert (await api.post("/v1/agent/draft", json={"message": "hi", "asset_ids": [photo["id"]]})).status_code == 401


def test_sites_only_use_our_files_and_render_video():
    saved = settings.public_url
    object.__setattr__(settings, "public_url", "https://app.creai.dev")
    try:
        _check_sites("https://app.creai.dev")
    finally:
        object.__setattr__(settings, "public_url", saved)


def _check_sites(base):
    ours = f"{base}/f/{'a' * 24}"
    s = site_spec.merge({}, {"hero_image": ours, "hero_video": ours})
    assert s["hero_image"] == ours and s["hero_video"] == ours
    html = site_spec.render(dict(s, business="Shine", layout="split"))
    assert "<video" in html and "muted" in html and "playsinline" in html
    for bad in (f"{base}/v1/auth/me", f"{base}/f/short", "https://evil.example/f/" + "a" * 24,
                "javascript:alert(1)", f"http://{base.split('://')[1]}/f/{'a' * 24}"):
        assert site_spec.merge({}, {"hero_video": bad})["hero_video"] == "", bad
        assert site_spec.merge({}, {"hero_image": bad})["hero_image"] == "", bad


def test_other_models_get_images_as_data_urls():
    msgs, _ = models.to_openai([{"role": "user", "content": [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
        {"type": "document", "title": "menu.pdf", "source": {"type": "base64", "media_type": "application/pdf", "data": "BB"}},
        {"type": "text", "text": "hi"}]}], [], "")
    parts = msgs[0]["content"]
    assert parts[0] == {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
    assert "menu.pdf" in parts[1]["text"] and parts[2]["text"] == "hi"


async def test_bucket_cors(api):
    await assets.ensure_cors()
    rule = api.s3.cors["CORSRules"][0]
    assert "capacitor://localhost" in rule["AllowedOrigins"] and rule["AllowedMethods"] == ["POST", "PUT"]
