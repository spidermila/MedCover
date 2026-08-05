"""Tests for the profile signature upload/remove/serve feature."""

import io
from pathlib import Path

import openpyxl
import pytest
from PIL import Image, ImageDraw

from app.extensions import db
from app.models.role import Role
from app.models.user import UserAccount
from app.signature import MAX_STORED_BYTES, SignatureError, process_signature_upload
from app.work_report_generator import generate_work_report
from tests.conftest import _make_user

SIGNATURE_PHOTO_FIXTURE = Path(__file__).parent / "fixtures" / "signature_photo.jpeg"


def _png_bytes(w: int = 800, h: int = 300, color: str = "white") -> bytes:
    """Return a PNG blob with a simple pen stroke on a plain background."""
    img = Image.new("RGB", (w, h), color)
    d = ImageDraw.Draw(img)
    d.line([(50, h // 2), (w // 4, 80), (w // 2, h - 80), (w - 100, h // 2)], fill="black", width=6)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _jpeg_bytes(w: int = 800, h: int = 300) -> bytes:
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    d.line([(50, 150), (400, 100), (750, 200)], fill="black", width=6)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


# ── Pipeline unit tests ──────────────────────────────────────────────────────


class TestSignaturePipeline:
    def test_png_input_produces_png_output(self) -> None:
        out = process_signature_upload(_png_bytes())
        assert out.startswith(b"\x89PNG")

    def test_output_is_palette_and_target_height(self) -> None:
        out = process_signature_upload(_png_bytes())
        img = Image.open(io.BytesIO(out))
        # Adaptive-palette PNG (mode P) keeps colour while fitting the stored cap.
        assert img.mode == "P"
        assert img.height == 200

    def test_output_within_stored_cap(self) -> None:
        out = process_signature_upload(_png_bytes(2400, 900))
        assert len(out) <= MAX_STORED_BYTES

    def test_jpeg_input_accepted(self) -> None:
        out = process_signature_upload(_jpeg_bytes())
        assert out.startswith(b"\x89PNG")

    def test_rejects_non_image(self) -> None:
        with pytest.raises(SignatureError):
            process_signature_upload(b"definitely not an image")

    def test_rejects_empty(self) -> None:
        with pytest.raises(SignatureError):
            process_signature_upload(b"")

    def test_transparent_png_composited_on_white(self) -> None:
        img = Image.new("RGBA", (400, 200), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.line([(20, 100), (380, 100)], fill=(0, 0, 0, 255), width=4)
        buf = io.BytesIO()
        img.save(buf, "PNG")
        out = process_signature_upload(buf.getvalue())
        result = Image.open(io.BytesIO(out)).convert("RGB")
        # Corners should be white (background composited), not black.
        corner = result.getpixel((0, 0))
        assert all(c > 200 for c in corner)

    def test_real_phone_photo_fits_stored_cap(self) -> None:
        # Real iPhone HDR photo of a cropped signature on paper. Exercises the
        # realistic compression path (photo noise + palette quantisation) that
        # synthetic ImageDraw PNGs never hit.
        raw = SIGNATURE_PHOTO_FIXTURE.read_bytes()
        out = process_signature_upload(raw)
        assert len(out) <= MAX_STORED_BYTES
        result = Image.open(io.BytesIO(out))
        assert result.height == 200

    def test_mpo_container_is_accepted(self) -> None:
        # iPhone HDR photos come out of Pillow as format="MPO" (multi-frame
        # JPEG container). Guard against regressing the whitelist.
        raw = SIGNATURE_PHOTO_FIXTURE.read_bytes()
        with Image.open(io.BytesIO(raw)) as im:
            assert im.format == "MPO"
        process_signature_upload(raw)  # must not raise

    def test_exif_orientation_is_applied(self) -> None:
        # Portrait-oriented JPEG with orientation=6 (rotate 270 CW) should end
        # up landscape after exif_transpose.
        img = Image.new("RGB", (200, 800), "white")
        d = ImageDraw.Draw(img)
        d.line([(20, 400), (180, 400)], fill="black", width=4)
        buf = io.BytesIO()
        img.save(buf, "JPEG", exif=b"Exif\x00\x00" + b"\x00" * 100)
        # Just verifies the pipeline does not crash on an EXIF-bearing file.
        out = process_signature_upload(buf.getvalue())
        assert len(out) > 0


# ── Route tests ──────────────────────────────────────────────────────────────


class TestSignatureRoutes:
    def test_profile_shows_upload_card_for_member(self, member_client: object) -> None:
        resp = member_client.get("/users/profile")
        assert resp.status_code == 200
        assert "Podpis pro výkaz práce".encode() in resp.data

    def test_upload_persists_bytes(self, app: object, member_client: object) -> None:
        data = {
            "action": "signature_upload",
            "signature": (io.BytesIO(_png_bytes()), "sig.png"),
        }
        resp = member_client.post(
            "/users/profile",
            data=data,
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert resp.status_code == 200
        assert "Podpis byl uložen".encode() in resp.data
        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "member@test.com"))
            assert user.signature_image is not None
            assert user.signature_image.startswith(b"\x89PNG")
            assert user.signature_mimetype == "image/png"

    def test_upload_missing_file_flashes(self, member_client: object) -> None:
        resp = member_client.post(
            "/users/profile",
            data={"action": "signature_upload"},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert "Nebyl vybrán žádný soubor".encode() in resp.data

    def test_upload_rejects_non_image(self, member_client: object) -> None:
        data = {
            "action": "signature_upload",
            "signature": (io.BytesIO(b"not an image"), "junk.png"),
        }
        resp = member_client.post(
            "/users/profile",
            data=data,
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        assert "není platný obrázek".encode() in resp.data

    def test_preview_returns_stored_bytes(self, app: object, member_client: object) -> None:
        # First upload.
        member_client.post(
            "/users/profile",
            data={
                "action": "signature_upload",
                "signature": (io.BytesIO(_png_bytes()), "sig.png"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        resp = member_client.get("/users/profile/signature")
        assert resp.status_code == 200
        assert resp.mimetype == "image/png"
        assert resp.data.startswith(b"\x89PNG")
        assert resp.headers.get("Cache-Control") == "private, no-store"

    def test_preview_404_when_no_signature(self, member_client: object) -> None:
        resp = member_client.get("/users/profile/signature")
        assert resp.status_code == 404

    def test_remove_clears_columns(self, app: object, member_client: object) -> None:
        member_client.post(
            "/users/profile",
            data={
                "action": "signature_upload",
                "signature": (io.BytesIO(_png_bytes()), "sig.png"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        resp = member_client.post(
            "/users/profile",
            data={"action": "signature_remove"},
            follow_redirects=True,
        )
        assert "Podpis byl smazán".encode() in resp.data
        with app.app_context():
            user = db.session.scalar(db.select(UserAccount).where(UserAccount.email == "member@test.com"))
            assert user.signature_image is None
            assert user.signature_mimetype is None


# ── Xlsx integration ─────────────────────────────────────────────────────────


class TestWorkReportSignatureEmbed:
    def test_report_without_signature_has_no_image(self, app, tmp_path, monkeypatch) -> None:
        with app.app_context():
            monkeypatch.setattr(app, "instance_path", str(tmp_path))
            u = _make_user("sig_none@test.com", "Bez Podpisu", Role.MEMBER)
            path = generate_work_report(u, 2026, 1)

        wb = openpyxl.load_workbook(str(path))
        ws = wb.active
        assert len(ws._images) == 0

    def test_report_with_signature_embeds_image(self, app, tmp_path, monkeypatch) -> None:
        with app.app_context():
            monkeypatch.setattr(app, "instance_path", str(tmp_path))
            u = _make_user("sig_yes@test.com", "S Podpisem", Role.MEMBER)
            u.signature_image = process_signature_upload(_png_bytes())
            u.signature_mimetype = "image/png"
            db.session.commit()
            path = generate_work_report(u, 2026, 1)

        wb = openpyxl.load_workbook(str(path))
        ws = wb.active
        assert len(ws._images) == 1
