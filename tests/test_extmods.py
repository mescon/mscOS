"""Modules installed beside mscOS: archives and catalogue files are checked before anything runs."""
import hashlib
import io
import os
import tarfile
import tempfile
import unittest

import tests  # noqa: F401 - sets the paths

import extmods


def tar_of(build) -> bytes:
    b = io.BytesIO()
    with tarfile.open(fileobj=b, mode="w:gz") as t:
        build(t)
    return b.getvalue()


def add(t, name, data=b"x = 1\n", **kw):
    i = tarfile.TarInfo(name)
    i.size = len(data)
    for k, v in kw.items():
        setattr(i, k, v)
    t.addfile(i, io.BytesIO(data) if i.isfile() else None)


class Archives(unittest.TestCase):
    def setUp(self):
        self.dest = tempfile.mkdtemp()

    def unpack(self, data, sha=None, mid="demo"):
        return extmods.unpack(data, sha or hashlib.sha256(data).hexdigest(), mid, "https://example.org/module.json", "1.0.0", self.dest)

    def test_good_archive_in_one_directory(self):
        data = tar_of(lambda t: (add(t, "demo-1.0/__init__.py"), add(t, "demo-1.0/driver.py")))
        p = self.unpack(data)
        self.assertEqual(sorted(os.listdir(p)), ["__init__.py", "driver.py", "module.json"])
        self.assertEqual(extmods.manifest_of(p)["source"], "https://example.org/module.json")

    def test_wrong_checksum(self):
        data = tar_of(lambda t: add(t, "__init__.py"))
        with self.assertRaises(ValueError):
            self.unpack(data, sha="0" * 64)

    def test_nothing_is_written_outside(self):
        data = tar_of(lambda t: (add(t, "__init__.py"), add(t, "../../evil.py")))
        with self.assertRaises(ValueError):
            self.unpack(data)
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.dest), "evil.py")))
        self.assertEqual(os.listdir(self.dest), [])

    def test_links_are_refused(self):
        data = tar_of(lambda t: (add(t, "__init__.py"), add(t, "key", b"", type=tarfile.SYMTYPE, linkname="/etc/shadow")))
        with self.assertRaises(ValueError):
            self.unpack(data)

    def test_package_without_init(self):
        with self.assertRaises(ValueError):
            self.unpack(tar_of(lambda t: add(t, "readme.txt")))

    def test_update_replaces_and_failure_keeps_the_old(self):
        self.unpack(tar_of(lambda t: add(t, "__init__.py", b"v = 1\n")))
        with self.assertRaises(ValueError):
            self.unpack(tar_of(lambda t: add(t, "readme.txt")))
        with open(os.path.join(self.dest, "demo", "__init__.py")) as f:
            self.assertEqual(f.read(), "v = 1\n")
        self.unpack(tar_of(lambda t: add(t, "__init__.py", b"v = 2\n")))
        with open(os.path.join(self.dest, "demo", "__init__.py")) as f:
            self.assertEqual(f.read(), "v = 2\n")

    def test_catalogue_files(self):
        p = extmods.install_files("demo", "1.2.0", "catalogue", [("__init__.py", b"x = 1\n", False), ("install.sh", b"#!/bin/sh\n", True),
                                                                 ("sub/unit.service", b"[Unit]\n", False)], self.dest)
        self.assertTrue(os.access(os.path.join(p, "install.sh"), os.X_OK))
        self.assertEqual(extmods.manifest_of(p), {"id": "demo", "version": "1.2.0", "source": "catalogue"})
        for bad in ("../x.py", "/etc/x", "a/../../x"):
            with self.assertRaises(ValueError):
                extmods.install_files("demo2", "1", "catalogue", [("__init__.py", b"", False), (bad, b"", False)], self.dest)

    def test_manifest(self):
        ok = {"id": "demo", "version": "1.0", "archive": "https://a/b.tgz", "sha256": "a" * 64}
        self.assertEqual(extmods.check_manifest(ok, remote=True)["id"], "demo")
        for bad in ({**ok, "id": "Demo"}, {**ok, "id": "../x"}, {**ok, "archive": "http://a/b.tgz"}, {**ok, "sha256": "zz"},
                    {**ok, "version": "1 0"}, {"id": "demo", "version": "1"}):
            with self.assertRaises(ValueError):
                extmods.check_manifest(bad, remote=True)

    def test_a_directory_others_can_write_is_not_safe(self):
        d = os.path.join(self.dest, "m")
        os.makedirs(d)
        open(os.path.join(d, "__init__.py"), "w").close()
        if os.geteuid() == 0:
            self.assertTrue(extmods.safe_dir(d))
            os.chmod(os.path.join(d, "__init__.py"), 0o666)
        self.assertFalse(extmods.safe_dir(d))


if __name__ == "__main__":
    unittest.main()
