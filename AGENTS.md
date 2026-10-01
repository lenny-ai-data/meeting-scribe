# AGENTS.md : reprendre Meeting Scribe

Ce document s'adresse à un agent ou à un développeur qui reprend le projet sans l'historique de sa conception. Il résume l'architecture, les règles de travail et **les décisions prises, avec leurs raisons**.

Pour le reste :
- l'usage, l'API et la configuration sont dans le [README](README.md) ;
- toutes les variables d'environnement sont dans [.env.example](.env.example) ;
- l'intégration n8n est dans [docs/n8n/README.md](docs/n8n/README.md) ;
- la vidéo de présentation est dans [promo/README.md](promo/README.md).

## 1. Le projet

Meeting Scribe transcrit des réunions :
- **entrées** : `.m4a` ou `.mp4` (iPhone, PC), ou URL YouTube ;
- **intervenants** : ils sont identifiés par diarisation, puis on les nomme dans l'interface en écoutant de courts extraits ;
- **sorties** : un transcript Markdown (frontmatter YAML et tours de parole horodatés) et, en option, un compte rendu généré par un LLM.

Le service est auto-hébergé sur une station avec une RTX 3090 et **remplace Whishper** (pluja/whishper, abandonné). Whishper a été arrêté le 01/10/2026 (voir § 3).

**Principe fondateur : toute la logique est dans l'API.** L'interface web n'en est qu'un client. Tout ce qu'elle fait doit rester faisable par n8n ou par un agent via `/api` (documentation OpenAPI sur `/docs`). Une fonctionnalité qui n'existerait que dans l'interface est un défaut.

Usage visé :
- **manuel** : l'interface web ;
- **automatique** : dossier Drive → n8n → API → `.md` renvoyé sur Drive → agent de l'utilisateur.

## 2. Règles de travail (à respecter)

- **Git** :
  - une branche par fonctionnalité (`feature/…`, `fix/…`, `docs/…`) ;
  - des commits réguliers, avec des messages en français ;
  - fusion dans `main` par `git merge --no-ff`, puis suppression de la branche.
- **Paternité : L. Jacquinot est le seul auteur.** Aucune ligne `Co-Authored-By`, aucune mention de Claude ou d'un outil dans les commits et les PR. Cette consigne explicite prime sur les réglages par défaut des agents.
- **Python : uniquement uv.**
  - Dépendances dans `pyproject.toml`, ajoutées avec `uv add` ; `uv.lock` est commité.
  - Commandes locales : `uv sync`, `uv run …`.
  - Dans l'image : `uv sync --frozen`.
  - Jamais de `pip install` ni de venv manuel. Seule exception : la mise à jour de yt-dlp au démarrage, faite par `uv pip` dans l'entrypoint.
- **Tests** : `uv run pytest` doit passer avant chaque fusion. Les tests tournent sans GPU, avec `FAKE_PIPELINE=true` et de faux serveurs Ollama, LLM, webhook et yt-dlp.
- **Ne jamais commiter de média.** Un `audio.mp4` déposé à la racine a dû être purgé de tout l'historique. `.gitignore` exclut `*.m4a`, `*.mp4`, `*.mp3` et `*.wav` à la racine, ainsi que `data/`, `models/` et `.env`.
- **Langue** : code commenté, interface, documentation et messages d'erreur en français, avec la typographie française : espaces insécables, guillemets « », apostrophes typographiques dans les textes affichés.
- **Interface : uniquement des URL relatives** (`fetch("api/…")`). L'utilisateur y accède depuis un autre poste ; une URL en `127.0.0.1` ou `localhost` casse tout.

## 3. Environnement cible

| Machine | Rôle |
|---|---|
| `mon-serveur` | Station : RTX 3090 24 Go (pilote 610, CUDA 13), Docker et runtime NVIDIA, 62 Go de RAM. Meeting Scribe sur le port **8090** (8000 dans le conteneur). Ollama sur l'hôte, `:11434`, modèle `qwen3.8:27b` (environ 17,7 Go) |
| `poste-client` | Poste de l'utilisateur (navigateur) |
| `n8n.local:5678` | n8n (autre machine du réseau local) |

