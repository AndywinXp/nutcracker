#!/usr/bin/env python3

import io
import logging
import os
from contextlib import contextmanager
from typing import Sequence, Optional, Iterator
from dataclasses import dataclass

from parse import parse

from .resource import read_chunks, keep_position
from .stream import StreamView

@dataclass
class Element:
    tag: str
    attribs: dict
    children: Sequence['Element']
    _stream: StreamView

    def read(self, *args, **kwargs) -> bytes:
        self._stream.seek(0)
        return self._stream.read(*args, **kwargs)

def findall(tag: str, root: Optional[Element]) -> Iterator[Element]:
    if not root:
        return
    for c in root.children:
        if parse(tag, c.tag, evaluate_result=False):
            yield c

def find(tag: str, root: Optional[Element]) -> Optional[Element]:
    return next(findall(tag, root), None)

def findpath(path: str, root: Optional[Element]) -> Optional[Element]:
    path = os.path.normpath(path)
    if not path or path == '.':
        return root
    dirname, basename = os.path.split(path)
    return find(basename, findpath(dirname, root))

def render(element, level=0):
    if not element:
        return
    attribs = ''.join(f' {key}="{value}"' for key, value in element.attribs.items() if value is not None)
    indent = '    ' * level
    closing = '' if element.children else ' /'
    print(f'{indent}<{element.tag}{attribs}{closing}>')
    if element.children:
        for c in element.children:
            render(c, level=level + 1)
        print(f'{indent}</{element.tag}>')

class MissingSchemaKey(Exception):
    def __init__(self, tag):
        super().__init__(f'Missing key in schema: {tag}')
        self.tag = tag

class MissingSchemaEntry(Exception):
    def __init__(self, ptag, tag):
        super().__init__(f'Missing entry for {tag} in {ptag} schema')
        self.ptag = ptag
        self.tag = tag

@contextmanager
def exception_ptag_context(ptag):
    try:
        yield
    except Exception as e:
        if not hasattr(e, 'ptag'):
            e.ptag = ptag
        raise e

@contextmanager
def schema_check(schema, ptag, tag, strict=False, logger=logging):
    try:
        if ptag and tag not in schema[ptag]:
            raise MissingSchemaEntry(ptag, tag)
        if tag not in schema:
            raise MissingSchemaKey(tag)
    except (MissingSchemaKey, MissingSchemaEntry) as e:
        if strict:
            raise e
        else:
            logger.warning(e)
    finally:
        yield

def create_element(schema, tag, offset, data, idgen, pid, **kwargs):
    gid = idgen.get(tag)
    with keep_position(data):
        gid = gid and gid(pid, data, offset)
    return Element(
        tag,
        {'offset': offset, 'size': len(data), 'gid': gid and f'{gid:04d}'},
        list(map_chunks(data, schema=schema, ptag=tag, idgen=idgen, pid=gid, **kwargs)) if schema.get(tag) else [],
        data
    )

def map_chunks(data, schema=None, ptag=None, strict=False, idgen=None, pid=None, **kwargs):
    schema = schema or {}
    idgen = idgen or {}
    chunks = read_chunks(data, **kwargs)
    with exception_ptag_context(ptag):
        for hoff, (tag, chunk) in chunks:
            with schema_check(schema, ptag, tag, strict=strict):
                yield create_element(schema, tag, hoff, chunk, strict=strict, idgen=idgen, pid=pid, **kwargs)

def generate_schema(data, **kwargs):
    schema = {}
    DATA = frozenset()
    DUMMY = frozenset({10})
    pos = data.tell()  # TODO: check if partial iterations are possible
    while True:
        data.seek(pos, io.SEEK_SET)
        try:
            for _ in map_chunks(data, strict=True, schema=schema, ptag=None, **kwargs):
                pass
            return {ptag: set(tags) for ptag, tags in schema.items() if tags != DUMMY}
        except MissingSchemaKey as miss:
            schema[miss.tag] = DUMMY  # creates new copy
        except MissingSchemaEntry as miss:
            schema[miss.ptag] -= DUMMY
            schema[miss.ptag] |= {miss.tag}
        except Exception as e:
            # pylint: disable=no-member
            assert hasattr(e, 'ptag')
            if schema.get(e.ptag) == DATA:
                raise ValueError('Cannot create schema for given file with given configuration')
            schema[e.ptag] = DATA

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='read smush file')
    parser.add_argument('filename', help='filename to read from')
    args = parser.parse_args()

    with open(args.filename, 'rb') as res:
        root = map_chunks(res)
        for t in root:
            render(t)
