"""Tests de los parseos delicados de FireDeck (solo biblioteca estándar).

Ejecutar desde la carpeta del proyecto:
    python -m unittest discover -s tests -v
"""
from __future__ import annotations

import hashlib
import io
import shutil
import struct
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))

import apkbuild as b  # noqa: E402
import app  # noqa: E402


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="fd_tests_"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
class StringPoolTests(unittest.TestCase):
    TEXTS = ["", "android", "Película ñ ✓", "x" * 200, "y" * 40000]

    def test_utf16_roundtrip(self):
        data = b.string_pool(self.TEXTS, utf8=False)
        self.assertEqual(app._parse_string_pool(data, 0), self.TEXTS)

    def test_utf8_roundtrip_including_two_byte_lengths(self):
        texts = self.TEXTS[:4]  # 200 caracteres obliga a longitud de 2 bytes
        data = b.string_pool(texts, utf8=True)
        self.assertEqual(app._parse_string_pool(data, 0), texts)

    def test_pool_at_nonzero_offset(self):
        data = b"\xAA" * 12 + b.string_pool(["a", "b"])
        self.assertEqual(app._parse_string_pool(data, 12), ["a", "b"])


class BinaryXmlTests(unittest.TestCase):
    def test_elements_and_attributes(self):
        data = b.manifest_xml("com.demo.app", version_code=42, version_name="2.0", min_sdk=21)
        events = app._parse_binary_xml(data)
        starts = [e for e in events if not e.get("end")]
        self.assertEqual([e["name"] for e in starts], ["manifest", "uses-sdk", "application"])
        self.assertEqual([e["name"] for e in events if e.get("end")],
                         ["uses-sdk", "application", "manifest"])
        manifest = starts[0]
        self.assertEqual(app._manifest_attr_text(manifest, "package"), "com.demo.app")
        self.assertEqual(app._manifest_attr_text(manifest, "versionName"), "2.0")
        self.assertEqual(app._manifest_attr_int(manifest, "versionCode"), 42)
        self.assertEqual(app._manifest_attr_int(starts[1], "minSdkVersion"), 21)
        self.assertEqual(app._manifest_attr_text(manifest, "inexistente"), "")
        self.assertIsNone(app._manifest_attr_int(manifest, "inexistente"))

    def test_utf8_string_pool_manifest(self):
        data = b.axml([("start", "manifest", [("package", "str", "com.ñandú.app")]),
                       ("end", "manifest")], utf8=True)
        events = app._parse_binary_xml(data)
        self.assertEqual(app._manifest_attr_text(events[0], "package"), "com.ñandú.app")

    def test_garbage_is_not_xml(self):
        self.assertEqual(app._parse_binary_xml(b""), [])
        self.assertEqual(app._parse_binary_xml(b"hola"), [])
        self.assertEqual(app._parse_binary_xml(b"<?xml version='1.0'?><manifest/>"), [])

    def test_truncated_data_never_crashes_with_unexpected_errors(self):
        data = b.manifest_xml("com.demo.app", min_sdk=21)
        for cut in range(0, len(data), 7):
            try:
                app._parse_binary_xml(data[:cut])
            except (struct.error, IndexError):
                pass  # apk_manifest_info los convierte en AdbError

    def test_manifest_icon_reference_and_string(self):
        ref = app._manifest_icon_id(b.manifest_xml("com.a", icon_ref=0x7F020000))
        self.assertEqual(ref, 0x7F020000)
        as_string = b.axml([("start", "application", [("icon", "str", "res/icon.png")]),
                            ("end", "application")])
        self.assertEqual(app._manifest_icon_id(as_string), "res/icon.png")
        self.assertIsNone(app._manifest_icon_id(b.manifest_xml("com.a")))


# ---------------------------------------------------------------------------
class ResourceTableTests(unittest.TestCase):
    STRINGS = ["", "res/mipmap-hdpi/ic.png", "res/mipmap-xxhdpi/ic.png"]

    def table(self, types):
        return app._ResourceTable(b.arsc(self.STRINGS, 0x7F, types))

    def test_resolves_string_and_prefers_highest_density(self):
        t = self.table([
            {"type_id": 2, "density": 240, "entries": {0: (b.TYPE_STRING, 1)}},
            {"type_id": 2, "density": 480, "entries": {0: (b.TYPE_STRING, 2)}},
        ])
        self.assertEqual(t.resolve(0x7F020000), "res/mipmap-xxhdpi/ic.png")

    def test_follows_references_without_looping_forever(self):
        t = self.table([
            {"type_id": 2, "entries": {0: (b.TYPE_STRING, 1)}},
            {"type_id": 3, "entries": {0: (b.TYPE_REFERENCE, 0x7F020000)}},
            {"type_id": 4, "entries": {0: (b.TYPE_REFERENCE, 0x7F040000)}},  # se apunta a sí misma
        ])
        self.assertEqual(t.resolve(0x7F030000), "res/mipmap-hdpi/ic.png")
        self.assertIsNone(t.resolve(0x7F040000))

    def test_missing_entries_and_unknown_ids(self):
        t = self.table([{"type_id": 2, "entries": {0: (b.TYPE_STRING, 1), 2: (b.TYPE_STRING, 2)}}])
        self.assertEqual(t.resolve(0x7F020000), "res/mipmap-hdpi/ic.png")
        self.assertIsNone(t.resolve(0x7F020001))      # hueco (offset 0xFFFFFFFF)
        self.assertEqual(t.resolve(0x7F020002), "res/mipmap-xxhdpi/ic.png")
        self.assertIsNone(t.resolve(0x7F990000))

    def test_sparse_entries(self):
        t = self.table([{"type_id": 5, "sparse": True,
                         "entries": {3: (b.TYPE_STRING, 1), 9: (b.TYPE_STRING, 2)}}])
        self.assertEqual(t.resolve(0x7F050003), "res/mipmap-hdpi/ic.png")
        self.assertEqual(t.resolve(0x7F050009), "res/mipmap-xxhdpi/ic.png")
        self.assertIsNone(t.resolve(0x7F050004))

    def test_colors(self):
        t = self.table([{"type_id": 6, "entries": {0: (0x1C, 0xFF112233),
                                                   1: (b.TYPE_REFERENCE, 0x7F060000)}}])
        self.assertEqual(t.resolve_color(0x7F060000), 0xFF112233)
        self.assertEqual(t.resolve_color(0x7F060001), 0xFF112233)
        self.assertIsNone(t.resolve_color(0x7F060002))

    def test_garbage_gives_empty_table(self):
        self.assertEqual(app._ResourceTable(b"").entries, {})
        self.assertEqual(app._ResourceTable(b"no es un arsc").entries, {})


