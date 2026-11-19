"""Triage des demandes de travail du cabinet reçues par mail.

Pour chaque thread inédit en provenance du domaine du cabinet (par défaut
`@altairavocats.com`) : le mail est transformé en prompt, le livrable est produit
en mode révision (commentaires explicatifs + vérifications), puis envoyé par mail
au destinataire configuré avec une copie du mail initial et un projet de réponse
à l'expéditeur. Le thread est ensuite étiqueté comme traité.

Les identifiants Gmail ne traversent jamais les frontières workflow/activité :
chaque activité les résout depuis l'environnement du worker (secrets workspace
`GMAIL_CLIENT_ID` / `GMAIL_CLIENT_SECRET` / `GMAIL_REFRESH_TOKEN`, ou
`GMAIL_ACCESS_TOKEN` pour un test court) et appelle l'API Gmail directement.
"""

import base64
import json
import os
from datetime import timedelta
from email.message import EmailMessage
from typing import Any

import mistralai.workflows as workflows
from mistralai.workflows import activity, workflow
from mistralai.workflows.plugins import mistralai as mistralai_plugin
from pydantic import BaseModel, Field

GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
CABINET_DOMAIN = "altairavocats.com"
DEFAULT_LABEL = "cabinet-traite"
DEFAULT_QUERY = f"from:{CABINET_DOMAIN} in:inbox -label:{DEFAULT_LABEL}"
DEFAULT_RECIPIENT = "corporatebdx@altairavocats.com"
DEFAULT_MODEL = "mistral-large-latest"


class TriageInput(BaseModel):
    query: str = DEFAULT_QUERY
    label_name: str = DEFAULT_LABEL
    deliverable_recipient: str = DEFAULT_RECIPIENT
    max_threads: int = Field(default=10, ge=1, le=50)
    model: str = DEFAULT_MODEL
    dry_run: bool = False


class MessageView(BaseModel):
    id: str
    sender: str
    to: list[str]
    cc: list[str]
    subject: str
    date: str
    body: str
    attachments: list[str]


class ThreadView(BaseModel):
    thread_id: str
    subject: str
    messages: list[MessageView]


class ThreadRef(BaseModel):
    id: str
    thread_id: str


class DeliverableDraft(BaseModel):
    titre: str
    livrable: str
    verifications: list[str]
    points_ouverts: list[str]
    projet_reponse: str


class EmailDraft(BaseModel):
    to: str
    subject: str
    body: str


class ThreadResult(BaseModel):
    thread_id: str
    subject: str
    sender: str
    status: str
    detail: str = ""


class TriageOutput(BaseModel):
    label_id: str
    candidates: int
    results: list[ThreadResult]


def _http_client(**kwargs: Any) -> Any:
    import httpx

    return httpx.AsyncClient(**kwargs)


async def _gmail_token() -> str:
    static_token = os.environ.get("GMAIL_ACCESS_TOKEN", "")
    if static_token:
        return static_token
    client_id = os.environ.get("GMAIL_CLIENT_ID", "")
    client_secret = os.environ.get("GMAIL_CLIENT_SECRET", "")
    refresh_token = os.environ.get("GMAIL_REFRESH_TOKEN", "")
    if not (client_id and client_secret and refresh_token):
        raise workflows.WorkflowError(
            "gmail_credentials_not_configured", non_retryable=True
        )
    async with _http_client(timeout=timedelta(seconds=30)) as http:
        response = await http.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
    if response.status_code != 200:
        retryable = response.status_code in {429, 500, 502, 503, 504}
        raise workflows.WorkflowError(
            f"gmail_token_http_{response.status_code}", non_retryable=not retryable
        )
    return str(response.json()["access_token"])


