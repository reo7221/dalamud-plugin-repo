import json
import re
import sys
import urllib.request
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError


try:
    import yaml
except ImportError as exc:
    raise SystemExit(
        "PyYAML is required. Install it with: pip install pyyaml"
    ) from exc


ROOT_DIR = Path(__file__).resolve().parent
PLUGINS_YAML = ROOT_DIR / "plugins.yaml"
PLUGINMASTER_JSON = ROOT_DIR / "pluginmaster.json"


REQUIRED_STORE_KEYS = [
    "Author",
    "Name",
    "InternalName",
    "AssemblyVersion",
    "Description",
    "ApplicableVersion",
    "RepoUrl",
    "DalamudApiLevel",
    "Punchline",
]


def load_config() -> dict[str, Any]:
    if not PLUGINS_YAML.exists():
        raise FileNotFoundError(f"Missing config file: {PLUGINS_YAML}")

    with PLUGINS_YAML.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    plugins = data.get("plugins", [])

    if not isinstance(plugins, list):
        raise ValueError("'plugins' must be a list")

    return {"plugins": plugins}


def fetch_json(url: str) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "dalamud-plugin-repo-generator",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        raise RuntimeError(f"HTTP error while fetching {url}: {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"URL error while fetching {url}: {exc.reason}") from exc

    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON from {url}: {exc}") from exc


def parse_github_repo_url(repo_url: str) -> tuple[str, str]:
    match = re.fullmatch(
        r"https://github\.com/([^/\s]+)/([^/\s]+?)(?:\.git)?/?",
        repo_url.strip(),
    )

    if not match:
        raise ValueError(f"Invalid GitHub repository URL: {repo_url}")

    return match.group(1), match.group(2)


def get_latest_release(owner: str, repo: str) -> dict[str, Any]:
    api_url = f"https://api.github.com/repos/{owner}/{repo}/releases/latest"
    release = fetch_json(api_url)

    if not isinstance(release, dict):
        raise ValueError(f"Invalid GitHub release response: {api_url}")

    return release


def find_asset(
    release: dict[str, Any],
    *,
    asset_name: str | None = None,
    predicate=None,
    description: str,
) -> dict[str, Any]:
    assets = release.get("assets", [])

    if not isinstance(assets, list):
        raise ValueError("GitHub release response does not contain an assets list")

    candidates: list[dict[str, Any]] = []

    for asset in assets:
        if not isinstance(asset, dict):
            continue

        name = asset.get("name")

        if not isinstance(name, str):
            continue

        if asset_name is not None:
            if name == asset_name:
                candidates.append(asset)
        elif predicate is not None and predicate(name):
            candidates.append(asset)

    if not candidates:
        release_name = release.get("name") or release.get("tag_name") or "<unknown>"
        raise ValueError(f"Could not find {description} in release {release_name}")

    if len(candidates) > 1:
        names = [asset.get("name") for asset in candidates]
        raise ValueError(
            f"Found multiple candidates for {description}: {names}. "
            f"Specify it explicitly in plugins.yaml."
        )

    return candidates[0]


def get_asset_download_url(asset: dict[str, Any]) -> str:
    url = asset.get("browser_download_url")

    if not isinstance(url, str) or not url:
        raise ValueError(f"Asset does not have browser_download_url: {asset.get('name')}")

    return url


def validate_manifest(manifest: dict[str, Any], source: str) -> None:
    missing = [key for key in REQUIRED_STORE_KEYS if key not in manifest]

    if missing:
        raise ValueError(f"Manifest from {source} is missing required keys: {missing}")


def build_entry_from_plugin_config(plugin: dict[str, Any]) -> dict[str, Any]:
    repo_url = plugin.get("repo")
    enabled = plugin.get("enabled", True)

    if enabled is False:
        raise ValueError("Disabled plugin should have been filtered before processing")

    if not isinstance(repo_url, str) or not repo_url:
        raise ValueError("Each plugin must have a non-empty 'repo'")

    asset_name = plugin.get("asset_name", "latest.zip")
    manifest_asset_name = plugin.get("manifest_asset_name")

    if not isinstance(asset_name, str) or not asset_name:
        raise ValueError(f"'asset_name' must be a non-empty string: {repo_url}")

    if manifest_asset_name is not None and not isinstance(manifest_asset_name, str):
        raise ValueError(f"'manifest_asset_name' must be a string: {repo_url}")

    owner, repo = parse_github_repo_url(repo_url)
    release = get_latest_release(owner, repo)

    zip_asset = find_asset(
        release,
        asset_name=asset_name,
        description=f"plugin zip asset '{asset_name}'",
    )

    manifest_asset = find_asset(
        release,
        asset_name=manifest_asset_name,
        predicate=lambda name: name.lower().endswith(".json"),
        description="manifest JSON asset",
    )

    manifest_url = get_asset_download_url(manifest_asset)
    zip_url = get_asset_download_url(zip_asset)

    manifest = fetch_json(manifest_url)

    if not isinstance(manifest, dict):
        raise ValueError(f"Manifest asset must be a JSON object: {manifest_url}")

    validate_manifest(manifest, manifest_url)

    entry = dict(manifest)
    entry["DownloadLinkInstall"] = zip_url
    entry["DownloadLinkUpdate"] = zip_url

    return entry


def collect_entries(plugins: list[Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen_internal_names: set[str] = set()

    for plugin in plugins:
        if not isinstance(plugin, dict):
            raise ValueError("Each item in 'plugins' must be an object")

        if plugin.get("enabled", True) is False:
            continue

        entry = build_entry_from_plugin_config(plugin)
        internal_name = entry.get("InternalName")

        if not isinstance(internal_name, str) or not internal_name:
            raise ValueError("Generated entry has invalid InternalName")

        if internal_name in seen_internal_names:
            raise ValueError(f"Duplicate InternalName detected: {internal_name}")

        entries.append(entry)
        seen_internal_names.add(internal_name)

    return entries


def write_pluginmaster(entries: list[dict[str, Any]]) -> None:
    sorted_entries = sorted(entries, key=lambda x: x.get("InternalName", "").lower())

    PLUGINMASTER_JSON.write_text(
        json.dumps(sorted_entries, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    print(f"Generated {PLUGINMASTER_JSON} with {len(sorted_entries)} entries")

    for entry in sorted_entries:
        print(
            f"- {entry.get('InternalName')} "
            f"{entry.get('AssemblyVersion')} "
            f"API {entry.get('DalamudApiLevel')}"
        )


def main() -> None:
    config = load_config()
    entries = collect_entries(config["plugins"])
    write_pluginmaster(entries)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