- Le conteneur joint Ollama par `host.docker.internal`, via `extra_hosts: host-gateway`.
- Whishper, sa base MongoDB et libretranslate-cuda (projet Compose `whishper`, dans `le dossier de Whishper`) sont arrêtés depuis le 01/10/2026, avec `docker compose stop`. Les conteneurs et les données sont conservés, et la politique `unless-stopped` les laisse éteints après un redémarrage. Pour les relancer : `docker compose start` dans ce dossier.

## 4. Architecture

```
            ┌──────────────── conteneur meeting-scribe (uid 1000) ─────────────────┐
navigateur ─┤ FastAPI (uvicorn, 1 worker)                                          │
n8n, agent ─┤   /api/*  ──► SQLite (WAL) : jobs, speakers, tasks, prompts, summaries│
            │   /       ──► web/ (fichiers statiques)                              │
            │                    │                                                 │
            │   Worker asyncio unique ── claim_next_task() ── une tâche à la fois  │
            │     ├─ transcribe / rediarize ─► sous-processus                      │
            │     │      python -m app.worker.pipeline <task_id>                   │
            │     │      (WhisperX, pyannote ; écrit progression et statut en base)│
            │     └─ summarize ─► inline : Ollama /api/chat ou API compatible OpenAI│
            │                    │                                                 │
            │   on_task_done ──► callbacks HTTP (n8n)                              │
            └──────────────────────────────────────────────────────────────────────┘
   ./data → /data (scribe.db, jobs/<id>/, config/)     ./models → /models (caches HF, torch)
```

**Statuts d'un job** : `queued → downloading → preparing → transcribing → aligning → diarizing → completed`, ou `failed` / `cancelled`.

**Contenu de `data/jobs/<id>/`** :
- la source (`source.<ext>` ou le fichier YouTube) et `audio.wav` (16 kHz mono) ;
- `transcription.json` ;
- `aligned.json`, conservé pour relancer la diarisation seule ;
- les résultats de diarisation et d'attribution ;
- `samples/` (extraits MP3) ;
- `waveform.json`, l'enveloppe d'amplitude de la frise, calculée à la première demande ;
- `pipeline.log`, le journal complet du sous-processus.

### Carte du code

| Chemin | Rôle |
|---|---|
| `app/main.py` | Application FastAPI : lifespan (base, reprise des tâches, worker, handler summarize, hook callbacks), `/api/health` sans auth, montage de `web/` |
| `app/config.py` | `Settings` (pydantic-settings), `WHISPER_MODELS`, `LANGUAGES`, `DEVICES` |
| `app/db.py` | Schéma SQLite et accès : une connexion par opération ; `claim_next_task` en `BEGIN IMMEDIATE` ; `recover_interrupted_tasks` |
| `app/api/` | Routes : `jobs`, `speakers`, `summaries`, `prompts`, `system`, et `common` pour les aides partagées |
| `app/auth.py` | `API_TOKEN` optionnel (`Authorization: Bearer` ou `?token=`) |
| `app/worker/queue.py` | Worker : file, lancement du sous-processus, annulation, échecs |
| `app/worker/pipeline.py` | Point d'entrée du sous-processus : `run_transcribe`, `run_rediarize`, `diarize_and_finish` |
| `app/worker/engine.py` | Protocole `Engine` et `FakeEngine` des tests |
| `app/worker/whisperx_engine.py` | Moteur réel : WhisperX, alignement, pyannote |
| `app/worker/gpu.py` | Déchargement d'Ollama et contrôle de la VRAM libre |
| `app/media.py` | ffprobe (durée, piste audio, `creation_time`) ; ffmpeg (WAV, extraits MP3) |
| `app/youtube.py` | yt-dlp avec relances |
| `app/speakers.py` | Libellés S1…, temps de parole, choix des extraits, `finalize` |
| `app/render.py` | Construction des tours et rendu Markdown ; titre, frontmatter, nom de fichier suggéré |
| `app/transcript.py` | Chargement des segments, `transcript_markdown` |
| `app/llm/` | `base` (fournisseur, `num_ctx`), `ollama`, `openai_compat`, `summarize` (tâche), `prompts` (prompt système par défaut) |
| `app/callbacks.py` | Webhooks sortants |
| `web/` | Interface : `index.html`, `job.html`, `settings.html`, `app.js`, `style.css`, `vendor/` (Alpine.js, marked, DOMPurify), logos et favicons |
| `tests/` | pytest : jobs, GPU, intervenants et rendu, comptes rendus, intégrations (YouTube, callbacks, auth) |
| `docs/n8n/` | Deux workflows à importer et leur documentation |
| `docs/branding/` | Logo d'origine et `make_logo.py`, qui génère tous les SVG, ICO et PNG de `web/` |
| `promo/` | Vidéo de présentation Remotion (projet Node indépendant) |

