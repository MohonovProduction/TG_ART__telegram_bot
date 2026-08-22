import asyncio
import gzip
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from app.renderer import (
    RenderError,
    VideoInfo,
    _parse_fps,
    _run,
    _tile_bounds,
    convert_webm_to_mov,
    convert_webp_to_png,
    prepare_sticker_files,
    prepare_video_note,
    probe_video,
)
from app.media import append_title_suffix, detect_source_kind
from app.config import Settings
from app.download_flow import _save_batch, configure as configure_download_flow
from app.path_utils import parse_local_path, strip_wrapping_quotes
from app.sticker_downloader import (
    StickerAsset,
    merge_assets,
    move_completed_file,
    parse_pack_link,
    unique_destination,
)
from app.sticker_pack import (
    build_tg_art_grid,
    make_sticker_set_name,
    parse_custom_emoji_pack_name,
    validate_pack_emoji,
    validate_pack_title,
)
from app.tgs_renderer import normalize_hex_color, parse_render_size, render_tgs_to_mov


class RendererTests(unittest.TestCase):
    def test_parse_fps(self):
        self.assertGreater(_parse_fps("30000/1001"), 29.9)
        self.assertEqual(_parse_fps("0/0"), 0)

    def test_alpha_detection(self):
        self.assertTrue(VideoInfo(100, 100, 3, 30, "yuva444p10le", "prores").has_alpha)
        self.assertFalse(VideoInfo(100, 100, 3, 30, "yuv420p", "h264").has_alpha)

    def test_tile_bounds_cover_every_pixel(self):
        bounds = [_tile_bounds(101, 50, 3, 1, column, 0) for column in range(3)]
        self.assertEqual(bounds, [(0, 0, 33, 50), (33, 0, 34, 50), (67, 0, 34, 50)])

    def test_sticker_set_name(self):
        self.assertEqual(
            make_sticker_set_name("Summer Art", "MyArtBot"),
            "summer_art_by_myartbot",
        )
        self.assertEqual(
            make_sticker_set_name("summer_art_by_myartbot", "MyArtBot"),
            "summer_art_by_myartbot",
        )

    def test_pack_values(self):
        self.assertEqual(validate_pack_title(" My pack "), "My pack")
        self.assertEqual(validate_pack_emoji(" 🎨 "), "🎨")
        with self.assertRaises(ValueError):
            make_sticker_set_name("123", "MyArtBot")

    def test_tg_art_grid_uses_custom_emoji_in_grid_order(self):
        text, entities = build_tg_art_grid(
            ["one", "two", "three", "four"], 2, 2, ["🎨", "⭐", "❤️", "🔥"]
        )

        self.assertEqual(text, "🎨⭐\n❤️🔥")
        self.assertEqual([entity.custom_emoji_id for entity in entities], ["one", "two", "three", "four"])
        self.assertEqual([(entity.offset, entity.length) for entity in entities], [(0, 2), (2, 1), (4, 2), (6, 2)])

    def test_parse_custom_emoji_pack_link(self):
        self.assertEqual(
            parse_custom_emoji_pack_name("https://t.me/addemoji/My_Pack_by_Bot"),
            "My_Pack_by_Bot",
        )
        with self.assertRaises(ValueError):
            parse_custom_emoji_pack_name("https://t.me/addstickers/not_emoji")

    def test_prepare_stickers_rejects_unknown_format(self):
        with self.assertRaises(RenderError):
            asyncio.run(prepare_sticker_files([], "animated", Path("output")))

    def test_prepare_stickers_rejects_invalid_size_limit(self):
        with self.assertRaises(RenderError):
            asyncio.run(
                prepare_sticker_files(
                    [Path("sticker.png")],
                    "static",
                    Path("output"),
                    max_static_size_kb=0,
                )
            )

    def test_prepare_video_note_rejects_invalid_options(self):
        with self.assertRaises(RenderError):
            asyncio.run(
                prepare_video_note(
                    Path("video.mp4"),
                    Path("output"),
                    max_duration=0,
                )
            )

    def test_media_detection_and_title_suffix(self):
        self.assertEqual(detect_source_kind(Path("art.png")), "image")
        self.assertEqual(detect_source_kind(Path("art.mov")), "video")
        self.assertEqual(
            append_title_suffix("My art", "by @mohonovproduction"),
            "My art by @mohonovproduction",
        )
        self.assertEqual(
            append_title_suffix("My art by @mohonovproduction", "by @mohonovproduction"),
            "My art by @mohonovproduction",
        )

    def test_finder_quoted_paths(self):
        value = "'/Users/me/Медицинские документы/Карточки'"
        self.assertEqual(
            strip_wrapping_quotes(value),
            "/Users/me/Медицинские документы/Карточки",
        )
        self.assertEqual(
            parse_local_path(value),
            Path("/Users/me/Медицинские документы/Карточки"),
        )
        self.assertEqual(strip_wrapping_quotes('"/tmp/a b"'), "/tmp/a b")

    def test_render_size_and_hex_color(self):
        self.assertEqual(parse_render_size("×1"), 512)
        self.assertEqual(parse_render_size("x3"), 1536)
        self.assertEqual(parse_render_size("2048"), 2048)
        self.assertEqual(normalize_hex_color("fff"), "#FFFFFF")
        self.assertEqual(normalize_hex_color("#72e6a6"), "#72E6A6")
        with self.assertRaises(ValueError):
            parse_render_size("8192")
        with self.assertRaises(ValueError):
            normalize_hex_color("white")

    def test_pack_link_and_asset_deduplication(self):
        self.assertEqual(
            parse_pack_link("https://t.me/addemoji/My_pack"),
            ("addemoji", "My_pack"),
        )
        asset = StickerAsset("file", "unique", "🎨", None, "animated", ".tgs")
        merged, added = merge_assets([], [asset, asset])
        self.assertEqual(added, 1)
        self.assertEqual(len(merged), 1)

    def test_completed_files_do_not_overwrite_existing_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "sticker.png").write_bytes(b"old")
            source = root / "temporary.png"
            source.write_bytes(b"new")
            self.assertEqual(unique_destination(root, "sticker.png").name, "sticker_2.png")
            destination = move_completed_file(source, root, "sticker.png")
            self.assertEqual(destination.name, "sticker_2.png")
            self.assertEqual((root / "sticker.png").read_bytes(), b"old")
            self.assertEqual(destination.read_bytes(), b"new")


class TgsRendererIntegrationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_tgs_renders_to_prores_mov_with_alpha(self):
        try:
            import rlottie_python  # noqa: F401
        except ImportError:
            self.skipTest("rlottie-python required")
        animation = {
            "v": "5.5.2", "fr": 60, "ip": 0, "op": 2,
            "w": 512, "h": 512, "nm": "test", "ddd": 0, "assets": [],
            "layers": [{
                "ddd": 0, "ind": 1, "ty": 4, "nm": "circle", "sr": 1,
                "ks": {
                    "o": {"a": 0, "k": 100}, "r": {"a": 0, "k": 0},
                    "p": {"a": 0, "k": [256, 256, 0]},
                    "a": {"a": 0, "k": [0, 0, 0]},
                    "s": {"a": 0, "k": [100, 100, 100]},
                },
                "ao": 0,
                "shapes": [
                    {"ty": "el", "p": {"a": 0, "k": [0, 0]}, "s": {"a": 0, "k": [256, 256]}, "nm": "Ellipse"},
                    {"ty": "fl", "c": {"a": 0, "k": [1, 0, 0, 1]}, "o": {"a": 0, "k": 100}, "r": 1, "nm": "Fill"},
                ],
                "ip": 0, "op": 2, "st": 0, "bm": 0,
            }],
            "tgs": 1,
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "test.tgs"
            destination = Path(directory) / "test.mov"
            with gzip.open(source, "wt", encoding="utf-8") as stream:
                json.dump(animation, stream)
            asyncio.run(render_tgs_to_mov(source, destination, 64, "#FFFFFF"))
            info = asyncio.run(probe_video(destination))
            self.assertEqual((info.width, info.height), (64, 64))
            self.assertEqual(info.codec, "prores")
            self.assertTrue(info.has_alpha)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_batch_keeps_only_final_files_and_cleans_temporary_job(self):
        class FakeStatus:
            def __init__(self):
                self.edits = []

            async def edit_text(self, text):
                self.edits.append(text)

        class FakeMessage:
            def __init__(self):
                self.status = FakeStatus()
                self.documents = []

            async def answer(self, *args, **kwargs):
                return self.status

            async def answer_document(self, document):
                self.documents.append(document)

        class FakeState:
            def __init__(self, data):
                self.data = data
                self.cleared = False

            async def get_data(self):
                return self.data

            async def clear(self):
                self.cleared = True

        class FakeBot:
            async def download(self, file_id, destination):
                shutil.copyfile(file_id, destination)

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            temp_dir = root / "temp"
            destination = root / "exports"
            temp_dir.mkdir()
            destination.mkdir()
            source = root / "source.webp"
            asyncio.run(_run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=red@0.5:size=32x24,format=rgba",
                "-frames:v", "1", str(source),
            ]))
            configure_download_flow(Settings(
                bot_token="token",
                allowed_user_id=1,
                output_dir=root / "output",
                temp_dir=temp_dir,
                download_dir=destination,
            ))
            asset = StickerAsset(str(source), "unique", "🎨", None, "static", ".webp")
            state = FakeState({
                "download_root": str(destination),
                "download_assets": [asset.to_dict()],
            })
            message = FakeMessage()
            asyncio.run(_save_batch(message, state, FakeBot(), None, None))

            self.assertEqual([path.suffix for path in destination.iterdir()], [".png"])
            self.assertEqual(list(temp_dir.iterdir()), [])
            self.assertTrue(state.cleared)
            self.assertTrue(message.status.edits)
            self.assertEqual(len(message.documents), 1)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_static_webp_converts_to_png(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sticker.webp"
            destination = Path(directory) / "sticker.png"
            asyncio.run(_run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=red@0.5:size=32x24,format=rgba",
                "-frames:v", "1", str(source),
            ]))
            asyncio.run(convert_webp_to_png(source, destination))
            info = asyncio.run(probe_video(destination))
            self.assertEqual((info.width, info.height), (32, 24))
            self.assertTrue(info.has_alpha)

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg required")
    def test_video_webm_converts_to_prores_mov(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "sticker.webm"
            destination = Path(directory) / "sticker.mov"
            asyncio.run(_run([
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", "color=red@0.5:size=32x24:rate=10,format=rgba",
                "-t", "0.2", "-an", "-c:v", "libvpx-vp9", "-pix_fmt", "yuva420p",
                "-auto-alt-ref", "0", str(source),
            ]))
            asyncio.run(convert_webm_to_mov(source, destination))
            info = asyncio.run(probe_video(destination))
            self.assertEqual((info.width, info.height), (32, 24))
            self.assertEqual(info.codec, "prores")
            self.assertTrue(info.has_alpha)


if __name__ == "__main__":
    unittest.main()
