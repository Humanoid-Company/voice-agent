"""«Знання»: a file → records → a team set in the store → Єва looks it up (server/knowledge*.py)."""
from __future__ import annotations

import asyncio
import io
import json
import os
import urllib.parse

import pytest
from fastapi.testclient import TestClient

import server.app as web
from server import knowledge
from server.knowledge import KnowledgeStore, Record, _score
from server.knowledge_import import KnowledgeImportError, parse_file, table_to_items
from tools.executor import ToolExecutionContext

PEOPLE_CSV = (
    "Ім'я;Прізвище;Вік;Хобі;Улюблені фільми\n"
    "Олег;Петренко;34;риболовля, шахи;Кримінальне чтиво\n"
    "Марина;Коваль;29;йога;Амелі\n"
    ";;;;\n"
).encode()


# The same tests on Postgres (Neon's engine) when a throwaway database is given:
# KNOWLEDGE_TEST_DATABASE_URL=postgresql://… pytest tests/test_knowledge.py — its kb_* tables are dropped.
TEST_PG = os.getenv("KNOWLEDGE_TEST_DATABASE_URL", "")


@pytest.fixture(params=["sqlite", "postgres"])
def store(request, tmp_path, monkeypatch):
    if request.param == "postgres":
        if not TEST_PG:
            pytest.skip("KNOWLEDGE_TEST_DATABASE_URL not set")
        import psycopg

        with psycopg.connect(TEST_PG) as conn:
            conn.execute("DROP TABLE IF EXISTS kb_items, kb_sets")
        s = KnowledgeStore(TEST_PG)
    else:
        s = KnowledgeStore("", str(tmp_path / "kb.db"))
    monkeypatch.setattr(knowledge, "store", s)
    return s


@pytest.fixture()
def client(store, monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "")
    return TestClient(web.app)


# ── files → records ──
def test_csv_semicolons_first_and_last_name_make_the_title():
    parsed = parse_file("люди.csv", PEOPLE_CSV)
    assert [i["title"] for i in parsed.items] == ["Олег Петренко", "Марина Коваль"]  # the empty row is skipped
    assert parsed.items[0]["fields"] == {"Вік": "34", "Хобі": "риболовля, шахи", "Улюблені фільми": "Кримінальне чтиво"}
    assert parsed.to_dict()["columns"] == ["Вік", "Хобі", "Улюблені фільми"]


def test_csv_in_windows_1251_and_commas():
    data = "ПІБ,Посада\nІрина Мельник,дизайнерка\n".encode("cp1251")
    assert parse_file("a.csv", data).items == [{"title": "Ірина Мельник", "fields": {"Посада": "дизайнерка"}}]


def test_no_name_column_takes_the_first():
    items = table_to_items([["Хто саме", "Хобі"], ["Тарас", "біг"]])
    assert items == [{"title": "Тарас", "fields": {"Хобі": "біг"}}]


@pytest.mark.parametrize("doc", [
    [{"name": "Олег", "age": 34, "films": ["Чтиво", "Матриця"]}],
    {"people": [{"name": "Олег", "age": 34, "films": ["Чтиво", "Матриця"]}]},
    {"Олег": {"age": 34, "films": ["Чтиво", "Матриця"]}},
])
def test_json_shapes(doc):
    items = parse_file("a.json", json.dumps(doc, ensure_ascii=False).encode()).items
    assert items == [{"title": "Олег", "fields": {"age": "34", "films": "Чтиво, Матриця"}}]


def test_xlsx():
    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.append(["ПІБ", "Вік", "Хобі"])
    sheet.append(["Олег Петренко", 34, "шахи"])
    buf = io.BytesIO()
    book.save(buf)
    assert parse_file("a.xlsx", buf.getvalue()).items == [{"title": "Олег Петренко", "fields": {"Вік": "34", "Хобі": "шахи"}}]


def test_docx_table_and_text_through_the_model():
    from docx import Document

    doc = Document()
    doc.add_paragraph("Корпоратив 20.10 починається о 19:00 у ресторані «Липа». " * 6)
    table = doc.add_table(rows=2, cols=2)
    table.rows[0].cells[0].text, table.rows[0].cells[1].text = "ПІБ", "Хобі"
    table.rows[1].cells[0].text, table.rows[1].cells[1].text = "Олег", "шахи"
    buf = io.BytesIO()
    doc.save(buf)
    seen = []

    def extract(text):
        seen.append(text)
        return [{"title": "Корпоратив 20.10", "fields": {"початок": "19:00", "місце": "ресторан «Липа»"}}]

    parsed = parse_file("event.docx", buf.getvalue(), extract)
    assert [i["title"] for i in parsed.items] == ["Олег", "Корпоратив 20.10"]
    assert parsed.kind == "mixed" and "Липа" in seen[0] and any("модель" in w for w in parsed.warnings)


def test_text_without_a_model_and_bad_files_say_why():
    with pytest.raises(KnowledgeImportError, match="таблицю"):
        parse_file("a.txt", "Олег любить шахи.".encode())
    with pytest.raises(KnowledgeImportError, match=r"\.docx"):
        parse_file("a.doc", b"x")
    with pytest.raises(KnowledgeImportError, match="CSV"):
        parse_file("a.pdf", b"x")
    with pytest.raises(KnowledgeImportError, match="записів"):
        parse_file("a.csv", b"only a header\n")


