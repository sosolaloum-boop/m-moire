import asyncio
import base64
import email
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest
from mistralai.workflows.testing import create_test_worker

from worker.workflows import cabinet_mail as cm


class FakeResponse:
    def __init__(self, status_code: int = 200, payload: object = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.content = b"x" if payload is not None else b""
        self.headers: dict[str, str] = {}

    def json(self) -> object:
        return self._payload


def _gmail_thread_payload() -> dict:
    text = (
        "Bonjour Solal,\n\n"
        "Peux-tu préparer une note sur la cession de parts sociales ? Merci."
    )
    body = base64.urlsafe_b64encode(text.encode()).decode()
    return {
        "id": "thread-1",
        "messages": [
            {
                "id": "msg-1",
                "payload": {
                    "headers": [
                        {"name": "From", "value": "pgramage@altairavocats.com"},
                        {"name": "To", "value": "laloumsolal@gmail.com"},
                        {"name": "Subject", "value": "Note cession de parts"},
                        {"name": "Date", "value": "Mon, 16 Nov 2026 10:00:00 +0100"},
                    ],
                    "mimeType": "text/plain",
                    "body": {"data": body},
                    "parts": [
                        {
                            "mimeType": "application/pdf",
                            "filename": "projet-statuts.pdf",
                            "body": {"size": 12345},
                        }
                    ],
                },
            }
        ],
    }


DRAFT = {
    "titre": "Note sur la cession de parts sociales",
    "livrable": "# Note\n\n<!-- commentaire de relecture --> Contenu...",
    "verifications": ["Vérification 1 : cohérence du plan"],
    "points_ouverts": ["Capital social à confirmer [À VÉRIFIER]"],
    "projet_reponse": "Bonjour Maître Gramage, vous trouverez ci-joint la note...",
}


class FakeAsyncClient:
    requests: list[tuple] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    async def __aenter__(self) -> "FakeAsyncClient":
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def post(self, url, data=None, **kwargs):
        FakeAsyncClient.requests.append(("POST", url, None, data))
        if "oauth2.googleapis.com" in url:
            assert data["grant_type"] == "refresh_token"
            return FakeResponse(200, {"access_token": "fresh-token"})
        return self._route("POST", url, None, data)

    async def request(self, method, url, params=None, json=None):
        FakeAsyncClient.requests.append((method, url, params, json))
        return self._route(method, url, params, json)

    def _route(self, method, url, params, payload):
        path = url.replace(cm.GMAIL_API_BASE, "")
        if path == "/users/me/labels" and method == "GET":
            return FakeResponse(200, {"labels": []})
        if path == "/users/me/labels" and method == "POST":
            assert payload["name"] == cm.DEFAULT_LABEL
            return FakeResponse(200, {"id": "Label_1"})
        if path == "/users/me/messages" and method == "GET":
            assert params["q"] == cm.DEFAULT_QUERY
            return FakeResponse(
                200, {"messages": [{"id": "msg-1", "threadId": "thread-1"}]}
            )
        if path == "/users/me/threads/thread-1" and method == "GET":
            return FakeResponse(200, _gmail_thread_payload())
        if path == "/users/me/messages/send" and method == "POST":
            return FakeResponse(200, {"id": "sent-1", "threadId": "thread-1"})
        if path == "/users/me/threads/thread-1/modify" and method == "POST":
            assert payload == {"addLabelIds": ["Label_1"]}
            return FakeResponse(200, {"id": "thread-1"})
        raise AssertionError(f"unexpected request: {method} {url} {params}")


class FakeChat:
    async def complete_async(self, **kwargs) -> SimpleNamespace:
        assert kwargs["response_format"] == {"type": "json_object"}
        assert "pgramage@altairavocats.com" in kwargs["messages"][0]["content"]
        return SimpleNamespace(
            choices=[
                SimpleNamespace(message=SimpleNamespace(content=json.dumps(DRAFT)))
            ]
        )


class FakeMistralClient:
    chat = FakeChat()


@pytest.fixture()
def fake_services(monkeypatch):
    FakeAsyncClient.requests = []
    monkeypatch.setattr(cm, "_http_client", lambda **kwargs: FakeAsyncClient(**kwargs))
    monkeypatch.setattr(
        cm.mistralai_plugin, "get_mistral_client", lambda *a, **kw: FakeMistralClient()
    )
    monkeypatch.setenv("GMAIL_CLIENT_ID", "cid")
    monkeypatch.setenv("GMAIL_CLIENT_SECRET", "secret")
    monkeypatch.setenv("GMAIL_REFRESH_TOKEN", "refresh")
    return FakeAsyncClient


def test_message_view_parses_body_and_attachments() -> None:
    view = cm._message_view(_gmail_thread_payload()["messages"][0])
    assert view.sender == "pgramage@altairavocats.com"
    assert view.subject == "Note cession de parts"
    assert "Peux-tu préparer une note" in view.body
    assert view.attachments == ["projet-statuts.pdf (application/pdf, 12345 octets)"]


def test_build_task_prompt_contains_request_context() -> None:
    thread = cm._thread_view(_gmail_thread_payload())
    request_message = cm._requesting_message(thread)
    prompt = cm.build_task_prompt(thread, request_message)
    assert "pgramage@altairavocats.com" in prompt
    assert "Note cession de parts" in prompt
    assert "Peux-tu préparer une note" in prompt
    assert "projet-statuts.pdf" in prompt
    assert "projet_reponse" in prompt


def test_build_deliverable_email_sections() -> None:
    thread = cm._thread_view(_gmail_thread_payload())
    request_message = cm._requesting_message(thread)
    draft = cm.DeliverableDraft.model_validate(DRAFT)
    mail = cm.build_deliverable_email(
        thread, request_message, draft, cm.DEFAULT_RECIPIENT
    )
    assert mail.to == "corporatebdx@altairavocats.com"
    assert mail.subject == "[Livrable] Note cession de parts"
    for section in (
        "=== LIVRABLE :",
        "=== VÉRIFICATIONS EFFECTUÉES ===",
        "=== POINTS OUVERTS ===",
        "=== COPIE DU MAIL INITIAL",
        "> Bonjour Solal,",
        "=== PROJET DE RÉPONSE À pgramage@altairavocats.com ===",
    ):
        assert section in mail.body


async def test_workflow_processes_thread_end_to_end(
    temporal_env, fake_services
) -> None:
    async with create_test_worker(
        temporal_env,
        workflows=[cm.CabinetMailTriage],
        activities=[
            cm.gmail_ensure_label,
            cm.gmail_list_threads,
            cm.gmail_get_thread,
            cm.produce_deliverable,
            cm.gmail_send_deliverable,
            cm.gmail_label_thread,
        ],
    ):
        handle = await temporal_env.client.start_workflow(
            "cabinet-mail-triage",
            {"max_threads": 5},
            id="test-e2e",
            task_queue="test-task-queue",
            execution_timeout=timedelta(seconds=30),
        )
        result = await asyncio.wait_for(handle.result(), timeout=45)
    assert result["candidates"] == 1
    assert result["label_id"] == "Label_1"
    assert result["results"][0]["status"] == "sent"
    assert result["results"][0]["sender"] == "pgramage@altairavocats.com"
    methods_paths = [(r[0], r[1]) for r in fake_services.requests]
    assert ("POST", "https://oauth2.googleapis.com/token") in methods_paths
    assert any(p == "/users/me/messages/send" for _, p in methods_paths)
    assert any(p == "/users/me/threads/thread-1/modify" for _, p in methods_paths)
    send_call = next(
        r for r in fake_services.requests if r[1].endswith("/users/me/messages/send")
    )
    raw = send_call[3]["raw"]
    parsed = email.message_from_bytes(base64.urlsafe_b64decode(raw))
    assert parsed["To"] == "corporatebdx@altairavocats.com"
    body = parsed.get_payload(decode=True).decode()
    assert "=== LIVRABLE :" in body
    assert "=== COPIE DU MAIL INITIAL" in body
    assert "=== PROJET DE RÉPONSE À pgramage@altairavocats.com ===" in body


async def test_workflow_dry_run_sends_nothing(temporal_env, fake_services) -> None:
    async with create_test_worker(
        temporal_env,
        workflows=[cm.CabinetMailTriage],
        activities=[
            cm.gmail_ensure_label,
            cm.gmail_list_threads,
            cm.gmail_get_thread,
            cm.produce_deliverable,
            cm.gmail_send_deliverable,
            cm.gmail_label_thread,
        ],
    ):
        handle = await temporal_env.client.start_workflow(
            "cabinet-mail-triage",
            {"dry_run": True},
            id="test-dry-run",
            task_queue="test-task-queue",
            execution_timeout=timedelta(seconds=30),
        )
        result = await asyncio.wait_for(handle.result(), timeout=45)
    assert result["results"][0]["status"] == "dry_run"
    assert "LIVRABLE" in result["results"][0]["detail"]
    assert not any(
        r[1].endswith("/users/me/messages/send") for r in fake_services.requests
    )
    assert not any(r[1].endswith("/modify") for r in fake_services.requests)


async def test_workflow_static_access_token_skips_refresh(
    temporal_env, fake_services, monkeypatch
) -> None:
    monkeypatch.setenv("GMAIL_ACCESS_TOKEN", "static-token")
    monkeypatch.delenv("GMAIL_CLIENT_ID")
    monkeypatch.delenv("GMAIL_CLIENT_SECRET")
    monkeypatch.delenv("GMAIL_REFRESH_TOKEN")
    async with create_test_worker(
        temporal_env,
        workflows=[cm.CabinetMailTriage],
        activities=[
            cm.gmail_ensure_label,
            cm.gmail_list_threads,
            cm.gmail_get_thread,
            cm.produce_deliverable,
            cm.gmail_send_deliverable,
            cm.gmail_label_thread,
        ],
    ):
        handle = await temporal_env.client.start_workflow(
            "cabinet-mail-triage",
            {"dry_run": True},
            id="test-static-token",
            task_queue="test-task-queue",
            execution_timeout=timedelta(seconds=30),
        )
        result = await asyncio.wait_for(handle.result(), timeout=45)
    assert result["results"][0]["status"] == "dry_run"
    assert not any("oauth2.googleapis.com" in r[1] for r in fake_services.requests)
