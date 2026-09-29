"""Installer auth contract: sandbox actual shell script, never touch live service."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[1]


class InstallerAuthTest(unittest.TestCase):
    def test_fresh_and_repeat_install_password_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bin_dir = root / "bin"
            bin_dir.mkdir()
            for name in ("apt-get", "hermes", "loginctl", "systemctl", "lsof"):
                stub = bin_dir / name
                stub.write_text("#!/bin/sh\nexit 0\n")
                stub.chmod(0o755)
            installer = (REPO / "install.sh").read_text()
            installer = installer.replace('INSTALL_DIR="/opt/AppData/hermes-native/hermes-data/scripts"', f'INSTALL_DIR="{root}/scripts"')
            installer = installer.replace('SECONDARY_DIR="/DATA/AppData/hermes-native/hermes-data/scripts"', f'SECONDARY_DIR="{root}/secondary"')
            installer = installer.replace('SERVICE_FILE="/etc/systemd/system/hermes-panel.service"', f'SERVICE_FILE="{root}/hermes-panel.service"')
            installer = installer.replace('ENV_FILE="/etc/hermes-panel.env"', f'ENV_FILE="{root}/hermes-panel.env"')
            installer = installer.replace('if [ -d "/DATA" ]; then', 'if false; then')
            script = root / "install.sh"
            script.write_text(installer)
            (root / "dashboard-toggle-server.py").write_text("print('fixture')\n")
            (root / "hermes-panel.env").write_text("PANEL_PORT=9120\nPANEL_TOKEN=old-token\nROUTER_HOST=192.0.2.9\n")
            env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}", PANEL_PASSWORD="fixture Pass42!'quote", PANEL_TOKEN="legacy-token")
            for iteration in range(2):
                if iteration:
                    env.pop("PANEL_PASSWORD")
                result = subprocess.run(["bash", str(script)], env=env, cwd=root, input="", text=True, capture_output=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
                content = (root / "hermes-panel.env").read_text()
                self.assertIn('PANEL_PASSWORD="fixture Pass42!\'quote"', content)
                self.assertNotIn("PANEL_TOKEN", content)
                self.assertIn("ROUTER_HOST=192.0.2.9", content)
                self.assertEqual((root / "hermes-panel.env").stat().st_mode & 0o777, 0o600)
                unit = (root / "hermes-panel.service").read_text()
                self.assertIn(f"EnvironmentFile={root}/hermes-panel.env", unit)
                self.assertNotIn("PANEL_TOKEN", unit)
                self.assertNotIn("fixture Pass42!'quote", unit)
                self.assertNotIn("?token=", result.stdout)
                self.assertNotIn("fixture Pass42!'quote", result.stdout)

    def test_noninteractive_without_password_refuses_before_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            installer = (REPO / "install.sh").read_text().replace('ENV_FILE="/etc/hermes-panel.env"', f'ENV_FILE="{root}/hermes-panel.env"')
            script = root / "install.sh"
            script.write_text(installer)
            env = {k: v for k, v in os.environ.items() if k not in ("PANEL_PASSWORD", "PANEL_TOKEN")}
            result = subprocess.run(["bash", str(script)], env=env, input="", text=True, capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PANEL_PASSWORD", result.stderr)
            self.assertFalse((root / "hermes-panel.env").exists())


if __name__ == "__main__":
    unittest.main()
