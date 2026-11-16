"""Panel ve config testleri. Gerçek veritabanı, config.yaml ve .env'ye DOKUNMAZ.

Çalıştırma: .venv/bin/python -m unittest discover -s tests -v
"""
import json
import re
import shutil
import tempfile
import unittest
from pathlib import Path

from igbot import config, dashboard, db, llm

ROOT = Path(__file__).resolve().parent.parent


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig = (db.DB_PATH, config.CONFIG_PATH, dashboard.CONFIG_PATH, dashboard.ENV_PATH, dict(config.CFG))
        cfg_copy = self.tmp / "config.yaml"
        shutil.copy(ROOT / "config.yaml", cfg_copy)
        db.DB_PATH = self.tmp / "test.sqlite3"
        config.CONFIG_PATH = dashboard.CONFIG_PATH = cfg_copy
        dashboard.ENV_PATH = self.tmp / ".env"
        dashboard.ENV_PATH.write_text("# yorum korunmalı\nOPENAI_API_KEY=sk-test-SECRETVALUE1234\nAPIFY_TOKEN=apify_xyz9876\n")
        self.app = dashboard.create_app(testing=True)
        self.client = self.app.test_client()

    def tearDown(self):
        db.DB_PATH, config.CONFIG_PATH, dashboard.CONFIG_PATH, dashboard.ENV_PATH, cfg = self._orig
        config.CFG.clear()
        config.CFG.update(cfg)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def login(self):
        r = self.client.post("/login", data={"password": "test"})
        self.assertEqual(r.status_code, 302)

    def csrf(self, path="/"):
        html = self.client.get(path).get_data(as_text=True)
        m = re.search(r'name="_csrf" value="([^"]+)"', html)
        self.assertIsNotNone(m, "CSRF jetonu sayfada yok")
        return m.group(1)


class AuthTests(Base):
    def test_pages_require_login(self):
        for path in ("/", "/content", "/dashboard/settings", "/dashboard/integrations", "/dashboard/ops"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 302, path)
            self.assertIn("/login", r.headers["Location"])

    def test_wrong_password(self):
        r = self.client.post("/login", data={"password": "yanlis"})
        self.assertEqual(r.status_code, 200)
        self.assertIn("Şifre hatalı", r.get_data(as_text=True))

    def test_post_without_csrf_rejected(self):
        self.login()
        r = self.client.post("/dashboard/inspiration/reference", data={"url": "https://x.com"})
        self.assertEqual(r.status_code, 400)

    def test_open_redirect_blocked(self):
        r = self.client.post("/login?next=//evil.com", data={"password": "test"})
        self.assertEqual(r.headers["Location"], "/")


class PageTests(Base):
    def test_all_pages_render(self):
        self.login()
        for path in ("/", "/content", "/content?status=published", "/dashboard/settings",
                     "/dashboard/schedule", "/dashboard/inspiration", "/dashboard/prompts",
                     "/dashboard/integrations", "/dashboard/ops"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)

    def test_secrets_never_rendered(self):
        self.login()
        html = self.client.get("/dashboard/integrations").get_data(as_text=True)
        self.assertNotIn("sk-test-SECRETVALUE1234", html)
        self.assertNotIn("apify_xyz9876", html)
        self.assertIn("••••1234", html)

    def test_media_rejects_paths_outside_data(self):
        self.login()
        did = db.create_draft({"title": "t", "format": "single"})
        db.update_draft(did, images_json=["/etc/passwd"])
        self.assertEqual(self.client.get(f"/media/{did}/0").status_code, 404)


class EnvWriteTests(Base):
    def test_env_update_preserves_other_lines(self):
        self.login()
        tok = self.csrf("/dashboard/integrations")
        r = self.client.post("/dashboard/integrations", data={"_csrf": tok, "APIFY_TOKEN": "apify_NEW"})
        self.assertEqual(r.status_code, 302)
        text = dashboard.ENV_PATH.read_text()
        self.assertIn("# yorum korunmalı", text)
        self.assertIn("OPENAI_API_KEY=sk-test-SECRETVALUE1234", text)
        self.assertIn("APIFY_TOKEN=apify_NEW", text)
        self.assertNotIn("PATH=", text)  # işlem ortamı .env'ye sızmamalı
        self.assertEqual(dashboard.ENV_PATH.stat().st_mode & 0o777, 0o600)

    def test_empty_fields_do_not_erase_keys(self):
        self.login()
        tok = self.csrf("/dashboard/integrations")
        self.client.post("/dashboard/integrations", data={"_csrf": tok, "OPENAI_API_KEY": ""})
        self.assertIn("OPENAI_API_KEY=sk-test-SECRETVALUE1234", dashboard.ENV_PATH.read_text())

    def test_newline_injection_rejected(self):
        with self.assertRaises(dashboard.FormError):
            dashboard.write_env_values({"APIFY_TOKEN": "x\nEVIL=1"})


