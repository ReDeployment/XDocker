import importlib.util
from pathlib import Path
import plistlib
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("frp_launchd", ROOT / "scripts/frp_client_launchd.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class ClientAgentTests(unittest.TestCase):
    def test_agent_uses_private_config_path_without_embedding_credentials(self):
        root = Path("/tmp/my workspace & test")
        config = module.descriptor(root, Path("/tmp/frpc"))
        encoded = plistlib.dumps(config)
        decoded = plistlib.loads(encoded)
        self.assertEqual(decoded["ProgramArguments"], ["/tmp/frpc", "-c", str(root / "data/frp/frpc.ini")])
        self.assertEqual(decoded["WorkingDirectory"], str(root))
        self.assertTrue(decoded["KeepAlive"])
        self.assertNotIn("EnvironmentVariables", decoded)
        self.assertNotIn("token", encoded.decode().lower())

    def test_other_checkout_gets_a_different_agent_identity(self):
        first = module.descriptor(Path("/tmp/first"), Path("/tmp/frpc"))
        second = module.descriptor(Path("/tmp/second"), Path("/tmp/frpc"))
        self.assertNotEqual(first["Label"], second["Label"])
