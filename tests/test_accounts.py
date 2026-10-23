"""Accounts (server/accounts.py): registration by the team's code, login, roles, «Історія» that follows the person,
and «Налаштування» → «Про систему»."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import server.app as web
from server import accounts, conversations
from server.accounts import AccountError, check_password, hash_password
from server.web_users import WebUserRegistry

LAPTOP = "laptop-browser-0123456789ab"
PHONE = "phone-browser-0123456789abc"


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "team-secret")
    monkeypatch.setattr(web, "INVITE_CODE", "")
    monkeypatch.setattr(web, "ADMIN_EMAILS", frozenset())
    monkeypatch.setattr(web, "users", WebUserRegistry())
    return TestClient(web.app)


def register(client, email, password="password1", code="team-secret", client_id=None, name=""):
    headers = {"X-Client-Id": client_id} if client_id else {}
    return client.post("/api/auth/register", headers=headers,
                       json={"email": email, "password": password, "invite_code": code, "name": name})


def bearer(res):
    return {"Authorization": "Bearer " + res.json()["token"]}


def test_passwords_are_salted_hashes():
    stored = hash_password("correct horse")
    assert "correct horse" not in stored and stored != hash_password("correct horse")
    assert check_password("correct horse", stored) and not check_password("wrong", stored)
    assert not check_password("x", "garbage")


def test_register_needs_the_team_code_and_the_first_account_is_the_admin(client):
    assert register(client, "a@team.ua", code="nope").status_code == 403
    first = register(client, "Boss@Team.ua", name="  Влад  ")
    assert first.status_code == 200 and first.json()["account"]["role"] == "admin"
    assert first.json()["account"]["email"] == "boss@team.ua" and first.json()["account"]["name"] == "Влад"
    assert register(client, "anna@team.ua").json()["account"]["role"] == "member"
    assert register(client, "anna@team.ua").status_code == 409  # taken
    assert register(client, "short@team.ua", password="1234").status_code == 400
    assert register(client, "not-an-email").status_code == 400


def join(client, name, code="team-secret", client_id=None):
    headers = {"X-Client-Id": client_id} if client_id else {}
    return client.post("/api/auth/join", headers=headers, json={"name": name, "invite_code": code})


def test_by_name_and_the_team_code(client):
    """The usual way in: a name and the code. The same name (any case) is the same account; always a member."""
    assert join(client, "Влад", code="wrong").status_code == 403
    assert join(client, " ", code="team-secret").status_code == 400
    first = join(client, "Влад", client_id=LAPTOP)
    assert first.status_code == 200
    account = first.json()["account"]
    assert account["name"] == "Влад" and account["role"] == "member" and account["by_name"] and account["email"] == ""
    again = join(client, "  влад ", client_id=PHONE)
    assert again.json()["account"]["id"] == account["id"]  # the same person on another device
    assert client.get("/api/me", headers={"X-Client-Id": PHONE, **bearer(again)}).status_code == 200
    # No password: the email login never opens it.
    assert client.post("/api/auth/login", json={"email": "name:влад", "password": ""}).status_code == 401
    # The first email account is still the admin, though by-name people came first.
    boss = register(client, "boss@team.ua")
    assert boss.json()["account"]["role"] == "admin"
    # …and a by-name account is never made an admin nor given a password.
    for patch in ({"role": "admin"}, {"password": "password1"}):
        assert client.post(f"/api/admin/users/{account['id']}", json=patch, headers=bearer(boss)).status_code == 409
    # Renamed, it opens with the new name; a name taken by someone else is refused.
    join(client, "Оля")
    assert client.post("/api/auth/me", json={"name": "Оля"}, headers=bearer(again)).status_code == 409
    assert client.post("/api/auth/me", json={"name": "Влад К"}, headers=bearer(again)).status_code == 200
    assert join(client, "Влад К").json()["account"]["id"] == account["id"]
    # A disabled one can't come in by name either.
    client.post(f"/api/admin/users/{account['id']}", json={"disabled": True}, headers=bearer(boss))
    assert join(client, "Влад К").status_code == 403


def test_wrong_codes_by_name_wait(client):
    for _ in range(accounts.FAILS_MAX):
        assert join(client, "Хтось", code="guess").status_code == 403
    assert join(client, "Хтось").status_code == 429


def test_admin_emails_are_admins_from_registration(client, monkeypatch):
    register(client, "first@team.ua")
    monkeypatch.setattr(web, "ADMIN_EMAILS", frozenset({"cto@team.ua"}))
    assert register(client, "cto@team.ua").json()["account"]["role"] == "admin"


def test_login_logout_and_a_wrong_password(client):
    register(client, "a@team.ua")
    assert client.post("/api/auth/login", json={"email": "a@team.ua", "password": "nope"}).status_code == 401
    res = client.post("/api/auth/login", json={"email": " A@team.ua ", "password": "password1"})
    assert res.status_code == 200
    me = client.get("/api/auth/me", headers=bearer(res))
    assert me.json()["account"]["email"] == "a@team.ua"
    client.post("/api/auth/logout", headers=bearer(res))
    assert client.get("/api/auth/me", headers=bearer(res)).status_code == 401


def test_too_many_wrong_passwords_wait(client):
    register(client, "a@team.ua")
    for _ in range(accounts.FAILS_MAX):
        client.post("/api/auth/login", json={"email": "a@team.ua", "password": "nope"})
    res = client.post("/api/auth/login", json={"email": "a@team.ua", "password": "password1"})
    assert res.status_code == 429


def test_change_password_logs_out_other_devices(client):
    register(client, "a@team.ua")
    laptop = client.post("/api/auth/login", json={"email": "a@team.ua", "password": "password1"})
    phone = client.post("/api/auth/login", json={"email": "a@team.ua", "password": "password1"})
    wrong = client.post("/api/auth/password", json={"current": "nope", "new": "password2"}, headers=bearer(laptop))
    assert wrong.status_code == 401
    assert client.post("/api/auth/password", json={"current": "password1", "new": "password2"},
                       headers=bearer(laptop)).status_code == 200
    assert client.get("/api/auth/me", headers=bearer(laptop)).status_code == 200
    assert client.get("/api/auth/me", headers=bearer(phone)).status_code == 401
    assert client.post("/api/auth/login", json={"email": "a@team.ua", "password": "password2"}).status_code == 200


def test_admin_manages_people_and_the_last_admin_stays(client):
    admin = bearer(register(client, "boss@team.ua"))
    anna = register(client, "anna@team.ua")
    anna_id = anna.json()["account"]["id"]
    assert client.get("/api/admin/users", headers=bearer(anna)).status_code == 403
    listing = client.get("/api/admin/users", headers=admin).json()
    assert [a["email"] for a in listing["accounts"]] == ["boss@team.ua", "anna@team.ua"]
    assert listing["invite_code"] == "team-secret"
    # A temporary password from the admin: her old login ends.
    assert client.post(f"/api/admin/users/{anna_id}", json={"password": "temporary1"}, headers=admin).status_code == 200
    assert client.get("/api/auth/me", headers=bearer(anna)).status_code == 401
    anna = client.post("/api/auth/login", json={"email": "anna@team.ua", "password": "temporary1"})
    # Disabled: out at once, and can't log in.
    client.post(f"/api/admin/users/{anna_id}", json={"disabled": True}, headers=admin)
    assert client.get("/api/auth/me", headers=bearer(anna)).status_code == 401
    assert client.post("/api/auth/login", json={"email": "anna@team.ua", "password": "temporary1"}).status_code == 403
    # The only admin can't make themselves a member or switch themselves off.
    boss_id = listing["accounts"][0]["id"]
    assert client.post(f"/api/admin/users/{boss_id}", json={"role": "member"}, headers=admin).status_code == 409
    assert client.post(f"/api/admin/users/{boss_id}", json={"disabled": True}, headers=admin).status_code == 409
    client.post(f"/api/admin/users/{anna_id}", json={"disabled": False, "role": "admin"}, headers=admin)
    assert client.post(f"/api/admin/users/{boss_id}", json={"role": "member"}, headers=admin).status_code == 200


def test_history_moves_into_the_account_and_follows_to_another_device(client):
    # Talked on the laptop before having an account.
    laptop_user = web.users.get(LAPTOP)
    laptop_user.conversation.add("user", "Нагадай про зустріч")
    laptop_user.conversation.end_turn()
    laptop_user.save_conversation()
    reg = register(client, "a@team.ua", client_id=LAPTOP)
    owner = f"acct-{reg.json()['account']['id']}"
    assert web.users.get(LAPTOP, owner).owner == owner
    listed = client.get("/api/conversations", headers={"X-Client-Id": LAPTOP, **bearer(reg)}).json()["conversations"]
    assert [c["title"] for c in listed] == ["Нагадай про зустріч"]
    # The phone, logged in later, sees the same history.
    phone = client.post("/api/auth/login", headers={"X-Client-Id": PHONE},
                        json={"email": "a@team.ua", "password": "password1"})
    listed = client.get("/api/conversations", headers={"X-Client-Id": PHONE, **bearer(phone)}).json()["conversations"]
    assert [c["title"] for c in listed] == ["Нагадай про зустріч"]
    # …but doesn't write into the conversation the laptop has open right now.
    assert web.users.get(PHONE, owner).conversation_id != web.users.get(LAPTOP, owner).conversation_id
    # Logged out on the laptop: the page there starts clean, the account keeps its history.
    client.post("/api/auth/logout", headers={"X-Client-Id": LAPTOP, **bearer(reg)})
    assert len(web.users.get(LAPTOP).conversation) == 0
    assert conversations.store.list(owner)[0]["title"] == "Нагадай про зустріч"


def test_without_a_team_code_nobody_needs_to_log_in(client, monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    assert client.get("/api/config").json() == {"login_required": False, "invite_required": False, "guest_allowed": True}
    assert client.get("/api/me", headers={"X-Client-Id": LAPTOP}).status_code == 200
    assert register(client, "dev@local.ua", code="").status_code == 200


def test_system_info_for_members_and_costs_for_admins(client, monkeypatch):
    monkeypatch.setenv("RENDER_GIT_COMMIT", "abcdef1234567890")
    monkeypatch.setenv("RENDER_GIT_REPO_SLUG", "team/voice-agent")
    assert client.get("/api/system").status_code == 401
    admin = bearer(register(client, "boss@team.ua"))
    member = bearer(register(client, "anna@team.ua"))
    info = client.get("/api/system", headers=member).json()
    assert info["deploy"]["commit"] == "abcdef1"
    assert info["deploy"]["commit_url"] == "https://github.com/team/voice-agent/commit/abcdef1234567890"
    assert info["models"]["live"] and info["accounts"] == 2 and "usage" not in info
    assert "usage" in client.get("/api/system", headers=admin).json()


def test_store_rejects_a_bad_role():
    s = accounts.store
    a = s.create("x@team.ua", "password1")
    with pytest.raises(AccountError):
        s.update(a.id, role="owner")
