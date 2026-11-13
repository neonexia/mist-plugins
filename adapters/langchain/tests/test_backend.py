import pytest

from mist_langchain import SKILLS_DIR, SKILLS_ROUTE, MistBackend, deep_agent_backend


@pytest.fixture
def backend(s3):
    return MistBackend("agent-files", "workspaces/7d/run-1/", s3=s3)


def test_write_lands_under_prefix_and_reads_back(backend, s3):
    assert backend.write("/notes/a.md", "one\ntwo\nthree\n").error is None
    body = s3.get_object(Bucket="agent-files", Key="workspaces/7d/run-1/notes/a.md")["Body"].read()
    assert body == b"one\ntwo\nthree\n"
    r = backend.read("/notes/a.md", offset=1, limit=1)
    assert r.error is None and r.file_data["content"] == "two\n" and r.start_line == 2


def test_read_missing(backend):
    assert "not found" in backend.read("/nope.txt").error


def test_ls_lists_files_and_dirs(backend):
    backend.write("/a.txt", "a")
    backend.write("/sub/b.txt", "b")
    entries = backend.ls("/").entries
    assert [(e["path"], e["is_dir"]) for e in entries] == [("/a.txt", False), ("/sub/", True)]
    assert [e["path"] for e in backend.ls("/sub").entries] == ["/sub/b.txt"]


def test_edit(backend):
    backend.write("/f.py", "x = 1\ny = 1\nz = 0\n")
    assert backend.edit("/f.py", "z = 0", "z = 2").occurrences == 1
    assert "z = 2" in backend.read("/f.py").file_data["content"]
    assert backend.edit("/f.py", "= 1", "= 3").error  # not unique
    assert backend.edit("/f.py", "= 1", "= 3", replace_all=True).occurrences == 2


def test_grep_and_glob(backend):
    backend.write("/src/app.py", "import os\n# TODO fix\n")
    backend.write("/src/lib/util.py", "def f():\n    pass  # TODO\n")
    backend.write("/README.md", "TODO docs\n")
    matches = backend.grep("TODO", "/src", "*.py").matches
    assert sorted((m["path"], m["line"]) for m in matches) == [("/src/app.py", 2), ("/src/lib/util.py", 2)]
    assert sorted(m["path"] for m in backend.glob("*.py").matches) == ["/src/app.py", "/src/lib/util.py"]
    assert [m["path"] for m in backend.glob("**/*.md", "/").matches] == ["/README.md"]


def test_delete_is_recursive_and_stays_in_prefix(backend, s3):
    s3.put_object(Bucket="agent-files", Key="other/keep.txt", Body=b"x")
    backend.write("/d/a.txt", "a")
    backend.write("/d/e/b.txt", "b")
    backend.write("/dd.txt", "c")
    assert backend.delete("/d").error is None
    keys = sorted(o["Key"] for o in s3.list_objects_v2(Bucket="agent-files")["Contents"])
    assert keys == ["other/keep.txt", "workspaces/7d/run-1/dd.txt"]
    assert backend.delete("/d").error


def test_upload_download_binary(backend):
    data = bytes(range(256))
    assert backend.upload_files([("/bin/x.bin", data)])[0].error is None
    out = backend.download_files(["/bin/x.bin", "/missing"])
    assert out[0].content == data and out[1].error == "file_not_found"


def test_rejects_parent_paths(backend):
    assert backend.write("/../escape.txt", "x").error
    assert backend.download_files(["/a/../../b"])[0].error == "invalid_path"


def test_deep_agent_backend_serves_skills_and_files(s3):
    assert SKILLS_DIR.is_dir(), "run scripts/sync-skills.sh"
    backend = deep_agent_backend("agent-files", "run/", s3=s3)
    assert "/skills/mist/mist-getting-started/" in [e["path"] for e in backend.ls(SKILLS_ROUTE).entries]
    skill = backend.download_files(["/skills/mist/mist-getting-started/SKILL.md"])[0].content
    assert skill.startswith(b"---\nname: mist-getting-started")
    backend.write("/out.txt", "hello")
    assert s3.get_object(Bucket="agent-files", Key="run/out.txt")["Body"].read() == b"hello"
