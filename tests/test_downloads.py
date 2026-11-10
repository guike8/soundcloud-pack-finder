import hashlib
import io
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import patch

from packfinder import downloads


ZIP_BYTES = b"PK\x03\x04" + b"example archive payload" * 80


class FakeResponse:
    def __init__(self, body=b"", status=200, headers=None):
        self.status = status
        self.headers = {key.lower(): value for key, value in (headers or {}).items()}
        self.body = io.BytesIO(body)
        self.closed = False

    def getheader(self, key):
        return self.headers.get(key.lower())

    def read(self, size):
        return self.body.read(size)

    def close(self):
        self.closed = True


class FakeConnection:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.destination = Path(self.directory.name)
        self.resolve = patch.object(downloads, "_resolve_public_addresses", return_value=("93.184.216.34",))
        self.resolve_mock = self.resolve.start()
        self.addCleanup(self.resolve.stop)

    def perform(self, response, **kwargs):
        connection = FakeConnection()
        with patch.object(downloads, "_open_response", return_value=(response, connection)):
            result = downloads.download_url("https://files.example/pack.zip", self.destination, **kwargs)
        self.assertTrue(response.closed)
        self.assertTrue(connection.closed)
        return result

    def test_download_hash_and_no_overwrite(self):
        (self.destination / "pack.zip").write_bytes(b"keep me")
        result = self.perform(FakeResponse(ZIP_BYTES, headers={"Content-Length": str(len(ZIP_BYTES))}))
        self.assertEqual((self.destination / "pack.zip").read_bytes(), b"keep me")
        self.assertEqual(Path(result["path"]).read_bytes(), ZIP_BYTES)
        self.assertEqual(result["filename"], "pack-1.zip")
        self.assertEqual(result["sha256"], hashlib.sha256(ZIP_BYTES).hexdigest())
        self.assertEqual(result["bytes"], len(ZIP_BYTES))
        self.assertFalse(list(self.destination.glob("*.part")))
        self.assertEqual(Path(result["path"]).stat().st_mode & 0o111, 0)

    def test_content_disposition_cannot_escape_destination(self):
        result = self.perform(FakeResponse(ZIP_BYTES, headers={"Content-Disposition": 'attachment; filename="../../outside.zip"'}))
        self.assertEqual(Path(result["path"]).parent, self.destination.resolve())
        self.assertEqual(result["filename"], "outside.zip")

    def test_size_limit_without_content_length_cleans_partial(self):
        with self.assertRaisesRegex(downloads.DownloadError, "size limit"):
            self.perform(FakeResponse(ZIP_BYTES), max_bytes=1200)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_content_length_limit_prevents_file_creation(self):
        with self.assertRaisesRegex(downloads.DownloadError, "size limit"):
            self.perform(FakeResponse(ZIP_BYTES, headers={"Content-Length": "100000"}), max_bytes=100)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_hash_mismatch_cleans_partial(self):
        with self.assertRaisesRegex(downloads.DownloadError, "SHA-256"):
            self.perform(FakeResponse(ZIP_BYTES), sha256_expected="0" * 64)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_webpage_and_mislabeled_executable_rejected(self):
        for body in (b"<!doctype html><html>sign in</html>", b"MZ" + b"x" * 100, b"not an archive"):
            with self.subTest(body=body):
                with self.assertRaises(downloads.DownloadError):
                    self.perform(FakeResponse(body))
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_scripts_and_insecure_urls_rejected_before_network(self):
        for url in ("http://files.example/pack.zip", "https://files.example/install.sh", "https://user:pass@files.example/pack.zip", "https://files.example:8443/pack.zip"):
            with self.subTest(url=url), patch.object(downloads, "_open_response") as network:
                with self.assertRaises(downloads.DownloadError):
                    downloads.download_url(url, self.destination)
                network.assert_not_called()

    def test_redirect_rechecks_destination_addresses(self):
        first = FakeResponse(status=302, headers={"Location": "https://private.example/pack.zip"})
        connection = FakeConnection()
        self.resolve_mock.side_effect = [("93.184.216.34",), downloads.DownloadError("non-public address")]
        with patch.object(downloads, "_open_response", return_value=(first, connection)) as network:
            with self.assertRaisesRegex(downloads.DownloadError, "non-public"):
                downloads.download_url("https://files.example/pack.zip", self.destination)
        self.assertEqual(network.call_count, 1)
        self.assertEqual(self.resolve_mock.call_args_list[-1].args, ("private.example",))
        self.assertTrue(first.closed)
        self.assertTrue(connection.closed)

    def test_incomplete_file_cleans_partial(self):
        with self.assertRaisesRegex(downloads.DownloadError, "incomplete"):
            self.perform(FakeResponse(ZIP_BYTES, headers={"Content-Length": str(len(ZIP_BYTES) + 1)}))
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_successful_redirect_keeps_original_and_final_urls(self):
        first = FakeResponse(status=302, headers={"Location": "https://cdn.example/artifact"})
        final = FakeResponse(ZIP_BYTES, headers={"Content-Disposition": "attachment; filename*=UTF-8''sound%20pack.zip"})
        with patch.object(downloads, "_open_response", side_effect=[(first, FakeConnection()), (final, FakeConnection())]):
            result = downloads.download_url("https://files.example/pack.zip", self.destination)
        self.assertEqual(result["source_url"], "https://files.example/pack.zip")
        self.assertEqual(result["final_url"], "https://cdn.example/artifact")
        self.assertEqual(result["filename"], "sound pack.zip")

    def test_local_telegram_artifact_uses_same_validation(self):
        temporary = self.destination / "telegram.part"
        temporary.write_bytes(ZIP_BYTES)
        result = downloads.validate_download_file(temporary, filename="pack.zip")
        self.assertEqual(result["sha256"], hashlib.sha256(ZIP_BYTES).hexdigest())
        self.assertEqual(result["bytes"], len(ZIP_BYTES))
        temporary.write_bytes(b"MZ" + b"x" * 32)
        with self.assertRaisesRegex(downloads.DownloadError, "executable"):
            downloads.validate_download_file(temporary, filename="pack.zip")
        self.assertTrue(temporary.exists())

    def test_supported_filename_disallows_scripts_and_paths(self):
        for filename in ("install.sh", "plugin.dll", "../../pack.zip", "..\\pack.zip", "pack\x00.zip", "plugin.vst3"):
            self.assertFalse(downloads.is_supported_filename(filename))
        for filename in ("Pack.zip", "sound.wav", "preset.vstpreset", "plugin.exe", "plugin.dmg", "plugin.msi", "plugin.pkg", "preset.fst", "preset.nksf", "preset.nkm", "device.adv", "device.amxd"):
            self.assertTrue(downloads.is_supported_filename(filename))

    def test_installers_saved_inert_with_expected_signature(self):
        formats = {
            "plugin.exe": b"MZ" + b"x" * 2048,
            "plugin.msi": b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"x" * 2048,
            "plugin.pkg": b"xar!" + b"x" * 2048,
            "plugin.dmg": b"x" * 2048 + b"koly" + b"\x00" * 508,
        }
        for filename, body in formats.items():
            with self.subTest(filename=filename):
                response = FakeResponse(body, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
                result = self.perform(response)
                saved = Path(result["path"])
                self.assertEqual(saved.read_bytes(), body)
                self.assertTrue(result["installer"])
                self.assertEqual(saved.stat().st_mode & 0o777, 0o600)

    def test_bad_installer_signature_and_dmg_trailer_rejected(self):
        for filename in ("plugin.exe", "plugin.msi", "plugin.pkg", "plugin.dmg"):
            with self.subTest(filename=filename):
                response = FakeResponse(b"x" * 2048, headers={"Content-Disposition": f'attachment; filename="{filename}"'})
                with self.assertRaisesRegex(downloads.DownloadError, "extension"):
                    self.perform(response)
        self.assertEqual(list(self.destination.iterdir()), [])

    def test_local_dmg_requires_valid_trailer(self):
        path = self.destination / "plugin.dmg"
        path.write_bytes(b"x" * 2048)
        with self.assertRaisesRegex(downloads.DownloadError, "UDIF"):
            downloads.validate_download_file(path)
        path.write_bytes(b"x" * 2048 + b"koly" + b"\x00" * 508)
        self.assertTrue(downloads.validate_download_file(path)["installer"])


class AddressValidationTests(unittest.TestCase):
    def test_rejects_local_reserved_and_metadata_addresses(self):
        for address in ("127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.0.1", "169.254.169.254", "0.0.0.0", "::1", "fe80::1", "fc00::1", "::ffff:127.0.0.1", "224.0.0.1"):
            with self.subTest(address=address):
                self.assertFalse(downloads._is_public_address(address))
        self.assertTrue(downloads._is_public_address("8.8.8.8"))
        self.assertTrue(downloads._is_public_address("2606:4700:4700::1111"))

    def test_mixed_public_and_private_dns_answers_rejected(self):
        answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 443)) for address in ("8.8.8.8", "127.0.0.1")]
        with patch.object(downloads.socket, "getaddrinfo", return_value=answers):
            with self.assertRaisesRegex(downloads.DownloadError, "non-public"):
                downloads._resolve_public_addresses("files.example")


if __name__ == "__main__":
    unittest.main()
