import json
from pathlib import Path


def _default(obj):
    # Scalaires numpy (np.float32…) produits par WhisperX / pyannote
    if hasattr(obj, "item"):
        return obj.item()
    raise TypeError(f"Type non sérialisable : {type(obj).__name__}")


def write_json(path: Path, data) -> None:
    """Écriture atomique (fichier temporaire puis renommage)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=_default), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))
