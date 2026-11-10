import json
import random
import re
from collections import defaultdict
from pathlib import Path


def filename_group(path: Path) -> tuple[str, str]:
    """Group actual variants without assuming contiguous numbers."""
    match = re.fullmatch(r"(.*?)(\d+)", path.stem)
    if match:
        prefix, number = match.groups()
        if not prefix:
            return path.parent.name, str(int(number))
        if any(c.isalpha() for c in prefix):
            return prefix, str(int(number))
    return path.stem, "unnumbered"


class Stickers:
    def __init__(self, root: Path, index: Path):
        self.root = root.resolve()
        self.entries = (
            json.loads(index.read_text(encoding="utf-8")) if index.exists() else []
        )
        self.groups: dict[str, dict[str, list[Path]]] = defaultdict(
            lambda: defaultdict(list)
        )
        for entry in self.entries:
            if not entry.get("enabled", False):
                continue
            path = (self.root / entry["path"]).resolve()
            if not path.is_relative_to(self.root) or not path.is_file():
                continue
            label, number = filename_group(path)
            self.groups[label][number].append(path)

    @property
    def emotions(self) -> list[str]:
        return sorted(self.groups)

    def select(self, emotion: str) -> Path | None:
        variants = {
            number: [p for p in paths if p.is_file()]
            for number, paths in self.groups.get(emotion, {}).items()
        }
        variants = {number: paths for number, paths in variants.items() if paths}
        if not variants:
            return None
        number = random.choice(list(variants))
        return random.choice(variants[number])


def load_stickers(config):
    if not config.get('sticker_root') or not config.get('sticker_index'):
        raise RuntimeError('Configure sticker_root and sticker_index')
    return Stickers(Path(config['sticker_root']),Path(config['sticker_index']))
