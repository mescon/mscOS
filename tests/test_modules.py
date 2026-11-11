"""Every module in modules/: its manifest, its class, its texts in both languages."""
import json
import os
import subprocess
import unittest

from tests import ROOT

import plugins

MODULES = os.path.join(ROOT, "modules")
KINDS = {"source", "amp", "device", "notify", "integration"}
OPTION_TYPES = {"text", "number", "select", "toggle", "secret"}


def both(d) -> bool:
    return isinstance(d, dict) and all(isinstance(d.get(k), str) and d[k].strip() for k in ("sv", "en"))


class Modules(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.found = plugins.discover()
        cls.dirs = sorted(d for d in os.listdir(MODULES) if os.path.isfile(os.path.join(MODULES, d, "__init__.py")))

    def test_every_directory_loads(self):
        self.assertEqual(sorted(self.found), self.dirs, "a module directory did not load (see the log above)")

    def test_manifest_matches_class(self):
        for mid, cls in self.found.items():
            with self.subTest(module=mid):
                with open(os.path.join(MODULES, mid, "module.json")) as f:
                    man = json.load(f)
                self.assertEqual(man["id"], mid)
                self.assertEqual(man["kind"], cls.kind)
                self.assertIn(cls.kind, KINDS)
                self.assertRegex(man["version"], r"^\d+\.\d+\.\d+$")
                self.assertEqual(cls.module_version, man["version"])
                self.assertTrue(both(man["name"]) and both(man["description"]))
                self.assertEqual(man["name"], cls.name)

    def test_texts_in_both_languages(self):
        for mid, cls in self.found.items():
            with self.subTest(module=mid):
                self.assertTrue(both(cls.name), "name")
                self.assertTrue(both(cls.description), "description")
                for o in cls.options:
                    self.assertIn(o["type"], OPTION_TYPES, o["key"])
                    self.assertTrue(both(o["label"]), f"label of {o['key']}")
                    if "help" in o:
                        self.assertTrue(both(o["help"]), f"help of {o['key']}")
                    for _, label in o.get("options", []):
                        self.assertTrue(both(label), f"a choice of {o['key']}")
                for a in cls.actions:
                    self.assertTrue(both(a["label"]), f"action {a['name']}")
                if cls.kind == "source":
                    self.assertTrue(cls.label and both(cls.hint) and both(cls.terms), "label, hint, terms")

    def test_sources_have_an_install_script(self):
        for mid, cls in self.found.items():
            if cls.kind != "source":
                continue
            with self.subTest(module=mid):
                script = os.path.join(MODULES, mid, "install.sh")
                self.assertTrue(os.path.isfile(script))
                self.assertEqual(subprocess.run(["bash", "-n", script]).returncode, 0)
                with open(script) as f:
                    self.assertIn("MSCOS_COMMON", f.read(), "must find common.sh wherever the module is installed")

    def test_catalogue_is_current(self):
        r = subprocess.run(["python3", os.path.join(ROOT, "tools", "module-index.py"), "--check"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr or r.stdout)

    def test_no_module_keeps_private_names(self):
        """Nothing of anyone's own installation in a module: no addresses on a private network."""
        import re
        for base, _, files in os.walk(MODULES):
            for n in files:
                if n.endswith((".py", ".sh", ".json", ".service")):
                    with open(os.path.join(base, n)) as f:
                        for i, line in enumerate(f, 1):
                            m = re.search(r"\b(10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+)\b", line)
                            if m and "192.168.1." not in m.group(1):  # 192.168.1.x is used in examples
                                self.fail(f"{n}:{i}: {m.group(1)}")


if __name__ == "__main__":
    unittest.main()
