import os
import yaml
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class WalletConfig:
    address: str
    label: str
    type: str  # "vote", "identity", "admin"


@dataclass
class Config:
    helius_api_key: str
    wallets: List[WalletConfig]
    cache_db: str = "cache/transactions.db"
    output_dir: str = "output"

    @property
    def our_addresses(self) -> set:
        return {w.address for w in self.wallets}

    @property
    def address_labels(self) -> Dict[str, str]:
        return {w.address: w.label for w in self.wallets}

    def label_for(self, address: str) -> str:
        labels = self.address_labels
        if address in labels:
            return labels[address]
        return address[:8] + "..." if address else "Unknown"


def load_config(path: str = "config.yaml") -> Config:
    with open(path) as f:
        data = yaml.safe_load(f)

    api_key = data.get("helius_api_key") or os.environ.get("HELIUS_API_KEY", "")
    if not api_key or api_key == "your-helius-api-key-here":
        raise ValueError(
            "helius_api_key is not set. Add it to config.yaml or set the "
            "HELIUS_API_KEY environment variable."
        )

    wallets = []
    for w in data.get("wallets", []):
        wallets.append(WalletConfig(
            address=w["address"],
            label=w["label"],
            type=w.get("type", "admin"),
        ))

    if not wallets:
        raise ValueError("No wallets defined in config.yaml")

    return Config(
        helius_api_key=api_key,
        wallets=wallets,
        cache_db=data.get("cache_db", "cache/transactions.db"),
        output_dir=data.get("output_dir", "output"),
    )
