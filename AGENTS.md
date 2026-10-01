# AGENTS.md : reprendre Meeting Scribe

Ce document s'adresse à un agent ou à un développeur qui reprend le projet sans l'historique de sa conception. Il résume l'architecture, les règles de travail et **les décisions prises, avec leurs raisons**.

Les particularités d'une installation (adresses, matériel, tâches locales) vont dans `AGENTS.local.md`, exclu de Git et chargé par `CLAUDE.md` s'il existe.

Pour le reste :
- l'usage, l'API et la configuration sont dans le [README](README.md) ;
- toutes les variables d'environnement sont dans [.env.example](.env.example).

## 1. Le projet

Meeting Scribe transcrit des réunions :
- **entrées** : `.m4a` ou `.mp4` (iPhone, PC), ou URL YouTube ;
- **intervenants** : ils sont identifiés par diarisation, puis on les nomme dans l'interface en écoutant de courts extraits ;
- **sorties** : un transcript Markdown (frontmatter YAML et tours de parole horodatés) et, en option, un compte rendu généré par un LLM.

Le service est auto-hébergé. Il est né pour **remplacer Whishper** (pluja/whishper, abandonné) sur une station équipée d'une RTX 3090, et tourne aussi sans GPU (image `cpu`).

**Principe fondateur : toute la logique est dans l'API.** L'interface web n'en est qu'un client. Tout ce qu'elle fait doit rester faisable par n8n ou par un agent via `/api` (documentation OpenAPI sur `/docs`). Une fonctionnalité qui n'existerait que dans l'interface est un défaut.

Usage visé :
- **manuel** : l'interface web ;
- **automatique** : dossier Drive → n8n → API → `.md` renvoyé sur Drive → agent ou CRM en aval.

Le service est volontairement **transitoire** : il transcrit et synthétise, puis la connaissance part en aval (CRM, base de connaissances, agent). Pas d'historique consultable, de recherche entre réunions ni de chat.

## 2. Règles de travail (à respecter)

- **Git** :
  - une branche par fonctionnalité (`feature/…`, `fix/…`, `docs/…`) ;
  - des commits réguliers, avec des messages en français ;
  - fusion dans `main` par `git merge --no-ff`, puis suppression de la branche.
- **Paternité** : les commits sont au nom du mainteneur, L. Jacquinot. Aucune ligne `Co-Authored-By`, aucune mention de Claude ou d'un autre outil dans les commits et les PR. Cette consigne explicite prime sur les réglages par défaut des agents.
- **Python : uniquement uv.**
  - Dépendances dans `pyproject.toml`, ajoutées avec `uv add` ; `uv.lock` est commité.
  - Commandes locales : `uv sync`, `uv run …`.
  - Dans l'image : `uv sync --frozen`.
  - Jamais de `pip install` ni de venv manuel. Seule exception : la mise à jour de yt-dlp au démarrage, faite par `uv pip` dans l'entrypoint.
- **Tests** : `uv run pytest` doit passer avant chaque fusion. Les tests tournent sans GPU, avec `FAKE_PIPELINE=true` et de faux serveurs Ollama, LLM, webhook et yt-dlp.
- **Ne jamais commiter de média.** Un `audio.mp4` déposé à la racine a dû être purgé de tout l'historique. `.gitignore` exclut `*.m4a`, `*.mp4`, `*.mp3` et `*.wav` à la racine, ainsi que `data/`, `models/` et `.env`.
- **Langue** : code commenté, interface, documentation et messages d'erreur en français, avec la typographie française : espaces insécables, guillemets « », apostrophes typographiques dans les textes affichés.
- **Interface : uniquement des URL relatives** (`fetch("api/…")`). On y accède souvent depuis un autre poste du réseau ; une URL en `127.0.0.1` ou `localhost` casse tout.

## 3. Environnement de référence

