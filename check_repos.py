import csv
import json
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError


REPOSITORY_URLS = [
    "https://love.puni.sh/ment.json",
    "https://puni.sh/api/repository/jukka",
    "https://puni.sh/api/repository/veyn",
    "https://puni.sh/api/repository/erdelf",
    "https://puni.sh/api/repository/ice",
    "https://puni.sh/api/repository/sourpuh",
    "https://puni.sh/api/repository/croizat",
    "https://raw.githubusercontent.com/FFXIV-CombatReborn/CombatRebornRepo/main/pluginmaster.json",
    "https://raw.githubusercontent.com/NightmareXIV/MyDalamudPlugins/main/pluginmaster.json",
    "https://raw.githubusercontent.com/Aida-Enna/XIVPlugins/main/repo.json",
    "https://raw.githubusercontent.com/Eisenhuth/TrustworthyDalamudPlugins/master/pluginmaster.json",
    "https://raw.githubusercontent.com/UnknownX7/DalamudPluginRepo/master/pluginmaster.json",
    "https://raw.githubusercontent.com/Aireil/MyDalamudPlugins/master/pluginmaster.json",
    "https://raw.githubusercontent.com/OhKannaDuh/plugins/refs/heads/master/manifest.json",
    "https://raw.githubusercontent.com/Haselnussbomber/MyDalamudPlugins/main/repo.json",
    "https://raw.githubusercontent.com/Etheirys/Brio/main/repo.json",
]


REMOVAL_CANDIDATE_REPOS = {
}


CURRENT_API_LEVEL = 15
STALE_DAYS = 365
REQUEST_INTERVAL_SECONDS = 0.5


