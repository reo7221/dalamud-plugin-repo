import io
import json
import re
import sys
import urllib.request
import zipfile
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


def fetch_bytes(url: str, accept: str = "*/*") -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": accept,
            "User-Agent": "dalamud-plugin-repo-generator",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.read()
    except HTTPError as exc:
        raise RuntimeError(f"HTTP error while fetching {url}: {exc.code}") from exc
    except URLError as exc:
        raise RuntimeError(f"URL error while fetching {url}: {exc.reason}") from exc


def fetch_json(url: str) -> Any:
    body = fetch_bytes(url, accept="application/json").decode("utf-8")

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


def get_release_assets(release: dict[str, Any]) -> list[dict[str, Any]]:
    assets = release.get("assets", [])

    if not isinstance(assets, list):
        raise ValueError("GitHub release response does not contain an assets list")

    return [asset for asset in assets if isinstance(asset, dict)]


def get_asset_name(asset: dict[str, Any]) -> str:
    name = asset.get("name")

    if not isinstance(name, str) or not name:
        raise ValueError("Release asset is missing a valid name")

    return name


def get_asset_download_url(asset: dict[str, Any]) -> str:
    url = asset.get("browser_download_url")

    if not isinstance(url, str) or not url:
        raise ValueError(f"Asset does not have browser_download_url: {asset.get('name')}")

    return url


def find_release_asset_by_name(
    release: dict[str, Any],
    asset_name: str,
) -> dict[str, Any] | None:
    for asset in get_release_assets(release):
        if get_asset_name(asset) == asset_name:
            return asset

    return None


def require_release_asset_by_name(
    release: dict[str, Any],
    asset_name: str,
    description: str,
) -> dict[str, Any]:
    asset = find_release_asset_by_name(release, asset_name)

    if asset is None:
        release_name = release.get("name") or release.get("tag_name") or "<unknown>"
        raise ValueError(f"Could not find {description} '{asset_name}' in release {release_name}")

    return asset


def find_manifest_asset(
    release: dict[str, Any],
    *,
    manifest_name: str | None,
    zip_asset_name: str,
) -> dict[str, Any] | None:
    assets = get_release_assets(release)

    if manifest_name is not None:
        return find_release_asset_by_name(release, manifest_name)

    candidates = [
        asset
        for asset in assets
        if get_asset_name(asset).lower().endswith(".json")
        and get_asset_name(asset) != zip_asset_name
    ]

    if len(candidates) == 1:
        return candidates[0]

    if len(candidates) > 1:
        names = [get_asset_name(asset) for asset in candidates]
        raise ValueError(
            f"Found multiple JSON release assets: {names}. "
            f"Specify 'manifest_name' in plugins.yaml."
        )

    return None


def is_probable_dalamud_manifest(data: Any) -> bool:
    if not isinstance(data, dict):
        return False

    return all(key in data for key in REQUIRED_STORE_KEYS)


def read_manifest_from_zip(
    zip_bytes: bytes,
    *,
    manifest_name: str | None,
    source_description: str,
) -> dict[str, Any]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        file_names = [
            name
            for name in zf.namelist()
            if not name.endswith("/")
        ]

        if manifest_name is not None:
            candidates = [
                name
                for name in file_names
                if Path(name).name == manifest_name
            ]

            if not candidates:
                raise ValueError(
                    f"Could not find manifest '{manifest_name}' inside {source_description}"
                )

            if len(candidates) > 1:
                raise ValueError(
                    f"Found multiple files named '{manifest_name}' inside {source_description}: "
                    f"{candidates}"
                )

            manifest_path = candidates[0]

            with zf.open(manifest_path) as f:
                return json.loads(f.read().decode("utf-8"))

        json_files = [
            name
            for name in file_names
            if name.lower().endswith(".json")
        ]

        valid_manifests: list[tuple[str, dict[str, Any]]] = []

        for json_file in json_files:
            try:
                with zf.open(json_file) as f:
                    data = json.loads(f.read().decode("utf-8"))
            except Exception:
                continue

            if is_probable_dalamud_manifest(data):
                valid_manifests.append((json_file, data))

        if len(valid_manifests) == 1:
            return valid_manifests[0][1]

        if len(valid_manifests) > 1:
            names = [name for name, _ in valid_manifests]
            raise ValueError(
                f"Found multiple possible manifest JSON files inside {source_description}: "
                f"{names}. Specify 'manifest_name' in plugins.yaml."
            )

        raise ValueError(
            f"Could not find a valid Dalamud manifest JSON inside {source_description}"
        )


def validate_manifest(manifest: dict[str, Any], source: str) -> None:
    missing = [key for key in REQUIRED_STORE_KEYS if key not in manifest]

    if missing:
        raise ValueError(f"Manifest from {source} is missing required keys: {missing}")


def load_manifest(
    release: dict[str, Any],
    *,
    zip_asset: dict[str, Any],
    zip_asset_name: str,
    manifest_name: str | None,
) -> dict[str, Any]:
    manifest_asset = find_manifest_asset(
        release,
        manifest_name=manifest_name,
        zip_asset_name=zip_asset_name,
    )

    if manifest_asset is not None:
        manifest_url = get_asset_download_url(manifest_asset)
        manifest = fetch_json(manifest_url)

        if not isinstance(manifest, dict):
            raise ValueError(f"Manifest asset must be a JSON object: {manifest_url}")

        validate_manifest(manifest, manifest_url)
        print(f"  manifest: release asset {get_asset_name(manifest_asset)}")
        return manifest

    zip_url = get_asset_download_url(zip_asset)
    zip_bytes = fetch_bytes(zip_url, accept="application/zip")
    manifest = read_manifest_from_zip(
        zip_bytes,
        manifest_name=manifest_name,
        source_description=zip_asset_name,
    )

    if not isinstance(manifest, dict):
        raise ValueError(f"Manifest inside {zip_asset_name} must be a JSON object")

    validate_manifest(manifest, f"{zip_asset_name} inside release zip")
    print(f"  manifest: inside {zip_asset_name}")
    return manifest


def build_entry_from_plugin_config(plugin: dict[str, Any]) -> dict[str, Any]:
    repo_url = plugin.get("repo")
    enabled = plugin.get("enabled", True)

    if enabled is False:
        raise ValueError("Disabled plugin should have been filtered before processing")

    if not isinstance(repo_url, str) or not repo_url:
        raise ValueError("Each plugin must have a non-empty 'repo'")

    asset_name = plugin.get("asset_name", "latest.zip")
    manifest_name = plugin.get("manifest_name")

    # Backward compatibility: fail loudly if the old name is still used.
    if "manifest_asset_name" in plugin:
        raise ValueError(
            "'manifest_asset_name' has been renamed to 'manifest_name'. "
            "Please update plugins.yaml."
        )

    if not isinstance(asset_name, str) or not asset_name:
        raise ValueError(f"'asset_name' must be a non-empty string: {repo_url}")

    if manifest_name is not None and not isinstance(manifest_name, str):
        raise ValueError(f"'manifest_name' must be a string: {repo_url}")

    owner, repo = parse_github_repo_url(repo_url)
    release = get_latest_release(owner, repo)

    print(f"Processing {repo_url}")
    print(f"  release: {release.get('tag_name')}")

    zip_asset = require_release_asset_by_name(
        release,
        asset_name,
        description="plugin zip asset",
    )

    zip_url = get_asset_download_url(zip_asset)

    manifest = load_manifest(
        release,
        zip_asset=zip_asset,
        zip_asset_name=asset_name,
        manifest_name=manifest_name,
    )

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
