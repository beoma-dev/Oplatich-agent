"""Versioned vendoring of Beoma rules. No network, third-party packages or source execution."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path, PurePosixPath

SOURCE = "https://github.com/beoma-dev/engineering"
LOCK = ".shared-standards.lock.json"
LEGACY_BUNDLES = {
    "common": {
        "docs/engineering/agent-workflow.md",
        "docs/engineering/shared-standards.md",
        ".agents/skills/beoma-review/SKILL.md",
        ".claude/skills/beoma-review/SKILL.md",
        ".cursor/rules/beoma-delivery.mdc",
        "tools/shared_standards.py",
    },
    "service": {
        "skills/beoma-service-standards/SKILL.md",
        "skills/beoma-service-standards/references/standard.md",
        "skills/beoma-service-standards/scripts/quality_gate.py",
        "skills/beoma-service-standards/scripts/branch_audit.py",
    },
}

# Schema 1 remains readable for reviewed migration/rollback. Schema 2 owns the
# expanded skills set; old installers reject its manifest before any writes.
BUNDLES = {name: set(files) for name, files in LEGACY_BUNDLES.items()}
BUNDLES["common"].add("docs/engineering/skills.md")
for skill in ("beoma-change", "beoma-db-change", "beoma-ui-check", "beoma-release"):
    for client in (".agents", ".claude"):
        BUNDLES["common"].add(f"{client}/skills/{skill}/SKILL.md")
for client in (".agents", ".claude"):
    BUNDLES["service"].add(f"{client}/skills/beoma-data-contract/SKILL.md")
LAYOUTS = {1: LEGACY_BUNDLES, 2: BUNDLES}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def encode(value):
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode()


def paths(bundles, schema=2):
    require(type(schema) is int and schema in LAYOUTS, "Unsupported layout schema")
    layout = LAYOUTS[schema]
    require(
        isinstance(bundles, list) and bundles and len(set(bundles)) == len(bundles),
        "Invalid bundles",
    )
    require("common" in bundles and set(bundles) <= set(layout), "Unknown bundles")
    return set().union(*(layout[b] for b in bundles))


def target(root, name):
    # Check each existing parent without following links, including the final file.
    require(
        name == LOCK or name in set().union(*BUNDLES.values()), "Unmanaged destination"
    )
    path = root
    for part in PurePosixPath(name).parts:
        path = path / part
        require(not path.is_symlink(), "Symlink in managed path: " + name)
    require(
        not path.exists() or path.is_file(), "Managed target is not a file: " + name
    )
    return path


def validate_lock(lock):
    require(
        isinstance(lock, dict)
        and type(lock.get("schema")) is int
        and lock["schema"] in LAYOUTS,
        "Unsupported lock schema",
    )
    require(lock.get("source") == SOURCE, "Unexpected standards source")
    require(
        re.fullmatch(r"[0-9a-f]{40}", lock.get("commit", "")),
        "Commit must be a full SHA",
    )
    require(
        re.fullmatch(r"\d+\.\d+\.\d+", lock.get("version", "")),
        "Invalid release version",
    )
    require(
        re.fullmatch(r"[A-Za-z0-9_-]+", lock.get("repository", "")),
        "Invalid repository name",
    )
    require(
        re.fullmatch(r"[0-9a-f]{64}", lock.get("manifest_sha256", "")),
        "Invalid manifest hash",
    )
    require(
        isinstance(lock.get("files"), dict)
        and set(lock["files"]) == paths(lock.get("bundles"), lock["schema"]),
        "Incomplete managed file set",
    )
    for digest in lock["files"].values():
        require(
            isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest),
            "Invalid file hash",
        )


def check(root):
    lock = json.loads(target(root, LOCK).read_bytes())
    validate_lock(lock)
    for name, digest in lock["files"].items():
        require(
            sha(target(root, name).read_bytes()) == digest, "Shared file drift: " + name
        )
    return lock


def git_blob(source, commit, name):
    path = PurePosixPath(name)
    require(
        not path.is_absolute()
        and all(p not in (".", "..") for p in path.parts)
        and "\\" not in name
        and str(path) == name,
        "Unsafe Git source path",
    )
    args = ["git", "-C", str(source)]
    entry = subprocess.check_output(
        args + ["ls-tree", commit, "--", name], stderr=subprocess.DEVNULL
    )
    require(
        entry.startswith((b"100644 blob ", b"100755 blob ")),
        "Source is not a regular Git blob: " + name,
    )
    data = subprocess.check_output(
        args + ["show", commit + ":" + name], stderr=subprocess.DEVNULL
    )
    require(len(data) <= 2_000_000, "Oversized shared file")
    return data


def release(source, commit, repository):
    require(
        re.fullmatch(r"[0-9a-f]{40}", commit),
        "Commit must be a full SHA, never a moving branch/tag",
    )
    require(
        Path(
            subprocess.check_output(
                ["git", "-C", str(source), "rev-parse", "--show-toplevel"], text=True
            ).strip()
        ).resolve()
        == source.resolve(),
        "Source must be the Git root",
    )
    raw = git_blob(source, commit, "manifest.json")
    manifest = json.loads(raw)
    require(
        type(manifest.get("schema")) is int and manifest["schema"] in LAYOUTS,
        "Unsupported manifest schema",
    )
    require(
        repository in manifest["repositories"],
        "Repository is not registered in this release",
    )
    bundles = manifest["repositories"][repository]
    selected = paths(bundles, manifest["schema"])
    layout = LAYOUTS[manifest["schema"]]
    files = {}
    contents = {}
    for name in sorted(selected):
        record = manifest["files"][name]
        require(
            record["bundle"] in bundles and name in layout[record["bundle"]],
            "Invalid bundle membership",
        )
        content = git_blob(source, commit, record["source"])
        require(sha(content) == record["sha256"], "Corrupt source manifest: " + name)
        files[name] = sha(content)
        contents[name] = content
    lock = {
        "schema": manifest["schema"],
        "source": SOURCE,
        "commit": commit,
        "version": manifest["version"],
        "repository": repository,
        "manifest_sha256": sha(raw),
        "bundles": bundles,
        "files": files,
    }
    validate_lock(lock)
    return lock, contents


def write_batch(root, contents):
    # Preflight ALL destinations before writing anything. The lock is written last.
    before = {}
    for name in contents:
        p = target(root, name)
        before[name] = p.read_bytes() if p.exists() else None
    applied = []
    with tempfile.TemporaryDirectory(prefix=".shared-standards-", dir=root) as tmp:
        scratch = Path(tmp)
        try:
            for index, (name, data) in enumerate(contents.items()):
                p = target(root, name)
                p.parent.mkdir(parents=True, exist_ok=True)
                staged = scratch / str(index)
                if data is None:
                    p.unlink()
                else:
                    staged.write_bytes(data)
                    staged.chmod(0o644)
                    os.replace(staged, p)
                applied.append(name)
        except BaseException:
            for index, name in enumerate(reversed(applied)):
                p = target(root, name)
                if before[name] is None:
                    p.unlink()
                else:
                    staged = scratch / ("rollback-" + str(index))
                    staged.write_bytes(before[name])
                    staged.chmod(0o644)
                    os.replace(staged, p)
            raise


def sync(root, source, commit, repository=None, adopt=False, migrate_layout=False):
    old = check(root) if target(root, LOCK).exists() else None
    require(not (old and adopt), "--adopt is only for the initial migration")
    if old:
        require(
            repository is None or repository == old["repository"],
            "Cannot change repository identity",
        )
        repository = old["repository"]
    require(repository is not None, "Initial sync requires --repository")
    lock, contents = release(source, commit, repository)
    require(
        old is None or old["schema"] == lock["schema"] or migrate_layout,
        "Layout change requires --migrate-layout with the reviewed new installer",
    )
    # Changing a service profile is distinct from a known schema upgrade.
    require(
        old is None or old["bundles"] == lock["bundles"],
        "Bundle changes require an explicit migration",
    )
    for name, data in contents.items():
        p = target(root, name)
        if (not old or name not in old["files"]) and p.exists():
            require(
                p.read_bytes() == data or (not old and adopt),
                "Existing file differs; review initial adoption: " + name,
            )
    if old:
        # Rollback removes only previously owned, hash-checked files. Preserve all
        # project files and refuse collisions when expanding the managed set.
        for name in sorted(set(old["files"]) - set(contents)):
            contents[name] = None
    contents[LOCK] = encode(lock)
    write_batch(root, contents)
    return check(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("check")
    p.add_argument(
        "--source-dir",
        type=Path,
        help="Also compare the receipt with its pinned canonical Git source",
    )
    p = sub.add_parser("sync")
    p.add_argument("--source-dir", type=Path, required=True)
    p.add_argument("--commit", required=True)
    p.add_argument("--repository")
    p.add_argument(
        "--adopt",
        action="store_true",
        help="Explicitly adopt existing files during first migration only",
    )
    p.add_argument(
        "--migrate-layout",
        action="store_true",
        help="Explicitly migrate between reviewed known schema layouts (including rollback)",
    )
    args = parser.parse_args()
    root = args.root.resolve()
    if args.action == "check":
        lock = check(root)
        if args.source_dir:
            canonical, _ = release(args.source_dir, lock["commit"], lock["repository"])
            require(canonical == lock, "Lock does not match pinned canonical release")
    else:
        lock = sync(
            root,
            args.source_dir,
            args.commit,
            args.repository,
            args.adopt,
            args.migrate_layout,
        )
    print(
        f"Shared standards {lock['version']} @ {lock['commit']}: {len(lock['files'])} files OK"
    )


if __name__ == "__main__":
    try:
        main()
    except (
        ValueError,
        OSError,
        KeyError,
        TypeError,
        subprocess.CalledProcessError,
    ) as error:
        raise SystemExit("Shared standards: " + str(error)) from error
