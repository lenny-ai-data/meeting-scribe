<p align="center"><img src="web/logo-mark.svg" width="72" alt=""></p>

# Meeting Scribe

Service auto-hébergé de transcription de réunions, en français ou en anglais.

## Objectif

Déposer l'enregistrement d'une réunion et obtenir :
- un **transcript Markdown** horodaté, avec le nom de chaque intervenant ;
- en option, un **compte rendu** rédigé par un LLM, local (Ollama) ou distant (toute API compatible OpenAI).

Les entrées acceptées sont les fichiers audio ou vidéo (`.m4a` d'iPhone, `.mp4`…) et les URL YouTube. Les intervenants sont séparés automatiquement ; on les nomme ensuite dans l'interface en écoutant de courts extraits.

Tout passe par une API : l'interface web n'en est qu'un client, et n8n, un script ou un agent peuvent l'utiliser de la même manière.

## Prérequis

Deux images Docker, selon la machine :

| | Image `cuda` | Image `cpu` |
|---|---|---|
| Pour | PC ou station avec carte NVIDIA (4 Go de VRAM ou plus) | PC sans carte graphique, ou Mac |
| Système | Linux x86_64, ou Windows 10/11 avec Docker Desktop | Linux, Windows ou macOS (x86_64 ou arm64) |
| Docker | Compose v2 et, sous Linux, le [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/latest/install-guide.html) | Compose v2, ou Docker Desktop |
| Mémoire vive | 16 Go | 16 Go |
| Disque | environ 20 Go | environ 8 Go |
| Durée pour 10 min d'audio | 20 à 60 s | 3 à 10 min |

Pour les comptes rendus (facultatifs) : Ollama ou une clé d'API.

Avec l'image `cuda`, vérifier que Docker voit le GPU :

```bash
docker run --rm --gpus all ubuntu nvidia-smi
```

## Installation

```bash
mkdir meeting-scribe && cd meeting-scribe
BASE=https://raw.githubusercontent.com/lenny-ai-data/meeting-scribe/main
curl -fsSLO $BASE/compose.yaml              # carte NVIDIA
curl -fsSLO $BASE/compose.cpu.yaml          # ou : sans GPU
curl -fsSL -o .env $BASE/.env.example       # réglages, tous facultatifs

mkdir -p data models                        # avant le premier démarrage
docker compose up -d                        # carte NVIDIA
docker compose -f compose.cpu.yaml up -d    # ou : sans GPU
```

Ouvrir ensuite `http://localhost:8090`, ou `http://<adresse-du-serveur>:8090` depuis un autre poste : le voyant en haut de page doit être vert.

- **Windows** : [Docker Desktop](https://docs.docker.com/desktop/setup/install/windows-install/) avec WSL2, puis les mêmes commandes dans PowerShell (`curl.exe` au lieu de `curl`, `mkdir data, models`).
- **Mac** : Docker Desktop et l'image `cpu`.
- **Linux** : le conteneur tourne avec l'uid 1000. Si `id -u` renvoie une autre valeur : `sudo chown -R 1000:1000 data models`.

Le premier job télécharge les modèles de transcription (de 0,5 à 4 Go selon le profil) : il est plus long que les suivants.

**Comptes rendus** : choisir le modèle de langage dans *Réglages*. Pour Ollama sur la même machine, il doit écouter sur toutes les interfaces (`OLLAMA_HOST=0.0.0.0`) ; voir [Comptes rendus](docs/technique.md#comptes-rendus).

## Utilisation

1. **Déposer** un fichier ou coller une URL YouTube, puis régler les options : titre, date, vocabulaire (noms propres, jargon), profil (de « Très rapide » à « Très précis »), langue et nombre d'intervenants. Indiquer ce nombre améliore nettement les profils rapides.
2. **Suivre** l'avancement : les réunions sont listées à gauche. Le voyant de l'en-tête indique l'état du service : vert prêt, doré occupé, rouge en panne (détail au survol).
3. **Nommer les intervenants** : la frise montre qui parle quand. Écouter les extraits de chaque voix (▶), puis taper son nom dans le titre de sa carte.
   - Donner le même nom à deux intervenants les fusionne.
   - Si le nombre de voix est faux, « Relancer la diarisation » avec le bon nombre (environ 1 min, sans refaire la transcription).
4. **Récupérer** le transcript `.md`, et générer, retoucher puis télécharger le compte rendu.
5. **Retrouver** un passage : le champ de recherche, en haut de la liste des réunions, parcourt tous les transcripts (accents et majuscules ignorés, `"entre guillemets"` pour une expression exacte). Un résultat ouvre le transcript au bon endroit, termes surlignés.

Par l'API, la même chose en trois appels :

```bash
ID=$(curl -s -F file=@reunion.m4a -F title="Point hebdo" http://localhost:8090/api/jobs | jq -r .id)
curl -s -X PUT -H 'Content-Type: application/json' -d '{"S1":"Alice","S2":"Bob"}' \
     http://localhost:8090/api/jobs/$ID/speakers        # une fois le job terminé
curl -s -o transcript.md http://localhost:8090/api/jobs/$ID/transcript.md
```

La documentation interactive de l'API est sur `/docs`.

## Pour aller plus loin

La [documentation technique](docs/technique.md) détaille le fonctionnement, la configuration, les profils de performance, l'API et les callbacks, l'exploitation, le dépannage et le développement.

## Licence

Code sous licence [MIT](LICENSE). Les images embarquent le modèle `pyannote/speaker-diarization-community-1` (CC-BY-4.0, © pyannote) et des composants sous leurs propres licences : voir [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