# ---------------------------------------------------------------------------
class ApkManifestInfoTests(TempDirCase):
    def write_apk(self, **kwargs):
        return b.make_apk(self.tmp / "t.apk", **kwargs)

    def test_reads_package_and_versions(self):
        info = app.apk_manifest_info(self.write_apk(package="com.demo.app"))
        self.assertEqual(info["package"], "com.demo.app")
        self.assertEqual(info["split"], "")
        self.assertEqual(info["version_name"], "1.2.3")
        self.assertEqual(info["version_code"], 7)

    def test_reads_split_name(self):
        info = app.apk_manifest_info(
            self.write_apk(package="com.demo.app", split="config.armeabi_v7a"))
        self.assertEqual(info["split"], "config.armeabi_v7a")

    def test_reads_min_sdk_from_uses_sdk(self):
        # En un APK real minSdkVersion está en <uses-sdk>, no en <manifest>.
        info = app.apk_manifest_info(self.write_apk(package="com.demo.app", min_sdk=26))
        self.assertEqual(info["min_sdk"], 26)

    def test_missing_min_sdk_is_none(self):
        info = app.apk_manifest_info(self.write_apk(package="com.demo.app"))
        self.assertIsNone(info["min_sdk"])

    def test_errors_become_adberror(self):
        path = self.tmp / "t.apk"
        path.write_bytes(b.zip_bytes({"otro.txt": b"x"}))
        with self.assertRaises(app.AdbError):
            app.apk_manifest_info(path)
        path.write_bytes(b"esto no es un zip")
        with self.assertRaises(app.AdbError):
            app.apk_manifest_info(path)
        path.write_bytes(b.zip_bytes({"AndroidManifest.xml": b"basura"}))
        with self.assertRaises(app.AdbError):
            app.apk_manifest_info(path)
        good = b.manifest_xml("com.demo.app", min_sdk=21)
        path.write_bytes(b.zip_bytes({"AndroidManifest.xml": good[: len(good) // 2]}))
        with self.assertRaises(app.AdbError):
            app.apk_manifest_info(path)


# ---------------------------------------------------------------------------
class DerAndSignatureTests(TempDirCase):
    def test_der_element_short_and_long_length(self):
        short = b.der(0x30, b"abc")
        self.assertEqual(app._der_element(short, 0), (0x30, 2, 5, 5))
        payload = b"z" * 300
        long_form = b.der(0x04, payload)
        tag, start, end, nxt = app._der_element(long_form, 0)
        self.assertEqual((tag, start, end, nxt), (0x04, 4, 304, 304))

    def test_der_truncated_or_invalid_raises(self):
        for bad in (b"", b"\x30", b"\x30\x05ab", b"\x30\x80", b"\x30\x82\x01"):
            with self.assertRaises(ValueError, msg=repr(bad)):
                app._der_element(bad, 0)

    def test_v1_certificate_extraction(self):
        cert = b.fake_certificate("v1")
        self.assertEqual(app._v1_signer_certificate(b.pkcs7_with_cert(cert)), cert)
        self.assertEqual(app._v1_signer_certificate(b.der(0x04, b"no pkcs7")), b"")

    def test_v2_certificate_extraction(self):
        cert = b.fake_certificate("v2")
        self.assertEqual(app._v2_signer_certificate(b.v2_value(cert)), cert)
        with self.assertRaises(ValueError):
            app._v2_signer_certificate(b.v2_value(cert)[:20])

    def test_signer_sha256_v2(self):
        path = b.make_apk(self.tmp / "a.apk", "com.a", cert="A", scheme="v2")
        self.assertEqual(app.apk_signer_sha256(path), sha(b.fake_certificate("A")))

    def test_signer_sha256_v1(self):
        path = b.make_apk(self.tmp / "a.apk", "com.a", cert="B", scheme="v1")
        self.assertEqual(app.apk_signer_sha256(path), sha(b.fake_certificate("B")))

    def test_different_certificates_give_different_hashes(self):
        a = b.make_apk(self.tmp / "a.apk", "com.a", cert="A")
        c = b.make_apk(self.tmp / "c.apk", "com.a", cert="C")
        self.assertNotEqual(app.apk_signer_sha256(a), app.apk_signer_sha256(c))

    def test_v2_takes_precedence_over_v1(self):
        files = {
            "AndroidManifest.xml": b.manifest_xml("com.a"),
            "META-INF/CERT.RSA": b.pkcs7_with_cert(b.fake_certificate("v1")),
        }
        data = b.add_signing_block(b.zip_bytes(files), {b.V2_ID: b.v2_value(b.fake_certificate("v2"))})
        path = self.tmp / "both.apk"
        path.write_bytes(data)
        self.assertEqual(app.apk_signer_sha256(path), sha(b.fake_certificate("v2")))

    def test_unsigned_and_corrupt_files_give_empty_string(self):
        unsigned = b.make_apk(self.tmp / "u.apk", "com.a", cert=None)
        self.assertEqual(app.apk_signer_sha256(unsigned), "")
        junk = self.tmp / "junk.apk"
        junk.write_bytes(b"x" * 100)
        self.assertEqual(app.apk_signer_sha256(junk), "")
        tiny = self.tmp / "tiny.apk"
        tiny.write_bytes(b"PK")
        self.assertEqual(app.apk_signer_sha256(tiny), "")

    def test_damaged_signing_block_falls_back_without_crashing(self):
        path = b.make_apk(self.tmp / "a.apk", "com.a", cert="A")
        data = bytearray(path.read_bytes())
        data[data.find(b.SIG_MAGIC)] ^= 0xFF          # corrompe la firma mágica
        path.write_bytes(bytes(data))
        self.assertEqual(app.apk_signer_sha256(path), "")

    def test_signing_block_pairs(self):
        data = b.add_signing_block(b.zip_bytes({"a.txt": b"hola" * 20}),
                                   {b.V2_ID: b"uno", 0x42726577: b"dos"})
        path = self.tmp / "p.apk"
        path.write_bytes(data)
        self.assertEqual(app._apk_signing_block(path), {b.V2_ID: b"uno", 0x42726577: b"dos"})
        plain = self.tmp / "plain.zip"
        plain.write_bytes(b.zip_bytes({"a.txt": b"hola" * 20}))
        self.assertEqual(app._apk_signing_block(plain), {})


# ---------------------------------------------------------------------------
def zf_from(files: dict[str, bytes]) -> zipfile.ZipFile:
    return zipfile.ZipFile(io.BytesIO(b.zip_bytes(files)))


class BundleSelectionTests(TempDirCase):
    def names(self, entries):
        return [name for name, _ in entries]

    def test_apks_with_splits_folder(self):
        zf = zf_from({"splits/base-master.apk": b"1", "splits/base-armeabi_v7a.apk": b"2",
                      "toc.pb": b"3", "otro.apk": b"4"})
        self.assertEqual(self.names(app._select_bundle_entries(zf, "x.apks", [])),
                         ["splits/base-master.apk", "splits/base-armeabi_v7a.apk"])

    def test_xapk_uses_manifest_and_ignores_unlisted_apks(self):
        manifest = (b'{"package_name":"com.x","split_apks":'
                    b'[{"file":"com.x.apk","id":"base"},{"file":"config.armeabi_v7a.apk"}]}')
        zf = zf_from({"manifest.json": manifest, "com.x.apk": b"1",
                      "config.armeabi_v7a.apk": b"2", "config.x86.apk": b"3"})
        self.assertEqual(self.names(app._select_bundle_entries(zf, "x.xapk", [])),
                         ["com.x.apk", "config.armeabi_v7a.apk"])

    def test_xapk_adds_unlisted_base_first(self):
        manifest = b'{"package_name":"com.x","split_apks":[{"file":"config.hdpi.apk"}]}'
        zf = zf_from({"manifest.json": manifest, "base.apk": b"1", "config.hdpi.apk": b"2"})
        self.assertEqual(self.names(app._select_bundle_entries(zf, "x.xapk", [])),
                         ["base.apk", "config.hdpi.apk"])

    def test_universal_apk_wins(self):
        zf = zf_from({"universal.apk": b"1", "other.apk": b"2"})
        self.assertEqual(self.names(app._select_bundle_entries(zf, "x.zip", [])), ["universal.apk"])

    def test_standalones_pick_matching_abi(self):
        zf = zf_from({"standalones/standalone-arm64_v8a.apk": b"1",
                      "standalones/standalone-armeabi_v7a.apk": b"2"})
        picked = app._select_bundle_entries(zf, "x.apks", ["armeabi-v7a", "armeabi"])
        self.assertEqual(self.names(picked), ["standalones/standalone-armeabi_v7a.apk"])
        fallback = app._select_bundle_entries(zf, "x.apks", ["x86"])
        self.assertEqual(self.names(fallback), ["standalones/standalone-arm64_v8a.apk"])

    def test_no_apks_gives_empty_list(self):
        self.assertEqual(app._select_bundle_entries(zf_from({"leeme.txt": b"x"}), "x.zip", []), [])

    def test_manifest_apk_names_variants(self):
        zf = zf_from({"info.json": b'{"package":"com.y","apks":["a.apk",{"file":"b.apk"}]}'})
        self.assertEqual(app._manifest_apk_names(zf), (["a.apk", "b.apk"], "com.y"))
        broken = zf_from({"manifest.json": b"{no json", "info.json": b"[1,2]"})
        self.assertEqual(app._manifest_apk_names(broken), ([], ""))

    def test_resolve_entries_by_name_and_unique_basename_only(self):
        zf = zf_from({"a/base.apk": b"1", "b/base.apk": b"2", "c/solo.apk": b"3"})
        entries = app._zip_apk_entries(zf)
        self.assertEqual(self.names(app._resolve_zip_entries(entries, ["a/base.apk"])), ["a/base.apk"])
        self.assertEqual(app._resolve_zip_entries(entries, ["base.apk"]), [])      # ambiguo
        self.assertEqual(self.names(app._resolve_zip_entries(entries, ["SOLO.apk"])), ["c/solo.apk"])
        self.assertEqual(self.names(app._resolve_zip_entries(entries, ["c\\solo.apk", "c/solo.apk"])),
                         ["c/solo.apk"])                                            # sin duplicados

    def test_extract_blocks_path_traversal(self):
        zf = zf_from({"../../evil.apk": b"contenido", "dir/ok.apk": b"otro"})
        dest = self.tmp / "out"
        dest.mkdir()
        paths = app._extract_bundle_apks(zf, app._zip_apk_entries(zf), dest)
        self.assertEqual({p.parent for p in paths}, {dest})
        self.assertEqual(sorted(p.name for p in paths), ["00-evil.apk", "01-ok.apk"])

    def test_extract_rejects_empty_encrypted_and_too_many(self):
        dest = self.tmp / "out"
        dest.mkdir()
        zf = zf_from({"vacio.apk": b""})
        with self.assertRaises(app.AdbError):
            app._extract_bundle_apks(zf, app._zip_apk_entries(zf), dest)
        zf = zf_from({"cifrado.apk": b"datos"})
        entries = app._zip_apk_entries(zf)
        entries[0][1].flag_bits |= 0x1
        with self.assertRaises(app.AdbError):
            app._extract_bundle_apks(zf, entries, dest)
        zf = zf_from({f"{i}.apk": b"x" for i in range(5)})
        with mock.patch.object(app, "INSTALL_MAX_APKS", 3):
            with self.assertRaises(app.AdbError):
                app._extract_bundle_apks(zf, app._zip_apk_entries(zf), dest)


class AbiFilterTests(TempDirCase):
    V7A = ["armeabi-v7a", "armeabi"]

    def item(self, name, package="com.a", split="", libs=()):
        path = b.make_apk(self.tmp / name, package, split=split, libs=tuple(libs))
        return {"path": path, "info": {"split": split}}

    def test_native_abis_listing(self):
        path = b.make_apk(self.tmp / "n.apk", "com.a", libs=("arm64-v8a", "armeabi-v7a"))
        self.assertEqual(app.apk_native_abis(path), ["arm64-v8a", "armeabi-v7a"])
        none = b.make_apk(self.tmp / "m.apk", "com.a")
        self.assertEqual(app.apk_native_abis(none), [])
        bad = self.tmp / "bad.apk"
        bad.write_bytes(b"no zip")
        with self.assertRaises(app.AdbError):
            app.apk_native_abis(bad)

    def test_skips_split_of_unsupported_architecture(self):
        parsed = [self.item("base.apk"),
                  self.item("config.armeabi_v7a.apk", split="config.armeabi_v7a"),
                  self.item("config.x86_64.apk", split="config.x86_64")]
        kept, skipped = app._filter_bundle_abis(parsed, self.V7A)
        self.assertEqual([i["path"].name for i in kept], ["base.apk", "config.armeabi_v7a.apk"])
        self.assertEqual([i["path"].name for i in skipped], ["config.x86_64.apk"])

    def test_prefers_primary_abi_when_several_are_supported(self):
        parsed = [self.item("base.apk"),
                  self.item("config.arm64_v8a.apk", split="config.arm64_v8a"),
                  self.item("config.armeabi_v7a.apk", split="config.armeabi_v7a")]
        kept, skipped = app._filter_bundle_abis(parsed, ["arm64-v8a", "armeabi-v7a"])
        self.assertEqual([i["path"].name for i in kept], ["base.apk", "config.arm64_v8a.apk"])
        self.assertEqual([i["path"].name for i in skipped], ["config.armeabi_v7a.apk"])

    def test_rejects_apk_with_only_incompatible_native_code(self):
        parsed = [self.item("base.apk", libs=("arm64-v8a",))]
        with self.assertRaises(app.AdbError) as ctx:
            app._filter_bundle_abis(parsed, self.V7A)
        self.assertIn("arquitectura", str(ctx.exception))

    def test_requires_a_base_apk(self):
        parsed = [self.item("config.armeabi_v7a.apk", split="config.armeabi_v7a")]
        with self.assertRaises(app.AdbError):
            app._filter_bundle_abis(parsed, self.V7A)

    def test_without_device_abis_keeps_everything(self):
        parsed = [self.item("base.apk"), self.item("config.x86.apk", split="config.x86")]
        kept, skipped = app._filter_bundle_abis(parsed, [])
        self.assertEqual((len(kept), skipped), (2, []))


# ---------------------------------------------------------------------------
class InspectInstallPackageTests(TempDirCase):
    SERIAL = "192.168.0.10:5555"

    def setUp(self):
        super().setUp()
        for target, value in (("device_abis", ["armeabi-v7a", "armeabi"]), ("device_sdk", 28)):
            patcher = mock.patch.object(app, target, lambda serial=None, v=value: v)
            patcher.start()
            self.addCleanup(patcher.stop)

    def apk(self, name, package="com.demo", **kwargs):
        return b.make_apk(self.tmp / name, package, **kwargs)

    def bundle(self, files: dict[str, Path], name="paquete.apks") -> Path:
        path = self.tmp / name
        path.write_bytes(b.zip_bytes({k: v.read_bytes() for k, v in files.items()}))
        return path

    def inspect(self, path, filename=None):
        plan = app.inspect_install_package(path, filename or path.name, serial=self.SERIAL)
        self.addCleanup(shutil.rmtree, plan["tempdir"], ignore_errors=True)
        return plan

    def test_single_apk(self):
        plan = self.inspect(self.apk("app.apk"))
        self.assertEqual((plan["kind"], plan["package"]), ("apk", "com.demo"))
        self.assertEqual((plan["version_name"], plan["version_code"]), ("1.2.3", 7))
        self.assertEqual(plan["signer"], sha(b.fake_certificate("A")))
        self.assertEqual(plan["skipped"], [])

    def test_split_bundle_skips_foreign_architecture(self):
        files = {
            "splits/base-master.apk": self.apk("b.apk"),
            "splits/config.armeabi_v7a.apk": self.apk("v.apk", split="config.armeabi_v7a"),
            "splits/config.x86_64.apk": self.apk("x.apk", split="config.x86_64"),
        }
        plan = self.inspect(self.bundle(files))
        self.assertEqual(plan["kind"], "bundle")
        self.assertEqual(len(plan["apks"]), 2)
        self.assertEqual([s["name"] for s in plan["skipped"]], ["02-config.x86_64.apk"])
        self.assertFalse(plan["obb"])

    def test_obb_is_detected(self):
        manifest = b'{"package_name":"com.demo","split_apks":[{"file":"base.apk"}]}'
        path = self.tmp / "juego.xapk"
        path.write_bytes(b.zip_bytes({
            "manifest.json": manifest, "base.apk": self.apk("b.apk").read_bytes(),
            "Android/obb/com.demo/main.1.com.demo.obb": b"obb"}))
        self.assertTrue(self.inspect(path)["obb"])

    def test_min_sdk_above_device_is_rejected(self):
        with self.assertRaises(app.AdbError) as ctx:
            app.inspect_install_package(self.apk("new.apk", min_sdk=30), "new.apk", serial=self.SERIAL)
        self.assertIn("30", str(ctx.exception))

    def test_min_sdk_within_device_is_accepted(self):
        self.assertEqual(self.inspect(self.apk("ok.apk", min_sdk=21))["package"], "com.demo")

    def test_mixed_packages_are_rejected(self):
        files = {"splits/base.apk": self.apk("b.apk"),
                 "splits/config.hdpi.apk": self.apk("o.apk", package="com.otra", split="config.hdpi")}
        with self.assertRaises(app.AdbError) as ctx:
            app.inspect_install_package(self.bundle(files), "p.apks", serial=self.SERIAL)
        self.assertIn("mezcla", str(ctx.exception))

    def test_two_base_variants_are_rejected(self):
        files = {"splits/base-a.apk": self.apk("a.apk"), "splits/base-b.apk": self.apk("c.apk")}
        with self.assertRaises(app.AdbError) as ctx:
            app.inspect_install_package(self.bundle(files), "p.apks", serial=self.SERIAL)
        self.assertIn("varias variantes base", str(ctx.exception))

    def test_different_signatures_are_rejected(self):
        files = {"splits/base.apk": self.apk("a.apk", cert="A"),
                 "splits/config.hdpi.apk": self.apk("c.apk", split="config.hdpi", cert="B")}
        with self.assertRaises(app.AdbError) as ctx:
            app.inspect_install_package(self.bundle(files), "p.apks", serial=self.SERIAL)
        self.assertIn("misma firma", str(ctx.exception))

    def test_unsigned_apk_is_rejected(self):
        with self.assertRaises(app.AdbError) as ctx:
            app.inspect_install_package(self.apk("u.apk", cert=None), "u.apk", serial=self.SERIAL)
        self.assertIn("firma", str(ctx.exception))

    def test_invalid_files_are_rejected(self):
        txt = self.tmp / "a.txt"
        txt.write_bytes(b"hola")
        empty = self.tmp / "e.apk"
        empty.write_bytes(b"")
        fake = self.tmp / "f.apk"
        fake.write_bytes(b"no soy un zip")
        for path in (txt, empty, fake):
            with self.assertRaises(app.AdbError, msg=path.name):
                app.inspect_install_package(path, path.name, serial=self.SERIAL)

    def test_bundle_without_apks_is_rejected(self):
        path = self.tmp / "vacio.apks"
        path.write_bytes(b.zip_bytes({"leeme.txt": b"hola"}))
        with self.assertRaises(app.AdbError):
            app.inspect_install_package(path, path.name, serial=self.SERIAL)


# ---------------------------------------------------------------------------
class RemotePathTests(unittest.TestCase):
    def test_normalize_remote_path_valid(self):
        n = app.normalize_remote_path
        self.assertEqual(n(""), "/sdcard")
        self.assertEqual(n(None), "/sdcard")
        self.assertEqual(n("Download"), "/sdcard/Download")
        self.assertEqual(n("/sdcard/"), "/sdcard")
        self.assertEqual(n("/sdcard/a/../b"), "/sdcard/b")
        self.assertEqual(n("/sdcard/./x//y"), "/sdcard/x/y")
        self.assertEqual(n("a\\b"), "/sdcard/a/b")

    def test_normalize_remote_path_escapes_are_rejected(self):
        for bad in ("/sdcard/..", "/sdcard/../data", "../x", "/data/app", "/", "/sdcardevil",
                    "/sdcard/../../etc", "a\x00b"):
            with self.assertRaises(app.AdbError, msg=repr(bad)):
                app.normalize_remote_path(bad)

    def test_clean_file_name(self):
        self.assertEqual(app.clean_file_name("  mi archivo.txt "), "mi archivo.txt")
        for bad in ("", "   ", ".", "..", "a/b", "a\\b", "x" * 201, "a\nb", "a\x00b", None):
            with self.assertRaises(app.AdbError, msg=repr(bad)):
                app.clean_file_name(bad)

    def test_parent_remote_path(self):
        self.assertIsNone(app.parent_remote_path("/sdcard"))
        self.assertEqual(app.parent_remote_path("/sdcard/a"), "/sdcard")
        self.assertEqual(app.parent_remote_path("/sdcard/a/b"), "/sdcard/a")

    def test_parse_ls_line(self):
        d = app._parse_ls_line("drwxrwx--x 2 root sdcard_rw 4096 2024-01-02 03:04 Download", "/sdcard")
        self.assertEqual((d["name"], d["dir"], d["date"], d["path"]),
                         ("Download", True, "02/01/2024 03:04", "/sdcard/Download"))
        f = app._parse_ls_line("-rw-rw---- 1 root sdcard_rw 12345 2024-01-02 03:04 mi foto.jpg", "/sdcard/DCIM")
        self.assertEqual((f["name"], f["size"], f["dir"]), ("mi foto.jpg", 12345, False))
        link = app._parse_ls_line("lrwxrwxrwx 1 root root 10 2024-01-02 03:04 sdcard -> /storage/x", "/")
        self.assertEqual((link["name"], link["link"], link["target"], link["path"]),
                         ("sdcard", True, "/storage/x", "/sdcard"))
        for skip in ("total 8", "drwx 2 r r 4096 2024-01-02 03:04 .", "drwx 2 r r 4096 2024-01-02 03:04 ..", ""):
            self.assertIsNone(app._parse_ls_line(skip, "/sdcard"), msg=skip)


class NetworkAndTextParsingTests(unittest.TestCase):
    def test_is_public_address(self):
        for private in ("10.0.0.1", "192.168.1.1", "172.16.5.4", "127.0.0.1", "169.254.169.254",
                        "0.0.0.0", "224.0.0.1", "100.64.0.1", "::1", "fe80::1",
                        "::ffff:127.0.0.1", "::ffff:10.0.0.1", "no-es-ip", ""):
            self.assertFalse(app._is_public_address(private), msg=private)
        for public in ("8.8.8.8", "1.1.1.1", "2001:4860:4860::8888"):
            self.assertTrue(app._is_public_address(public), msg=public)

    def test_ping_summary(self):
        text = ("3 packets transmitted, 3 received, 0% packet loss, time 2003ms\n"
                "rtt min/avg/max/mdev = 1.2/2.5/4.0/1.1 ms")
        self.assertEqual(app._ping_summary(text),
                         {"loss": 0, "sent": 3, "received": 3, "min": 1.2, "avg": 2.5, "max": 4.0})
        self.assertEqual(app._ping_summary("4 packets transmitted, 0 received, 100% packet loss")["loss"], 100)
        self.assertIsNone(app._ping_summary(""))
        self.assertIsNone(app._ping_summary("ping: unknown host"))

    def test_netmask_from_prefix(self):
        self.assertEqual(app._netmask_from_prefix(24), "255.255.255.0")
        self.assertEqual(app._netmask_from_prefix(20), "255.255.240.0")
        self.assertEqual(app._netmask_from_prefix(0), "0.0.0.0")
        self.assertEqual(app._netmask_from_prefix(32), "255.255.255.255")
        self.assertEqual(app._netmask_from_prefix(33), "")
        self.assertEqual(app._netmask_from_prefix(-1), "")

    def test_wifi_quality_and_security(self):
        self.assertEqual(app._wifi_quality(-60), 80)
        self.assertEqual(app._wifi_quality(-100), 0)
        self.assertEqual(app._wifi_quality(-120), 0)
        self.assertEqual(app._wifi_quality(-30), 100)
        self.assertIsNone(app._wifi_quality("mucho"))
        self.assertIsNone(app._wifi_quality(None))
        s = app._wifi_security
        self.assertEqual(s("WPA_PSK", "RSN"), "WPA2-PSK")
        self.assertEqual(s("WPA_PSK", "WPA"), "WPA-PSK")
        self.assertEqual(s("SAE", ""), "WPA3-SAE")
        self.assertEqual(s("NONE", ""), "Abierta")
        self.assertEqual(s("WPA_EAP", "RSN"), "WPA2-Enterprise")
        self.assertEqual(s("", "RSN"), "")

    def test_package_names(self):
        self.assertEqual(app.normalize_package("package:/data/app/~~ab==/com.x-1/base.apk=com.x"), "com.x")
        self.assertEqual(app.normalize_package("com.x"), "com.x")
        self.assertEqual(app.normalize_package(None), "")
        for ok in ("com.netflix.ninja", "a", "com.a_b.c1"):
            self.assertTrue(app.valid_package(ok), msg=ok)
        for bad in ("", "com..x", ".com", "com.x;rm -rf /", "com.x y", "com/x", "$(id)"):
            self.assertFalse(app.valid_package(bad), msg=bad)

    def test_pretty_name_documented_examples(self):
        self.assertEqual(app.pretty_name("com.tubitv"), "Tubitv")
        self.assertEqual(app.pretty_name("org.foo.downloader"), "Foo Downloader")
        self.assertTrue(app.pretty_name("com"))  # nunca devuelve vacío

    def test_human_size(self):
        self.assertIn("KB", app.human_size(2))
        self.assertIn("MB", app.human_size(5 * 1024))
        self.assertIn("GB", app.human_size(3 * 1024 * 1024))


# ---------------------------------------------------------------------------
class RequestOriginTests(unittest.TestCase):
    """request_allowed(): defensa contra CSRF y DNS rebinding."""

    def allowed(self, **headers):
        handler = app.Handler.__new__(app.Handler)
        handler.headers = headers
        return handler.request_allowed()

    def test_same_origin_requests_pass(self):
        self.assertTrue(self.allowed(Host="127.0.0.1:8080"))
        self.assertTrue(self.allowed(Host="localhost:8080", Origin="http://localhost:8080"))
        self.assertTrue(self.allowed(Host="192.168.1.5:8080", Origin="http://192.168.1.5:8080"))
        self.assertTrue(self.allowed(Host="[::1]:8080"))
        self.assertTrue(self.allowed(**{"Host": "localhost:8080", "Sec-Fetch-Site": "same-origin"}))
        self.assertTrue(self.allowed(**{"Host": "localhost:8080", "Sec-Fetch-Site": "none"}))

    def test_foreign_requests_are_blocked(self):
        self.assertFalse(self.allowed(Host="evil.example.com:8080"))
        self.assertFalse(self.allowed(Host="localhost.evil.com:8080"))
        self.assertFalse(self.allowed())
        self.assertFalse(self.allowed(Host="localhost:8080", Origin="http://evil.example.com"))
        self.assertFalse(self.allowed(Host="localhost:8080", Origin="null"))
        self.assertFalse(self.allowed(Host="localhost:8080", Origin="http://localhost:9999"))
        self.assertFalse(self.allowed(**{"Host": "localhost:8080", "Sec-Fetch-Site": "cross-site"}))
        self.assertFalse(self.allowed(**{"Host": "localhost:8080", "Sec-Fetch-Site": "same-site"}))


# ---------------------------------------------------------------------------
class RemoteRangeTests(unittest.TestCase):
    """Lectura del icono pidiendo solo trozos del APK (sin Fire TV)."""

    @classmethod
    def setUpClass(cls):
        import random

        cls.blob = random.Random(7).randbytes(3 * 1024 * 1024)  # no comprime
        cls.icon = b.png_image(96)
        cls.apk = b.zip_bytes({
            "AndroidManifest.xml": b.manifest_xml("com.example.range"),
            "res/mipmap-xhdpi/ic_launcher.png": cls.icon,
            "assets/blob.bin": cls.blob,
            "classes.dex": b"dex\n" + cls.blob[:200_000],
        })

    @staticmethod
    def fetcher(data: bytes, log: list | None = None, block: int = app.ICON_RANGE_BLOCK):
        def fetch(offset, length):
            assert offset % block == 0, "los trozos deben ir alineados"
            if log is not None:
                log.append((offset, length))
            return data[offset:offset + length]
        return fetch

    def remote(self, data=None, log=None, **kwargs):
        data = self.apk if data is None else data
        return app.RemoteRangeFile(len(data), self.fetcher(data, log), **kwargs)

    def test_zipfile_reads_entries_correctly(self):
        with zipfile.ZipFile(self.remote()) as zf:
            self.assertEqual(zf.read("res/mipmap-xhdpi/ic_launcher.png"), self.icon)
            self.assertEqual(zf.read("AndroidManifest.xml"),
                             b.manifest_xml("com.example.range"))

    def test_reads_only_a_fraction_of_the_file(self):
        source = self.remote()
        with zipfile.ZipFile(source) as zf:
            zf.read("res/mipmap-xhdpi/ic_launcher.png")
        self.assertLess(source.fetched, len(self.apk) // 4)

    def test_icon_matches_full_download(self):
        path = Path(tempfile.mkdtemp(prefix="fd_range_")) / "x.apk"
        self.addCleanup(shutil.rmtree, path.parent, ignore_errors=True)
        path.write_bytes(self.apk)
        expected = app.extract_app_icon(path)
        self.assertIsNotNone(expected)
        with mock.patch.object(app, "_adb_range_fetcher",
                               lambda serial, remote, block: self.fetcher(self.apk)):
            self.assertEqual(app.icon_from_remote_apk("s", "/r.apk", len(self.apk)), expected)

    def test_seek_and_read_semantics(self):
        data = bytes(range(256)) * 1024  # 256 KiB
        source = self.remote(data)
        self.assertEqual(source.seek(-10, 2), len(data) - 10)
        self.assertEqual(source.read(), data[-10:])
        self.assertEqual(source.read(5), b"")
        source.seek(70_000)
        self.assertEqual(source.read(70_000), data[70_000:140_000])  # cruza bloques
        self.assertEqual(source.tell(), 140_000)
        source.seek(5, 0)
        source.seek(5, 1)
        self.assertEqual(source.tell(), 10)
        with self.assertRaises(OSError):
            source.seek(-1)

    def test_cached_blocks_are_not_requested_twice(self):
        log: list = []
        source = self.remote(log=log)
        source.seek(1000)
        source.read(10)
        count = len(log)
        source.seek(1000)
        source.read(10)
        self.assertEqual(len(log), count)

    def test_sequential_reads_grow_the_readahead(self):
        data = bytes(2 * 1024 * 1024)
        log: list = []
        source = self.remote(data, log)
        while source.read(8192):
            pass
        lengths = [length for _offset, length in log]
        self.assertEqual(sum(lengths), len(data))
        self.assertGreater(max(lengths), lengths[0])
        self.assertLess(len(log), 16)

    def test_wrong_length_is_flagged_as_integrity_error(self):
        source = app.RemoteRangeFile(
            len(self.apk), lambda offset, length: self.apk[offset:offset + length - 1])
        with self.assertRaises(app.RemoteReadError) as ctx:
            source.read(10)
        self.assertTrue(ctx.exception.integrity)

    def test_budget_and_request_limits_force_fallback(self):
        with self.assertRaises(app.RemoteReadError) as ctx:
            self.remote(budget=1024).read(100)
        self.assertFalse(ctx.exception.integrity)
        source = self.remote(max_requests=1)
        source.read(10)
        with self.assertRaises(app.RemoteReadError):
            source.seek(2_000_000)
            source.read(10)

    def test_non_zip_becomes_remote_read_error(self):
        data = b"no soy un zip" * 5000
        with mock.patch.object(app, "_adb_range_fetcher",
                               lambda serial, remote, block: self.fetcher(data)):
            with self.assertRaises(app.RemoteReadError):
                app.icon_from_remote_apk("s", "/r.apk", len(data))

    def test_adb_fetcher_builds_dd_command_and_checks_status(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return mock.Mock(returncode=0, stdout=b"abc", stderr=b"")

        with mock.patch.object(app.subprocess, "run", fake_run):
            fetch = app._adb_range_fetcher("1.2.3.4:5555", "/data/app/x y/base.apk", 65536)
            self.assertEqual(fetch(131072, 3), b"abc")
            self.assertEqual(calls[0][1:5], ["-s", "1.2.3.4:5555", "exec-out", calls[0][4]])
            command = calls[0][4]
            self.assertIn("bs=65536", command)
            self.assertIn("skip=2", command)
            self.assertIn("count=1", command)
            self.assertIn("'/data/app/x y/base.apk'", command)
            with self.assertRaises(app.RemoteReadError):
                fetch(100, 3)  # sin alinear

        failing = mock.Mock(returncode=1, stdout=b"", stderr=b"dd: not found")
        with mock.patch.object(app.subprocess, "run", lambda *a, **k: failing):
            with self.assertRaises(app.RemoteReadError):
                app._adb_range_fetcher("s", "/r", 65536)(0, 10)


class AppIconFlowTests(TempDirCase):
    """app_icon(): lectura parcial, vuelta a la descarga completa y caché."""

    PNG = b.png_image(96)

    def setUp(self):
        super().setUp()
        for name, value in (
            ("ICON_DIR", self.tmp / "icons"),
            ("ICON_MISSES", {}),
            ("ICON_RANGE_DISABLED", False),
        ):
            patcher = mock.patch.object(app, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.apk_path = self.tmp / "device.apk"
        self.apk_path.write_bytes(b.zip_bytes({
            "AndroidManifest.xml": b.manifest_xml("com.example.flow"),
            "res/mipmap-xhdpi/ic_launcher.png": self.PNG,
        }))

    def run_icon(self, size, partial, pull):
        def shell(command, timeout=40):
            if command.startswith("pm path"):
                return 0, "package:/data/app/x/base.apk"
            return 0, ""

        with mock.patch.object(app, "pick_serial", lambda: "s"), \
             mock.patch.object(app, "device_shell_quiet", shell), \
             mock.patch.object(app, "remote_file_size", lambda remote: size), \
             mock.patch.object(app, "icon_from_remote_apk", partial), \
             mock.patch.object(app, "_adb_pull", pull):
            return app.app_icon("com.example.flow")

    def pull_ok(self, serial, remote, local, timeout=300):
        shutil.copy(self.apk_path, local)
        return 0, ""

    def test_partial_read_avoids_the_full_download(self):
        pulled = []
        result = self.run_icon(
            1000, lambda *a: (self.PNG, ".png"),
            lambda *a, **k: pulled.append(a) or (1, "no debería bajarse"))
        self.assertEqual(result, (self.PNG, ".png"))
        self.assertEqual(pulled, [])

    def test_falls_back_to_full_download_and_caches(self):
        def broken(*args):
            raise app.RemoteReadError("fallo parcial")

        first = self.run_icon(1000, broken, self.pull_ok)
        self.assertEqual(first[1], ".png")
        self.assertFalse(app.ICON_RANGE_DISABLED)
        # Segunda petición: sale de la caché sin tocar ningún método de lectura.
        second = self.run_icon(1000, broken, lambda *a, **k: (1, "no debería bajarse"))
        self.assertEqual(second, first)

    def test_integrity_error_disables_partial_reads(self):
        def corrupt(*args):
            raise app.RemoteReadError("bytes de menos", integrity=True)

        self.run_icon(1000, corrupt, self.pull_ok)
        self.assertTrue(app.ICON_RANGE_DISABLED)
        calls = []
        # Otra app: ya no se intenta la lectura parcial.
        with mock.patch.object(app, "pick_serial", lambda: "s"), \
             mock.patch.object(app, "device_shell_quiet",
                               lambda c, timeout=40: (0, "package:/data/app/y/base.apk")), \
             mock.patch.object(app, "remote_file_size", lambda remote: 1000), \
             mock.patch.object(app, "icon_from_remote_apk", lambda *a: calls.append(a)), \
             mock.patch.object(app, "_adb_pull", self.pull_ok):
            app.app_icon("com.example.other")
        self.assertEqual(calls, [])

    def test_oversized_apk_is_not_downloaded_when_partial_fails(self):
        def broken(*args):
            raise app.RemoteReadError("fallo parcial")

        pulled = []
        result = self.run_icon(
            app.ICON_MAX_APK + 1, broken,
            lambda *a, **k: pulled.append(a) or (0, ""))
        self.assertIsNone(result)
        self.assertEqual(pulled, [])

    def test_unknown_size_goes_straight_to_full_download(self):
        called = []
        result = self.run_icon(None, lambda *a: called.append(a), self.pull_ok)
        self.assertEqual(called, [])
        self.assertEqual(result[1], ".png")

    def test_apk_without_icon_is_remembered_as_missing(self):
        result = self.run_icon(1000, lambda *a: None, self.pull_ok)
        self.assertIsNone(result)
        self.assertIn("com.example.flow", app.ICON_MISSES)


# ---------------------------------------------------------------------------
class UnexpectedErrorLoggingTests(unittest.TestCase):
    """Los fallos inesperados no tumban el servidor y dejan traceback."""

    @staticmethod
    def boom():
        raise ZeroDivisionError("division rota")

    def capture(self, fn):
        buffer = io.StringIO()
        with mock.patch.object(sys, "stderr", buffer):
            result = fn()
        return result, buffer.getvalue()

    def test_log_unexpected_includes_traceback_and_location(self):
        try:
            self.boom()
        except ZeroDivisionError as exc:
            _, text = self.capture(lambda: app.log_unexpected("Contexto de prueba", exc))
        self.assertIn("Contexto de prueba", text)
        self.assertIn("ZeroDivisionError", text)
        self.assertIn("Traceback (most recent call last)", text)
        self.assertIn("in boom", text)  # dónde ocurrió, no solo el mensaje

    def make_handler(self):
        sent = []
        handler = mock.Mock()
        handler.send_json = lambda payload, status=200: sent.append((payload, status))
        return handler, sent

    def test_guard_turns_unexpected_error_into_500_with_traceback(self):
        handler, sent = self.make_handler()
        _, text = self.capture(lambda: app.Handler.guard(handler, self.boom))
        self.assertEqual(sent[0][1], 500)
        self.assertFalse(sent[0][0]["ok"])
        self.assertIn("Traceback", text)

    def test_guard_keeps_adb_errors_as_400_without_traceback(self):
        handler, sent = self.make_handler()

        def fail():
            raise app.AdbError("fallo esperado")

        _, text = self.capture(lambda: app.Handler.guard(handler, fail))
        self.assertEqual(sent, [({"ok": False, "error": "fallo esperado"}, 400)])
        self.assertEqual(text, "")

    def test_guard_ignores_browser_disconnects(self):
        # Caso real del log: ConnectionAbortedError 10053 al cerrar una pestaña.
        for error in (ConnectionAbortedError(10053, "anulada"), BrokenPipeError(),
                      ConnectionResetError()):
            handler, sent = self.make_handler()

            def drop(error=error):
                raise error

            _, text = self.capture(lambda: app.Handler.guard(handler, drop))
            self.assertEqual(sent, [], "no se responde por una conexión ya cerrada")
            self.assertEqual(text, "", "y tampoco se registra como fallo")
            self.assertTrue(handler.close_connection)

    def test_network_errors_are_covered_by_the_narrowed_adb_download_catch(self):
        # El arranque solo captura (OSError, BadZipFile, RuntimeError): un fallo de
        # red es URLError, que hereda de OSError; un bug (TypeError) no se oculta.
        import urllib.error

        self.assertTrue(issubclass(urllib.error.URLError, OSError))
        self.assertFalse(issubclass(TypeError, (OSError, zipfile.BadZipFile, RuntimeError)))


if __name__ == "__main__":
    unittest.main()
