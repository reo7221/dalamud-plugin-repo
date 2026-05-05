import json
from pathlib import Path

try:
    import yaml
except ImportError as exc:
    raise SystemExit(
        "PyYAML is required. Install it with: pip install pyyaml"
    ) from exc


ROOT_DIR = Path(__file__).resolve().parent
PLUGINS_YAML = ROOT_DIR / "plugins.yaml"
PLUGINMASTER_JSON = ROOT_DIR / "pluginmaster.json"


def load_plugins_config() -> dict:
    if not PLUGINS_YAML.exists():
        raise FileNotFoundError(f"Missing config file: {PLUGINS_YAML}")

    with PLUGINS_YAML.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if "plugins" not in data:
        raise ValueError("plugins.yaml must contain a top-level 'plugins' key")

    if not isinstance(data["plugins"], list):
        raise ValueError("'plugins' must be a list")

    return data


def main() -> None:
    config = load_plugins_config()

    # TODO:
    # Later, each plugin URL in plugins.yaml will be resolved into
    # a Dalamud plugin repository entry.
    entries = []

    PLUGINMASTER_JSON.write_text(
        json.dumps(entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Generated {PLUGINMASTER_JSON} with {len(entries)} entries")


if __name__ == "__main__":
    main()