## 5. Journal des décisions

### Exécution et GPU

- **Un seul traitement à la fois**, grâce à une file unique en base (`tasks`) et un seul worker asyncio.
  - **Pourquoi** : la 3090 ne peut pas porter à la fois le LLM d'Ollama (environ 17 Go) et la chaîne WhisperX (pic d'environ 10 Go en `large-v3`).
  - **Conséquence** : les comptes rendus passent par la même file que les transcriptions, pour que le LLM et WhisperX ne tournent jamais en même temps.
- **Un sous-processus par tâche GPU.**
  - **Pourquoi** : ctranslate2 et torch retiennent de la VRAM tant que le processus vit. À la sortie du sous-processus, tout est rendu (vérifié : 280 Mo après un job).
  - **Annulation** : SIGTERM au groupe de processus (`start_new_session`, `os.killpg`), puis SIGKILL après `TERMINATE_GRACE` = 10 s.
  - **Écart avec le plan initial** : celui-ci prévoyait de faire remonter la progression en JSON sur stdout. En réalité, le sous-processus écrit lui-même progression et statut en base (au plus une écriture par seconde). Le parent ne traite que les sorties en erreur et l'annulation.
- **Libération du GPU** (`Engine.prepare_gpu`, code dans `app/worker/gpu.py`) :
  1. `GET /api/ps` sur Ollama ;
  2. pour chaque modèle chargé, `POST /api/generate {"keep_alive": 0}`, ou `/api/embed` pour un modèle d'embedding ;
  3. attente de la libération (`OLLAMA_UNLOAD_TIMEOUT`, 30 s).
  - **Quand** : au début de la transcription, au début d'une `rediarize`, puis de nouveau avant l'alignement.
  - **Contrôle de la VRAM** : au début seulement, vérifier qu'il reste assez de VRAM libre : seuil selon le modèle (`MIN_VRAM_GB` dans `config.py` : 10 Go en `large-v3`, 6 Go en `large-v3-turbo`, pour les cartes de 8 Go), ou `MIN_FREE_VRAM_GB` s'il est défini. En dessous, le job échoue avec un message clair, plutôt que d'aller jusqu'à une erreur CUDA de mémoire. Le pic réellement alloué par torch est écrit dans `pipeline.log` après chaque étape.
  - **Pourquoi** : Open WebUI peut recharger un modèle à tout moment.
  - Tout cela est sauté si le job tourne en `device=cpu`.
- **Reprise après redémarrage** : une tâche restée `running` est remise en file une seule fois (`max_attempts=2`), puis marquée en échec.
- **`rediarize`** : relance seulement la diarisation à partir de `aligned.json` (environ 1 min). Un échec garde le résultat précédent ; le job reste `completed`, avec le message d'erreur.

### Image Docker

- **Base `ubuntu:24.04` plutôt que `nvidia/cuda`.** CUDA, cuDNN et cuBLAS viennent des roues pip de torch (index `pytorch-cu128`).
  - `LD_LIBRARY_PATH` pointe sur `site-packages/nvidia/{cudnn,cublas}/lib` pour que ctranslate2, utilisé par faster-whisper, les trouve.