Le projet est développé et mesuré sur :
- une station Linux (Ubuntu 24.04) avec une **RTX 3090 24 Go**, un i7-12700 et 62 Go de RAM, Docker et le runtime NVIDIA ;
- Ollama sur le même hôte, joint par le conteneur via `host.docker.internal` (`extra_hosts: host-gateway`), avec un modèle d'environ 17 Go ;
- n8n sur une autre machine du réseau local, pour l'automatisation ;
- un navigateur sur un autre poste.

Les deux images visent plus large : `cuda` pour toute carte NVIDIA de 8 Go ou plus (Linux, Windows avec WSL2), `cpu` pour les PC et Mac sans GPU.

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
| `app/config.py` | `Settings` (pydantic-settings), `LANGUAGES`, `DEVICES`, appareils disponibles |
| `app/profiles.py` | Profils de performance (modèle et pas de diarisation par appareil), pics de VRAM mesurés, choix de la précision et des lots selon la VRAM libre |
| `app/db.py` | Schéma SQLite et accès : une connexion par opération ; `claim_next_task` en `BEGIN IMMEDIATE` ; `recover_interrupted_tasks` |
| `app/api/` | Routes : `jobs`, `speakers`, `summaries`, `prompts`, `settings` (réglages LLM), `system`, et `common` pour les aides partagées |
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
| `app/llm/` | `base` (fournisseur, `num_ctx`), `config` (réglages effectifs : base puis `.env`), `ollama`, `openai_compat`, `summarize` (tâche), `prompts` (prompt système par défaut) |
| `app/callbacks.py` | Webhooks sortants |
| `web/` | Interface : `index.html`, `job.html`, `settings.html`, `app.js`, `style.css`, `vendor/` (Alpine.js, marked, DOMPurify), logos et favicons |
| `tests/` | pytest : jobs, GPU, intervenants et rendu, comptes rendus, intégrations (YouTube, callbacks, auth) |
| `docs/branding/` | Logo d'origine et `make_logo.py`, qui génère tous les SVG, ICO et PNG de `web/` |

## 5. Journal des décisions

### Exécution et GPU

- **Un seul traitement à la fois**, grâce à une file unique en base (`tasks`) et un seul worker asyncio.
  - **Pourquoi** : une carte de 24 Go ne peut pas porter à la fois un LLM d'environ 17 Go dans Ollama et la chaîne WhisperX (pic d'environ 10 Go en `large-v3`).
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
  - **Contrôle de la VRAM** : au début seulement, vérifier qu'il reste assez de VRAM libre pour la configuration la plus économe du modèle (`profiles.min_vram_gb` : 4,9 Go en `large-v3`, 3 Go en `large-v3-turbo`, 2,6 Go en `small` ; 2,6 Go pour une nouvelle diarisation seule), ou `MIN_FREE_VRAM_GB` s'il est défini. En dessous, le job échoue avec un message clair, plutôt que d'aller jusqu'à une erreur CUDA de mémoire. La VRAM libre mesurée sert ensuite à choisir précision et taille des lots (voir Profils). Le pic réellement alloué par torch est écrit dans `pipeline.log` après chaque étape.
  - **Pourquoi** : un autre client d'Ollama (Open WebUI, par exemple) peut recharger un modèle à tout moment.
  - Tout cela est sauté si le job tourne en `device=cpu`.
