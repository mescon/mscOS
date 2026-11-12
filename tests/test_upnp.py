"""UPnP renderer: the settings it writes and how it reads what an app sent (no MPD here)."""
import importlib.util
import os
import sys
import unittest

from tests import ROOT

spec = importlib.util.spec_from_file_location("mscos_test_upnp", os.path.join(ROOT, "modules", "upnp", "__init__.py"))
upnp = importlib.util.module_from_spec(spec)
sys.modules["mscos_test_upnp"] = upnp
spec.loader.exec_module(upnp)

DIDL = ('<DIDL-Lite xmlns="urn:schemas-upnp-org:metadata-1-0/DIDL-Lite/" xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:upnp="urn:schemas-upnp-org:metadata-1-0/upnp/"><item id="1" parentID="0" restricted="1">'
        '<dc:title>A &amp; B</dc:title><dc:creator>Someone</dc:creator><upnp:artist>The Artist</upnp:artist>'
        '<upnp:album>The Album</upnp:album><upnp:albumArtURI>https://example.org/c.jpg</upnp:albumArtURI>'
        '<res protocolInfo="http-get:*:audio/flac:*">https://example.org/t.flac</res></item></DIDL-Lite>')


class Upnp(unittest.TestCase):
    def test_what_the_app_sent(self):
        self.assertEqual(upnp.didl(DIDL), {"title": "A & B", "artist": "Someone", "album": "The Album", "image": "https://example.org/c.jpg"})

    def test_metadata_from_the_network_is_not_trusted(self):
        self.assertEqual(upnp.didl("not xml"), {})
        self.assertEqual(upnp.didl('<!DOCTYPE d [<!ENTITY x SYSTEM "file:///etc/passwd">]>' + DIDL.replace("A &amp; B", "&x;")), {}, "no document type, no entities")
        self.assertEqual(upnp.didl("<a>" + "x" * 70000 + "</a>"), {}, "too long")
        odd = DIDL.replace("https://example.org/c.jpg", "javascript:alert(1)")
        self.assertNotIn("image", upnp.didl(odd), "a sleeve address is http(s) or nothing")

    def test_mpd_passes_the_samples_untouched(self):
        conf = upnp.mpd_conf("Audio")
        for line in ('device "hw:CARD=Audio,DEV=0"', 'mixer_type "none"', 'auto_resample "no"', 'auto_format "no"',
                     'auto_channels "no"', 'replaygain "off"', 'volume_normalization "no"', 'bind_to_address "127.0.0.1"', 'user "mpd"'):
            self.assertIn(line, conf)

    def test_the_name_on_the_network(self):
        conf = upnp.upmpdcli_conf("Living  room\nNº1")
        self.assertIn("friendlyname = Living room Nº1\n", conf, "one line, whatever the name holds")
        self.assertIn("openhome = 1", conf)
