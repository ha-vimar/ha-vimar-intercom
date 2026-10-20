"""Remove only the mounted development state from a one-shot offline container."""
import shutil
from pathlib import Path


def reset(state: Path) -> None:
    if not state.is_mount():
        raise ValueError("Reset requires the development state mount.")
    for child in state.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    print("Development state cleared.")


if __name__ == "__main__":
    reset(Path("/state"))
