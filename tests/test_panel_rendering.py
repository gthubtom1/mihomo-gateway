import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INJECT_HTML = ROOT / "panel" / "inject.html"


class PanelProviderRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = INJECT_HTML.read_text(encoding="utf-8")
        cls.socks_renderer = cls.source.split(
            "  async function loadSocks(showOk){", 1
        )[1].split("  async function loadProviders(){", 1)[0]
        cls.provider_renderer = cls.source.split(
            "  async function loadProviders(){", 1
        )[1].split("  async function createSocks(){", 1)[0]

    def test_socks_rows_and_groups_use_dom_text_rendering(self):
        self.assertNotIn("innerHTML", self.socks_renderer)
        self.assertNotRegex(self.socks_renderer, re.compile(r"\$\{\s*[sg]\."))
        self.assertIn("document.createElement('option')", self.socks_renderer)
        self.assertIn("document.createElement('tr')", self.socks_renderer)
        self.assertIn("textContent", self.socks_renderer)

    def test_independent_egress_controls_and_status_are_rendered(self):
        self.assertIn('id="mx-migrate"', self.source)
        self.assertIn("/panel-api/socks/migrate", self.source)
        self.assertIn("s.primary || '-'", self.socks_renderer)
        self.assertIn("s.mode || 'legacy'", self.socks_renderer)

    def test_socks_route_survives_metacubexd_hash_normalization(self):
        self.assertIn("const SOCKS_ROUTE = '#/overview?gateway=socks5'", self.source)
        self.assertIn("function isSocksHash()", self.source)
        self.assertIn("location.hash === '#/socks5'", self.source)
        self.assertIn("a.setAttribute('href', SOCKS_ROUTE)", self.source)
        self.assertIn("history.replaceState(null, '', SOCKS_ROUTE)", self.source)
        self.assertNotIn("location.hash === '#socks5') showSocks()", self.source)

    def test_provider_rows_use_dom_text_rendering_without_raw_urls(self):
        self.assertNotIn("innerHTML", self.provider_renderer)
        self.assertNotRegex(self.provider_renderer, re.compile(r"\$\{\s*p\."))
        self.assertNotIn("p.url", self.provider_renderer)
        self.assertIn("document.createElement('tr')", self.provider_renderer)
        self.assertIn("textContent", self.provider_renderer)
        self.assertRegex(
            self.provider_renderer,
            re.compile(r"p\.display_url\s*\|\|\s*p\.path"),
        )

    def test_provider_delete_uses_encoded_api_id_instead_of_name(self):
        self.assertNotIn("data-name", self.provider_renderer)
        self.assertNotIn("/panel-api/providers?name=", self.source)
        self.assertRegex(
            self.provider_renderer,
            re.compile(r"delProvider\(p\.id(?:,\s*p\.name)?\)"),
        )
        self.assertRegex(
            self.source,
            re.compile(
                r"/panel-api/providers\?id='\s*\+\s*encodeURIComponent\(id\)"
            ),
        )

    def test_yaml_file_import_uses_raw_body_and_encoded_name_header(self):
        self.assertIn('id="mx-prov-file"', self.source)
        self.assertIn('id="mx-yaml-name"', self.source)
        self.assertIn('id="mx-yaml-status"', self.source)
        self.assertIn("/panel-api/providers/yaml", self.source)
        self.assertIn("file.arrayBuffer()", self.source)
        self.assertIn("'X-Provider-Name': encodeURIComponent(name)", self.source)
        self.assertIn("defaultYamlName(file.name)", self.source)
        self.assertIn("document.getElementById('mx-yaml-name').value.trim()", self.source)


if __name__ == "__main__":
    unittest.main()
