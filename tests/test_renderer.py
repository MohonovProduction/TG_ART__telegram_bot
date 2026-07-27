import asyncio
import unittest
from pathlib import Path

from app.renderer import (
    RenderError,
    VideoInfo,
    _parse_fps,
    _tile_bounds,
    prepare_sticker_files,
    prepare_video_note,
)
from app.media import append_title_suffix, detect_source_kind
from app.sticker_pack import (
    build_tg_art_grid,
    make_sticker_set_name,
    parse_custom_emoji_pack_name,
    validate_pack_emoji,
    validate_pack_title,
)


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


if __name__ == "__main__":
    unittest.main()
