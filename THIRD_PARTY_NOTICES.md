# Composants tiers

Le code de Meeting Scribe est publié sous licence MIT (voir [LICENSE](LICENSE)). Les images Docker et l'interface embarquent ou téléchargent les composants suivants, chacun sous sa propre licence.

## Modèle embarqué dans les images Docker

**pyannote/speaker-diarization-community-1** : pipeline de diarisation, © pyannote (Hervé Bredin et contributeurs).
- Licence : [Creative Commons Attribution 4.0 International (CC-BY-4.0)](https://creativecommons.org/licenses/by/4.0/).
- Source : <https://huggingface.co/pyannote/speaker-diarization-community-1>.
- Le modèle est redistribué sans modification, dans `/opt/models/pyannote/speaker-diarization-community-1` ; sa fiche d'origine (`README.md`) l'accompagne.

## Modèles téléchargés au premier usage

Ils ne sont pas inclus dans les images ; ils sont téléchargés dans `./models` au premier job.

| Modèle | Usage | Licence |
|---|---|---|
| OpenAI Whisper large-v3, large-v3-turbo (conversion CTranslate2) | Transcription | MIT |
| wav2vec2 (modèles d'alignement par langue choisis par WhisperX, par exemple `jonatasgrosman/wav2vec2-large-xlsr-53-french`) | Alignement mot à mot | Apache-2.0 |

## Bibliothèques principales

| Composant | Licence |
|---|---|
| [WhisperX](https://github.com/m-bain/whisperX) | BSD-2-Clause |
| [pyannote.audio](https://github.com/pyannote/pyannote-audio) | MIT |
| [faster-whisper](https://github.com/SYSTRAN/faster-whisper), [CTranslate2](https://github.com/OpenNMT/CTranslate2) | MIT |
| [PyTorch](https://pytorch.org) | BSD-3-Clause |
| [FastAPI](https://fastapi.tiangolo.com), [Uvicorn](https://www.uvicorn.org) | MIT, BSD-3-Clause |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Unlicense |
| [Deno](https://deno.com) (moteur JavaScript de yt-dlp) | MIT |
| [FFmpeg](https://ffmpeg.org) (paquet Ubuntu) | LGPL-2.1+ / GPL-2.0+ |
| Image de base [Ubuntu 24.04](https://ubuntu.com/legal/open-source-licences) | licences des paquets Ubuntu |

La liste complète des dépendances Python et de leurs versions figure dans [uv.lock](uv.lock).

## Interface web (`web/vendor/`)

| Composant | Licence |
|---|---|
| [Alpine.js](https://alpinejs.dev) | MIT |
| [marked](https://marked.js.org) 15.0.12, © Christopher Jeffrey | MIT |
| [DOMPurify](https://github.com/cure53/DOMPurify) 3.4.16, © Cure53 | Apache-2.0 ou MPL-2.0 |
