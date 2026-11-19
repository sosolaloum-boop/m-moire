# cabinet-mail-triage

Workflow Mistral qui traite les demandes de travail reçues par mail de la part des
membres du cabinet (domaine `@altairavocats.com`) :

1. **Relevé** — recherche dans la boîte Gmail les threads inédits du domaine du
   cabinet (`from:altairavocats.com in:inbox -label:cabinet-traite`).
2. **Mail → prompt** — chaque mail est transformé en un prompt structuré
   (`build_task_prompt`) : reformulation de la demande, consignes de production.
3. **Livrable en mode révision** — un appel modèle produit le livrable en
   Markdown avec commentaires explicatifs de relecture, une section
   « Vérifications effectuées » et une section « Points ouverts », plus un
   projet de réponse à l'expéditeur (sortie JSON structurée `DeliverableDraft`).
4. **Envoi du livrable** — un mail est envoyé au destinataire configuré
   (`corporatebdx@altairavocats.com`) contenant le livrable, une copie du mail
   initial (contexte) et le projet de réponse à l'expéditeur.
5. **Classement** — le thread reçoit le label `cabinet-traite` pour ne plus être
   retraité.

## Entrée du workflow `cabinet-mail-triage`

| Champ | Défaut | Rôle |
| --- | --- | --- |
| `query` | `from:altairavocats.com in:inbox -label:cabinet-traite` | Requête de recherche Gmail |
| `label_name` | `cabinet-traite` | Label appliqué aux threads traités |
| `deliverable_recipient` | `corporatebdx@altairavocats.com` | Destinataire du mail de livrable |
| `max_threads` | `10` | Nombre max de threads traités par run |
| `model` | `mistral-large-latest` | Modèle produisant le livrable |
| `dry_run` | `false` | Si `true`, produit le livrable sans envoyer de mail ni poser de label |

## Identifiants

Les runs planifiés (sans utilisateur) ne peuvent pas utiliser le connecteur Gmail
(OAuth utilisateur uniquement) : le worker appelle l'API Gmail directement avec
des secrets workspace liés au déploiement :

- `GMAIL_CLIENT_ID`, `GMAIL_CLIENT_SECRET`, `GMAIL_REFRESH_TOKEN` — refresh token
  OAuth2 du compte `laloumsolal@gmail.com`, scope
  `https://www.googleapis.com/auth/gmail.modify` (créés dans Studio, Secrets) ;
- `GMAIL_ACCESS_TOKEN` — alternative court terme pour un test local.

Les secrets ne traversent jamais les frontières workflow/activité : chaque activité
les lit dans son propre environnement.

## Développement local

```bash
cd workflows
uv sync --all-groups
uv run ruff check src tests
uv run ruff format --check src tests
uv run pytest
```

## Déploiement managé

Déploiement construit depuis ce dépôt (`build_directory=workflows`,
`dockerfile_path=Dockerfile`) ; le schedule (toutes les 5 min) est créé via
`l'API schedules` sur le workflow `cabinet-mail-triage`.
