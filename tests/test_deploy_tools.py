import importlib.util
import io
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gen = load("generate_compose", "scripts/generate_compose.py")
cf = load("cloudflare_sync", "scripts/cloudflare_sync.py")

MANIFEST = """SERVICE=myapp-api
DOCKERFILE=api/Dockerfile
PORT=3001
PUBLIC=true
HEALTHCHECK=true
REQUIRED_ENV=DATABASE_URL,API_SECRET
"""

SERVER_ENV = """PUBLIC_HOSTNAME=api.example.com
CORS_EXTRA_ORIGINS=https://front.example.com
DATABASE_URL=postgresql://postgres:pw123@db:5432/myapp
API_SECRET=sekret123
"""


class GenerateComposeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.repo = root / "repos" / "myapp"
        self.repo.mkdir(parents=True)
        self.env_dir = root / "env"
        self.env_dir.mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, manifest=MANIFEST, server_env=SERVER_ENV):
        (self.repo / "homeserver.env").write_text(manifest, encoding="utf-8")
        if server_env is not None:
            (self.env_dir / "myapp.env").write_text(server_env, encoding="utf-8")
        return gen.build_app(self.repo, self.env_dir)

    def test_happy_path_derives_web_url_and_cors(self):
        app, errors, _ = self.build()
        self.assertEqual(errors, [])
        self.assertEqual(app["web_url"], "https://api.example.com")
        self.assertEqual(
            app["cors_origins"], "https://api.example.com,https://front.example.com"
        )

    def test_rendered_compose_uses_only_per_app_env(self):
        app, _, _ = self.build()
        out = gen.render_compose([app])
        self.assertIn("myapp-api:", out)
        self.assertIn("- env/myapp.env", out)
        self.assertNotIn("- .env\n", out)

    def test_rendered_compose_does_not_inject_database_url(self):
        # DATABASE_URL comes only from env/<app>.env via env_file.
        app, _, _ = self.build()
        out = gen.render_compose([app])
        self.assertNotIn("DATABASE_URL:", out)
        self.assertNotIn("depends_on", out)

    def test_db_name_manifest_field_rejected(self):
        app, errors, _ = self.build(manifest=MANIFEST + "DB_NAME=myapp\n")
        self.assertIsNone(app)
        self.assertTrue(any("DB_NAME" in e for e in errors))

    def test_empty_database_url_is_error_when_required(self):
        env = SERVER_ENV.replace(
            "DATABASE_URL=postgresql://postgres:pw123@db:5432/myapp", "DATABASE_URL="
        )
        app, errors, _ = self.build(server_env=env)
        self.assertIsNone(app)
        self.assertTrue(any("DATABASE_URL" in e for e in errors))

    def test_missing_server_env_creates_template_and_fails(self):
        app, errors, created = self.build(server_env=None)
        self.assertTrue(created)
        self.assertIsNone(app)
        self.assertTrue((self.env_dir / "myapp.env").is_file())
        self.assertTrue(any("template" in e for e in errors))
        template = (self.env_dir / "myapp.env").read_text(encoding="utf-8")
        self.assertIn("DATABASE_URL=", template)

    def test_empty_required_env_is_error(self):
        app, errors, _ = self.build(server_env="PUBLIC_HOSTNAME=api.example.com\nDATABASE_URL=postgresql://x\nAPI_SECRET=\n")
        self.assertIsNone(app)
        self.assertTrue(any("API_SECRET" in e for e in errors))

    def test_missing_manifest_is_error(self):
        app, errors, _ = gen.build_app(self.repo, self.env_dir)
        self.assertIsNone(app)
        self.assertTrue(any("no homeserver.env" in e for e in errors))

    def test_fallback_manifest_used_when_repo_has_none(self):
        manifests = Path(self.tmp.name) / "manifests"
        manifests.mkdir()
        (manifests / "myapp.homeserver.env").write_text(MANIFEST, encoding="utf-8")
        (self.env_dir / "myapp.env").write_text(SERVER_ENV, encoding="utf-8")
        app, errors, _ = gen.build_app(self.repo, self.env_dir, manifests)
        self.assertEqual(errors, [])
        self.assertEqual(app["service"], "myapp-api")

    def test_repo_manifest_wins_over_fallback(self):
        manifests = Path(self.tmp.name) / "manifests"
        manifests.mkdir()
        (manifests / "myapp.homeserver.env").write_text(
            MANIFEST.replace("myapp-api", "fallback-api"), encoding="utf-8"
        )
        app, errors, _ = self.build()
        app2, errors2, _ = gen.build_app(self.repo, self.env_dir, manifests)
        self.assertEqual(errors2, [])
        self.assertEqual(app2["service"], "myapp-api")

    def test_shipped_example_manifest_is_valid(self):
        values, errors = gen.parse_env_file(ROOT / "examples" / "example-app.homeserver.env")
        self.assertEqual(errors, [])
        self.assertEqual(values["SERVICE"], "example-api")
        self.assertNotIn("DB_NAME", values)
        required = [k for k in values["REQUIRED_ENV"].split(",") if k]
        self.assertIn("DATABASE_URL", required)

    def test_shipped_compose_example_is_template_only(self):
        text = (ROOT / "docker-compose.example.yml").read_text(encoding="utf-8")
        self.assertIn("cloudflared:", text)
        self.assertIn("# db:", text)
        self.assertIn("#   pgdata:", text)
        gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn("\ndocker-compose.yml\n", "\n" + gitignore)

    def test_unknown_manifest_key_rejected(self):
        app, errors, _ = self.build(manifest=MANIFEST + "EVIL=1\n")
        self.assertTrue(any("EVIL" in e for e in errors))

    def test_shell_metachars_in_value_rejected(self):
        app, errors, _ = self.build(manifest=MANIFEST.replace("3001", "3001;rm"))
        self.assertIsNone(app)
        self.assertTrue(errors)

    def test_json_array_origins_allowed(self):
        for value in (
            '\'["https://a.example.com","https://b.example.com"]\'',
            '["https://a.example.com","https://b.example.com"]',
        ):
            with tempfile.NamedTemporaryFile(
                "w", suffix=".env", delete=False, encoding="utf-8"
            ) as f:
                f.write(f"ALLOWED_ORIGINS={value}\n")
                name = f.name
            values, errors = gen.parse_env_file(Path(name))
            self.assertEqual(errors, [])
            self.assertEqual(values["ALLOWED_ORIGINS"], value)

    def test_json_array_with_shell_metachars_rejected(self):
        with tempfile.NamedTemporaryFile(
            "w", suffix=".env", delete=False, encoding="utf-8"
        ) as f:
            f.write('ALLOWED_ORIGINS=\'["https://a.example.com;rm"]\'\n')
            name = f.name
        _, errors = gen.parse_env_file(Path(name))
        self.assertTrue(errors)

    def test_explicit_cors_wins_over_derived(self):
        app, errors, _ = self.build(
            server_env=SERVER_ENV + "CORS_ORIGINS=https://only.example.com\n"
        )
        self.assertEqual(errors, [])
        self.assertEqual(app["cors_origins"], "https://only.example.com")

    def test_main_end_to_end_writes_compose_and_tsv(self):
        self.build()
        root = Path(self.tmp.name)
        self.assertEqual(gen.main(["--root", str(root)]), 0)
        compose = (root / "docker-compose.override.yml").read_text(encoding="utf-8")
        self.assertIn("myapp-api:", compose)
        self.assertIn('WEB_URL: "https://api.example.com"', compose)
        tsv = (root / ".deploy" / "apps.tsv").read_text(encoding="utf-8")
        self.assertIn("myapp\tmyapp-api\t3001\ttrue\tapi.example.com", tsv)


