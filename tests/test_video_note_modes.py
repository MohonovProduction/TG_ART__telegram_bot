import asyncio
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from app.renderer import prepare_video_note, probe_video


def command(args):
    return subprocess.run(args, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg is required')
class VideoNoteModesTest(unittest.IsolatedAsyncioTestCase):
    async def test_actual_encoding_positions_fit_backgrounds_fill_and_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wide = root / 'wide.mp4'
            tall = root / 'tall.mp4'
            await asyncio.to_thread(command, ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i', 'color=red:s=240x80:r=30:d=0.3,drawbox=x=80:y=0:w=80:h=80:color=green:t=fill,drawbox=x=160:y=0:w=80:h=80:color=blue:t=fill', '-f', 'lavfi', '-i', 'sine=frequency=440:duration=0.3', '-c:v', 'libx264', '-c:a', 'aac', '-shortest', str(wide)])
            await asyncio.to_thread(command, ['ffmpeg', '-v', 'error', '-y', '-i', str(wide), '-vf', 'transpose=1', '-c:v', 'libx264', '-c:a', 'copy', str(tall)])
            cases = [
                (wide, {'mode': 'cover', 'position': 'left'}, (255, 0, 0)),
                (wide, {'mode': 'cover', 'position': 'center'}, (0, 128, 0)),
                (wide, {'mode': 'cover', 'position': 'right'}, (0, 0, 255)),
                (tall, {'mode': 'cover', 'position': 'top'}, (255, 0, 0)),
                (tall, {'mode': 'cover', 'position': 'bottom'}, (0, 0, 255)),
                (wide, {'mode': 'fill'}, (0, 128, 0)),
                (wide, {'mode': 'fit', 'background': '#000000'}, (0, 128, 0)),
                (wide, {'mode': 'fit', 'background': '#FFFFFF'}, (0, 128, 0)),
                (wide, {'mode': 'fit', 'background': '#242424'}, (0, 128, 0)),
                (wide, {'mode': 'fit', 'background': 'blur'}, (0, 128, 0)),
            ]
            for source, options, expected in cases:
                with self.subTest(options=options):
                    result = await prepare_video_note(source, root, length=96, **options)
                    info = await probe_video(result.file)
                    self.assertEqual((info.width, info.height, info.codec), (96, 96, 'h264'))
                    data = await asyncio.to_thread(command, ['ffmpeg', '-v', 'error', '-i', str(result.file), '-frames:v', '1', '-f', 'rawvideo', '-pix_fmt', 'rgb24', 'pipe:1'])
                    def pixel(x, y):
                        offset = (y * 96 + x) * 3
                        return tuple(data[offset:offset + 3])
                    self.assertTrue(all(abs(a-b) < 25 for a,b in zip(pixel(48,48), expected)), pixel(48,48))
                    if options['mode'] == 'fit':
                        border = {'#000000': (0,0,0), '#FFFFFF': (255,255,255), '#242424': (36,36,36), 'blur': (0,128,0)}[options['background']]
                        self.assertTrue(all(abs(a-b) < 30 for a,b in zip(pixel(48,4), border)), pixel(48,4))
                    metadata = json.loads(await asyncio.to_thread(command, ['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(result.file)]))
                    self.assertTrue(any(stream['codec_type'] == 'audio' and stream['codec_name'] == 'aac' for stream in metadata['streams']))
