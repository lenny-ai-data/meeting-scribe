<p align="center"><img src="web/logo-mark.svg" width="72" alt=""></p>

# Meeting Scribe

Transcription de réunions en français (ou en anglais), identification des intervenants, transcript Markdown et comptes rendus générés par un LLM. Le service est auto-hébergé et remplace Whishper.

- **Toute la logique est dans l'API** (`/api`, documentation interactive sur `/docs`). L'interface web n'en est qu'un client : n8n ou un agent peuvent faire exactement la même chose.
- **Chaîne de traitement** : WhisperX 3.8.6, c'est-à-dire faster-whisper `large-v3` / `large-v3-turbo`, puis alignement mot à mot, puis diarisation avec `pyannote/speaker-diarization-community-1`.
- **Comptes rendus** : Ollama en local (`qwen3.8:27b` par défaut), ou n'importe quelle API compatible OpenAI.
- **Un seul traitement à la fois**. Avant de charger Whisper, les modèles d'Ollama sont déchargés : la RTX 3090 ne peut pas porter les deux.

## Installation

Prérequis :
- Docker et le NVIDIA Container Toolkit ;
- un jeton Hugging Face en lecture, avec les conditions du modèle [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1) acceptées sur le compte correspondant.

```bash
cp .env.example .env           # renseigner au moins HF_TOKEN
mkdir -p data models           # à créer avant le premier démarrage, sinon Docker les crée en root
docker compose up -d --build
```

- Interface : <http://mon-serveur:8090>
- API : <http://mon-serveur:8090/docs>

Au premier job, les modèles sont téléchargés dans `./models` : environ 3 Go pour large-v3, 1,6 Go pour turbo, plus l'alignement et pyannote.

## Utilisation manuelle

1. Déposer un `.m4a` ou un `.mp4`, ou coller une URL YouTube, puis régler les options affichées sous la zone de dépôt : titre, date, vocabulaire, modèle, langue, calcul et nombre maximal d'intervenants.
2. Les réunions sont listées dans la barre de gauche. Le voyant de l'en-tête indique l'état du service : vert prêt, doré occupé, rouge en panne (détail au survol).
3. Une fois le job terminé, la frise montre qui parle quand ; un clic sur la frise lance la lecture à cet endroit. Nommer chaque voix directement dans le titre de sa carte, après avoir écouté l'extrait (▶) ou les trois (⌄) :
   - donner le même nom à deux intervenants les fusionne ;
   - si le nombre de voix est faux, « Relancer la diarisation » en imposant le bon nombre (environ 1 min, sans refaire la transcription).
4. Générer un **compte rendu** (prompt système stocké, modifiable dans *Réglages*, plus les consignes propres à la réunion), le retoucher au besoin avec « Modifier », puis le télécharger. Le **transcript `.md`** est dans la dernière section, repliée par défaut.

## Utilisation automatique (n8n)

Voir [docs/n8n/](docs/n8n/README.md) : un workflow envoie les fichiers d'un dossier Drive à l'API, un second reçoit le transcript par webhook et le dépose dans un autre dossier.

## API en bref

| Appel | Rôle |
|---|---|
| `POST /api/jobs` (multipart) | `file` ou `url`, plus `model`, `language`, `device`, `num_speakers` / `min_speakers` / `max_speakers`, `vocabulary`, `title`, `meeting_date`, `source_name`, `external_ref`, `callback_url` |
| `GET /api/jobs`, `GET /api/jobs/{id}` | Statut (`queued`, `downloading`, `preparing`, `transcribing`, `aligning`, `diarizing`, `completed`, `failed`, `cancelled`), progression, intervenants |
| `PATCH /api/jobs/{id}` | Titre, date |
| `POST /api/jobs/{id}/cancel`, `DELETE /api/jobs/{id}` | Annuler, supprimer |
| `GET /api/jobs/{id}/speakers` | Intervenants, temps de parole, extraits (`…/samples/{n}.mp3`) |
| `PUT /api/jobs/{id}/speakers` | `{"S1": "Alice", "S2": "Bob"}` |
| `POST /api/jobs/{id}/rediarize` | `{"num_speakers": 3}` |
| `GET /api/jobs/{id}/transcript.md` / `.json` | Transcript (noms à jour) |
| `GET /api/jobs/{id}/timeline?bins=240` | Frise : passages de parole par intervenant et enveloppe d'amplitude de l'audio |
| `POST /api/jobs/{id}/summaries` | `{"meeting_prompt": "…", "provider": "ollama" \| "openai", "prompt_id": "…", "think": false}` |
| `GET /api/summaries/{id}.md` | Compte rendu |
| `PATCH /api/summaries/{id}` | `{"content": "…"}` : retoucher le texte d'un compte rendu terminé |
| `GET/POST/PUT/DELETE /api/prompts` | Prompts système |
| `GET /api/system` | GPU, Ollama, file d'attente, versions ; `status.state` = `error`, `busy` ou `ready` |