- **`libpython3.12t64`** : torchcodec, utilisé par pyannote 4, en a besoin. Sans elle, avertissement puis échec du décodage audio.
- **Utilisateur `ubuntu` (uid 1000)** : `./data` et `./models` appartiennent à l'utilisateur de l'hôte. Les données de Whishper, elles, étaient en root.
- **Caches** : `HF_HOME`, `TORCH_HOME` et `MPLCONFIGDIR` sont tous sous `/models`, donc persistants.
- **uv 0.11** et **Deno** sont copiés depuis leurs images officielles.
- **uvicorn `--workers 1`, obligatoire** : le worker et la file vivent dans le processus.

### Transcription et diarisation

- **WhisperX 3.8.6** : torch 2.8 cu128, pyannote-audio 4.0.7, faster-whisper ≥ 1.2.
  - Modèles : `large-v3` par défaut, ou `large-v3-turbo`.
  - Langues : `fr` par défaut, ou `en`.
  - Matériel : GPU par défaut, CPU possible.
- **`interleaved_context`** n'existe pas en 3.8.6 : il n'est activé que si `inspect.signature` le trouve, ce qui prépare les versions suivantes.
- **Vocabulaire** (noms propres, jargon) : passé en `asr_options.initial_prompt`.
- **Diarisation** : `pyannote/speaker-diarization-community-1`.
  - Le dépôt est à accès restreint, mais le modèle est sous CC-BY-4.0, donc redistribuable avec attribution. **Il est embarqué dans l'image** (`DIARIZATION_MODEL_DIR`, 32 Mo, chemins relatifs `$model/…` dans `config.yaml`) : les utilisateurs n'ont besoin ni de jeton ni de réseau (vérifié avec `HF_HUB_OFFLINE=1`). Sans copie locale, repli sur l'identifiant HF et `HF_TOKEN`.
  - `assign_word_speakers(fill_nearest=True)`.
- **Image CPU : WhisperX, pas Parakeet ni Canary.** Mesures du 01/10/2026 sur l'interview de 7 min 54, i7-12700, 10 threads :
  - Parakeet TDT 0.6B v3 (onnx-asr, int8 ou fp32) : 33 s, mais dérive en anglais au milieu des phrases (88 à 122 mots anglais sur environ 1 000) et passages perdus ; le modèle n'a pas de langue forcée ;
  - Canary-1B-v2 (onnx-asr, int8, `language="fr"`) : 574 s, boucles d'hallucinations, pas d'horodatage par mot ;
  - Whisper large-v3-turbo int8 (faster-whisper, par lots) : 92 s, 1 540 mots, aucune dérive. Puis alignement 24 s, diarisation pyannote 199 s : environ 0,7 × la durée de la réunion au total sur CPU.
- **Date de réunion par défaut** : `creation_time` du fichier (les `.m4a` d'iPhone la portent), sinon la date de dépôt. Pour YouTube, la date de publication.

### Intervenants

- **Libellés** : `SPEAKER_xx` est renommé en `S1`, `S2`… dans l'ordre de première prise de parole.
  - Noms par défaut : « Intervenant N », ou « Speaker N » en anglais.
- **3 extraits par intervenant** (`app/speakers.py`) :
  - durée de 2,5 à 12 s, 4 s visées ;
  - jamais de chevauchement avec une autre voix ;
  - répartis sur les tiers de la réunion ;
  - MP3 64 kb/s mono, lisible sur Safari iOS ;
  - accompagnés du texte prononcé.
- **Fusion** : donner le même nom à deux libellés les fusionne ; les tours consécutifs sont regroupés au rendu.
- **Pas de reconnaissance vocale ni de base d'intervenants.** L'utilisateur a explicitement refusé une bibliothèque d'empreintes vocales : l'identification se fait à la main dans l'interface. La seule suggestion proposée est `OWNER_NAME` (l'utilisateur lui-même). `/api/people` existe toujours, mais l'interface ne s'en sert plus, à la demande de l'utilisateur.
- **Frise** (`GET /api/jobs/{id}/timeline`, `transcript.speaker_blocks`) :
  - les passages viennent de `build_units`, donc avec le même recalage que le transcript ; deux passages d'un même intervenant séparés de moins d'1 s sont fusionnés ;
  - l'enveloppe est un RMS par intervalle, lu par blocs dans `audio.wav` (jamais chargé en entier), compressé en puissance 0,6 pour que les passages calmes restent visibles ;
  - dans l'interface, deux intervenants de même nom partagent la même couleur.