- **Progression** (`Reporter` dans `pipeline.py`) : `progress` (0-100) par étape, et `progress_detail`, un texte libre affiché sous la barre (téléchargement d'un modèle avec sa taille, chargement, taille des lots). Un champ plutôt qu'un nouveau statut, pour ne pas changer la liste des statuts sur laquelle s'appuient les intégrations.
  - WhisperX signale l'avancée par rafales, à la fin de chaque lot : une avancée d'au moins 0,5 point est toujours écrite, les plus petites au plus une fois par seconde. L'ancienne règle (une écriture par seconde au plus) perdait toute la rafale sauf son premier point : 0 %, 5 %, puis 100 % sur CPU.
  - **Lots de 4 sur CPU** (`cpu_batch_size`). Mesuré sur l'interview de 7 min 54 en turbo int8 : 91 s en lots de 16 (premier retour à 71 s), 92 s en lots de 8, 95 s en lots de 4 (un retour toutes les 18 s environ).
  - **Téléchargement des modèles Whisper** au premier usage : fait à part (`_download_whisper`), dans un fil, en mesurant le dossier `blobs` du cache Hugging Face (le fichier `.incomplete` grossit au fil de l'eau) ; la taille attendue vient de l'API Hugging Face.
- **Reprise après redémarrage** : une tâche restée `running` est remise en file une seule fois (`max_attempts=2`), puis marquée en échec.
- **`rediarize`** : relance seulement la diarisation à partir de `aligned.json` (environ 1 min). Un échec garde le résultat précédent ; le job reste `completed`, avec le message d'erreur.

### Image Docker

- **Deux variantes, un seul Dockerfile** (`ARG FLAVOR`) : `cuda` (amd64, torch cu128, 13,5 Go) et `cpu` (amd64 et arm64, torch CPU, 4,5 Go, `DEFAULT_DEVICE=cpu`, `DEFAULT_MODEL=large-v3-turbo`). Extras uv `cuda` et `cpu` (mêmes noms que les variantes) déclarés en conflit ; torch vient de l'index correspondant.
  - Compose : `compose.yaml` (cuda) et `compose.cpu.yaml` (sans réservation de GPU). Tous deux portent `image: ghcr.io/lenny-ai-data/meeting-scribe:{cuda,cpu}` et `build:` pour la construction locale.
  - Publication sur GHCR par `.github/workflows/docker.yml` à chaque tag `v*` : tests, image cuda, image cpu construite nativement par architecture (runner `ubuntu-24.04-arm`) puis manifeste commun. Étiquettes `:cuda`, `:cpu`, `:X.Y.Z-cuda`, `:X.Y.Z-cpu` ; un lancement manuel publie seulement `<branche>-cuda` et `<branche>-cpu`.
  - **Modèle de diarisation embarqué** par un secret BuildKit `hf_token` (le `HF_TOKEN` du `.env` via `secrets: environment` dans Compose ; le secret de dépôt `HF_TOKEN` en CI). Le jeton ne reste pas dans l'image (vérifié dans l'historique et le système de fichiers).
  - Dans `.env.example`, `DEFAULT_MODEL` et `DEFAULT_DEVICE` sont commentés : une valeur, même vide, dans le `.env` écraserait celle de l'image.
- **Base `ubuntu:24.04` plutôt que `nvidia/cuda`.** CUDA, cuDNN et cuBLAS viennent des roues pip de torch (index `pytorch-cu128`).
  - `LD_LIBRARY_PATH` pointe sur `site-packages/nvidia/{cudnn,cublas}/lib` pour que ctranslate2, utilisé par faster-whisper, les trouve.
- **`libpython3.12t64`** : torchcodec, utilisé par pyannote 4, en a besoin. Sans elle, avertissement puis échec du décodage audio.
- **Pas de torchcodec sur Linux arm64** (`override-dependencies` dans `pyproject.toml`) : la version 0.7, liée à torch 2.8, n'a pas de roue arm64. pyannote s'en passe, car l'audio lui est passé déjà décodé (vérifié : diarisation identique sans torchcodec). Ne jamais lui passer un chemin de fichier. À revoir en passant à torch 2.9 ou plus, dont les torchcodec ont des roues arm64.
- **Utilisateur `ubuntu` (uid 1000)** : `./data` et `./models` appartiennent à l'utilisateur de l'hôte, pas à root.
- **Caches** : `HF_HOME`, `TORCH_HOME` et `MPLCONFIGDIR` sont tous sous `/models`, donc persistants.
- **uv 0.11** et **Deno** sont copiés depuis leurs images officielles.
- **uvicorn `--workers 1`, obligatoire** : le worker et la file vivent dans le processus.

### Transcription et diarisation

- **WhisperX 3.8.6** : torch 2.8 cu128, pyannote-audio 4.0.7, faster-whisper ≥ 1.2.
  - Modèles : `small`, `large-v3-turbo` ou `large-v3`, choisis par le profil de performance (voir plus bas).
  - Langues : `fr` par défaut, ou `en`.
  - Matériel : GPU par défaut, CPU possible.
- **`interleaved_context`** n'existe pas en 3.8.6 : il n'est activé que si `inspect.signature` le trouve, ce qui prépare les versions suivantes.
- **Vocabulaire** (noms propres, jargon) : passé en `asr_options.initial_prompt`.
- **Diarisation** : `pyannote/speaker-diarization-community-1`.
  - Le dépôt est à accès restreint, mais le modèle est sous CC-BY-4.0, donc redistribuable avec attribution. **Il est embarqué dans l'image** (`DIARIZATION_MODEL_DIR`, 32 Mo, chemins relatifs `$model/…` dans `config.yaml`) : les utilisateurs n'ont besoin ni de jeton ni de réseau (vérifié avec `HF_HUB_OFFLINE=1`). Sans copie locale, repli sur l'identifiant HF et `HF_TOKEN`.
  - `assign_word_speakers(fill_nearest=True)`.
- **Profils de performance** (`app/profiles.py`), décidés le 01/10/2026 : l'utilisateur ne choisit plus un modèle mais un profil, qui fixe le modèle Whisper et le pas de diarisation selon l'appareil. Origine : 17 min pour 7 min d'audio sur un portable avec `large-v3-turbo` et le pas de 1 s, jugé trop long ; une transcription un peu moins fidèle suffit souvent, le compte rendu par LLM rattrape.

  | Profil | CPU | GPU |
  |---|---|---|
  | Très rapide (`tres_rapide`) | `small`, pas de 5 s | `small`, pas de 2,5 s (cartes de 3 à 4 Go) |
  | Rapide (`rapide`) | `small`, pas de 2,5 s (défaut) | `large-v3-turbo`, pas de 2,5 s |
  | Précis (`precis`) | `large-v3-turbo`, pas de 2,5 s | `large-v3`, pas de 2,5 s |
  | Très précis (`tres_precis`) | `large-v3-turbo`, pas de 1 s | `large-v3`, pas de 1 s (défaut) |

  - Le job enregistre `profile`, `model` et `diarization_step` ; le frontmatter du transcript porte le profil. L'API ne prend plus de `model`.
  - **Nombre d'intervenants** : l'interface ne propose plus que le nombre exact (`num_speakers`), « améliore les modes rapides ». Aucun repli automatique : le profil « Très rapide » sans nombre peut confondre deux voix (mesuré : 12 % d'écart au pas de 5 s sans nombre, 3,5 % avec). L'API garde `min_speakers` et `max_speakers`.
  - Durées mesurées (7 min 54, modèles en cache, nombre d'intervenants donné), par étape :

    | | Transcription | Alignement | Diarisation | Total |
    |---|---|---|---|---|
    | CPU très rapide | 48 s | 24 s | 34 s (pas de 5 s) | ≈ 1 min 50 |
    | CPU rapide | 48 s | 24 s | 67 s (2,5 s) | ≈ 2 min 20 |
    | CPU précis | 99 s | 24 s | 67 s | ≈ 3 min 15 |
    | CPU très précis | 99 s | 24 s | 164 s (1 s) | ≈ 4 min 50 |
    | GPU très rapide | 3,4 s | 4,3 s | 3,1 s | ≈ 16 s |
    | GPU rapide | 6,5 s | 4,3 s | 3,1 s | ≈ 19 s |
    | GPU précis | 10,2 s | 4,3 s | 3,1 s | ≈ 23 s |
    | GPU très précis | 10,2 s | 4,3 s | 6,6 s | ≈ 26 s |

- **Choix des moteurs.** Mesures sur une interview radio de 7 min 54 en français, i7-12700, 10 threads, WER contre `large-v3` sur GPU :

    | Moteur | Transcription | WER | Verdict |
    |---|---|---|---|
    | Whisper `small` int8 | 47 s | 15 % | profils rapides |
    | Whisper `large-v3-turbo` int8 | 94 s | 6 % | profils précis (CPU) |
    | Whisper `medium` int8 | 117 s | 9 % | écarté : plus lent que turbo (décodeur complet) |
    | Whisper `base` / `tiny` int8 | 19 / 13 s | 28 / 36 % | écartés : un mot sur trois ou quatre faux |
    | Parakeet TDT 0.6B v3 (onnx-asr) | 33 s | 61 % | écarté : dérive en anglais au milieu des phrases, pas de langue forcée |
    | Phonon-2 (Parakeet v3 quantifié 2 bits) | — | 36 % | écarté : reste en français, mais niveau `tiny` et 22 % des mots omis |
    | Canary-1B-v2 (onnx-asr, `language="fr"`) | 574 s | — | écarté : boucles d'hallucinations, pas d'horodatage par mot |

  - Les variantes q5 de whisper.cpp n'ont pas été mesurées : CTranslate2 (WhisperX) descend au plus à int8.
- **Pas de diarisation** (appliqué à `pipeline.model._segmentation.step`). pyannote analyse des fenêtres de 10 s avec un pas de 1 s : chaque instant est vu par une dizaine de fenêtres, et une empreinte vocale (WeSpeaker) est calculée par fenêtre et par intervenant local. Ces empreintes font 198 s des 203 s de diarisation sur CPU. Le pas ne change pas la précision des frontières (trames d'environ 17 ms) mais le nombre de fenêtres, donc d'empreintes et de « votes » par instant. La fenêtre de 10 s, elle, est fixée par l'entraînement du modèle de segmentation (trois intervenants locaux au plus) : ne pas la modifier.

  | Pas | Durée (CPU) | Intervenants trouvés | Écart (DER) avec le pas de 1 s |
  |---|---|---|---|
  | 1 s | 203 s | 4 | référence |
  | 2,5 s | 81 s | 3 | 3,9 % (4,2 % avec `num_speakers=3`) |
  | 5 s | 41 s | 2 | 11,8 % (3,5 % avec `num_speakers=3`) |

  Les prises de parole très brèves risquent d'être absorbées (environ 13 s de relances de la troisième personne sur l'interview). Donner le nombre d'intervenants supprime l'essentiel du risque restant, qui porte sur le comptage.
- **VRAM adaptative sur GPU** (`profiles.choose_gpu_config`). La transcription est toujours l'étape limitante (alignement 0,7 Go, diarisation 1,6 Go). Au début du job, la VRAM libre mesurée choisit la première configuration qui tient avec 1 Go de marge : float16 lots de 16, puis int8_float16 lots de 16, 8, 4. `BATCH_SIZE` fixe la taille des lots ; seule la précision s'adapte alors.

  | Pic de transcription (chargement compris) | float16 · 16 | int8 · 16 | int8 · 8 | int8 · 4 |
  |---|---|---|---|---|
  | `small` | 2,5 Go · 5 s | 2,1 Go · 6 s | 1,5 Go · 6 s | 1,2 Go · 7 s |
  | `large-v3-turbo` | 4,3 Go · 7 s | 3,3 Go · 7 s | 2,6 Go · 7 s | 2,0 Go · 8 s |
  | `large-v3` | 9,8 Go · 12 s | 8,0 Go · 12 s | 5,4 Go · 13 s | 3,9 Go · 15 s |

  int8 ne dégrade pas la qualité (WER de turbo : 6,2 % en int8 contre 6,4 % en float16 ; `large-v3` int8 lots de 4 : 2,4 % d'écart avec float16 lots de 16). Une carte de 6 Go fait donc tourner tous les profils. `/api/system` grise les profils dont le minimum dépasse la VRAM totale de la carte.
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
- **Pas de reconnaissance vocale ni de base d'intervenants.** Choix délibéré : aucune empreinte vocale n'est conservée, l'identification se fait à la main dans l'interface. La seule suggestion proposée est `OWNER_NAME` (le propriétaire de l'instance). `/api/people` existe toujours, mais l'interface ne s'en sert plus.
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
  - **Distant : un seul connecteur générique compatible OpenAI** (`/chat/completions`), plutôt qu'un connecteur par fournisseur. Il couvre OpenAI, Mistral, OpenRouter, et les serveurs locaux LM Studio, llama.cpp, vLLM.
- **Réglages LLM en base** (`app/llm/config.py`, table `settings`, clé `llm`), modifiables dans *Réglages* et par `/api/settings/llm` : ils priment sur le `.env` champ par champ ; `null` rend la valeur du `.env`. Pour une diffusion où chacun branche son LLM sans éditer de fichier.
  - La clé d'API peut donc être stockée en base (en clair dans `scribe.db`) ; elle est en écriture seule, jamais renvoyée par l'API.
  - Pas de modèle Ollama par défaut (`OLLAMA_MODEL` vide) : l'API comme l'interface prennent alors le premier modèle installé.
  - **Déchargement d'Ollama** avant un job GPU : automatique seulement si son hôte est `host.docker.internal`, `localhost` ou `127.0.0.1`. Un Ollama distant ne partage pas le GPU, et le décharger gênerait ses autres utilisateurs. Forçable dans *Réglages* ou par `OLLAMA_UNLOAD_BEFORE_GPU`.
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
- **Auth** : `API_TOKEN` est vide par défaut, le service étant pensé pour un réseau local ; le définir dès que le service est exposé au-delà. `/api/health` reste toujours ouvert pour le healthcheck.

### Interface (`web/`)

- **Sans étape de build** : Alpine.js, marked et DOMPurify sont stockés dans `web/vendor/`, sans CDN.
- **Coquille commune** (`app.js`) : `withShell(page)` fusionne la barre latérale des réunions et le voyant d'état avec les données propres à la page. Les descripteurs sont copiés, pour que les accesseurs `get` restent calculés. Le balisage de l'en-tête et de la barre latérale est dupliqué dans les trois pages.
- **Voyant d'état** : l'état vient de l'API (`/api/system` → `status`) :
  - `error` (rouge) si le modèle de diarisation n'est ni embarqué ni téléchargeable (`HF_TOKEN` absent), ou si le GPU est introuvable alors que `DEFAULT_DEVICE=cuda` ;
  - `busy` (doré, pulsation lente) pendant une tâche ;
  - `ready` (vert) sinon.
  - Ollama injoignable n'est qu'un avertissement (`warnings`), affiché dans l'infobulle.
- **Fond** : trois halos aux couleurs de l'identité visuelle, plus une trame de points, insérés par `app.js` (`.backdrop`). Ils dérivent lentement, sauf si `prefers-reduced-motion` est actif. Leur opacité est plus faible en clair. Les cartes sont translucides (`backdrop-filter`).
- **Page d'une réunion** : sections dépliantes (`<details class="card section">`) dans cet ordre : Intervenants, Transcript (replié par défaut, car ce n'est pas le cœur de l'usage), puis Compte rendu. Ordre et repli voulus.
- **Réglages** : pas de barre latérale (`withShell(page, null, { sidebar: false })`), mais le menu « Réunions » reste dans l'en-tête de toutes les pages.
- **Thème** : Auto, Clair ou Sombre, choisi dans l'en-tête et mémorisé dans `localStorage` (`meeting-scribe.theme`).
  - **Mécanisme** : attribut `data-theme` sur `<html>`. Les variables sombres sont définies deux fois : sous `@media (prefers-color-scheme: dark) :root:not([data-theme="light"])` et sous `:root[data-theme="dark"]`.
  - **Logo** : un SVG chargé en `<img>` ne voit que le thème du système. D'où les variantes figées `logo-mark-light.svg` et `logo-mark-dark.svg`, choisies par `app.js` quand le thème est forcé.
  - **Favicon** : il suit toujours le thème du système (limite du navigateur).

### Identité visuelle

- **Logo** : vectorisé depuis `docs/branding/logo-original.jpg` par `docs/branding/make_logo.py`, qui génère tous les fichiers de `web/` (favicons, variantes du logo). **Pour modifier le logo, modifier le script, pas les SVG.**
- **Palette** : violet `#5b2bd9` en clair, `#a98bff` en sombre.

## 6. Mesures de référence (RTX 3090 et i7-12700, modèles déjà en cache)

| Mesure | Valeur |
|---|---|
| Déchargement d'Ollama | environ 1 s (VRAM de 20,3 à 0,5 Go) |
| Interview de 7 min 54 | environ 60 s en `large-v3`, environ 40 s en `large-v3-turbo` |
| Interview télévisée de 5 min 13 | 22 s |
| Pic de VRAM, transcription | voir le tableau de la VRAM adaptative (§ 5) |
| Pic de VRAM, alignement et diarisation | 0,7 Go et 1,6 Go réellement nécessaires. Sur une carte libre, la diarisation monte à 9,8 Go sur certains fichiers (espace de travail opportuniste, mesure de `torch.cuda.max_memory_allocated`) ; plafonnée à 5,9 Go, elle donne le même résultat |
| VRAM après un job | environ 280 Mo |
| Image `cpu`, interview de 7 min 54, profil rapide (`small`, pas de 2,5 s) | 2 min 23 (transcription 50 s, alignement 25 s, diarisation 66 s) |
| Image `cpu`, interview de 7 min 54, `large-v3-turbo` et pas de 1 s (avant le profil rapide) | 4 min 55 (transcription 1 min 50 avec téléchargement du modèle, alignement 27 s, diarisation 2 min 36) |

## 7. Commandes courantes

```bash
# Développement (sans GPU)
uv sync
uv run pytest

# Construction locale et déploiement (HF_TOKEN du .env pour embarquer pyannote)
docker compose up -d --build                        # image cuda
docker compose -f compose.cpu.yaml up -d --build    # image cpu
docker compose logs -f meeting-scribe
curl -s http://localhost:8090/api/system | jq       # GPU, Ollama, file d'attente, versions

# Journal d'un job
less data/jobs/<id>/pipeline.log

# Logo et favicons (après modification du script)
uv run python docs/branding/make_logo.py
```

## 8. Pièges connus

- **`data/` et `models/`** : les créer avant le premier `docker compose up`, sinon Docker les crée en root et le conteneur (uid 1000) ne peut plus y écrire.
- **Diarisation en échec avec une erreur 401 ou 403 de Hugging Face** : seulement avec une image construite sans le modèle embarqué. Le jeton est absent, ou les conditions de `pyannote/speaker-diarization-community-1` ne sont pas acceptées sur le compte.
- **onnxruntime 1.30 et le cache Hugging Face 2.0** : un modèle ONNX à données externes (`*.onnx.data`) ne se charge pas depuis le cache HF (« External data path escapes model directory »). Le télécharger dans un dossier ordinaire (`snapshot_download(local_dir=…)`). Rencontré en évaluant Parakeet, sans effet sur le code actuel.
- **« VRAM libre insuffisante »** : un autre programme occupe la carte, par exemple un autre service de transcription ou un modèle chargé hors Ollama.
- **YouTube réclame une connexion** : vérifier que yt-dlp est à jour (logs de démarrage), puis fournir les cookies.
- **Workflows n8n** : ils ont été écrits sans accès à l'instance. Les versions de nœuds peuvent demander un ajustement à l'import.
- **`/docs`** : la page OpenAPI de FastAPI garde son favicon par défaut et n'a pas le sélecteur de thème.
- **Tests** : ils forcent `FAKE_PIPELINE=true` et un `DATA_DIR` temporaire ; ne jamais les pointer sur `./data`.

## 9. Pistes

- Tester un enregistrement de 1 h 30 : durée totale, VRAM, taille de contexte du compte rendu.
- Accélérer encore la diarisation sur CPU, toujours la plus longue étape du profil rapide (66 s sur 2 min 23) : empreintes vocales en ONNX, par exemple.
- Application Windows sans Docker (uv/PyPI ou `.exe`). À traiter : annulation sans `os.killpg`, chemins `/data` et `/models`, ffmpeg et Deno.
- Favicon et sélecteur de thème de la page `/docs`.
