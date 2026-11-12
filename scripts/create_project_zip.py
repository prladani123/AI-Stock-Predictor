"""Create a source-only project export without modifying installed dependencies."""
import argparse
import os
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


EXCLUDED_DIRECTORIES = {'.venv', 'venv', 'node_modules', 'dist', 'vendor', '__pycache__',
                        '.pytest_cache', '.mypy_cache', '.ruff_cache', '.git'}


def excluded(relative):
    parts = relative.parts
    return (any(part in EXCLUDED_DIRECTORIES or part.endswith('.egg-info') for part in parts)
            or any(parts[index:index + 2] == ('var', 'cache') for index in range(len(parts) - 1))
            or relative.name == '.env' or (relative.name.startswith('.env.') and relative.name != '.env.example')
            or relative.suffix in {'.pyc', '.pyo', '.tsbuildinfo'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2],
                        help='AI Trading root; includes reference repositories without generated dependencies')
    parser.add_argument('--output', type=Path, required=True, help='New ZIP path; never overwrites an existing file')
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    if not root.is_dir() or output.suffix.lower() != '.zip':
        parser.error('A source directory and .zip output are required')
    if output.exists():
        parser.error('Output already exists; choose a new export filename')
    output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(output, mode='x', compression=ZIP_DEFLATED, compresslevel=6) as archive:
        for directory, names, filenames in os.walk(root, followlinks=False):
            parent = Path(directory)
            names[:] = sorted(name for name in names if not (parent / name).is_symlink()
                              and not excluded((parent / name).relative_to(root)))
            for name in sorted(filenames):
                path = parent / name
                relative = path.relative_to(root)
                if path.resolve() == output or path.is_symlink() or excluded(relative):
                    continue
                archive.write(path, arcname=(Path(root.name) / relative).as_posix())
    print(output)


if __name__ == '__main__':
    main()