Exemple :

```bash
curl -F file=@reunion.m4a -F title="Point hebdo" http://mon-serveur:8090/api/jobs
curl http://mon-serveur:8090/api/jobs/<id>                      # jusqu'à "status": "completed"
curl -X PUT -H 'Content-Type: application/json' -d '{"S1":"Alice","S2":"Bob"}' \
     http://mon-serveur:8090/api/jobs/<id>/speakers
curl -o transcript.md http://mon-serveur:8090/api/jobs/<id>/transcript.md
```

Si `API_TOKEN` est défini, chaque appel doit porter `Authorization: Bearer <jeton>`.

## Format du transcript

En-tête YAML (`title`, `date`, `duration`, `duration_seconds`, `language`, `model`, `diarization`, `speakers`, `source_file`, `source_url`, `job_id`, `generated_at`), puis les tours de parole :

```markdown
**Alice** [00:00:12]
Bonjour à tous…

[00:01:45] Deuxième paragraphe du même tour, après une pause.

**Bob** [00:02:10]
…
```

## Configuration

Toutes les variables sont décrites dans [.env.example](.env.example). Les principales :

| Variable | Défaut | |
|---|---|---|
| `HF_TOKEN` | — | Obligatoire pour la diarisation |
| `DEFAULT_MODEL` / `DEFAULT_LANGUAGE` / `DEFAULT_DEVICE` | `large-v3` / `fr` / `cuda` | Valeurs par défaut des jobs |
| `MIN_FREE_VRAM_GB` | `10` | Le job échoue proprement si la carte est occupée par autre chose |
| `OLLAMA_MODEL` / `OLLAMA_MAX_CTX` | `qwen3.8:27b` / `65536` | Le contexte réel est ajusté à la longueur du transcript |
| `LLM_API_BASE_URL` / `LLM_API_KEY` / `LLM_API_MODEL` | — | API compatible OpenAI (OpenAI, Mistral, OpenRouter…) |
| `API_TOKEN` / `CALLBACK_TOKEN` | — | Authentification de l'API, et jeton envoyé dans les callbacks |
| `PUBLIC_BASE_URL` | — | Pour des liens absolus dans les callbacks |
| `OWNER_NAME` | — | Nom proposé dans l'interface pour nommer un intervenant (vous-même) |

YouTube :
- yt-dlp est mis à jour à chaque démarrage (`YTDLP_AUTO_UPDATE=true`) ;
- il utilise Deno, présent dans l'image ;
- si YouTube réclame une connexion, exporter les cookies d'un navigateur connecté vers `data/config/youtube-cookies.txt` (format Netscape).

## Exploitation

- **Données**, dans `./data` :
  - `scribe.db` : base SQLite ;
  - `jobs/<id>/` : pour chaque job, la source, `audio.wav`, les résultats JSON, les extraits et `pipeline.log`, le journal complet du traitement.
- **Performances** mesurées sur la 3090, pour une interview de 8 min : environ 40 s au total en large-v3-turbo et 60 s en large-v3, modèles déjà téléchargés.
- **VRAM** : le pic, environ 12 Go, est atteint pendant la diarisation. Toute la mémoire est rendue à la fin du job, car chaque traitement tourne dans un sous-processus.
- **Tâche interrompue par un redémarrage** : elle est relancée une fois, puis marquée en échec.

## Développement

```bash
uv sync                  # environnement local (sans la pile GPU)
uv run pytest            # tests sans GPU : moteur factice (FAKE_PIPELINE=true)
```

- La pile GPU (`uv sync --extra gpu`) n'est installée que dans l'image Docker.
- Organisation du code :
  - `app/api/` : routes ;
  - `app/worker/` : file de tâches, sous-processus du pipeline, moteur WhisperX, libération du GPU ;
  - `app/render.py` : le Markdown ;
  - `app/llm/` : les comptes rendus ;
  - `web/` : l'interface (Alpine.js, sans étape de build).