def fetch_json(url: str):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json,text/plain,*/*",
            "User-Agent": "dalamud-repo-checker",
        },
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        status = response.status
        body = response.read().decode("utf-8-sig")

    return status, json.loads(body)


def normalize_entries(data):
    """
    Dalamud custom repo は基本的に JSON 配列。
    念のため dict 形式も軽く吸収する。
    """
    if isinstance(data, list):
        return data

    if isinstance(data, dict):
        for key in ["plugins", "Plugins", "items", "Items"]:
            value = data.get(key)
            if isinstance(value, list):
                return value

    raise ValueError("JSON is not a Dalamud plugin entry list")


def parse_last_update(value):
    if value is None or value == "":
        return None

    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None

    # 明らかにミリ秒っぽい場合
    if timestamp > 10_000_000_000:
        timestamp //= 1000

    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    except Exception:
        return None


def parse_int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_datetime_or_none(value):
    if not value:
        return None

    try:
        return datetime.fromisoformat(value)
    except Exception:
        return None


def normalize_internal_name(value):
    """
    InternalName 比較用。
    大文字小文字や前後空白の違いで別物扱いしない。
    """
    if value is None:
        return ""

    return str(value).strip().casefold()


def version_key(version: str):
    """
    AssemblyVersion のざっくり比較用。

    例:
      1.2.3.4 -> (1, 2, 3, 4)
      v1.2.3 -> (1, 2, 3)

    セマンティックバージョン以外の特殊な文字列は弱めに扱う。
    """
    if not isinstance(version, str):
        return ()

    version = version.strip().removeprefix("v")
    parts = []

    for part in version.split("."):
        try:
            parts.append(int(part))
        except ValueError:
            break

    return tuple(parts)


def plugin_sort_key(row):
    """
    同じ InternalName の中で「新しそうな候補」をざっくり判定するための並び順。
    厳密な正解ではないが、確認用としては実用的。
    """
    api = parse_int_or_none(row.get("dalamud_api_level"))
    last_update = parse_datetime_or_none(row.get("last_update_utc"))
    version = version_key(row.get("assembly_version", ""))

    return (
        api if api is not None else -1,
        last_update.timestamp() if last_update is not None else 0,
        version,
    )


def classify_repo(entries):
    if not entries:
        return "EMPTY"

    api_levels = []
    last_updates = []

    for entry in entries:
        if not isinstance(entry, dict):
            continue

        api = entry.get("DalamudApiLevel")
        try:
            api_levels.append(int(api))
        except (TypeError, ValueError):
            pass

        dt = parse_last_update(entry.get("LastUpdate"))
        if dt is not None:
            last_updates.append(dt)

    max_api = max(api_levels) if api_levels else None
    latest_update = max(last_updates) if last_updates else None

    reasons = []

    if max_api is None:
        reasons.append("no_api_level")
    elif max_api < CURRENT_API_LEVEL:
        reasons.append(f"old_api_{max_api}")

    if latest_update is None:
        reasons.append("no_last_update")
    else:
        age_days = (datetime.now(timezone.utc) - latest_update).days
        if age_days >= STALE_DAYS:
            reasons.append(f"stale_{age_days}d")

    if reasons:
        return "WARN:" + ",".join(reasons)

    return "OK"


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def write_duplicate_and_unique_reports(plugin_rows):
    by_internal_name = defaultdict(list)

    for row in plugin_rows:
        internal_name = normalize_internal_name(row.get("internal_name"))

        if not internal_name:
            continue

        by_internal_name[internal_name].append(row)

    duplicate_rows = []
    unique_rows = []

    for internal_name_key, rows in sorted(by_internal_name.items()):
        sorted_rows = sorted(rows, key=plugin_sort_key, reverse=True)
        best_row = sorted_rows[0]
        repo_count = len(rows)

        display_internal_name = best_row.get("internal_name", "")

        if repo_count >= 2:
            for row in sorted_rows:
                duplicate_rows.append({
                    "internal_name": display_internal_name,
                    "repo_count": repo_count,
                    "is_best_candidate": "yes" if row is best_row else "no",
                    "name": row.get("name", ""),
                    "author": row.get("author", ""),
                    "assembly_version": row.get("assembly_version", ""),
                    "dalamud_api_level": row.get("dalamud_api_level", ""),
                    "last_update_utc": row.get("last_update_utc", ""),
                    "repo_url": row.get("repo_url", ""),
                    "download_link": row.get("download_link", ""),
                })
        else:
            row = rows[0]
            unique_rows.append({
                "internal_name": row.get("internal_name", ""),
                "name": row.get("name", ""),
                "author": row.get("author", ""),
                "assembly_version": row.get("assembly_version", ""),
                "dalamud_api_level": row.get("dalamud_api_level", ""),
                "last_update_utc": row.get("last_update_utc", ""),
                "repo_url": row.get("repo_url", ""),
                "download_link": row.get("download_link", ""),
            })

    write_csv(
        "duplicate_plugins.csv",
        [
            "internal_name",
            "repo_count",
            "is_best_candidate",
            "name",
            "author",
            "assembly_version",
            "dalamud_api_level",
            "last_update_utc",
            "repo_url",
            "download_link",
        ],
        duplicate_rows,
    )

    write_csv(
        "unique_plugins.csv",
        [
            "internal_name",
            "name",
            "author",
            "assembly_version",
            "dalamud_api_level",
            "last_update_utc",
            "repo_url",
            "download_link",
        ],
        unique_rows,
    )

    print("- duplicate_plugins.csv")
    print("- unique_plugins.csv")


def write_removal_impact_report(plugin_rows):
    """
    削除候補 repo を消したときに、どのプラグインが失われる可能性があるかを見る。

    判定単位は InternalName。
    バージョン違いでも InternalName が同じなら、基本的には代替ありとみなす。
    ただし、同名 fork や古い版を固定利用しているケースは別途確認する。
    """
    by_internal_name = defaultdict(list)

    for row in plugin_rows:
        internal_name = normalize_internal_name(row.get("internal_name"))

        if not internal_name:
            continue

        by_internal_name[internal_name].append(row)

    impact_rows = []

    for internal_name_key, rows in sorted(by_internal_name.items()):
        rows_in_removal_candidates = [
            row for row in rows
            if row.get("repo_url") in REMOVAL_CANDIDATE_REPOS
        ]

        if not rows_in_removal_candidates:
            continue

        rows_outside_removal_candidates = [
            row for row in rows
            if row.get("repo_url") not in REMOVAL_CANDIDATE_REPOS
        ]

        if rows_outside_removal_candidates:
            impact = "SAFE_DUPLICATED_OUTSIDE"
        else:
            impact = "RISK_ONLY_IN_REMOVAL_CANDIDATES"

        all_repo_urls = sorted({row.get("repo_url", "") for row in rows})
        candidate_repo_urls = sorted({
            row.get("repo_url", "")
            for row in rows_in_removal_candidates
        })
        outside_repo_urls = sorted({
            row.get("repo_url", "")
            for row in rows_outside_removal_candidates
        })

        all_versions = sorted({
            str(row.get("assembly_version", "")).strip()
            for row in rows
            if str(row.get("assembly_version", "")).strip()
        })

        candidate_versions = sorted({
            str(row.get("assembly_version", "")).strip()
            for row in rows_in_removal_candidates
            if str(row.get("assembly_version", "")).strip()
        })

        outside_versions = sorted({
            str(row.get("assembly_version", "")).strip()
            for row in rows_outside_removal_candidates
            if str(row.get("assembly_version", "")).strip()
        })

        all_api_levels = sorted({
            str(row.get("dalamud_api_level", "")).strip()
            for row in rows
            if str(row.get("dalamud_api_level", "")).strip()
        })

        outside_api_levels = sorted({
            str(row.get("dalamud_api_level", "")).strip()
            for row in rows_outside_removal_candidates
            if str(row.get("dalamud_api_level", "")).strip()
        })

        best_overall = sorted(rows, key=plugin_sort_key, reverse=True)[0]
        best_outside = (
            sorted(rows_outside_removal_candidates, key=plugin_sort_key, reverse=True)[0]
            if rows_outside_removal_candidates
            else None
        )

        display_internal_name = best_overall.get("internal_name", "")

        for row in rows_in_removal_candidates:
            impact_rows.append({
                "impact": impact,
                "internal_name": display_internal_name,
                "name": row.get("name", ""),
                "author": row.get("author", ""),
                "candidate_version": row.get("assembly_version", ""),
                "candidate_api_level": row.get("dalamud_api_level", ""),
                "candidate_last_update_utc": row.get("last_update_utc", ""),
                "removal_candidate_repo": row.get("repo_url", ""),
                "also_available_outside_removal_candidates": (
                    "yes" if rows_outside_removal_candidates else "no"
                ),
                "outside_repo_count": len(outside_repo_urls),
                "outside_repos": " | ".join(outside_repo_urls),
                "outside_versions": " | ".join(outside_versions),
                "outside_api_levels": " | ".join(outside_api_levels),
                "best_outside_version": (
                    best_outside.get("assembly_version", "") if best_outside else ""
                ),
                "best_outside_api_level": (
                    best_outside.get("dalamud_api_level", "") if best_outside else ""
                ),
                "best_outside_repo": (
                    best_outside.get("repo_url", "") if best_outside else ""
                ),
                "all_repo_count": len(all_repo_urls),
                "all_repos": " | ".join(all_repo_urls),
                "all_versions": " | ".join(all_versions),
                "all_api_levels": " | ".join(all_api_levels),
                "download_link": row.get("download_link", ""),
            })

    def impact_sort_key(row):
        impact_order = {
            "RISK_ONLY_IN_REMOVAL_CANDIDATES": 0,
            "SAFE_DUPLICATED_OUTSIDE": 1,
        }

        return (
            impact_order.get(row.get("impact", ""), 99),
            str(row.get("internal_name", "")).casefold(),
            str(row.get("removal_candidate_repo", "")).casefold(),
        )

    impact_rows = sorted(impact_rows, key=impact_sort_key)

    write_csv(
        "removal_impact.csv",
        [
            "impact",
            "internal_name",
            "name",
            "author",
            "candidate_version",
            "candidate_api_level",
            "candidate_last_update_utc",
            "removal_candidate_repo",
            "also_available_outside_removal_candidates",
            "outside_repo_count",
            "outside_repos",
            "outside_versions",
            "outside_api_levels",
            "best_outside_version",
            "best_outside_api_level",
            "best_outside_repo",
            "all_repo_count",
            "all_repos",
            "all_versions",
            "all_api_levels",
            "download_link",
        ],
        impact_rows,
    )

    print("- removal_impact.csv")


def main():
    summary_rows = []
    plugin_rows = []

    for url in REPOSITORY_URLS:
        print(f"Checking: {url}")

        try:
            status, data = fetch_json(url)
            entries = normalize_entries(data)
            repo_status = classify_repo(entries)

            api_levels = []
            last_updates = []

            for entry in entries:
                if not isinstance(entry, dict):
                    continue

                internal_name = entry.get("InternalName", "")
                name = entry.get("Name", "")
                author = entry.get("Author", "")
                api = entry.get("DalamudApiLevel", "")
                version = entry.get("AssemblyVersion", "")
                download = (
                    entry.get("DownloadLinkInstall")
                    or entry.get("DownloadLinkUpdate")
                    or entry.get("DownloadLinkTesting")
                    or ""
                )
                last_update = parse_last_update(entry.get("LastUpdate"))

                if api != "":
                    try:
                        api_levels.append(int(api))
                    except (TypeError, ValueError):
                        pass

                if last_update is not None:
                    last_updates.append(last_update)

                plugin_rows.append({
                    "repo_url": url,
                    "internal_name": internal_name,
                    "normalized_internal_name": normalize_internal_name(internal_name),
                    "name": name,
                    "author": author,
                    "assembly_version": version,
                    "dalamud_api_level": api,
                    "last_update_utc": last_update.isoformat() if last_update else "",
                    "download_link": download,
                })

            summary_rows.append({
                "repo_url": url,
                "fetch_status": status,
                "repo_status": repo_status,
                "plugin_count": len(entries),
                "max_api_level": max(api_levels) if api_levels else "",
                "latest_update_utc": max(last_updates).isoformat() if last_updates else "",
            })

            time.sleep(REQUEST_INTERVAL_SECONDS)

        except HTTPError as exc:
            summary_rows.append({
                "repo_url": url,
                "fetch_status": f"HTTP {exc.code}",
                "repo_status": "BROKEN",
                "plugin_count": 0,
                "max_api_level": "",
                "latest_update_utc": "",
            })
        except URLError as exc:
            summary_rows.append({
                "repo_url": url,
                "fetch_status": f"URL error: {exc.reason}",
                "repo_status": "BROKEN",
                "plugin_count": 0,
                "max_api_level": "",
                "latest_update_utc": "",
            })
        except Exception as exc:
            summary_rows.append({
                "repo_url": url,
                "fetch_status": "error",
                "repo_status": f"BROKEN: {exc}",
                "plugin_count": 0,
                "max_api_level": "",
                "latest_update_utc": "",
            })

    write_csv(
        "repo_summary.csv",
        [
            "repo_url",
            "fetch_status",
            "repo_status",
            "plugin_count",
            "max_api_level",
            "latest_update_utc",
        ],
        summary_rows,
    )

    write_csv(
        "plugin_details.csv",
        [
            "repo_url",
            "internal_name",
            "normalized_internal_name",
            "name",
            "author",
            "assembly_version",
            "dalamud_api_level",
            "last_update_utc",
            "download_link",
        ],
        plugin_rows,
    )

    print()
    print("Generated:")
    print("- repo_summary.csv")
    print("- plugin_details.csv")

    write_duplicate_and_unique_reports(plugin_rows)
    write_removal_impact_report(plugin_rows)

    print()
    print("Summary:")
    for row in summary_rows:
        print(
            f"{row['repo_status']:35} "
            f"count={row['plugin_count']:3} "
            f"api={row['max_api_level']} "
            f"{row['repo_url']}"
        )


if __name__ == "__main__":
    main()
