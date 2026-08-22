"""Out-of-band Testnet signing credential boundary."""

from collections.abc import Mapping
from pathlib import Path
import os
import re
import stat

from ...runtime import RuntimeBoundaryError, SignerReference

_PRIVATE_KEY = re.compile(r"^(?:0x)?[0-9a-fA-F]{64}$")
_ALLOWED_ENV_KEYS = frozenset(
    {
        "HYPERLIQUID_TESTNET_PK",
        "HYPERLIQUID_PK",
        "PRIVATE_KEY",
        "API_PRIVATE_KEY",
    }
)


class LocalFileSecretProvider:
    """Resolve an opaque signer reference to a local private key at runtime.

    The returned key is intentionally confined to the adapter boundary.  This
    provider is not part of the canonical model and never exposes the value in
    its representation or in a boundary error.
    """

    def __init__(self, references: Mapping[str, Path]) -> None:
        self._references = {str(reference): Path(path) for reference, path in references.items()}

    def resolve_private_key(self, signer: SignerReference) -> str:
        """Read and validate one private key for an already-bound signer."""

        path = self._references.get(signer.reference)
        if path is None:
            raise RuntimeBoundaryError(
                "secret_reference_unknown",
                "signer reference is not registered with the secret provider",
            )
        try:
            mode = stat.S_IMODE(path.stat().st_mode)
        except OSError as exc:
            raise RuntimeBoundaryError(
                "secret_file_unavailable",
                "signing secret file cannot be inspected",
            ) from exc
        if mode & 0o077:
            raise RuntimeBoundaryError(
                "secret_file_permissions_invalid",
                "signing secret file must not be readable by group or other users",
            )
        try:
            content = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeBoundaryError(
                "secret_file_unavailable",
                "signing secret file cannot be read",
            ) from exc
        value = self._parse(content)
        if not _PRIVATE_KEY.fullmatch(value):
            raise RuntimeBoundaryError(
                "signing_key_format_invalid",
                "signing secret must be a 32-byte hexadecimal private key",
            )
        return value if value.startswith("0x") else "0x" + value

    def sign(self, signer: SignerReference, payload: bytes) -> bytes:
        """Keep the runtime protocol honest without duplicating Nautilus signing."""

        raise RuntimeBoundaryError(
            "signing_owned_by_nautilus",
            "the local file provider supplies credentials; Nautilus owns signing",
        )

    @staticmethod
    def _parse(content: str) -> str:
        lines = [line.strip() for line in content.splitlines()]
        meaningful = [line for line in lines if line and not line.startswith("#")]
        if len(meaningful) != 1:
            raise RuntimeBoundaryError(
                "signing_key_format_invalid",
                "signing secret must contain one raw value or one key assignment",
            )
        line = meaningful[0]
        if "=" not in line:
            return line.strip("\"'")
        key, value = line.split("=", 1)
        if key.strip() not in _ALLOWED_ENV_KEYS:
            raise RuntimeBoundaryError(
                "signing_key_format_invalid",
                "signing secret assignment has an unsupported key name",
            )
        return value.strip().strip("\"'")
