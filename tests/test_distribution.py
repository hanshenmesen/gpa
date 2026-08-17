import tempfile
import unittest
import zipfile
from pathlib import Path

from scripts.verify_distribution import REQUIRED_MEMBERS, verify_wheel


class DistributionTests(unittest.TestCase):
    def test_runtime_frontend_assets_are_required(self):
        self.assertIn("demo_web/case_lab.html", REQUIRED_MEMBERS)
        self.assertIn("demo_web/control.html", REQUIRED_MEMBERS)
        self.assertIn("demo_web/community.html", REQUIRED_MEMBERS)
        self.assertIn("demo_web/environment.js", REQUIRED_MEMBERS)
        self.assertIn("gpa/replay/gate.py", REQUIRED_MEMBERS)
        self.assertIn("gpa/replay/client_lease.py", REQUIRED_MEMBERS)
        self.assertIn("gpa/replay/request.py", REQUIRED_MEMBERS)
        self.assertIn("gpa/replay/worker_protocol.py", REQUIRED_MEMBERS)
        self.assertIn("gpa/execution/decision_policy.py", REQUIRED_MEMBERS)
        self.assertIn("gpa/desktop/app.py", REQUIRED_MEMBERS)
        self.assertIn("gpa/cloud_server/migrations/0001_initial.sql", REQUIRED_MEMBERS)
        self.assertIn(
            "gpa/cloud_server/migrations/0002_security_and_operations.sql",
            REQUIRED_MEMBERS,
        )
        self.assertIn("gpa/cloud_server/auth.py", REQUIRED_MEMBERS)
        self.assertIn("demo_web/product.css", REQUIRED_MEMBERS)
        self.assertIn("demo_web/product.js", REQUIRED_MEMBERS)

    def test_verify_wheel_rejects_missing_frontend_assets(self):
        with tempfile.TemporaryDirectory() as temporary:
            wheel = Path(temporary) / "gpa-0.1.0-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                for member in REQUIRED_MEMBERS - {"demo_web/product.css"}:
                    archive.writestr(member, "fixture")
                archive.writestr(
                    "gpa-0.1.0.dist-info/entry_points.txt",
                    "[console_scripts]\n"
                    "gpa = gpa.integration.cli:main\n"
                    "gpa-cloud = gpa.cloud_server.app:main\n"
                    "gpa-desktop = gpa.desktop.app:main\n"
                    "gpa-web = demo_web.server:main\n",
                )

            with self.assertRaisesRegex(ValueError, "product.css"):
                verify_wheel(wheel)

    def test_primary_pages_share_complete_navigation_icons(self):
        root = Path(__file__).resolve().parents[1] / "demo_web"
        for filename in ("index.html", "store.html", "community.html", "setup.html", "control.html"):
            page = (root / filename).read_text(encoding="utf-8")
            with self.subTest(filename=filename):
                self.assertIn('href="/community" data-icon="community"', page)
                self.assertIn('href="/setup" data-icon="setup"', page)

    def test_shared_styles_define_new_navigation_icons_and_light_hero_override(self):
        css = (
            Path(__file__).resolve().parents[1]
            / "demo_web"
            / "product.css"
        ).read_text(encoding="utf-8")
        self.assertIn('a[data-icon="community"]::before', css)
        self.assertIn('a[data-icon="setup"]::before', css)
        self.assertIn('main:not(.store-page):not(.control-page):not(.studio-layout)', css)

    def test_setup_distinguishes_session_and_startup_desktop_access(self):
        page = (
            Path(__file__).resolve().parents[1] / "demo_web" / "setup.html"
        ).read_text(encoding="utf-8")
        self.assertIn("本次会话授权", page)
        self.assertIn("下次启动时提醒授权", page)
        self.assertIn("每次启动仍需你在本机确认", page)
        self.assertNotIn("下次启动将自动申请", page)
        self.assertIn("startup_default_enabled", page)

    def test_community_is_a_user_facing_replay_hub(self):
        page = (
            Path(__file__).resolve().parents[1] / "demo_web" / "community.html"
        ).read_text(encoding="utf-8")
        self.assertIn("发现真实工作流", page)
        self.assertIn("发布或导入", page)
        self.assertIn("回传复现结果", page)


if __name__ == "__main__":
    unittest.main()