class CloudflareIngressTest(unittest.TestCase):
    def test_merge_preserves_foreign_and_single_catchall(self):
        existing = [
            {"hostname": "old.example.com", "service": "http://old:1"},
            {"hostname": "api.example.com", "service": "http://stary:1"},
            {"service": "http_status:404"},
        ]
        desired = [{"hostname": "api.example.com", "service": "http://myapp-api:3001"}]
        merged = cf.merge_ingress(existing, desired)
        hosts = [e.get("hostname") for e in merged]
        self.assertIn("old.example.com", hosts)
        self.assertEqual(hosts.count("api.example.com"), 1)
        self.assertEqual(merged[-1], {"service": "http_status:404"})
        self.assertEqual(
            len([e for e in merged if e.get("service") == "http_status:404"]), 1
        )
        svc = [e for e in merged if e.get("hostname") == "api.example.com"][0]
        self.assertEqual(svc["service"], "http://myapp-api:3001")

    def test_load_apps_uses_new_tsv_format(self):
        with tempfile.TemporaryDirectory() as td:
            tsv = Path(td) / "apps.tsv"
            tsv.write_text(
                "repo\tservice\tport\thealthcheck\thostname\tcf_zone_id\n"
                "myapp\tmyapp-api\t3001\ttrue\tapi.example.com\tzone1\n"
                "priv\tpriv-api\t3002\tfalse\t\t\n",
                encoding="utf-8",
            )
            rows = cf.load_apps(tsv)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["service"], "http://myapp-api:3001")
        self.assertEqual(rows[0]["zone_id"], "zone1")

    def test_zone_404_becomes_readable_error_not_traceback(self):
        body = b'{"success":false,"errors":[{"code":7003,"message":"zone not found"}]}'
        err = urllib.error.HTTPError(
            "https://api.cloudflare.com/client/v4/zones/z1/dns_records",
            404, "Not Found", hdrs=None, fp=io.BytesIO(body),
        )
        api = cf.CfApi("token")
        with mock.patch.object(cf.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(RuntimeError) as ctx:
                api.call("GET", "/zones/z1/dns_records?type=CNAME&name=api.example.com")
        self.assertIn("HTTP 404", str(ctx.exception))
        self.assertIn("CF_ZONE_ID", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