class ConfigFormTests(Base):
    def test_invalid_number_shows_error_not_500(self):
        self.login()
        tok = self.csrf("/dashboard/schedule")
        before = config.CONFIG_PATH.read_text()
        r = self.client.post("/dashboard/schedule", data={"_csrf": tok, "posts_per_day": "abc", "slots": "12:30"},
                             follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        self.assertIn("tam sayı", r.get_data(as_text=True))
        self.assertEqual(before, config.CONFIG_PATH.read_text())

    def test_more_posts_than_slots_rejected(self):
        self.login()
        tok = self.csrf("/dashboard/schedule")
        r = self.client.post("/dashboard/schedule", follow_redirects=True, data={
            "_csrf": tok, "posts_per_day": "3", "slots": "12:30\n20:00", "max_queue_days": "3", "run_at": "12:00",
            "ideas_per_run": "3", "carousel_ratio": "0.7", "max_carousel_slides": "7", "portfolio_per_week": "2"})
        self.assertIn("en az 3 yayın saati", r.get_data(as_text=True))

    def test_schedule_save_updates_live_cfg(self):
        self.login()
        tok = self.csrf("/dashboard/schedule")
        r = self.client.post("/dashboard/schedule", data={
            "_csrf": tok, "posts_per_day": "1", "slots": "19:00\n09:15", "max_queue_days": "4", "run_at": "10:30",
            "ideas_per_run": "2", "carousel_ratio": "0,5", "max_carousel_slides": "6", "portfolio_per_week": "1"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(config.CFG["posting"]["slots"], ["09:15", "19:00"])
        self.assertEqual(config.CFG["ideation"]["run_at"], "10:30")
        self.assertEqual(config.CFG["ideation"]["carousel_ratio"], 0.5)
        # iç içe yapılar korunmalı
        self.assertIn("pillars", config.CFG["brand"])

    def test_manual_reference_round_trip(self):
        self.login()
        tok = self.csrf("/dashboard/inspiration")
        self.client.post("/dashboard/inspiration/reference",
                         data={"_csrf": tok, "url": "https://example.com/x", "title": "Örnek", "source": "web"})
        refs = db.list_manual_references(10)
        self.assertEqual(len(refs), 1)
        self.client.post(f"/dashboard/inspiration/reference/{refs[0]['id']}/delete", data={"_csrf": tok})
        self.assertEqual(len(db.list_manual_references(10)), 0)

    def test_reference_requires_http_url(self):
        self.login()
        tok = self.csrf("/dashboard/inspiration")
        self.client.post("/dashboard/inspiration/reference", data={"_csrf": tok, "url": "javascript:alert(1)"})
        self.assertEqual(len(db.list_manual_references(10)), 0)


class LlmConfigTests(Base):
    def test_brand_block_reads_nested_structures(self):
        block = llm.system_prompt()
        b = config.CFG["brand"]
        if isinstance(b.get("pillars"), dict):
            first = next(iter(b["pillars"].values()))
            if isinstance(first, dict) and first.get("topics"):
                self.assertIn(first["topics"][0], block)
        self.assertIn(b["name"], block)

    def test_portfolio_types_list_or_dict(self):
        config.CFG["ideation"]["portfolio_types"] = {"tasarim": ["Logo seti"], "otomasyon": ["DM botu"]}
        self.assertEqual(llm.portfolio_type_list(), ["Logo seti (tasarim)", "DM botu (otomasyon)"])
        config.CFG["ideation"]["portfolio_types"] = ["A", "B"]
        self.assertEqual(llm.portfolio_type_list(), ["A", "B"])

    def test_reload_if_changed(self):
        text = config.CONFIG_PATH.read_text(encoding="utf-8")
        import os, time
        config.CONFIG_PATH.write_text(text.replace("max_queue_days: 3", "max_queue_days: 9"), encoding="utf-8")
        os.utime(config.CONFIG_PATH, (time.time() + 5, time.time() + 5))
        self.assertTrue(config.reload_if_changed())
        self.assertEqual(config.CFG["posting"]["max_queue_days"], 9)


if __name__ == "__main__":
    unittest.main()
