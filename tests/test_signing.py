"""Signatures: a device installs from the catalogue or a release only what the project signed."""
import base64
import json
import os
import shutil
import subprocess
import tempfile
import unittest

from aiohttp import web
from aiohttp.test_utils import TestServer
from aiohttp import ClientSession

from tests import ROOT

import app as panel

VERIFY = os.path.join(ROOT, "core", "mscos-verify")


def keypair(d: str, name: str) -> str:
    """A private key in d/<name>.key, its public key in d/keys/<name>.pem."""
    os.makedirs(os.path.join(d, "keys"), exist_ok=True)
    key = os.path.join(d, name + ".key")
    subprocess.run(["openssl", "genpkey", "-algorithm", "ed25519", "-out", key], check=True, capture_output=True)
    subprocess.run(["openssl", "pkey", "-in", key, "-pubout", "-out", os.path.join(d, "keys", name + ".pem")], check=True, capture_output=True)
    return key


def sign(key: str, data: bytes) -> bytes:
    with tempfile.NamedTemporaryFile() as f:
        f.write(data); f.flush()
        raw = subprocess.run(["openssl", "pkeyutl", "-sign", "-inkey", key, "-rawin", "-in", f.name], check=True, capture_output=True).stdout
    return base64.b64encode(raw) + b"\n"


@unittest.skipUnless(shutil.which("openssl"), "needs openssl")
class Signatures(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="mscos-sign-")
        self.key = keypair(self.d, "project")
        self.env = {"MSCOS_KEYS": os.path.join(self.d, "keys")}
        self._old = (panel.MSCOS_VERIFY, os.environ.get("MSCOS_KEYS"))
        panel.MSCOS_VERIFY = VERIFY
        os.environ["MSCOS_KEYS"] = self.env["MSCOS_KEYS"]

    def tearDown(self):
        panel.MSCOS_VERIFY = self._old[0]
        if self._old[1] is None:
            os.environ.pop("MSCOS_KEYS", None)
        else:
            os.environ["MSCOS_KEYS"] = self._old[1]
        os.environ.pop("MSCOS_CATALOGUE", None)
        os.environ.pop("MSCOS_ALLOW_UNSIGNED", None)
        shutil.rmtree(self.d, ignore_errors=True)

    def test_only_the_projects_key_signs(self):
        data = b'{"modules": []}\n'
        good = sign(self.key, data)
        self.assertTrue(panel.signed_by_project(data, good))
        self.assertFalse(panel.signed_by_project(data + b" ", good), "changed after signing")
        other = keypair(tempfile.mkdtemp(dir=self.d), "someone-else")
        self.assertFalse(panel.signed_by_project(data, sign(other, data)), "another key")
        for junk in (b"", b"not base64 !!", base64.b64encode(b"short"), base64.b64encode(b"\0" * 64)):
            self.assertFalse(panel.signed_by_project(data, junk), junk)

    def test_a_key_that_is_a_link_is_not_used(self):
        data = b"x"
        sig = sign(self.key, data)
        real = os.path.join(self.d, "keys", "project.pem")
        moved = os.path.join(self.d, "elsewhere.pem")
        os.rename(real, moved)
        os.symlink(moved, real)
        self.assertFalse(panel.signed_by_project(data, sig))

    def test_the_repositorys_own_keys_are_public_keys(self):
        keys = os.path.join(ROOT, "core", "keys")
        names = [n for n in os.listdir(keys) if n.endswith(".pem")]
        self.assertTrue(names, "no public key to ship")
        for n in names:
            with open(os.path.join(keys, n)) as f:
                s = f.read()
            self.assertIn("BEGIN PUBLIC KEY", s, n)
            self.assertNotIn("PRIVATE", s, n)

    async def test_the_catalogue_must_be_signed(self):
        cat = json.dumps({"serial": 7, "modules": [{"id": "demo", "version": "1.0.0", "kind": "device", "files": []}]}).encode()
        served = {"/index.json": cat}

        async def handler(request):
            if request.path not in served:
                raise web.HTTPNotFound()
            return web.Response(body=served[request.path])

        srv_app = web.Application()
        srv_app.router.add_get("/{tail:.*}", handler)
        srv = TestServer(srv_app)
        await srv.start_server()
        try:
            os.environ["MSCOS_CATALOGUE"] = str(srv.make_url("/index.json"))
            async with ClientSession() as http:
                with self.assertRaises(PermissionError):            # no signature beside it
                    await panel.fetch_catalogue(http)
                served["/index.json.sig"] = sign(keypair(tempfile.mkdtemp(dir=self.d), "thief"), cat)
                with self.assertRaises(PermissionError):            # signed, but not by the project
                    await panel.fetch_catalogue(http)
                served["/index.json.sig"] = sign(self.key, cat)
                self.assertEqual([m["id"] for m in await panel.fetch_catalogue(http)], ["demo"])
                served["/index.json"] = cat.replace(b"1.0.0", b"9.0.0")  # changed after it was signed
                with self.assertRaises(PermissionError):
                    await panel.fetch_catalogue(http)
                # an older edition, validly signed, played back: refused once a newer one was accepted
                newer = cat.replace(b'"serial": 7', b'"serial": 8')
                served["/index.json"], served["/index.json.sig"] = newer, sign(self.key, newer)
                await panel.fetch_catalogue(http)
                served["/index.json"], served["/index.json.sig"] = cat, sign(self.key, cat)
                with self.assertRaises(PermissionError) as old:
                    await panel.fetch_catalogue(http)
                self.assertEqual(str(old.exception), "older")
                noserial = b'{"modules": []}'                       # and one without a serial at all
                served["/index.json"], served["/index.json.sig"] = noserial, sign(self.key, noserial)
                with self.assertRaises(PermissionError):
                    await panel.fetch_catalogue(http)
                served["/index.json"] = cat.replace(b"1.0.0", b"9.0.0")
                os.environ["MSCOS_ALLOW_UNSIGNED"] = "1"            # the owner's own choice on their device
                self.assertEqual((await panel.fetch_catalogue(http))[0]["version"], "9.0.0")
        finally:
            await srv.close()