# ── the store ──
def test_store_create_toggle_delete(store):
    created = store.create_set("Корпоратив 20.10", parse_file("a.csv", PEOPLE_CSV).items, note="хто прийде", source="a.csv")
    assert created["count"] == 2 and created["items"][1]["title"] == "Марина Коваль"
    sets, records = store.enabled_records()
    assert sets[0]["name"] == "Корпоратив 20.10" and sets[0]["titles"] == ["Олег Петренко", "Марина Коваль"]
    assert records[0].line() == "Олег Петренко — Вік: 34; Хобі: риболовля, шахи; Улюблені фільми: Кримінальне чтиво"
    store.update_set(created["id"], enabled=False)
    assert store.enabled_records() == ([], [])  # off: Єва doesn't know it
    assert store.delete_set(created["id"]) and store.list_sets() == []


def test_lookup_returns_everything_while_it_fits_else_the_closest(store, monkeypatch):
    store.create_set("Корпоратив", parse_file("a.csv", PEOPLE_CSV).items)
    found = store.lookup("Олегові")
    assert found["shown"] == found["total"] == 2 and "Марина Коваль" in found["text"]
    monkeypatch.setattr(knowledge, "LOOKUP_ALL_CHARS", 60)
    store._changed()
    found = store.lookup("що любить Марина")  # too big to send all: the closest record comes first
    assert found["shown"] == 2 and found["text"].startswith("[Корпоратив] Марина Коваль")


def test_score_matches_declined_and_misheard_names():
    oleh = Record("s", "Олег Петренко", {"хобі": "шахи"})
    maryna = Record("s", "Марина Коваль", {"хобі": "йога"})
    assert _score("Олегові", oleh) > _score("Олегові", maryna)
    assert _score("про Марину", maryna) > _score("про Марину", oleh)
    assert _score("хто грає в шахи", oleh) > _score("хто грає в шахи", maryna)


# ── HTTP: the page's two steps, the switch, delete, the access code ──
def test_upload_preview_then_save(client, store):
    res = client.post("/api/knowledge/parse", content=PEOPLE_CSV,
                      headers={"X-File-Name": urllib.parse.quote("люди корпоратив.csv")})
    assert res.status_code == 200 and len(res.json()["items"]) == 2
    assert client.get("/api/knowledge").json()["sets"] == []  # nothing saved by the preview
    items = res.json()["items"][:1]  # the user removed Марина in the preview
    saved = client.post("/api/knowledge", json={"name": "Корпоратив 20.10", "items": items, "source": "люди.csv"}).json()
    assert saved["count"] == 1
    listing = client.get("/api/knowledge").json()
    assert listing["storage"] == store.kind and listing["sets"][0]["name"] == "Корпоратив 20.10"
    assert client.post(f"/api/knowledge/{saved['id']}", json={"enabled": False}).json()["enabled"] is False
    assert client.get(f"/api/knowledge/{saved['id']}").json()["items"][0]["title"] == "Олег Петренко"
    assert client.delete(f"/api/knowledge/{saved['id']}").json() == {"ok": True}
    assert client.get(f"/api/knowledge/{saved['id']}").status_code == 404


def test_bad_file_and_empty_set_are_refused_with_a_reason(client):
    res = client.post("/api/knowledge/parse", content=b"x", headers={"X-File-Name": "a.pdf"})
    assert res.status_code == 422 and "CSV" in res.json()["detail"]
    res = client.post("/api/knowledge", json={"name": "Порожній", "items": []})
    assert res.status_code == 503 and "запису" in res.json()["detail"]


def test_knowledge_needs_the_access_code(client, monkeypatch):
    monkeypatch.setattr(web, "ACCESS_CODE", "team-secret")
    assert client.get("/api/knowledge").status_code == 401
    assert client.post("/api/knowledge/parse", content=PEOPLE_CSV, headers={"X-File-Name": "a.csv"}).status_code == 401
    assert client.get("/api/knowledge", headers={"X-Access-Code": "team-secret"}).status_code == 200


# ── Єва ──
def test_the_tool_and_the_prompts(store):
    user = web.users.get("kb-browser-0123456789abc")
    ctx = ToolExecutionContext(session_id="s")
    empty = asyncio.run(user.executor.execute("knowledge_lookup", {"query": "Олег"}, ctx))
    assert empty.status == "empty" and "Знання" in empty.message
    store.create_set("Корпоратив 20.10", parse_file("a.csv", PEOPLE_CSV).items)
    found = asyncio.run(user.executor.execute("knowledge_lookup", {"query": "Олегові", "set": "корпоратив"}, ctx))
    assert found.ok and "Олег Петренко — Вік: 34" in found.message and "лише з них" in found.message
    config = web._session_config(user, web._knowledge_sets())
    assert "«Корпоратив 20.10» (2: Олег Петренко, Марина Коваль)" in config["instructions"]
    backend = config["delegation"]["responses"]
    assert "knowledge_lookup" in backend["instructions"] and any(t["name"] == "knowledge_lookup" for t in backend["tools"])
    assert "Team knowledge" not in web._session_config(user)["instructions"]  # nothing uploaded → not mentioned
