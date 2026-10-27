import os

import pytest

from reencoder import paths, replace


def write(p, data=b"x"):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def test_stale_temp_cleanup(media):
    t1 = write(media / "movies" / ".Movie.reencode.tmp.mkv")
    t2 = write(media / "tvshows" / "Show" / ".S01E01.reencode.tmp.mp4")
    keep = write(media / "movies" / "Movie.mkv")
    other = write(media / "movies" / ".hidden.mkv")
    removed = replace.clean_stale_temps([str(media)])
    assert sorted(removed) == sorted([str(t1), str(t2)])
    assert keep.exists() and other.exists() and not t1.exists() and not t2.exists()


def test_stale_cleanup_ignores_folders_outside_roots(media, tmp_path):
    outside = write(tmp_path / "elsewhere" / ".x.reencode.tmp.mkv")
    assert replace.clean_stale_temps([str(tmp_path / "elsewhere")]) == []
    assert outside.exists()


def test_temp_path():
    assert paths.temp_path("/data/media/movies/A.mkv") == "/data/media/movies/.A.reencode.tmp.mkv"


def test_safe_path_rejects_traversal(media):
    with pytest.raises(paths.PathError):
        paths.safe_path(str(media / ".." / ".." / "etc"))
    with pytest.raises(paths.PathError):
        paths.safe_path("/etc/passwd")
    assert paths.safe_path(str(media / "movies")) == str(media / "movies")


def test_safe_path_rejects_symlink_escape(media, tmp_path):
    (tmp_path / "secret").mkdir()
    os.symlink(tmp_path / "secret", media / "link")
    with pytest.raises(paths.PathError):
        paths.safe_path(str(media / "link"))


def test_replace_same_name_deletes_original(media, s):
    orig = write(media / "movies" / "A.mkv", b"old" * 100)
    tmp = write(media / "movies" / ".A.reencode.tmp.mkv", b"new")
    replace.replace(str(orig), str(tmp), str(orig), s)
    assert orig.read_bytes() == b"new" and not tmp.exists()
    assert not (media / ".reencoder-trash").exists()


def test_replace_extension_change(media, s):
    orig = write(media / "movies" / "A.avi", b"old")
    final = media / "movies" / "A.mkv"
    tmp = write(media / "movies" / ".A.reencode.tmp.mkv", b"new")
    replace.replace(str(orig), str(tmp), str(final), s)
    assert final.read_bytes() == b"new" and not orig.exists() and not tmp.exists()


def test_replace_refuses_to_overwrite_other_file(media, s):
    orig = write(media / "movies" / "A.avi", b"old")
    final = write(media / "movies" / "A.mkv", b"someone else")
    tmp = write(media / "movies" / ".A.reencode.tmp.mkv", b"new")
    with pytest.raises(FileExistsError):
        replace.replace(str(orig), str(tmp), str(final), s)
    assert orig.read_bytes() == b"old" and final.read_bytes() == b"someone else"


def test_replace_keeps_original_in_trash(media, s):
    s["trash_days"] = 7
    orig = write(media / "tvshows" / "Show" / "E1.mkv", b"old")
    tmp = write(media / "tvshows" / "Show" / ".E1.reencode.tmp.mkv", b"new")
    replace.replace(str(orig), str(tmp), str(orig), s)
    trashed = list((media / ".reencoder-trash").rglob("E1.mkv"))
    assert len(trashed) == 1 and trashed[0].read_bytes() == b"old"
    assert orig.read_bytes() == b"new"


def test_purge_trash(media):
    old = write(media / ".reencoder-trash" / "2000-01-01" / "movies" / "A.mkv")
    import datetime
    new = write(media / ".reencoder-trash" / datetime.date.today().isoformat() / "B.mkv")
    replace.purge_trash(3)
    assert not old.exists() and new.exists()