### Transcript Markdown (`app/render.py`)

- **Rendu à la demande** avec les noms courants : renommer un intervenant ne régénère rien.
- **Frontmatter** sérialisé par `yaml.safe_dump`, jamais par concaténation de chaînes.
- **Attribution mot à mot** : un segment WhisperX peut être partagé entre deux intervenants.

Réglages issus de tests sur des enregistrements réels :
- **Ponctuation isolée** (` ?`, ` !`…, `CLOSING_PUNCT`) : rattachée au mot précédent. Sinon, le « ? » d'une question passait à l'intervenant suivant.
- **Recalage des tours sur les fins de phrase** (`_snap`). La diarisation a un léger retard et coupait les phrases. Une frontière de tour est déplacée vers la fin de phrase la plus proche si elle respecte toutes ces limites :
  - `SNAP_MAX_WORDS = 4` mots ;
  - `SNAP_MAX_SPAN = 2.0` s de parole (1,5 s a été testé et ne suffisait pas) ;
  - pas de pause de plus de `SNAP_MAX_GAP = 1.0` s.
- **Paragraphes** : un nouveau paragraphe horodaté s'ouvre dans un même tour après une pause de plus de 3 s (`PARAGRAPH_GAP`) ou au-delà de 90 s (`PARAGRAPH_MAX`).

### Comptes rendus LLM (`app/llm/`)

- **Deux fournisseurs** :
  - **Ollama**, en local : `/api/chat`, `stream=false`, `think` désactivé par défaut. Les blocs `<think>` résiduels sont retirés.
  - **Distant : un seul connecteur générique compatible OpenAI** (`/chat/completions`), choix de l'utilisateur. Il couvre OpenAI, Mistral, OpenRouter… La clé ne vient que de l'environnement et n'est jamais renvoyée par l'API.
- **`num_ctx`** = caractères ÷ 3,5 × 1,15 + 8192 (réservés à la réponse), plafonné par `OLLAMA_MAX_CTX` (65536). Plus de contexte coûterait de la VRAM pour rien.
- **Messages envoyés** :
  - `system` : le prompt système stocké, modifiable dans *Réglages* ; un prompt par défaut en français est créé au premier démarrage ;
  - `user` : le prompt propre à la réunion, puis le transcript complet avec les noms.
- **Historique** : tous les comptes rendus sont conservés ; le dernier est affiché.
- **Retouche** : `PATCH /api/summaries/{id}` remplace le texte (`content`) d'un compte rendu terminé. Le `.md` téléchargé sert ensuite la version retouchée ; l'original n'est pas conservé.

### YouTube (`app/youtube.py`)

- **`yt-dlp[default]`**, avec les scripts EJS, et **Deno** comme moteur JavaScript : sans Deno, YouTube répond « Please sign in ». C'est la panne de Whishper.
- **Format** : `bestaudio/best`.
- **Mise à jour au démarrage** (`YTDLP_AUTO_UPDATE=true`) : YouTube casse régulièrement les anciennes versions.
- **Relances** après 5 puis 15 s sur les erreurs passagères : 403, 429, 5xx, délais dépassés.
- **Cookies optionnels** dans `data/config/youtube-cookies.txt` ; le message d'erreur les suggère.

### Intégrations

