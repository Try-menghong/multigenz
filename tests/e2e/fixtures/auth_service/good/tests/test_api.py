from fastapi.testclient import TestClient

from app.main import USERS, app

client = TestClient(app)


def setup_function(_):
    USERS.clear()


def register(name="alice", password="s3cretpass"):
    return client.post("/register", json={"username": name, "password": password})


def login(name="alice", password="s3cretpass"):
    return client.post("/login", json={"username": name, "password": password})


def test_register_then_duplicate_conflicts():
    assert register().status_code == 201
    assert register().status_code == 409


def test_passwords_are_never_stored_in_clear():
    register()
    assert "s3cretpass" not in USERS["alice"] and USERS["alice"].startswith("pbkdf2_sha256$")


def test_login_wrong_password_is_unauthorized():
    register()
    assert login(password="not-the-password").status_code == 401
    assert login(name="nobody").status_code == 401


def test_login_and_me_flow():
    register()
    r = login()
    assert r.status_code == 200 and r.json()["token_type"] == "bearer"
    me = client.get("/me", headers={"Authorization": f"Bearer {r.json()['access_token']}"})
    assert me.status_code == 200 and me.json() == {"username": "alice"}


def test_me_requires_a_valid_token():
    assert client.get("/me").status_code == 401
    assert client.get("/me", headers={"Authorization": "Bearer nope"}).status_code == 401
    register()
    token = login().json()["access_token"]
    assert client.get("/me", headers={"Authorization": f"Bearer {token}x"}).status_code == 401


def test_input_validation():
    assert client.post("/register", json={"username": "ab", "password": "s3cretpass"}).status_code == 422
    assert client.post("/register", json={"username": "alice", "password": "short"}).status_code == 422
