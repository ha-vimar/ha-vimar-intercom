"""away_config.save_upload on its own: the copy into the messages folder that both
the options upload and the card's upload view go through.

It must never write through a link or over a different file, and an upload of a
file that is already there must not pile up -2, -3... copies."""
from __future__ import annotations

import os
import sys

import pytest

from custom_components.vimar_intercom import away_config


def _src(tmp_path, name="msg.mp3", data=b"ID3 voice"):
    up = tmp_path / "upload"
    up.mkdir(exist_ok=True)
    path = up / name
    path.write_bytes(data)
    return path


def test_the_same_file_uploaded_again_is_not_copied_twice(tmp_path):
    folder = tmp_path / "messaggi"
    src = _src(tmp_path)
    first = away_config.save_upload(src, str(folder))
    assert away_config.save_upload(src, str(folder)) == first
    assert sorted(os.listdir(folder)) == ["msg.mp3"]


def test_different_files_with_the_same_name_get_numbered_names(tmp_path):
    folder = tmp_path / "messaggi"
    paths = [away_config.save_upload(_src(tmp_path, data=bytes([n]) * 4), str(folder)) for n in range(3)]
    assert [os.path.basename(p) for p in paths] == ["msg.mp3", "msg-2.mp3", "msg-3.mp3"]
    assert [open(p, "rb").read() for p in paths] == [bytes([n]) * 4 for n in range(3)]


# Windows answers O_EXCL on an existing folder with EACCES, not EEXIST; Home
# Assistant does not run on Windows natively, so only the POSIX behaviour matters.
@pytest.mark.skipif(sys.platform == "win32", reason="O_EXCL on a folder is EACCES on Windows")
def test_a_folder_with_the_target_name_is_skipped_not_compared(tmp_path):
    folder = tmp_path / "messaggi"
    (folder / "msg.mp3").mkdir(parents=True)
    dest = away_config.save_upload(_src(tmp_path), str(folder))
    assert os.path.basename(dest) == "msg-2.mp3"
    assert (folder / "msg.mp3").is_dir()


def test_a_dangling_link_with_the_target_name_is_never_written_through(tmp_path):
    folder = tmp_path / "messaggi"
    folder.mkdir()
    outside = tmp_path / "outside.mp3"
    try:
        os.symlink(outside, folder / "msg.mp3")
    except (OSError, NotImplementedError) as e:  # Windows without the symlink privilege
        pytest.skip(f"symlinks not available: {e}")
    dest = away_config.save_upload(_src(tmp_path), str(folder))
    assert os.path.basename(dest) == "msg-2.mp3"
    assert not outside.exists(), "the link target must not be created"


def test_the_name_given_by_the_card_wins_over_the_source_name(tmp_path):
    folder = tmp_path / "messaggi"
    dest = away_config.save_upload(_src(tmp_path, name="tmpXYZ"), str(folder), name="Benvenuto (1).wav")
    assert dest == os.path.join(str(folder), "Benvenuto (1).wav")


def test_the_missing_folder_is_created(tmp_path):
    folder = tmp_path / "media" / "citofono" / "messaggi"
    dest = away_config.save_upload(_src(tmp_path), str(folder))
    assert os.path.isfile(dest)


def test_without_a_folder_the_upload_fails(tmp_path):
    with pytest.raises(ValueError, match="upload_failed"):
        away_config.save_upload(_src(tmp_path), None)


def test_an_empty_file_is_not_a_message(tmp_path):
    with pytest.raises(ValueError, match="upload_bad_type"):
        away_config.save_upload(_src(tmp_path, data=b""), str(tmp_path / "messaggi"))
    assert not (tmp_path / "messaggi").exists()


def test_a_file_over_the_cap_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(away_config, "UPLOAD_MAX", 4)
    with pytest.raises(ValueError, match="upload_too_big"):
        away_config.save_upload(_src(tmp_path, data=b"12345"), str(tmp_path / "messaggi"))


@pytest.mark.parametrize("name", [".hidden.mp3", "voice.ogg", "a/../b.txt", "x" * 197 + ".mp3", "è;rm.mp3"])
def test_names_that_are_not_plain_audio_files_are_refused(tmp_path, name):
    with pytest.raises(ValueError, match="upload_bad_type"):
        away_config.save_upload(_src(tmp_path), str(tmp_path / "messaggi"), name=name)


def test_a_path_in_the_name_keeps_only_the_last_piece(tmp_path):
    folder = tmp_path / "messaggi"
    dest = away_config.save_upload(_src(tmp_path), str(folder), name="..\\..\\evil.mp3")
    assert dest == os.path.join(str(folder), "evil.mp3")
