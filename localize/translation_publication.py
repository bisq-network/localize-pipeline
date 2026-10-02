"""Exclude skipped imports from publication and retain their diagnostic inputs."""

import argparse
import json
from pathlib import Path, PurePosixPath
import shutil
import tempfile


def plan_publication(summary_path: Path, repo: Path, input_folder: Path, candidates: list[str]) -> dict:
    """Validate explicit skip records before filtering any publication candidates."""
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    skipped = summary.get("skipped_files")
    if not isinstance(skipped, list):
        raise ValueError("Validation summary must contain an explicit skipped_files list")
    repo = repo.resolve(strict=True)
    input_folder = input_folder.resolve(strict=True)
    relative_input = input_folder.relative_to(repo)
    excluded = set()
    sources = []
    for name in skipped:
        if (
            not isinstance(name, str)
            or not name
            or "\\" in name
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
            or PurePosixPath(name).is_absolute()
            or any(part in ("", ".", "..") for part in name.split("/"))
        ):
            raise ValueError("Invalid skipped translation path")
        source = input_folder / name
        if source.resolve(strict=True) != source or not source.is_file():
            raise ValueError("Skipped translation must be a regular file without symlinks")
        excluded.add((relative_input / name).as_posix())
        sources.append((name, source))
    evidence = None
    if sources:
        evidence = Path(tempfile.mkdtemp(prefix="skipped-inputs-", dir=summary_path.parent))
        for name, source in sources:
            destination = evidence / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        shutil.copyfile(summary_path, evidence / "validation-summary.json")
    return {
        "files": [path for path in candidates if path not in excluded],
        "skipped_count": len(excluded),
        "evidence_directory": str(evidence) if evidence else None,
    }


def main() -> None:
    """Print the publication plan or fail before any Git mutation."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--summary", type=Path, required=True)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("files", nargs="*")
    args = parser.parse_args()
    print(json.dumps(plan_publication(args.summary, args.repo, args.input, args.files)))


if __name__ == "__main__":
    main()
