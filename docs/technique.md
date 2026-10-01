# Meeting Scribe : documentation technique

Complément du [README](../README.md), qui couvre l'objectif, les prérequis, l'installation et l'utilisation courante. Ce document détaille le fonctionnement, la configuration, l'API et l'exploitation. Les choix de conception et leurs raisons sont dans [AGENTS.md](../AGENTS.md).

## Sommaire

- [Fonctionnement](#fonctionnement)
- [Construction locale](#construction-locale)
- [Configuration](#configuration)
- [Profils de performance](#profils-de-performance)
- [Comptes rendus](#comptes-rendus)
- [YouTube](#youtube)
- [API](#api)
- [Format du transcript](#format-du-transcript)
- [Exploitation](#exploitation)
- [Dépannage](#dépannage)
- [Développement](#développement)

## Fonctionnement

### Chaîne de traitement

- **Transcription** : [WhisperX](https://github.com/m-bain/whisperX), c'est-à-dire faster-whisper (`small`, `large-v3-turbo` ou `large-v3`, selon le [profil de performance](#profils-de-performance)), puis alignement mot à mot.
- **Diarisation** : `pyannote/speaker-diarization-community-1`, embarqué dans les images publiées.
- **Comptes rendus** : [Ollama](https://ollama.com) en local, ou toute API compatible OpenAI (OpenAI, Mistral, OpenRouter…).
- **Un traitement à la fois**, pensé pour une station mono-GPU : avant chaque transcription, les modèles d'Ollama sont déchargés de la VRAM quand ils tournent sur la même machine.
- **Toute la logique est dans l'API.** L'interface web n'en est qu'un client : n8n, un script ou un agent peuvent la consommer de la même manière.

### Flux d'une réunion

```mermaid
flowchart LR
    A["Fichier .m4a / .mp4<br>ou URL YouTube"] -->|"POST /api/jobs"| Q["File d'attente<br>(un traitement à la fois)"]
    Q --> G["Libération du GPU<br>(déchargement d'Ollama)"]
    G --> T["Transcription<br>faster-whisper"]
    T --> AL["Alignement<br>mot à mot"]
    AL --> D["Diarisation<br>pyannote"]
    D --> S["Intervenants S1, S2…<br>+ extraits audio"]
    S -->|"nommage dans l'interface<br>ou PUT /speakers"| MD["Transcript Markdown<br>(noms à jour)"]
    MD -->|"optionnel"| CR["Compte rendu LLM<br>Ollama ou API OpenAI"]
    MD -.->|"callback"| W["Webhook<br>(n8n…)"]
    CR -.->|"callback"| W
```

1. Un fichier ou une URL est déposé, par l'interface ou par l'API. Le job entre dans la file.
2. Le worker libère le GPU, convertit l'audio, transcrit, aligne chaque mot sur l'audio, puis attribue chaque mot à une voix.
3. Les voix sont nommées `S1`, `S2`… avec trois courts extraits chacune. On leur donne un nom en les écoutant ; donner le même nom à deux voix les fusionne.
4. Le transcript Markdown est rendu à la demande, toujours avec les noms courants.
5. En option, un LLM rédige un compte rendu à partir du transcript et peut identifier les intervenants.
6. Si une `callback_url` a été fournie, chaque étape importante (job terminé ou en échec, intervenants renommés, compte rendu prêt) y est envoyée, Markdown compris.

### Déploiement

```mermaid
flowchart LR
    U["Navigateur"] -->|":8090"| MS
    subgraph H["Hôte Docker avec GPU NVIDIA"]
        MS["Conteneur meeting-scribe<br>FastAPI + worker"]
        O["Ollama (optionnel)<br>:11434"]
        MS -->|"host.docker.internal"| O
        MS --- V1[("./data<br>base SQLite, jobs")]
        MS --- V2[("./models<br>cache des modèles")]
    end
    MS -.->|"optionnel"| L["API externe"]
```

Un seul conteneur suffit. Sans GPU, tout fonctionne avec l'image `cpu`, plus lentement ; un GPU permet en plus de faire tourner un LLM local (Ollama) pour les comptes rendus, sans clé d'API.

## Construction locale

```bash
git clone https://github.com/lenny-ai-data/meeting-scribe.git
cd meeting-scribe
cp .env.example .env
mkdir -p data models
docker compose up -d --build                        # image cuda
docker compose -f compose.cpu.yaml up -d --build    # ou : image cpu
```

Le modèle de diarisation n'est embarqué à la construction que si `HF_TOKEN` est renseigné dans le `.env` ; sinon, ce même jeton sera exigé à l'exécution.

### Jeton Hugging Face

Inutile avec les images publiées. Il ne sert qu'à construire soi-même une image avec le modèle de diarisation, ou à utiliser un autre modèle pyannote :

1. Se connecter sur Hugging Face et accepter les conditions de [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1).
2. Créer un jeton de type *Read* dans [Settings → Access Tokens](https://huggingface.co/settings/tokens), puis le mettre dans `HF_TOKEN`.

## Configuration

Toutes les variables sont dans [.env.example](../.env.example), commentées. Après une modification du `.env` : `docker compose up -d`.

### Variables principales

| Variable | Défaut | Rôle |
|---|---|---|
| `HF_TOKEN` | — | Jeton Hugging Face. Inutile si le modèle de diarisation est embarqué dans l'image (`DIARIZATION_MODEL_DIR`) |
| `DEFAULT_PROFILE` | `rapide` sur CPU, `tres_precis` sur GPU | Profil de performance proposé par défaut : `tres_rapide`, `rapide`, `precis` ou `tres_precis` |
| `DEFAULT_LANGUAGE` | `fr` | `fr` ou `en` |
| `DEFAULT_DEVICE` | `cuda` | `cuda` ou `cpu` |
| `BATCH_SIZE` | selon la VRAM sur GPU, 4 sur CPU | Passages de 30 s transcrits par lot. Sur GPU, 16, 8 ou 4 selon la VRAM libre, 4 sur CPU. Une valeur fixe désactive le choix automatique |
| `MIN_FREE_VRAM_GB` | selon le profil | En dessous, le job échoue et une alerte est levée. Par défaut : 5 Go en `large-v3`, 3 Go en `large-v3-turbo`, 2,6 Go en `small` |
| `DIARIZATION_MODEL_DIR` | `/opt/models/pyannote/speaker-diarization-community-1` | Copie locale du modèle pyannote ; si elle existe, ni jeton ni réseau ne sont nécessaires |
| `OLLAMA_URL` | `http://host.docker.internal:11434` | Ollama de l'hôte (valeur initiale, modifiable dans *Réglages*) |
| `OLLAMA_MODEL` | — | Modèle des comptes rendus ; vide = premier modèle installé |
| `OLLAMA_UNLOAD_BEFORE_GPU` | automatique | Décharger Ollama avant une transcription GPU ; par défaut, seulement s'il tourne sur la même machine |
| `OLLAMA_MAX_CTX` | `65536` | Plafond du contexte ; le contexte réel est ajusté à la longueur du transcript |
| `LLM_API_BASE_URL`, `LLM_API_KEY`, `LLM_API_MODEL` | — | API compatible OpenAI, en plus ou à la place d'Ollama (valeurs initiales, modifiables dans *Réglages*) |
| `API_TOKEN` | — | Si défini, chaque appel à `/api` doit porter `Authorization: Bearer <jeton>` |
| `CALLBACK_TOKEN` | — | Envoyé dans l'en-tête `X-Scribe-Token` des callbacks |
| `PUBLIC_BASE_URL` | — | Adresse publique du service, pour mettre des liens absolus dans les callbacks |
| `OWNER_NAME` | — | Votre nom, proposé dans l'interface pour nommer un intervenant |
| `MAX_UPLOAD_MB` | `4096` | Taille maximale d'un fichier déposé |
| `TZ` | `Europe/Paris` | Fuseau horaire des dates de réunion |

### Port et accès

Le service écoute sur le port 8000 du conteneur, publié sur le **8090** de l'hôte. Pour en changer, modifier `ports` dans [compose.yaml](../compose.yaml), par exemple `"9000:8000"`.

Par défaut, l'API n'a pas d'authentification : c'est prévu pour un réseau local. Si le service est exposé plus largement :
- définir `API_TOKEN` ;
- le placer derrière un reverse proxy en HTTPS (les en-têtes `X-Forwarded-*` sont pris en compte).

L'interface ne s'appuie que sur des URL relatives : elle fonctionne quelle que soit l'adresse par laquelle on y accède.

## Profils de performance

Le formulaire et l'API (`profile`) proposent quatre profils, qui fixent le modèle Whisper et le pas de diarisation. La diarisation analyse l'audio par fenêtres de 10 s ; le pas est l'écart entre deux fenêtres. Un pas plus grand calcule moins d'empreintes vocales, donc va plus vite, mais attribue moins bien les interventions très brèves et compte moins sûrement les intervenants. **Indiquer le nombre d'intervenants** (`num_speakers`) compense largement ce dernier point.

Mesures sur une interview de 7 min 54 en français, modèles déjà téléchargés. WER : part de mots erronés par rapport à `large-v3`.

**Sans GPU** (image `cpu`, Intel i7-12700, 10 threads ; compter 2 à 3 fois plus sur un portable) :

| Profil | Modèle | Pas | Durée | WER |
|---|---|---|---|---|
| Très rapide | `small` | 5 s | 1 min 50 | 15 % |
| **Rapide** (par défaut) | `small` | 2,5 s | 2 min 20 | 15 % |
| Précis | `large-v3-turbo` | 2,5 s | 3 min 15 | 6 % |
| Très précis | `large-v3-turbo` | 1 s | 4 min 50 | 6 % |

**Avec GPU** (image `cuda`, RTX 3090) :

| Profil | Modèle | Pas | Durée | WER | VRAM minimale |
|---|---|---|---|---|---|
| Très rapide | `small` | 2,5 s | 16 s | 15 % | 2,6 Go |
| Rapide | `large-v3-turbo` | 2,5 s | 19 s | 6 % | 3 Go |
| Précis | `large-v3` | 2,5 s | 23 s | — | 4,9 Go |
| **Très précis** (par défaut) | `large-v3` | 1 s | 26 s | — | 4,9 Go |

Sur GPU, la précision des calculs et la taille des lots s'adaptent à la VRAM libre au début du job : float16 par lots de 16 si la place le permet, sinon int8 par lots de 16, 8 ou 4. int8 ne change pas la qualité, et coûte au plus 4 s. `large-v3` tient ainsi en 3,9 Go : une carte de 6 ou 8 Go fait tourner tous les profils. Les profils trop gourmands pour la carte sont grisés dans le formulaire.

| Pic de VRAM de la transcription | float16, lots de 16 | int8, lots de 16 | int8, lots de 8 | int8, lots de 4 |
|---|---|---|---|---|
| `small` | 2,5 Go | 2,1 Go | 1,5 Go | 1,2 Go |
| `large-v3-turbo` | 4,3 Go | 3,3 Go | 2,6 Go | 2,0 Go |
| `large-v3` | 9,8 Go | 8,0 Go | 5,4 Go | 3,9 Go |

L'alignement prend 0,7 Go et la diarisation 1,6 Go ; s'y ajoute environ 1 Go de marge (contexte CUDA, affichage).

Au premier job, les modèles de transcription et d'alignement sont téléchargés dans `./models` : environ 3 Go pour `large-v3`, 1,6 Go pour `large-v3-turbo`, 500 Mo pour `small`, plus 1,2 Go d'alignement pour le français.

## Comptes rendus

Trois possibilités, cumulables. Le fournisseur se choisit au moment de générer le compte rendu. Tout se règle dans *Réglages › Modèles de langage* (ou par `PUT /api/settings/llm`), avec un bouton « Tester » qui liste les modèles disponibles ; ces réglages priment sur le `.env`, qui ne fournit que les valeurs initiales.

**Ollama sur le même hôte** (configuration par défaut) :
1. Installer Ollama et télécharger un modèle : `ollama pull qwen3:8b`, ou un modèle plus gros si la VRAM le permet. Sans modèle par défaut choisi, le premier modèle installé est utilisé.
2. Le rendre joignable depuis les conteneurs. Par défaut, Ollama n'écoute que sur `127.0.0.1`, que le conteneur ne peut pas atteindre :
   ```bash
   sudo systemctl edit ollama
   # ajouter :
   # [Service]
   # Environment="OLLAMA_HOST=0.0.0.0"
   sudo systemctl restart ollama
   ```
   Dans ce cas, protéger le port 11434 par le pare-feu si la machine est accessible de l'extérieur.

Ollama et WhisperX ne tiennent généralement pas ensemble sur la carte. Meeting Scribe décharge donc les modèles d'Ollama avant chaque transcription, et fait passer les comptes rendus par la même file d'attente : les deux ne tournent jamais en même temps.

**Ollama ailleurs** (une station du réseau) : indiquer son adresse, par exemple `http://192.168.1.20:11434`, avec Ollama lancé en `OLLAMA_HOST=0.0.0.0` sur cette machine. Meeting Scribe ne décharge Ollama avant une transcription que s'il tourne sur la même machine (`host.docker.internal`, `localhost`) ; le réglage « Libération du GPU » force l'un ou l'autre comportement.

**API compatible OpenAI** : un service en ligne (OpenAI, Mistral, OpenRouter…) ou un serveur local qui expose `/v1/chat/completions` (LM Studio, llama.cpp, vLLM). Renseigner l'adresse (préréglages proposés), la clé si nécessaire et le modèle. La clé est enregistrée en base et n'est jamais renvoyée par l'API. Avec un service en ligne, le transcript complet est envoyé à ce fournisseur.

**Aucun LLM** : la transcription fonctionne seule. Le voyant signale simplement qu'Ollama est injoignable.

Sans GPU, préférer une API distante, ou un Ollama sur une autre machine du réseau.

Le prompt système des comptes rendus se modifie dans *Réglages* ; un prompt par défaut en français est créé au premier démarrage.

## YouTube

- yt-dlp est mis à jour à chaque démarrage (`YTDLP_AUTO_UPDATE=true`), car YouTube casse régulièrement les anciennes versions. Il s'appuie sur Deno, inclus dans l'image.
- Si YouTube réclame une connexion, exporter les cookies d'un navigateur connecté au format Netscape (avec une extension du type *Get cookies.txt*) vers `data/config/youtube-cookies.txt`.

## API

La documentation interactive (OpenAPI) est servie sur `/docs`.

| Appel | Rôle |
|---|---|
| `POST /api/jobs` (multipart) | `file` ou `url`, plus `profile` (`tres_rapide`, `rapide`, `precis`, `tres_precis`), `language`, `device`, `num_speakers` (ou `min_speakers` / `max_speakers`), `vocabulary`, `title`, `meeting_date`, `source_name`, `external_ref`, `callback_url` |
| `GET /api/jobs`, `GET /api/jobs/{id}` | Statut (`queued`, `downloading`, `preparing`, `transcribing`, `aligning`, `diarizing`, `completed`, `failed`, `cancelled`), progression (`progress`, et `progress_detail` : téléchargement d'un modèle, lot en cours…), intervenants |
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
| `GET/PUT /api/settings/llm` | Fournisseurs LLM : adresse, modèle, clé (écriture seule) ; mise à jour partielle, `null` = valeur du `.env` |
| `POST /api/settings/llm/test`, `GET /api/llm/models?provider=` | Tester un fournisseur, lister ses modèles |
| `GET /api/system` | GPU, Ollama, file d'attente, versions ; `status.state` = `error`, `busy` ou `ready` |
| `GET /api/health` | Toujours ouvert, même avec `API_TOKEN` |

Exemple complet :

```bash
SCRIBE=http://localhost:8090        # adresse du service
# AUTH=(-H "Authorization: Bearer $API_TOKEN")   # si API_TOKEN est défini, puis ajouter "${AUTH[@]}" à chaque appel

ID=$(curl -s -F file=@reunion.m4a -F title="Point hebdo" $SCRIBE/api/jobs | jq -r .id)
curl -s $SCRIBE/api/jobs/$ID | jq .status                     # jusqu'à "completed"
curl -s -X PUT -H 'Content-Type: application/json' -d '{"S1":"Alice","S2":"Bob"}' \
     $SCRIBE/api/jobs/$ID/speakers
curl -s -o transcript.md $SCRIBE/api/jobs/$ID/transcript.md
```

### Callbacks

Avec une `callback_url` au `POST /api/jobs`, le service envoie les événements `job.completed`, `job.failed`, `job.speakers_updated` et `summary.completed`, Markdown compris :
- `external_ref` (par exemple un identifiant de fichier Drive) est renvoyé tel quel ;
- en-tête `X-Scribe-Event`, et `X-Scribe-Token` si `CALLBACK_TOKEN` est défini ;
- 4 tentatives au total (relances après 5, 15 puis 45 s), résultat visible dans `callback_status` ;
- liens absolus si `PUBLIC_BASE_URL` est défini.

## Format du transcript

En-tête YAML (`title`, `date`, `duration`, `duration_seconds`, `language`, `profile`, `model`, `diarization`, `speakers`, `source_file`, `source_url`, `job_id`, `generated_at`), puis les tours de parole :

```markdown
**Alice** [00:00:12]
Bonjour à tous…

[00:01:45] Deuxième paragraphe du même tour, après une pause.

**Bob** [00:02:10]
…
```

Les frontières des tours sont recalées sur les fins de phrase, et la ponctuation isolée reste attachée au mot qui la précède. Un nouveau paragraphe horodaté s'ouvre après une pause de plus de 3 s ou au-delà de 90 s de parole continue.

La date de la réunion est préremplie avec la date d'enregistrement du fichier quand elle est connue (c'est le cas des mémos vocaux d'iPhone), sinon avec la date de dépôt ; pour YouTube, la date de publication.

## Exploitation

- **Données**, dans `./data` :
  - `scribe.db` : base SQLite (jobs, intervenants, prompts, comptes rendus, réglages) ;
  - `jobs/<id>/` : pour chaque job, la source, `audio.wav`, les résultats JSON, les extraits et `pipeline.log`, le journal complet du traitement ;
  - `config/` : cookies YouTube éventuels.
- **Sauvegarde** : le dossier `./data` suffit. `./models` n'est qu'un cache, retéléchargeable.
- **Mise à jour** : `docker compose pull && docker compose up -d` avec l'image publiée (ajouter `-f compose.cpu.yaml` pour l'image `cpu`), ou `git pull && docker compose up -d --build` depuis le dépôt.
- **Journaux** : `docker compose logs -f meeting-scribe`, et `data/jobs/<id>/pipeline.log` pour un job précis.
- **VRAM** : le pic est atteint pendant la transcription et dépend du profil ; toute la mémoire est rendue à la fin du job, car chaque traitement tourne dans un sous-processus.
- **Redémarrage** : une tâche interrompue est relancée une fois, puis marquée en échec.

## Dépannage

| Symptôme | Cause probable |
|---|---|
| Voyant rouge, « HF_TOKEN manquant » | Image construite sans le modèle de diarisation : renseigner `HF_TOKEN` dans le `.env`, ou utiliser l'image publiée |
| Voyant rouge, « GPU introuvable » | NVIDIA Container Toolkit absent ou mal configuré : tester `docker run --rm --gpus all ubuntu nvidia-smi`. Sans carte NVIDIA, utiliser l'image `cpu` |
| `could not select device driver "nvidia"` au démarrage | Pas de runtime NVIDIA : utiliser `compose.cpu.yaml` |
| Diarisation en échec, erreur 401 ou 403 | Conditions de `pyannote/speaker-diarization-community-1` non acceptées sur le compte du jeton |
| « VRAM libre insuffisante » | Un autre programme occupe la carte (autre service de transcription, modèle chargé hors Ollama…) |
| « Carte de N Go : trop petite pour ce profil » | Choisir un profil plus rapide, ou le CPU |
| `Permission denied` sur `/data` ou `/models` | Dossiers créés en root ou par un autre uid : `sudo chown -R 1000:1000 data models` |
| « Ollama injoignable » | Ollama arrêté, ou n'écoute que sur `127.0.0.1` (voir [Comptes rendus](#comptes-rendus)) |
| YouTube réclame une connexion | Vérifier la mise à jour de yt-dlp dans les journaux de démarrage, puis fournir les cookies |
| Callback jamais reçu | Consulter `callback_status` dans `GET /api/jobs/{id}` ; l'URL doit être joignable depuis le conteneur |

## Développement

```bash
uv sync                  # environnement local, sans la pile GPU
uv run pytest            # tests sans GPU : moteur factice (FAKE_PIPELINE=true)
```

- Les dépendances sont gérées uniquement avec [uv](https://docs.astral.sh/uv/) ; la pile de transcription n'est installée que dans l'image Docker : `uv sync --extra cuda` (torch CUDA) ou `--extra cpu` (torch CPU), deux extras incompatibles entre eux.
- Images : `docker build --build-arg FLAVOR=cpu --build-arg DEFAULT_DEVICE=cpu --secret id=hf_token,env=HF_TOKEN .` ; publication sur GHCR par [.github/workflows/docker.yml](../.github/workflows/docker.yml) à chaque tag `vX.Y.Z`.
- Organisation du code :
  - `app/api/` : routes ;
  - `app/worker/` : file de tâches, sous-processus du pipeline, moteur WhisperX, libération du GPU ;
  - `app/render.py` : le Markdown ;
  - `app/llm/` : les comptes rendus ;
  - `web/` : l'interface (Alpine.js, sans étape de build).
- L'architecture et les choix de conception sont détaillés dans [AGENTS.md](../AGENTS.md).
