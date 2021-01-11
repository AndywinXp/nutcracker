#!/usr/bin/env python3

import itertools
import struct
import zlib
from dataclasses import dataclass, replace
from functools import partial
from operator import attrgetter
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Sequence, Tuple

from nutcracker.utils.fileio import read_file
from nutcracker.graphics import image, grid
from nutcracker.smush import anim
from nutcracker.smush.types import Chunk, Element
from nutcracker.smush.ahdr import AnimationHeader
from nutcracker.smush.fobj import unobj
from nutcracker.smush.preset import smush
from nutcracker.codex.codex import get_decoder
from nutcracker.graphics.frame import save_single_frame_image


def open_anim_file(path: str) -> Element:
    resource = read_file(path)

    it = itertools.count()

    def set_frame_id(
        parent: Optional[Element], chunk: Chunk, offset: int
    ) -> Dict[str, Any]:
        if chunk.tag != 'FRME':
            return {}
        return {'id': next(it)}

    return next(smush.map_chunks(resource, extra=set_frame_id))


def clip(lower: int, upper: int, value: int) -> int:
    return lower if value < lower else upper if value > upper else value


clip_byte = partial(clip, 0, 255)


def delta_color(org_color: int, delta_color: int) -> int:
    return clip_byte((129 * org_color + delta_color) // 128)


@dataclass(frozen=True)
class FrameGenCtx:
    palette: bytes
    screen: Tuple[image.ImagePosition, image.Matrix] = (
        image.ImagePosition(),
        (),
    )
    delta_pal: Sequence[int] = ()
    frame: Optional[Element] = None


def npal(ctx: FrameGenCtx, data: bytes) -> FrameGenCtx:
    return replace(ctx, palette=tuple(data))


def xpal(ctx: FrameGenCtx, data: bytes) -> FrameGenCtx:
    sub_size = len(data)

    if sub_size == 0x300 * 3 + 4:
        # print('LARGE XPAL', data[: 4])
        assert data[:4] == b'\00\00\00\02', (ctx.frame, data[:4])
        delta_pal = struct.unpack(f'<{0x300}h', data[4 : 4 + 2 * 0x300])
        palette = data[4 + 2 * 0x300 :]
        return replace(ctx, delta_pal=delta_pal, palette=palette)

    if sub_size == 6:
        # print('SMALL XPAL', data)
        assert data == b'\00\00\00\01\00\00', (ctx.frame, data)
        assert len(ctx.delta_pal) == 0x300
        assert len(ctx.palette) == 0x300
        palette = bytes(
            delta_color(pal, delta) for pal, delta in zip(ctx.palette, ctx.delta_pal)
        )
        return replace(ctx, palette=palette)

    assert False


def decode_frame_object(ctx: FrameGenCtx, data: bytes) -> FrameGenCtx:
    screen = convert_fobj(data)
    # im = save_single_frame_image(ctx.screen)
    # im.putpalette(ctx.palette)
    # im.save(f'out/FRME_{idx:05d}_{cidx:05d}.png')
    return replace(ctx, screen=screen)


def decode_compressed_frame_object(ctx: FrameGenCtx, data: bytes) -> FrameGenCtx:
    decompressed_size = struct.unpack('>I', data[:4])[0]
    data = zlib.decompress(data[4:])
    assert len(data) == decompressed_size
    return decode_frame_object(ctx, data)


def unsupported_frame_comp(ctx: FrameGenCtx, data: bytes) -> FrameGenCtx:
    # print(f'support for tag {tag} not implemented yet')
    return ctx


DECODE_FRAME_IMAGE = {
    'NPAL': npal,
    'XPAL': xpal,
    'ZFOB': decode_compressed_frame_object,
    'FOBJ': decode_frame_object,
}


def generate_frames(
    header: AnimationHeader,
    frames: Iterator[Element],
    parser: Mapping[str, Callable[[FrameGenCtx, bytes], FrameGenCtx]],
) -> Iterator[FrameGenCtx]:
    ctx = FrameGenCtx(header.palette)
    for frame in frames:
        ctx = replace(ctx, frame=frame)
        for comp in frame.children:
            ctx = DECODE_FRAME_IMAGE.get(comp.tag, unsupported_frame_comp)(
                ctx, comp.data
            )
        assert ctx.screen is not None
        yield ctx


def decode_nut(root: Element, output_dir: str) -> None:
    header, frames = anim.parse(root)
    os.makedirs(output_dir, exist_ok=True)
    chars = [ctx.screen for ctx in generate_frames(header, frames, DECODE_FRAME_IMAGE)]
    lchars = [(loc.x1, loc.y1, image.convert_to_pil_image(im)) for loc, im in chars]
    nchars = len(lchars)
    transparency = 39
    BGS = [b'\05', b'\04']
    bim = grid.create_char_grid(
        nchars, enumerate(lchars), transparency=transparency, bgs=BGS
    )
    palette = list(header.palette)
    palette[3 * transparency : 3 * transparency + 3] = [109, 109, 109]
    bim.putpalette(palette)
    bim.save(os.path.join(output_dir, 'chars.png'))


def decode_san(root: Element, output_dir: str) -> None:
    header, frames = anim.parse(root)
    os.makedirs(output_dir, exist_ok=True)
    for idx, ctx in enumerate(generate_frames(header, frames, DECODE_FRAME_IMAGE)):
        if ctx.screen:
            assert ctx.palette
            im = save_single_frame_image(ctx.screen)
            # im = im.crop(box=(0,0,320,200))
            im.putpalette(ctx.palette)
            im.save(os.path.join(output_dir, f'FRME_{idx:05d}.png'))


def convert_fobj(datam: bytes) -> Optional[Tuple[image.ImagePosition, bytes]]:
    meta, data = attrgetter('header', 'data')(unobj(datam))
    width = meta.x2 - meta.x1 if meta.codec != 1 else meta.x2
    height = meta.y2 - meta.y1 if meta.codec != 1 else meta.y2
    decode = get_decoder(meta.codec)
    if decode == NotImplemented:
        print(f"Codec not implemented: {meta.codec}")
        return None

    # assert len(datam) % 2 == 0, (basename, meta['codec'])

    if meta.x1 != 0 or meta.y1 != 0:
        print('TELL ME')

    print(meta)

    locs = image.ImagePosition(x1=meta.x1, y1=meta.y1, x2=meta.x2, y2=meta.y2)
    return locs, decode(width, height, data)


if __name__ == '__main__':
    import argparse
    import glob
    import os

    from nutcracker.utils.funcutils import flatten

    parser = argparse.ArgumentParser(description='read smush file')
    parser.add_argument('files', nargs='+', help='files to read from')
    parser.add_argument('--map', '-m', action='store_true')
    parser.add_argument('--nut', '-n', action='store_true')
    parser.add_argument('--target', '-t', help='target directory', default='out')
    args = parser.parse_args()

    files = set(flatten(glob.iglob(r) for r in args.files))

    for filename in files:
        basename = os.path.basename(filename)
        print(f'Decoding file: {basename}')
        root = open_anim_file(filename)
        if args.map:
            smush.render(root)
            continue

        output_dir = os.path.join(args.target, basename)
        if not args.nut:
            decode_san(root, output_dir)
        else:
            decode_nut(root, output_dir)