async def _gmail_request(
    method: str,
    path: str,
    params: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> Any:
    token = await _gmail_token()
    async with _http_client(
        base_url=GMAIL_API_BASE,
        timeout=timedelta(seconds=30),
        headers={"Authorization": f"Bearer {token}"},
    ) as http:
        response = await http.request(method, path, params=params, json=payload)
    if response.status_code >= 400:
        retryable = response.status_code in {429, 500, 502, 503, 504}
        raise workflows.WorkflowError(
            f"gmail_http_{response.status_code}", non_retryable=not retryable
        )
    if not response.content:
        return {}
    return response.json()


def _header_value(headers: list[dict[str, Any]], name: str) -> str:
    lowered = name.lower()
    for header in headers:
        if str(header.get("name", "")).lower() == lowered:
            return str(header.get("value", ""))
    return ""


def _decode_body(data: str | None) -> str:
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    except ValueError:
        return ""


def _collect_text_and_attachments(
    part: dict[str, Any],
    texts: list[str],
    attachments: list[str],
) -> None:
    mime = str(part.get("mimeType", ""))
    filename = str(part.get("filename", "") or "")
    if filename:
        size = part.get("body", {}).get("size", "?")
        attachments.append(f"{filename} ({mime}, {size} octets)")
    elif mime == "text/plain":
        texts.append(_decode_body(part.get("body", {}).get("data")))
    for sub in part.get("parts", []) or []:
        _collect_text_and_attachments(sub, texts, attachments)


def _message_view(message: dict[str, Any]) -> MessageView:
    payload = message.get("payload", {})
    headers = payload.get("headers", []) or []
    texts: list[str] = []
    attachments: list[str] = []
    _collect_text_and_attachments(payload, texts, attachments)
    return MessageView(
        id=str(message.get("id", "")),
        sender=_header_value(headers, "From"),
        to=[v for v in _header_value(headers, "To").split(",") if v],
        cc=[v for v in _header_value(headers, "Cc").split(",") if v],
        subject=_header_value(headers, "Subject"),
        date=_header_value(headers, "Date"),
        body="\n".join(t for t in texts if t),
        attachments=attachments,
    )


def _thread_view(thread: dict[str, Any]) -> ThreadView:
    messages = [_message_view(m) for m in thread.get("messages", []) or []]
    subject = messages[-1].subject if messages else ""
    return ThreadView(
        thread_id=str(thread.get("id", "")), subject=subject, messages=messages
    )


def _requesting_message(thread: ThreadView) -> MessageView:
    domain = f"@{CABINET_DOMAIN}"
    for message in reversed(thread.messages):
        if message.sender.lower().endswith(domain):
            return message
    if not thread.messages:
        raise workflows.WorkflowError("thread_without_messages", non_retryable=True)
    return thread.messages[-1]


def build_task_prompt(thread: ThreadView, request_message: MessageView) -> str:
    attachments = "\n".join(f"- {name}" for name in request_message.attachments)
    if not attachments:
        attachments = "- (aucune)"
    recipients = ", ".join(request_message.to)
    prompt = f"""Tu es l'assistant de Solal Laloum, stagiaire au cabinet d'avocats
Altaïr Avocats (Bordeaux). Un membre du cabinet vient de lui adresser le mail
ci-dessous, qui formule une demande de travail.

MISSION
1. Reformule la demande en une tâche claire et actionable : quoi produire,
   pour qui, dans quel délai si précisé, sous quel format.
2. Produis le livrable demandé, exactement comme dans une session de travail
   en mode révision (mode Work) :
   - le livrable en Markdown, structuré et directement exploitable ;
   - des commentaires explicatifs à chaque choix ou étape importante, comme
     des remarques de relecture placées à côté du passage concerné ;
   - une section « Vérifications effectuées » listant les contrôles faits
     (cohérence, complétude, sources, points invérifiables) ;
   - une section « Points ouverts » listant ce qui manque ou reste à confirmer.
   Ne invente aucun fait, aucun arrêt, aucun montant : marque [À VÉRIFIER] là
   où une information fait défaut. Ton professionnel, sobre, juridique.
3. Rédige un projet de réponse par mail à l'expéditeur : bref, professionnel,
   qui présente le livrable et les points ouverts.

MAIL INITIAL (contexte de la demande)
- Expéditeur : {request_message.sender}
- Destinataires : {recipients}
- Date : {request_message.date}
- Objet : {request_message.subject}
- Pièces jointes :
{attachments}
- Le fil contient {len(thread.messages)} message(s).

Corps du mail :
\"\"\"
{request_message.body}
\"\"\"

RÉPONDS UNIQUEMENT avec un objet JSON valide (aucun texte autour), avec
 exactement ces champs :
{{
  "titre": "titre court du livrable",
  "livrable": "livrable en Markdown, avec commentaires de relecture",
  "verifications": ["liste des vérifications effectuées"],
  "points_ouverts": ["liste des points ouverts / questions"],
  "projet_reponse": "projet de mail de réponse à l'expéditeur"
}}
"""
    return prompt


def build_deliverable_email(
    thread: ThreadView,
    request_message: MessageView,
    draft: DeliverableDraft,
    recipient: str,
) -> EmailDraft:
    subject = f"[Livrable] {thread.subject}"
    quoted = "\n".join(f"> {line}" for line in request_message.body.splitlines())
    verifications = (
        "\n".join(f"- {item}" for item in draft.verifications) or "- (aucune)"
    )
    open_points = "\n".join(f"- {item}" for item in draft.points_ouverts) or "- (aucun)"
    attachments = "\n".join(f"- {name}" for name in request_message.attachments)
    if not attachments:
        attachments = "- (aucune)"
    recipients = ", ".join(request_message.to)
    intro = (
        "Un membre du cabinet t'a adressé une demande de travail (mail initial "
        "copié ci-dessous). Voici le livrable produit en mode révision, avec les "
        "commentaires explicatifs et les vérifications."
    )
    footer = "Mail généré automatiquement par le workflow « cabinet-mail-triage »."
    body = f"""Bonjour Solal,

{intro}

DEMANDE
- Expéditeur : {request_message.sender}
- Objet : {request_message.subject}
- Date : {request_message.date}

=== LIVRABLE : {draft.titre} ===

{draft.livrable}

=== VÉRIFICATIONS EFFECTUÉES ===

{verifications}

=== POINTS OUVERTS ===

{open_points}

=== COPIE DU MAIL INITIAL (contexte) ===

De : {request_message.sender}
À : {recipients}
Date : {request_message.date}
Objet : {request_message.subject}
Pièces jointes :
{attachments}

{quoted}

=== PROJET DE RÉPONSE À {request_message.sender} ===

{draft.projet_reponse}

--
{footer}
"""
    return EmailDraft(to=recipient, subject=subject, body=body)


def _parse_json_object(content: str) -> dict[str, Any]:
    text = content.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char == "{":
            try:
                value, _ = decoder.raw_decode(text[index:])
            except ValueError:
                continue
            if isinstance(value, dict):
                return value
    raise workflows.WorkflowError("llm_output_not_json", non_retryable=True)


@activity(start_to_close_timeout=timedelta(minutes=2))
async def gmail_ensure_label(label_name: str) -> str:
    data = await _gmail_request("GET", "/users/me/labels")
    for label in data.get("labels", []) or []:
        if str(label.get("name", "")).lower() == label_name.lower():
            return str(label["id"])
    created = await _gmail_request(
        "POST",
        "/users/me/labels",
        payload={
            "name": label_name,
            "labelListVisibility": "labelShow",
            "messageListVisibility": "show",
        },
    )
    return str(created["id"])


@activity(start_to_close_timeout=timedelta(minutes=2))
async def gmail_list_threads(query: str, max_results: int) -> list[ThreadRef]:
    data = await _gmail_request(
        "GET", "/users/me/messages", params={"q": query, "maxResults": str(max_results)}
    )
    return [
        ThreadRef(id=str(m["id"]), thread_id=str(m["threadId"]))
        for m in data.get("messages", []) or []
    ]


@activity(start_to_close_timeout=timedelta(minutes=2))
async def gmail_get_thread(thread_id: str) -> ThreadView:
    data = await _gmail_request(
        "GET", f"/users/me/threads/{thread_id}", params={"format": "full"}
    )
    return _thread_view(data)


@activity(start_to_close_timeout=timedelta(minutes=8))
async def produce_deliverable(prompt: str, model: str) -> DeliverableDraft:
    client = mistralai_plugin.get_mistral_client()
    response = await client.chat.complete_async(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        response_format={"type": "json_object"},
        temperature=0.2,
        max_tokens=16000,
    )
    content = response.choices[0].message.content
    if isinstance(content, list):
        content = "".join(str(chunk) for chunk in content)
    return DeliverableDraft.model_validate(_parse_json_object(str(content)))


@activity(start_to_close_timeout=timedelta(minutes=2))
async def gmail_send_deliverable(
    to: str, subject: str, body: str, thread_id: str
) -> dict[str, Any]:
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
    return await _gmail_request(
        "POST", "/users/me/messages/send", payload={"raw": raw, "threadId": thread_id}
    )


@activity(start_to_close_timeout=timedelta(minutes=2))
async def gmail_label_thread(thread_id: str, label_id: str) -> dict[str, Any]:
    return await _gmail_request(
        "POST",
        f"/users/me/threads/{thread_id}/modify",
        payload={"addLabelIds": [label_id]},
    )


@workflow.define(
    name="cabinet-mail-triage",
    workflow_display_name="Triage des demandes du cabinet par mail",
    workflow_description=(
        "Relève les mails inédits du domaine du cabinet, transforme chaque "
        "demande en prompt, produit le livrable en mode révision avec "
        "vérifications, puis l'envoie par mail au destinataire configuré avec "
        "une copie du mail initial et un projet de réponse, et étiquette le "
        "thread comme traité."
    ),
)
class CabinetMailTriage:
    @workflow.entrypoint
    async def run(self, input: TriageInput) -> TriageOutput:
        label_id = await gmail_ensure_label(input.label_name)
        refs = await gmail_list_threads(input.query, input.max_threads)
        results: list[ThreadResult] = []
        for ref in refs:
            try:
                thread = await gmail_get_thread(ref.thread_id)
                request_message = _requesting_message(thread)
                prompt = build_task_prompt(thread, request_message)
                draft = await produce_deliverable(prompt, input.model)
                mail = build_deliverable_email(
                    thread, request_message, draft, input.deliverable_recipient
                )
                if input.dry_run:
                    results.append(
                        ThreadResult(
                            thread_id=thread.thread_id,
                            subject=thread.subject,
                            sender=request_message.sender,
                            status="dry_run",
                            detail=mail.body,
                        )
                    )
                else:
                    await gmail_send_deliverable(
                        mail.to, mail.subject, mail.body, thread.thread_id
                    )
                    await gmail_label_thread(thread.thread_id, label_id)
                    results.append(
                        ThreadResult(
                            thread_id=thread.thread_id,
                            subject=thread.subject,
                            sender=request_message.sender,
                            status="sent",
                        )
                    )
            except Exception as exc:
                results.append(
                    ThreadResult(
                        thread_id=ref.thread_id,
                        subject="",
                        sender="",
                        status="error",
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )
        return TriageOutput(label_id=label_id, candidates=len(refs), results=results)