- **Callbacks** (`callback_url` donné au `POST /api/jobs`) :
  - événements : `job.completed`, `job.failed`, `job.speakers_updated`, `summary.completed` ;
  - le Markdown est inclus dans le message, pour que n8n n'ait pas besoin d'une seconde requête ;
  - `external_ref` (par exemple l'identifiant Drive) est renvoyé tel quel ;
  - en-têtes `X-Scribe-Event`, et `X-Scribe-Token` si `CALLBACK_TOKEN` est défini ;
  - 4 tentatives au total (relances après 5, 15 puis 45 s) ; le résultat est visible dans le champ `callback_status` ;
  - liens absolus si `PUBLIC_BASE_URL` est défini.
- **Webhook n8n** : `http://n8n.local:5678/webhook/meeting-scribe`.
- **Auth** : `API_TOKEN` est vide par défaut, puisque tout reste sur le réseau local. `/api/health` reste toujours ouvert pour le healthcheck.

### Interface (`web/`)

- **Sans étape de build** : Alpine.js, marked et DOMPurify sont stockés dans `web/vendor/`, sans CDN.
- **Coquille commune** (`app.js`) : `withShell(page)` fusionne la barre latérale des réunions et le voyant d'état avec les données propres à la page. Les descripteurs sont copiés, pour que les accesseurs `get` restent calculés. Le balisage de l'en-tête et de la barre latérale est dupliqué dans les trois pages.
- **Voyant d'état** : l'état vient de l'API (`/api/system` → `status`) :
  - `error` (rouge) si `HF_TOKEN` manque ou si le GPU est introuvable alors que `DEFAULT_DEVICE=cuda` ;
  - `busy` (doré, pulsation lente) pendant une tâche ;
  - `ready` (vert) sinon.
  - Ollama injoignable n'est qu'un avertissement (`warnings`), affiché dans l'infobulle.
- **Fond** : trois halos aux couleurs de la vidéo de présentation, plus une trame de points, insérés par `app.js` (`.backdrop`). Ils dérivent lentement, sauf si `prefers-reduced-motion` est actif. Leur opacité est plus faible en clair. Les cartes sont translucides (`backdrop-filter`).
- **Page d'une réunion** : sections dépliantes (`<details class="card section">`) dans cet ordre : Intervenants, Transcript (replié par défaut, car ce n'est pas le cœur de l'usage), puis Compte rendu. Ordre et repli choisis par l'utilisateur.
- **Réglages** : pas de barre latérale (`withShell(page, null, { sidebar: false })`), mais le menu « Réunions » reste dans l'en-tête de toutes les pages.
- **Thème** : Auto, Clair ou Sombre, choisi dans l'en-tête et mémorisé dans `localStorage` (`meeting-scribe.theme`).
  - **Mécanisme** : attribut `data-theme` sur `<html>`. Les variables sombres sont définies deux fois : sous `@media (prefers-color-scheme: dark) :root:not([data-theme="light"])` et sous `:root[data-theme="dark"]`.
  - **Logo** : un SVG chargé en `<img>` ne voit que le thème du système. D'où les variantes figées `logo-mark-light.svg` et `logo-mark-dark.svg`, choisies par `app.js` quand le thème est forcé.
  - **Favicon** : il suit toujours le thème du système (limite du navigateur).

### Identité visuelle

- **Logo** : vectorisé depuis `docs/branding/logo-original.jpg` par `docs/branding/make_logo.py`, qui génère tous les fichiers de `web/` (favicons, variantes du logo). **Pour modifier le logo, modifier le script, pas les SVG.**
- **Palette** : violet `#5b2bd9` en clair, `#a98bff` en sombre.

### Vidéo de présentation (`promo/`)

- **Remotion 4**, rendu dans un conteneur Node : pas de Node sur l'hôte.
- **Durée** : environ 1 min, en 1080p30. L'horloge est ralentie (`SPEED = 0.8`) et les fondus durent 16 images.
- **Données** : celles d'un vrai job, une interview télévisée.
- **Droits** : seul le texte produit par l'application est montré, jamais le son ni l'image de l'émission. Choix de l'utilisateur pour éviter un problème de droits.
- **Correction de la reconnaissance** : « Alia Salamé » devient « Léa Salamé » à l'export (`scripts/export_demo.py`).
- **Partie automatisation** : la chaîne iPhone → cloud → n8n → API → agent → CRM est fictive (Acme Industrie, Claire Dumont, Thomas Leroy), et la vidéo le signale.

## 6. Mesures de référence (station, modèles déjà en cache)

| Mesure | Valeur |
|---|---|
| Déchargement d'Ollama | environ 1 s (VRAM de 20,3 à 0,5 Go) |
| Interview de 7 min 54 | environ 60 s en `large-v3`, environ 40 s en `large-v3-turbo` |
| Interview télévisée de 5 min 13 | 22 s |
| Pic de VRAM, transcription (`BATCH_SIZE=16`) | environ 9,9 Go en `large-v3`, 4,4 Go en `large-v3-turbo` (mesuré par la VRAM libre de la carte, ctranslate2 compris) |
| Pic de VRAM, alignement et diarisation | 0,7 Go et 1,6 Go réellement nécessaires. Sur une carte libre, la diarisation monte à 9,8 Go sur certains fichiers (espace de travail opportuniste, mesure de `torch.cuda.max_memory_allocated`) ; plafonnée à 5,9 Go, elle donne le même résultat |
| VRAM après un job | environ 280 Mo |

## 7. Commandes courantes

```bash
# Développement (sans GPU)
uv sync
uv run pytest

# Déploiement sur la station
docker compose up -d --build
docker compose logs -f meeting-scribe
curl -s http://mon-serveur:8090/api/system | jq    # GPU, Ollama, file d'attente, versions

# Journal d'un job
less data/jobs/<id>/pipeline.log

# Logo et favicons (après modification du script)
uv run python docs/branding/make_logo.py

# Vidéo de présentation
cd promo && docker build -t meeting-scribe-promo . && \
docker run --rm -u 1000:1000 -e HOME=/tmp -v $PWD:/work meeting-scribe-promo \
  sh -c "npm install && npx remotion render Promo out/meeting-scribe-promo.mp4"
```

## 8. Pièges connus

- **`data/` et `models/`** : les créer avant le premier `docker compose up`, sinon Docker les crée en root et le conteneur (uid 1000) ne peut plus y écrire.
- **Diarisation en échec avec une erreur 401 ou 403 de Hugging Face** : le jeton est absent, ou les conditions de `pyannote/speaker-diarization-community-1` ne sont pas acceptées sur le compte.
- **« VRAM libre insuffisante »** : un autre programme occupe la carte, par exemple Whishper, libretranslate ou Open WebUI avec un modèle hors Ollama.
- **YouTube réclame une connexion** : vérifier que yt-dlp est à jour (logs de démarrage), puis fournir les cookies.
- **Workflows n8n** : ils ont été écrits sans accès à l'instance. Les versions de nœuds peuvent demander un ajustement à l'import.
- **`/docs`** : la page OpenAPI de FastAPI garde son favicon par défaut et n'a pas le sélecteur de thème.
- **Tests** : ils forcent `FAKE_PIPELINE=true` et un `DATA_DIR` temporaire ; ne jamais les pointer sur `./data`.

## 9. Reste à faire

- [x] **Bascule** : Whishper et libretranslate-cuda arrêtés le 01/10/2026, sur demande de l'utilisateur.
- [ ] En option : reprendre le port 8082, ou supprimer définitivement la pile Whishper. **Uniquement sur feu vert de l'utilisateur.**
- [ ] Mettre `PUBLIC_BASE_URL=http://mon-serveur:8090` dans `.env` (à faire par l'utilisateur).
- [ ] Importer les workflows n8n et les régler : identifiants Drive, identifiants de dossiers, activation. Puis test de bout en bout : dépôt Drive → `.md` sur Drive → renommage dans l'interface → `.md` mis à jour.
- [ ] Tester un enregistrement de 1 h 30 : durée totale, VRAM, taille de contexte du compte rendu.
- [ ] En option : favicon de la page `/docs` ; musique pour la vidéo, si l'utilisateur fournit une piste.
