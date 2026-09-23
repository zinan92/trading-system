import os
import hashlib
from pathlib import Path
import stat
import tempfile
import unittest

from standard_broker.models import SignerKind
from standard_broker.runtime import RuntimeBoundaryError, SignerProvider, SignerReference
from standard_broker.adapters.hyperliquid.credentials import LocalFileSecretProvider


class LocalFileSecretProviderTests(unittest.TestCase):
    @staticmethod
    def synthetic_key(label: str) -> str:
        return hashlib.sha256(label.encode("utf-8")).hexdigest()

    def signer(self) -> SignerReference:
        return SignerReference(
            SignerKind.API_AGENT,
            "file",
            "file-secret://hyperliquid-testnet",
        )

    def write_secret(self, directory: str, content: str, mode: int = 0o600) -> Path:
        path = Path(directory) / "testnet-key"
        path.write_text(content, encoding="utf-8")
        os.chmod(path, mode)
        return path

    def test_resolves_raw_private_key_without_exposing_it_in_reference(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key = "0x" + self.synthetic_key("raw-private-key-fixture")
            path = self.write_secret(directory, key)
            provider = LocalFileSecretProvider({self.signer().reference: path})

            self.assertEqual(provider.resolve_private_key(self.signer()), key)
            self.assertNotIn(key, repr(self.signer()))
            self.assertIsInstance(provider, SignerProvider)

    def test_resolves_testnet_env_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key = self.synthetic_key("dotenv-private-key-fixture")
            path = self.write_secret(directory, f"HYPERLIQUID_TESTNET_PK={key}\n")
            provider = LocalFileSecretProvider({self.signer().reference: path})

            self.assertEqual(provider.resolve_private_key(self.signer()), "0x" + key)

    def test_rejects_public_wallet_address_as_signing_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_secret(directory, "0x" + "12" * 20)
            provider = LocalFileSecretProvider({self.signer().reference: path})

            with self.assertRaises(RuntimeBoundaryError) as raised:
                provider.resolve_private_key(self.signer())

            self.assertEqual(raised.exception.reason_code, "signing_key_format_invalid")
            self.assertNotIn("12" * 20, str(raised.exception))

    def test_rejects_secret_file_readable_by_group_or_other(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_secret(directory, self.synthetic_key("permission-fixture"), mode=0o644)
            provider = LocalFileSecretProvider({self.signer().reference: path})

            with self.assertRaises(RuntimeBoundaryError) as raised:
                provider.resolve_private_key(self.signer())

            self.assertEqual(raised.exception.reason_code, "secret_file_permissions_invalid")

    def test_rejects_unregistered_reference_before_reading_a_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self.write_secret(directory, "01" * 32)
            provider = LocalFileSecretProvider({"file-secret://other": path})

            with self.assertRaises(RuntimeBoundaryError) as raised:
                provider.resolve_private_key(self.signer())

            self.assertEqual(raised.exception.reason_code, "secret_reference_unknown")


if __name__ == "__main__":
    unittest.main()
