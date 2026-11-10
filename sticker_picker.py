"""Per-article shuffle bags: stable rebuilds, no reuse before exhausting a label."""
import hashlib
import random
from pathlib import Path


def sticker_pools(bank):
    return {label:sorted({p for paths in groups.values() for p in paths if p.is_file()},key=str)
            for label,groups in bank.groups.items() if any(p.is_file() for paths in groups.values() for p in paths)}


class StickerPicker:
    def __init__(self,pools,seed):
        self.pools={label:sorted(set(map(Path,value if isinstance(value,(list,tuple)) else [value])),key=str)
                    for label,value in pools.items()}
        self.seed=str(seed);self.bags={};self.last={};self.rounds={}

    def select(self,label):
        choices=self.pools.get(label,[])
        if not choices:return None
        if not self.bags.get(label):
            bag=choices.copy();cycle=self.rounds.get(label,0)
            rng=random.Random(hashlib.sha256(f'{self.seed}:{label}:{cycle}'.encode()).digest());rng.shuffle(bag)
            if len(bag)>1 and bag[-1]==self.last.get(label):bag[0],bag[-1]=bag[-1],bag[0]
            self.bags[label]=bag;self.rounds[label]=cycle+1
        chosen=self.bags[label].pop();self.last[label]=chosen
        return chosen
