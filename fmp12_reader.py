"""Read-only reader/exporter for FileMaker fp3, fp5, fp7, and fmp12 files.

The format notes and reference behavior are documented by Evan Miller's
fmptools project (MIT licensed): https://github.com/evanmiller/fmptools
This module is a small Python implementation aimed at inspecting table data.
"""

from __future__ import annotations

import hashlib
import json
import mmap
import os
import sqlite3
import struct
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator


ProgressCallback = Callable[[str, int, int], None]

MAGIC = bytes.fromhex("00 01 00 00 00 02 00 01 00 05 00 02 00 02 C0")
SECTOR_SIZE = 4096
SECTOR_HEADER_SIZE = 20
LEGACY_SECTOR_SIZE = 1024
LEGACY_SECTOR_HEADER_SIZE = 14
REPETITION_SEPARATOR = "\x1d"
SUPPORTED_EXTENSIONS = {".fp3", ".fp5", ".fp7", ".fmp12"}


class FMPError(Exception):
    """Raised when a file cannot be read safely or consistently."""


@dataclass(slots=True)
class BlockRef:
    block_id: int
    level: int
    previous_id: int
    next_id: int
    payload_length: int | None = None


@dataclass(slots=True)
class Chunk:
    code: int
    kind: str
    data: bytes = b""
    simple_key: int | None = None
    long_key: bytes = b""
    segment_index: int | None = None
    path: tuple[bytes, ...] = ()
    legacy: bool = False


@dataclass(slots=True)
class FieldInfo:
    index: int
    name: str
    export_name: str = ""
    type_name: str = "Non specificato"


@dataclass(slots=True)
class TableInfo:
    index: int
    name: str
    fields: list[FieldInfo] = field(default_factory=list)
    export_name: str = ""
    order_field: str = "__fmp_record_order"
    record_count: int = 0
    data_file: str = ""
    row_offsets: list[int] = field(default_factory=list)
    index_stride: int = 256


def _read_uint(data: bytes) -> int:
    if not data:
        return 0
    return int.from_bytes(data, "big")


def _path_value(component: bytes, legacy: bool = False) -> int:
    if len(component) == 1:
        return component[0]
    if len(component) == 2:
        return 0x80 + ((component[0] & 0x7F) << 8) + component[1]
    if len(component) == 3:
        if legacy:
            return 0xC000 + ((component[0] & 0x3F) << 16) + (component[1] << 8) + component[2]
        return 0x80 + (component[1] << 8) + component[2]
    return 0


def _path_values(path: tuple[bytes, ...], legacy: bool = False) -> tuple[int, ...]:
    return tuple(_path_value(component, legacy) for component in path)


def _offset_table(value: int) -> int:
    if 0 < value < 0x68:
        return value * 0x80
    if value < 0xA8:
        return value * 0x80 + 0xAC00
    if value == 0xF9:
        return 0xC0
    if value == 0xFA:
        return 0x0250
    if value == 0xFB:
        return 0x0370
    if value == 0xFC:
        return 0x0530
    if value == 0xFD:
        return 0x3040
    if value == 0xFE:
        return 0x30A0
    if value == 0xFF:
        return 0xFF60
    return 0


def _extended_offset(high: int, low: int) -> int:
    return 0x10000 + 0x80 * (((high & 0x1F) << 8) + low)


def decode_scsu(data: bytes) -> str:
    """Decode FileMaker's SCSU text, including the common Unicode-mode forms."""
    static_windows = (0x0000, 0x0080, 0x0100, 0x0300, 0x2000, 0x2080, 0x2100, 0x3000)
    dynamic_windows = [0x0080, 0x00C0, 0x0400, 0x0600, 0x0900, 0x3040, 0x30A0, 0xFF00]
    pos = 0
    active = 0
    shift_window: int | None = None
    unicode_mode = False
    high_surrogate: int | None = None
    last_codepoint: int | None = None
    output: list[str] = []

    def read_u16() -> int | None:
        nonlocal pos
        if pos + 2 > len(data):
            pos = len(data)
            return None
        value = (data[pos] << 8) | data[pos + 1]
        pos += 2
        return value

    def emit(codepoint: int) -> None:
        nonlocal high_surrogate, last_codepoint
        if 0xDC00 <= codepoint <= 0xDFFF:
            if high_surrogate is not None:
                codepoint = 0x10000 + (high_surrogate << 10) + (codepoint & 0x3FF)
            else:
                codepoint = 0xFFFD
            high_surrogate = None
        elif 0xD800 <= codepoint <= 0xDBFF:
            if high_surrogate is not None:
                output.append("\uFFFD")
            high_surrogate = codepoint & 0x3FF
            last_codepoint = codepoint
            return
        elif high_surrogate is not None:
            output.append("\uFFFD")
            high_surrogate = None
        if codepoint > 0x10FFFF:
            codepoint = 0xFFFD
        output.append(chr(codepoint) if codepoint else "")
        last_codepoint = codepoint

    while pos < len(data):
        char = data[pos]
        pos += 1
        codepoint = 0

        if unicode_mode:
            if char == 0xF0:
                quoted = read_u16()
                if quoted is None:
                    emit(0xFFFD)
                else:
                    codepoint = quoted
            elif 0xE0 <= char <= 0xE7:
                active = char - 0xE0
                unicode_mode = False
                continue
            elif 0xE8 <= char <= 0xEF:
                if pos >= len(data):
                    emit(0xFFFD)
                    break
                active = char - 0xE8
                dynamic_windows[active] = _offset_table(data[pos])
                pos += 1
                unicode_mode = False
                continue
            elif char == 0xF1:
                if pos + 2 > len(data):
                    emit(0xFFFD)
                    break
                active = (data[pos] & 0xE0) >> 5
                dynamic_windows[active] = _extended_offset(data[pos], data[pos + 1])
                pos += 2
                unicode_mode = False
                continue
            else:
                if pos >= len(data):
                    emit(0xFFFD)
                    break
                codepoint = (char << 8) | data[pos]
                pos += 1
        elif shift_window is not None:
            window = shift_window - 1
            if char < 0x80:
                codepoint = static_windows[window] + char
            else:
                codepoint = dynamic_windows[window] + (char - 0x80)
            shift_window = None
        elif char == 0x0F:  # SCU
            unicode_mode = True
            continue
        elif char == 0x0E:  # SQU
            quoted = read_u16()
            if quoted is None:
                emit(0xFFFD)
            else:
                codepoint = quoted
        elif 0x01 <= char <= 0x08:  # SQ0..SQ7
            shift_window = char
            continue
        elif 0x10 <= char <= 0x17:  # SC0..SC7
            active = char - 0x10
            continue
        elif 0x18 <= char <= 0x1F:  # SD0..SD7
            if pos >= len(data):
                emit(0xFFFD)
                break
            active = char - 0x18
            dynamic_windows[active] = _offset_table(data[pos])
            pos += 1
            continue
        elif char == 0x0B:  # SDX
            if pos + 2 > len(data):
                emit(0xFFFD)
                break
            active = (data[pos] & 0xE0) >> 5
            dynamic_windows[active] = _extended_offset(data[pos], data[pos + 1])
            pos += 2
            continue
        elif char == 0x09:
            codepoint = ord(" ")
        elif char == 0x0A and last_codepoint == 0x0D:
            last_codepoint = char
            continue
        elif char in (0x0A, 0x0B, 0x0D):
            codepoint = ord("\n")
        elif 0x20 <= char <= 0x7F:
            codepoint = char
        elif char >= 0x80:
            codepoint = dynamic_windows[active] + (char - 0x80)
        else:
            codepoint = 0xFFFD

        emit(codepoint)

    if high_surrogate is not None:
        output.append("\uFFFD")
    return "".join(output)


def decode_fmp_text(data: bytes) -> str:
    decoded = bytes(value ^ 0x5A for value in data)
    return decode_scsu(decoded.lstrip(b" "))


def decode_legacy_text(data: bytes) -> str:
    """Decode fp3/fp5 text, which uses the creator machine's legacy encoding."""
    return data.lstrip(b" ").decode("mac_roman", errors="replace")


def _parse_legacy_payload(payload: bytes | memoryview, block_id: int) -> Iterator[Chunk]:
    """Parse the fp3/fp5 byte codes described by fmptools/HACKING."""
    pos = 0

    def take(length: int) -> bytes:
        nonlocal pos
        if length < 0 or pos + length > len(payload):
            raise FMPError(
                f"Settore {block_id}: chunk fp3/fp5 oltre il limite "
                f"(offset {pos}, lunghezza {length})."
            )
        value = bytes(payload[pos : pos + length])
        pos += length
        return value

    while pos < len(payload):
        offset = pos
        code = take(1)[0]

        if code == 0x00:
            length = take(1)[0]
            chunk = Chunk(code, "simple_ref", take(length), simple_key=0, legacy=True)
        elif code == 0x01 and pos + 2 < len(payload) and payload[pos : pos + 2] == b"\xff\x05":
            # A known eight-byte bookkeeping sequence in some fp5 files.
            take(7)
            continue
        elif 0x01 <= code <= 0x3F:
            key_length = take(1)[0]
            key = take(key_length)
            value_length = take(1)[0]
            value = take(value_length)
            if key_length == 1:
                chunk = Chunk(code, "simple_ref", value, simple_key=key[0], legacy=True)
            else:
                chunk = Chunk(code, "long_ref", value, long_key=key, legacy=True)
        elif 0x40 <= code <= 0x7F:
            key = code - 0x40
            value_length = take(1)[0]
            chunk = Chunk(code, "simple_ref", take(value_length), simple_key=key, legacy=True)
        elif 0x80 <= code <= 0xBF:
            chunk = Chunk(code, "data", take(code - 0x80), legacy=True)
        elif code == 0xC0:
            chunk = Chunk(code, "pop", legacy=True)
        elif 0xC1 <= code <= 0xFE:
            chunk = Chunk(code, "push", take(code - 0xC0), legacy=True)
        elif code == 0xFF:
            kind = take(1)[0]
            if 1 <= kind <= 4:
                key = take(kind)
                value_length = _read_uint(take(2))
                value = take(value_length)
                if kind == 1:
                    chunk = Chunk(code, "simple_ref", value, simple_key=key[0], legacy=True)
                else:
                    chunk = Chunk(code, "long_ref", value, long_key=key, legacy=True)
            elif 0x40 <= kind <= 0x80:
                key = kind - 0x40
                value_length = _read_uint(take(2))
                chunk = Chunk(code, "simple_ref", take(value_length), simple_key=key, legacy=True)
            else:
                raise FMPError(
                    f"Settore {block_id}: codice fp3/fp5 non riconosciuto "
                    f"0xFF 0x{kind:02X} all'offset {offset}."
                )
        else:
            raise FMPError(
                f"Settore {block_id}: codice fp3/fp5 non riconosciuto "
                f"0x{code:02X} all'offset {offset}."
            )
        yield chunk


def _parse_payload(payload: bytes | memoryview, block_id: int) -> Iterator[Chunk]:
    pos = 0
    delayed_pops = 0

    def take(length: int) -> bytes:
        nonlocal pos
        if length < 0 or pos + length > len(payload):
            raise FMPError(
                f"Settore {block_id}: dati oltre il limite durante la lettura di un chunk "
                f"(offset {pos}, lunghezza {length})."
            )
        data = bytes(payload[pos : pos + length])
        pos += length
        return data

    while pos < len(payload):
        offset = pos
        original_code = take(1)[0]
        code = original_code
        if code & 0xC0 == 0xC0:
            code &= 0x3F
            delayed_pops += 1

        chunk: Chunk | None = None
        if code == 0x00:
            if pos >= len(payload) or payload[pos] == 0:
                break
            chunk = Chunk(code, "data", take(1))
        elif 0x01 <= code <= 0x05:
            key = take(1)[0]
            value_len = (1 if code == 0x01 else 0) + 2 * (code - 0x01)
            chunk = Chunk(code, "simple_ref", take(value_len), simple_key=key)
        elif code == 0x06:
            key = take(1)[0]
            value_len = take(1)[0]
            chunk = Chunk(code, "simple_ref", take(value_len), simple_key=key)
        elif code == 0x07:
            segment = take(1)[0]
            value_len = _read_uint(take(2))
            chunk = Chunk(code, "segment", take(value_len), segment_index=segment)
        elif code == 0x08:
            chunk = Chunk(code, "data", take(2))
        elif code == 0x0E and pos < len(payload) and payload[pos] == 0xFF:
            # The 0xFF byte is included in the six-byte payload by the
            # reference parser; consuming it separately shifts every later offset.
            chunk = Chunk(code, "data", take(6))
        elif 0x09 <= code <= 0x0D:
            key_bytes = take(2)
            key = 0x80 + ((key_bytes[0] & 0x7F) << 8) + key_bytes[1]
            value_len = (1 if code == 0x09 else 0) + 2 * (code - 0x09)
            chunk = Chunk(code, "simple_ref", take(value_len), simple_key=key)
        elif code == 0x0E:
            key_bytes = take(2)
            key = 0x80 + ((key_bytes[0] & 0x7F) << 8) + key_bytes[1]
            value_len = take(1)[0]
            chunk = Chunk(code, "simple_ref", take(value_len), simple_key=key)
        elif code == 0x0F:
            key_bytes = take(2)
            segment = 0x80 + ((key_bytes[0] & 0x7F) << 8) + key_bytes[1]
            value_len = _read_uint(take(2))
            chunk = Chunk(code, "segment", take(value_len), segment_index=segment)
        elif code == 0x10:
            chunk = Chunk(code, "data", take(3))
        elif 0x11 <= code <= 0x15:
            value_len = 3 + (1 if code == 0x11 else 0) + 2 * (code - 0x11)
            chunk = Chunk(code, "data", take(value_len))
        elif code == 0x16:
            key = take(3)
            value_len = take(1)[0]
            chunk = Chunk(code, "long_ref", take(value_len), long_key=key)
        elif code == 0x17:
            key = take(3)
            value_len = _read_uint(take(2))
            chunk = Chunk(code, "long_ref", take(value_len), long_key=key)
        elif code == 0x18:
            value_len = take(1)[0]
            chunk = Chunk(code, "data", take(value_len))
        elif code == 0x1B and pos + 1 < len(payload) and payload[pos] == 0:
            take(1)
            key = take(1)[0]
            chunk = Chunk(code, "simple_ref", take(4), simple_key=key)
        elif 0x19 <= code <= 0x1D:
            value_len = take(1)[0]
            data = take(value_len)
            trailing_len = (1 if code == 0x19 else 0) + 2 * (code - 0x19)
            take(trailing_len)
            chunk = Chunk(code, "data", data)
        elif code in (0x1E, 0x1F):
            key_len = take(1)[0]
            key = take(key_len)
            if code == 0x1E:
                value_len = take(1)[0]
            else:
                value_len = _read_uint(take(2))
            chunk = Chunk(code, "long_ref", take(value_len), long_key=key)
        elif code == 0x20:
            if pos < len(payload) and payload[pos] == 0xFE:
                take(1)
                chunk = Chunk(code, "push", take(8))
            else:
                chunk = Chunk(code, "push", take(1))
        elif code == 0x23:
            value_len = take(1)[0]
            chunk = Chunk(code, "data", take(value_len))
        elif code == 0x28:
            chunk = Chunk(code, "push", take(2))
        elif code == 0x30:
            chunk = Chunk(code, "push", take(3))
        elif code == 0x38:
            value_len = take(1)[0]
            chunk = Chunk(code, "push", take(value_len))
        elif code in (0x3D, 0x40):
            chunk = Chunk(code, "pop")
        elif code in (0x80, 0x88):
            chunk = None
        else:
            raise FMPError(
                f"Settore {block_id}: codice FileMaker non riconosciuto "
                f"0x{original_code:02X} all'offset {offset}."
            )

        if chunk is not None:
            yield chunk
        while delayed_pops:
            yield Chunk(0x40, "pop")
            delayed_pops -= 1


class FMP12Reader:
    """Indexes FileMaker sectors and streams legacy or modern leaf blocks."""

    def __init__(
        self,
        path: str | Path,
        progress: ProgressCallback | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.path = Path(path)
        self.progress = progress
        self.cancel = cancel
        self.version = 0
        self.format_name = ""
        self.legacy = False
        self.file_size = 0
        self.mtime_ns = 0
        self.blocks: list[BlockRef | None] = []
        self.leaf_chain: list[int] = []
        self._index_file()

    def _check_cancel(self) -> None:
        if self.cancel is not None and self.cancel.is_set():
            raise FMPError("Estrazione annullata.")

    def _report(self, phase: str, done: int, total: int) -> None:
        if self.progress is not None:
            self.progress(phase, done, total)

    def _index_file(self) -> None:
        try:
            stat = self.path.stat()
            self.file_size = stat.st_size
            self.mtime_ns = stat.st_mtime_ns
            extension = self.path.suffix.lower()
            if extension not in SUPPORTED_EXTENSIONS:
                raise FMPError("Estensioni supportate: .fp3, .fp5, .fp7 e .fmp12.")
            with self.path.open("rb") as stream:
                header = stream.read(1024)
                if len(header) != 1024 or header[:15] != MAGIC:
                    raise FMPError("Il file non ha un'intestazione FileMaker riconosciuta.")
                marker = header[15:20]
                if marker == b"HBAMe":
                    raise FMPError(
                        "Il file usa Encryption at Rest. Il lettore non può decifrarlo."
                    )
                if marker == b"HBAM7":
                    if extension in {".fp3", ".fp5"}:
                        raise FMPError(
                            "L'intestazione è di un file FP7/FMP12, ma l'estensione è "
                            f"{extension}. Rinomina il file con l'estensione corretta."
                        )
                    self.version = 12 if header[521] == 0x1E else 7
                    self.format_name = "FMP12" if self.version == 12 else "FP7"
                    self._index_modern(stream)
                else:
                    if extension in {".fp7", ".fmp12"}:
                        raise FMPError(
                            f"L'intestazione non corrisponde a un file {extension}."
                        )
                    self.legacy = True
                    self.version = 3 if extension == ".fp3" else 5
                    self.format_name = "FP3" if self.version == 3 else "FP5"
                    self._index_legacy(stream)

            after = self.path.stat()
            if after.st_size != self.file_size or after.st_mtime_ns != self.mtime_ns:
                raise FMPError(
                    "Il file è cambiato durante la lettura. Riprovare con una copia stabile."
                )
        except OSError as exc:
            raise FMPError(f"Impossibile leggere il file: {exc}") from exc

    def _index_modern(self, stream) -> None:
        if self.file_size % SECTOR_SIZE:
            raise FMPError("La dimensione del file non è multipla del settore FP7/FMP12.")
        sector_count = self.file_size // SECTOR_SIZE
        if sector_count < 3:
            raise FMPError("Il file è troppo corto per contenere settori dati.")

        self.blocks = [None] * sector_count
        final_id = 0
        stream.seek(SECTOR_SIZE)
        for block_id in range(1, sector_count):
            self._check_cancel()
            sector = stream.read(SECTOR_SIZE)
            if len(sector) != SECTOR_SIZE:
                raise FMPError(f"Settore {block_id}: lettura incompleta del file.")
            block = BlockRef(
                block_id,
                _read_uint(sector[12:14]),
                _read_uint(sector[4:8]),
                _read_uint(sector[8:12]),
            )
            self.blocks[block_id] = block
            if block_id == 1:
                final_id = block.next_id
            if block_id % 512 == 0:
                self._report("Indicizzazione settori", block_id, sector_count - 1)

        if final_id == 0 or final_id != sector_count - 1:
            raise FMPError("Il conteggio dei settori non coincide con l'intestazione.")
        self._report("Indicizzazione settori", sector_count - 1, sector_count - 1)
        first_leaf = next(
            (
                block_id
                for block_id in range(2, final_id + 1)
                if self.blocks[block_id] is not None
                and self.blocks[block_id].level == 0
                and self.blocks[block_id].previous_id == 0
            ),
            2,
        )
        self._follow_leaf_chain(first_leaf, final_id)

    def _index_legacy(self, stream) -> None:
        if self.file_size % LEGACY_SECTOR_SIZE:
            raise FMPError("La dimensione del file non è multipla del settore fp3/fp5.")
        sector_count = self.file_size // LEGACY_SECTOR_SIZE
        if sector_count < 4:
            raise FMPError("Il file è troppo corto per contenere settori fp3/fp5.")

        # fmptools treats the sector at physical index 2 as the initial/root
        # sector and follows the leaf chain beginning at logical block 2.
        stream.seek(2 * LEGACY_SECTOR_SIZE)
        first_sector = stream.read(LEGACY_SECTOR_SIZE)
        if len(first_sector) != LEGACY_SECTOR_SIZE:
            raise FMPError("Il settore iniziale fp3/fp5 è incompleto.")
        final_id = _read_uint(first_sector[6:10])
        if final_id == 0 or final_id + 2 != sector_count:
            raise FMPError(
                "Il conteggio dei settori fp3/fp5 non coincide con l'intestazione."
            )

        self.blocks = [None] * (final_id + 1)
        for block_id in range(1, final_id + 1):
            self._check_cancel()
            if block_id == 1:
                sector = first_sector
            else:
                physical_index = block_id + 1
                stream.seek(physical_index * LEGACY_SECTOR_SIZE)
                sector = stream.read(LEGACY_SECTOR_SIZE)
            if len(sector) != LEGACY_SECTOR_SIZE:
                raise FMPError(f"Settore fp3/fp5 {block_id}: lettura incompleta.")
            payload_length = _read_uint(sector[12:14])
            if payload_length > LEGACY_SECTOR_SIZE - LEGACY_SECTOR_HEADER_SIZE:
                raise FMPError(f"Settore fp3/fp5 {block_id}: lunghezza non valida.")
            self.blocks[block_id] = BlockRef(
                block_id,
                sector[1],
                _read_uint(sector[2:6]),
                _read_uint(sector[6:10]),
                payload_length,
            )
            if block_id % 512 == 0:
                self._report("Indicizzazione settori", block_id, final_id)
        self._report("Indicizzazione settori", final_id, final_id)
        self._follow_leaf_chain(2, final_id)

    def _follow_leaf_chain(self, first_leaf: int, final_id: int) -> None:
        visited: set[int] = set()
        block_id = first_leaf
        while block_id:
            self._check_cancel()
            if block_id <= 0 or block_id > final_id:
                raise FMPError(f"Il collegamento al settore {block_id} è fuori dal file.")
            if block_id in visited:
                raise FMPError("La catena dei settori contiene un ciclo.")
            visited.add(block_id)
            block = self.blocks[block_id]
            if block is None:
                raise FMPError(f"Settore {block_id} mancante.")
            self.leaf_chain.append(block_id)
            block_id = block.next_id
        if not self.leaf_chain:
            raise FMPError("Non sono stati trovati settori di dati.")

    def iter_chunks(self, phase: str) -> Iterator[Chunk]:
        try:
            with self.path.open("rb") as stream:
                with mmap.mmap(stream.fileno(), 0, access=mmap.ACCESS_READ) as mapped:
                    for number, block_id in enumerate(self.leaf_chain, 1):
                        self._check_cancel()
                        if self.legacy:
                            physical_index = block_id + 1
                            block = self.blocks[block_id]
                            assert block is not None and block.payload_length is not None
                            start = (
                                physical_index * LEGACY_SECTOR_SIZE
                                + LEGACY_SECTOR_HEADER_SIZE
                            )
                            end = start + block.payload_length
                            parser = _parse_legacy_payload
                        else:
                            start = block_id * SECTOR_SIZE + SECTOR_HEADER_SIZE
                            end = (block_id + 1) * SECTOR_SIZE
                            parser = _parse_payload
                        payload = memoryview(mapped)[start:end]
                        try:
                            path_stack: list[bytes] = []
                            for chunk in parser(payload, block_id):
                                if chunk.kind == "push":
                                    path_stack.append(chunk.data)
                                elif chunk.kind == "pop":
                                    if path_stack:
                                        path_stack.pop()
                                else:
                                    chunk.path = tuple(path_stack)
                                    chunk.legacy = self.legacy
                                    yield chunk
                        finally:
                            payload.release()
                        if number % 128 == 0 or number == len(self.leaf_chain):
                            self._report(phase, number, len(self.leaf_chain))
        except OSError as exc:
            raise FMPError(f"Impossibile rileggere il file: {exc}") from exc

    def discover_schema(self) -> list[TableInfo]:
        if self.legacy:
            return self._discover_legacy_schema()

        table_names: dict[int, str] = {}
        column_names: dict[int, dict[int, str]] = {}

        for chunk in self.iter_chunks("Lettura tabelle e campi"):
            path = chunk.path
            values = _path_values(path, chunk.legacy)

            if len(path) >= 4 and values[:3] == (3, 16, 5) and values[3] >= 129:
                table_index = values[3] - 128
                fragment = _name_fragment(chunk, 4)
                if fragment is not None:
                    first, raw = fragment
                    prior = "" if first else table_names.get(table_index, "")
                    table_names[table_index] = prior + decode_fmp_text(raw)
                continue

            if (
                len(path) >= 4
                and values[0] >= 129
                and values[1:3] == (3, 5)
            ):
                table_index = values[0] - 128
                column_index = values[3]
                if column_index <= 0 or column_index > 100_000:
                    continue
                fragment = _name_fragment(chunk, 4)
                if fragment is not None:
                    first, raw = fragment
                    names = column_names.setdefault(table_index, {})
                    prior = "" if first else names.get(column_index, "")
                    names[column_index] = prior + decode_fmp_text(raw)

        indexes = sorted(set(table_names) | set(column_names))
        if not indexes:
            raise FMPError(
                "Non ho trovato tabelle. Il file potrebbe usare una variante non supportata."
            )
        tables: list[TableInfo] = []
        for table_index in indexes:
            name = table_names.get(table_index, "").strip()
            if not name:
                name = f"Tabella_{table_index}"
            names = column_names.get(table_index, {})
            fields = [
                FieldInfo(index, names.get(index, "").strip() or f"Campo_{index}")
                for index in sorted(names)
            ]
            tables.append(TableInfo(table_index, name, fields))
        return tables

    def _discover_legacy_schema(self) -> list[TableInfo]:
        column_names: dict[int, str] = {}
        column_types: dict[int, str] = {}
        legacy_types = {
            0: "Sconosciuto",
            1: "Testo",
            2: "Numero",
            3: "Data",
            4: "Ora",
            5: "Contenitore",
            6: "Calcolo",
            7: "Riassunto",
            8: "Globale",
        }
        for chunk in self.iter_chunks("Lettura tabelle e campi"):
            values = _path_values(chunk.path, legacy=True)
            if len(values) != 3 or values[:2] != (3, 5):
                continue
            column_index = values[2]
            if not 0 < column_index <= 100_000 or chunk.kind != "simple_ref":
                continue
            if chunk.simple_key == 1:
                column_names[column_index] = decode_legacy_text(chunk.data).strip()
            elif chunk.simple_key == 2 and len(chunk.data) >= 2:
                column_types[column_index] = legacy_types.get(
                    chunk.data[1], "Sconosciuto"
                )

        if not column_names:
            raise FMPError(
                "Non ho trovato campi nel file fp3/fp5. Il file potrebbe usare "
                "una variante non supportata."
            )
        table_name = self.path.stem.strip() or self.format_name
        fields = [
            FieldInfo(
                index=index,
                name=name or f"Campo_{index}",
                type_name=column_types.get(index, "Sconosciuto"),
            )
            for index, name in sorted(column_names.items())
        ]
        return [TableInfo(1, table_name, fields)]


def _name_fragment(chunk: Chunk, node_depth: int) -> tuple[bool, bytes] | None:
    path = chunk.path
    if len(path) == node_depth:
        if chunk.kind == "simple_ref" and chunk.simple_key == 16:
            return True, chunk.data
        return None
    if (
        len(path) == node_depth + 1
        and _path_value(path[node_depth], chunk.legacy) == 16
    ):
        if chunk.kind == "simple_ref" and chunk.simple_key is not None:
            if chunk.simple_key >= 1:
                return chunk.simple_key == 1, chunk.data
        if chunk.kind == "segment" and chunk.segment_index is not None:
            if chunk.segment_index >= 1:
                return chunk.segment_index == 1, chunk.data
    return None


def _decode_column_key(key: int, key_len: int, column_count: int) -> tuple[int, int]:
    if 1 <= key <= column_count:
        return key, 1
    if key_len == 2 and key >= 0x80:
        candidate = (key - 0x80) >> 8
        repetition = (key - 0x80) & 0xFF
        if 1 <= candidate <= column_count and repetition >= 1:
            return candidate, repetition
    return 0, 1


def _key_length(chunk: Chunk) -> int:
    if chunk.kind == "segment":
        return 2 if chunk.code == 0x0F else 1
    return 2 if 0x09 <= chunk.code <= 0x0E else 1


class _ValueState:
    def __init__(self, table: TableInfo) -> None:
        self.table = table
        self.root = table.index + 128
        self.column_count = max((item.index for item in table.fields), default=0)
        self.current_row = 0
        self.last_row = 0
        self.last_column = 0
        self.last_repetition = 0
        self.have_value = False
        self.raw = bytearray()
        self.text_parts: list[str] = []

    def _finish_repetition(self) -> None:
        self.text_parts.append(decode_fmp_text(bytes(self.raw)))
        self.raw.clear()

    def _flush(self) -> tuple[int, int, str] | None:
        if not self.have_value:
            return None
        self._finish_repetition()
        result = (self.current_row, self.last_column, "".join(self.text_parts))
        self.have_value = False
        self.text_parts.clear()
        return result

    def consume(self, chunk: Chunk) -> tuple[int, int, str] | None:
        path = chunk.path
        values = _path_values(path)
        column_index = 0
        repetition = 1
        long_value = False

        if (
            len(path) == 4
            and values[0] == self.root
            and values[1] == 5
        ):
            row = values[2]
            column_key = values[3]
            column_index, repetition = _decode_column_key(
                column_key, len(path[3]), self.column_count
            )
            if column_index == 0:
                return None
            if self.last_column == 0 or column_index < self.last_column:
                if row <= self.last_row:
                    return None
            elif row != self.last_row:
                return None
            if chunk.kind == "simple_ref" and chunk.simple_key == 0:
                return None
            long_value = True
        elif (
            len(path) == 3
            and values[0] == self.root
            and values[1] == 5
        ):
            row = values[2]
            if chunk.kind == "simple_ref" and chunk.simple_key is not None:
                if chunk.simple_key == 252:
                    return None
                column_index, repetition = _decode_column_key(
                    chunk.simple_key, _key_length(chunk), self.column_count
                )
            elif chunk.kind == "segment" and chunk.segment_index is not None:
                column_index, repetition = _decode_column_key(
                    chunk.segment_index, _key_length(chunk), self.column_count
                )
            else:
                return None
        else:
            return None

        if column_index == 0:
            return None

        event = None
        if self.have_value and (
            column_index != self.last_column or row != self.last_row
        ):
            event = self._flush()
        elif self.have_value and repetition != self.last_repetition:
            self._finish_repetition()
            gap = repetition - self.last_repetition if repetition > self.last_repetition else 1
            self.text_parts.extend(REPETITION_SEPARATOR for _ in range(gap))
        elif self.have_value and not long_value:
            event = self._flush()

        if row != self.last_row or column_index < self.last_column:
            self.current_row += 1
        if not self.have_value:
            self.text_parts.extend(REPETITION_SEPARATOR for _ in range(repetition - 1))
            self.have_value = True

        self.raw.extend(chunk.data)
        self.last_row = row
        self.last_column = column_index
        self.last_repetition = repetition
        return event


class _LegacyValueState:
    """Assemble fp3/fp5 values from their single-table path layout."""

    def __init__(self, table: TableInfo) -> None:
        self.table = table
        self.column_count = max((item.index for item in table.fields), default=0)
        self.current_row = 0
        self.last_row = 0
        self.last_column = 0
        self.have_value = False
        self.raw = bytearray()

    def _flush(self) -> tuple[int, int, str] | None:
        if not self.have_value:
            return None
        result = (
            self.current_row,
            self.last_column,
            decode_legacy_text(bytes(self.raw)),
        )
        self.raw.clear()
        self.have_value = False
        return result

    def consume(self, chunk: Chunk) -> tuple[int, int, str] | None:
        path = chunk.path
        values = _path_values(path, legacy=True)
        if len(path) == 2 and values[0] == 5:
            row = values[1]
            if chunk.kind != "simple_ref" or chunk.simple_key is None:
                return None
            if chunk.simple_key == 252:
                return None
            column = chunk.simple_key
            long_value = False
        elif len(path) == 3 and values[0] == 5:
            row = values[1]
            column = values[2]
            if chunk.kind == "simple_ref" and chunk.simple_key == 0:
                return None
            long_value = True
        else:
            return None

        if not 1 <= column <= self.column_count:
            return None
        if not any(field.index == column for field in self.table.fields):
            return None

        event = None
        if self.have_value and (column != self.last_column or row != self.last_row):
            event = self._flush()
        elif self.have_value and not long_value:
            event = self._flush()

        if row != self.last_row or column < self.last_column:
            self.current_row += 1
        if not self.have_value:
            self.have_value = True
        self.raw.extend(chunk.data)
        self.last_row = row
        self.last_column = column
        return event


def _iter_value_events(
    reader: FMP12Reader, tables: list[TableInfo]
) -> Iterator[tuple[int, int, int, str]]:
    if reader.legacy:
        if not tables:
            return
        table = tables[0]
        state = _LegacyValueState(table)
        for chunk in reader.iter_chunks("Estrazione record"):
            event = state.consume(chunk)
            if event is not None:
                row, column, value = event
                yield table.index, row, column, value
        event = state._flush()
        if event is not None:
            row, column, value = event
            yield table.index, row, column, value
        return

    by_index = {table.index: table for table in tables}
    states = {table.index: _ValueState(table) for table in tables}
    for chunk in reader.iter_chunks("Estrazione record"):
        if len(chunk.path) < 3:
            continue
        root = _path_value(chunk.path[0])
        table_index = root - 128
        state = states.get(table_index)
        if state is None:
            continue
        event = state.consume(chunk)
        if event is not None:
            row, column, value = event
            yield table_index, row, column, value
    for table_index, state in states.items():
        event = state._flush()
        if event is not None:
            row, column, value = event
            yield table_index, row, column, value


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _unique_name(value: str, used: set[str], fallback: str) -> str:
    clean = (value or fallback).replace("\x00", "\uFFFD").strip() or fallback
    if clean.casefold().startswith("sqlite_"):
        clean = "fmp_" + clean
    candidate = clean
    suffix = 2
    while candidate.casefold() in used:
        candidate = f"{clean}_{suffix}"
        suffix += 1
    used.add(candidate.casefold())
    return candidate


class _RowWriter:
    def __init__(
        self,
        connection: sqlite3.Connection,
        table: TableInfo,
        field_to_position: dict[int, int],
    ) -> None:
        self.connection = connection
        self.table = table
        self.field_to_position = field_to_position
        self.row_number: int | None = None
        self.values: dict[int, str] = {}
        field_sql = ", ".join(
            _quote_identifier(field.export_name) + " TEXT" for field in table.fields
        )
        if field_sql:
            field_sql = ", " + field_sql
        connection.execute(
            f"CREATE TABLE {_quote_identifier(table.export_name)} "
            f"({_quote_identifier(table.order_field)} INTEGER PRIMARY KEY{field_sql})"
        )
        columns = [table.order_field] + [field.export_name for field in table.fields]
        sql_columns = ", ".join(_quote_identifier(name) for name in columns)
        placeholders = ", ".join("?" for _ in columns)
        self.insert_sql = (
            f"INSERT INTO {_quote_identifier(table.export_name)} "
            f"({sql_columns}) VALUES ({placeholders})"
        )

    def add(self, row: int, column: int, value: str) -> None:
        if self.row_number is not None and row != self.row_number:
            self.flush()
        if self.row_number is None:
            self.row_number = row
            self.values = {}
        position = self.field_to_position.get(column)
        if position is not None:
            self.values[position] = value

    def flush(self) -> None:
        if self.row_number is None:
            return
        row = [self.row_number] + [None] * len(self.table.fields)
        for position, value in self.values.items():
            row[position + 1] = value
        self.connection.execute(self.insert_sql, row)
        self.table.record_count += 1
        self.row_number = None
        self.values = {}


JSON_INDEX_STRIDE = 256


class _JsonlWriter:
    """Write one table as JSON Lines and retain sparse byte offsets for paging."""

    def __init__(
        self,
        path: Path,
        table: TableInfo,
        field_to_position: dict[int, int],
    ) -> None:
        self.path = path
        self.table = table
        self.field_to_position = field_to_position
        self.file = path.open("wb")
        self.row_number: int | None = None
        self.values: dict[int, str] = {}
        self.row_offsets: list[int] = []

    def add(self, row: int, column: int, value: str) -> None:
        if self.row_number is not None and row != self.row_number:
            self.flush()
        if self.row_number is None:
            self.row_number = row
            self.values = {}
        position = self.field_to_position.get(column)
        if position is not None:
            self.values[position] = value

    def flush(self) -> None:
        if self.row_number is None:
            return
        values: list[str | None] = [None] * len(self.table.fields)
        for position, value in self.values.items():
            values[position] = value
        self.write_record(self.row_number, values)
        self.row_number = None
        self.values = {}

    def write_record(self, row_number: int, values: list[object]) -> None:
        if self.table.record_count % JSON_INDEX_STRIDE == 0:
            self.row_offsets.append(self.file.tell())
        encoded = json.dumps(
            [row_number, *values], ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        self.file.write(encoded + b"\n")
        self.table.record_count += 1

    def close(self) -> None:
        self.flush()
        self.file.flush()
        self.file.close()


def _prepare_json_tables(tables: list[TableInfo]) -> dict[int, dict[int, int]]:
    used_table_names: set[str] = set()
    field_positions: dict[int, dict[int, int]] = {}
    for table in tables:
        table.export_name = _unique_name(
            table.name, used_table_names, f"Tabella_{table.index}"
        )
        used_field_names: set[str] = set()
        table.order_field = _unique_name(
            "__fmp_record_order", used_field_names, "__fmp_record_order"
        )
        for field_info in table.fields:
            field_info.export_name = _unique_name(
                field_info.name, used_field_names, f"Campo_{field_info.index}"
            )
        field_positions[table.index] = {
            field_info.index: position
            for position, field_info in enumerate(table.fields)
        }
    return field_positions


def _commit_json_cache(
    source_path: Path,
    destination_path: Path,
    temporary_directory: Path,
    tables: list[TableInfo],
    writers: dict[int, _JsonlWriter],
    source_size: int,
    source_mtime_ns: int,
    format_version: int,
    source_format: str,
) -> Path:
    for writer in writers.values():
        if not writer.file.closed:
            writer.close()

    table_manifest: list[dict[str, object]] = []
    for table in tables:
        writer = writers[table.index]
        filename = f"{destination_path.stem}_table_{table.index}.jsonl"
        final_data_path = destination_path.parent / filename
        os.replace(writer.path, final_data_path)
        table.data_file = filename
        table.row_offsets = writer.row_offsets
        table.index_stride = JSON_INDEX_STRIDE
        table_manifest.append(
            {
                "index": table.index,
                "name": table.name,
                "export_name": table.export_name,
                "record_count": table.record_count,
                "order_field": table.order_field,
                "data_file": filename,
                "data_size": final_data_path.stat().st_size,
                "index_stride": table.index_stride,
                "row_offsets": table.row_offsets,
                "fields": [
                    {
                        "index": field_info.index,
                        "name": field_info.name,
                        "export_name": field_info.export_name,
                        "type_name": field_info.type_name,
                    }
                    for field_info in table.fields
                ],
            }
        )

    manifest = {
        "format": "fmp12-viewer-json-cache",
        "cache_version": 1,
        "source_path": str(source_path),
        "source_size": source_size,
        "source_mtime_ns": source_mtime_ns,
        "source_format": source_format,
        "format_version": format_version,
        "tables": table_manifest,
    }
    temporary_manifest = temporary_directory / "manifest.json"
    with temporary_manifest.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(manifest, stream, ensure_ascii=False, separators=(",", ":"))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary_manifest, destination_path)
    return destination_path


def export_to_json_cache(
    source: str | Path,
    destination: str | Path,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> tuple[Path, list[TableInfo]]:
    """Extract records to a reusable, paged JSON Lines cache."""
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    reader = FMP12Reader(source_path, progress, cancel)
    tables = reader.discover_schema()
    _check_cancel(cancel)
    field_positions = _prepare_json_tables(tables)

    with tempfile.TemporaryDirectory(
        prefix=destination_path.stem + ".partial-", dir=destination_path.parent
    ) as temporary_name:
        temporary_directory = Path(temporary_name)
        writers = {
            table.index: _JsonlWriter(
                temporary_directory / f"table_{table.index}.jsonl",
                table,
                field_positions[table.index],
            )
            for table in tables
        }
        try:
            if progress is not None:
                progress("Estrazione record", 0, len(reader.leaf_chain))
            for table_index, row, column, value in _iter_value_events(reader, tables):
                _check_cancel(cancel)
                writers[table_index].add(row, column, value)
            for writer in writers.values():
                writer.close()

            _check_cancel(cancel)
            final_stat = source_path.stat()
            if (
                final_stat.st_size != reader.file_size
                or final_stat.st_mtime_ns != reader.mtime_ns
            ):
                raise FMPError(
                    "Il file sorgente è cambiato durante l'estrazione; "
                    "il risultato è stato scartato."
                )
            result = _commit_json_cache(
                source_path,
                destination_path,
                temporary_directory,
                tables,
                writers,
                reader.file_size,
                reader.mtime_ns,
                reader.version,
                reader.format_name,
            )
        except Exception:
            for writer in writers.values():
                if not writer.file.closed:
                    writer.file.close()
            raise

    if progress is not None:
        progress("Completato", 1, 1)
    return result, tables


def export_to_sqlite(
    source: str | Path,
    destination: str | Path,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> tuple[Path, list[TableInfo]]:
    """Extract table rows into SQLite without modifying the source file."""
    source_path = Path(source).resolve()
    destination_path = Path(destination).resolve()
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    reader = FMP12Reader(source_path, progress, cancel)
    tables = reader.discover_schema()
    _check_cancel(cancel)

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=destination_path.name + ".", suffix=".partial", dir=destination_path.parent
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    try:
        with sqlite3.connect(temporary_path) as connection:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(
                "CREATE TABLE _fmp_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE _fmp_tables "
                "(table_index INTEGER PRIMARY KEY, fmp_name TEXT NOT NULL, "
                "export_name TEXT NOT NULL, record_count INTEGER NOT NULL DEFAULT 0, "
                "record_order_column TEXT NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE _fmp_schema "
                "(table_index INTEGER NOT NULL, field_index INTEGER NOT NULL, "
                "fmp_table_name TEXT NOT NULL, fmp_field_name TEXT NOT NULL, "
                "export_table_name TEXT NOT NULL, export_field_name TEXT NOT NULL, "
                "PRIMARY KEY (table_index, field_index))"
            )

            used_table_names: set[str] = set()
            writers: dict[int, _RowWriter] = {}
            field_positions: dict[int, dict[int, int]] = {}
            for table in tables:
                table.export_name = _unique_name(
                    table.name, used_table_names, f"Tabella_{table.index}"
                )
                used_field_names: set[str] = set()
                table.order_field = _unique_name(
                    "__fmp_record_order", used_field_names, "__fmp_record_order"
                )
                for field_info in table.fields:
                    field_info.export_name = _unique_name(
                        field_info.name, used_field_names, f"Campo_{field_info.index}"
                    )
                field_positions[table.index] = {
                    field_info.index: position
                    for position, field_info in enumerate(table.fields)
                }
                writers[table.index] = _RowWriter(
                    connection, table, field_positions[table.index]
                )
                connection.execute(
                    "INSERT INTO _fmp_tables "
                    "(table_index, fmp_name, export_name, record_order_column) "
                    "VALUES (?, ?, ?, ?)",
                    (table.index, table.name, table.export_name, table.order_field),
                )
                connection.executemany(
                    "INSERT INTO _fmp_schema "
                    "(table_index, field_index, fmp_table_name, fmp_field_name, "
                    "export_table_name, export_field_name) VALUES (?, ?, ?, ?, ?, ?)",
                    [
                        (
                            table.index,
                            field_info.index,
                            table.name,
                            field_info.name,
                            table.export_name,
                            field_info.export_name,
                        )
                        for field_info in table.fields
                    ],
                )

            if progress is not None:
                progress("Estrazione record", 0, len(reader.leaf_chain))
            for table_index, row, column, value in _iter_value_events(reader, tables):
                _check_cancel(cancel)
                writers[table_index].add(row, column, value)
            for writer in writers.values():
                writer.flush()

            connection.executemany(
                "UPDATE _fmp_tables SET record_count=? WHERE table_index=?",
                [(table.record_count, table.index) for table in tables],
            )
            stat = source_path.stat()
            metadata = {
                "source_path": str(source_path),
                "source_size": str(stat.st_size),
                "source_mtime_ns": str(stat.st_mtime_ns),
                "format": reader.format_name,
                "format_version": str(reader.version),
                "reader": "Python FMP12 Viewer",
            }
            connection.executemany(
                "INSERT INTO _fmp_metadata (key, value) VALUES (?, ?)",
                list(metadata.items()),
            )

        connection.close()
        _check_cancel(cancel)
        final_stat = source_path.stat()
        if final_stat.st_size != reader.file_size or final_stat.st_mtime_ns != reader.mtime_ns:
            raise FMPError(
                "Il file sorgente è cambiato durante l'estrazione; il risultato è stato scartato."
            )
        os.replace(temporary_path, destination_path)
        if progress is not None:
            progress("Completato", 1, 1)
        return destination_path, tables
    except Exception:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def legacy_cache_is_fresh(source: str | Path, database: str | Path) -> bool:
    source_path = Path(source).resolve()
    database_path = Path(database)
    try:
        stat = source_path.stat()
        with sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True) as connection:
            metadata = dict(
                connection.execute("SELECT key, value FROM _fmp_metadata").fetchall()
            )
        return (
            metadata.get("source_path") == str(source_path)
            and metadata.get("source_size") == str(stat.st_size)
            and metadata.get("source_mtime_ns") == str(stat.st_mtime_ns)
        )
    except (OSError, sqlite3.Error):
        return False


def _cache_identity(source: str | Path) -> tuple[Path, str, Path]:
    source_path = Path(source).resolve()
    stat = source_path.stat()
    identity = f"{source_path}|{stat.st_size}|{stat.st_mtime_ns}".encode("utf-8")
    digest = hashlib.sha256(identity).hexdigest()[:16]
    folder = Path(__file__).resolve().parent / "cache"
    folder.mkdir(parents=True, exist_ok=True)
    safe_stem = "".join(
        char if char.isalnum() or char in "-_." else "_" for char in source_path.stem
    )[:60]
    return source_path, f"{safe_stem}_{digest}", folder


def cache_path_for(source: str | Path) -> Path:
    _source_path, name, folder = _cache_identity(source)
    return folder / f"{name}.json"


def legacy_cache_path_for(source: str | Path) -> Path:
    _source_path, name, _folder = _cache_identity(source)
    local_app_data = Path(
        os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
    )
    folder = local_app_data / "FMP12Viewer" / "cache"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{name}.sqlite"


def cache_is_fresh(source: str | Path, manifest_path: str | Path) -> bool:
    source_path = Path(source).resolve()
    path = Path(manifest_path)
    try:
        stat = source_path.stat()
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not (
            manifest.get("format") == "fmp12-viewer-json-cache"
            and manifest.get("cache_version") == 1
            and manifest.get("source_path") == str(source_path)
            and manifest.get("source_size") == stat.st_size
            and manifest.get("source_mtime_ns") == stat.st_mtime_ns
            and isinstance(manifest.get("tables"), list)
        ):
            return False
        for table in manifest["tables"]:
            filename = table.get("data_file")
            if not isinstance(filename, str) or Path(filename).name != filename:
                return False
            record_count = table.get("record_count")
            index_stride = table.get("index_stride")
            row_offsets = table.get("row_offsets")
            if (
                not isinstance(record_count, int)
                or record_count < 0
                or not isinstance(index_stride, int)
                or index_stride < 1
                or not isinstance(row_offsets, list)
                or len(row_offsets) != (record_count + index_stride - 1) // index_stride
                or any(not isinstance(offset, int) or offset < 0 for offset in row_offsets)
            ):
                return False
            data_path = path.parent / filename
            if not data_path.is_file() or data_path.stat().st_size != table.get("data_size"):
                return False
        return True
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def load_catalogue(manifest_path: str | Path) -> list[TableInfo]:
    manifest_file = Path(manifest_path)
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        if manifest.get("format") != "fmp12-viewer-json-cache":
            raise FMPError("Il file di cache JSON non è riconosciuto.")
        tables: list[TableInfo] = []
        for row in manifest["tables"]:
            fields = [
                FieldInfo(
                    index=field_info["index"],
                    name=field_info["name"],
                    export_name=field_info.get("export_name", field_info["name"]),
                    type_name=field_info.get("type_name", "Non specificato"),
                )
                for field_info in row["fields"]
            ]
            tables.append(
                TableInfo(
                    index=row["index"],
                    name=row["name"],
                    export_name=row.get("export_name", row["name"]),
                    record_count=row["record_count"],
                    order_field=row.get("order_field", "__fmp_record_order"),
                    fields=fields,
                    data_file=row["data_file"],
                    row_offsets=row["row_offsets"],
                    index_stride=row["index_stride"],
                )
            )
        return tables
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise FMPError(f"Cache JSON non leggibile: {exc}") from exc


def read_json_page(
    manifest_path: str | Path,
    table: TableInfo,
    offset: int,
    page_size: int,
) -> list[list[object]]:
    manifest_file = Path(manifest_path)
    if not table.data_file or table.index_stride < 1:
        raise FMPError("Indice della cache JSON non valido.")
    checkpoint = offset // table.index_stride
    if checkpoint >= len(table.row_offsets):
        return []
    rows_to_skip = offset - checkpoint * table.index_stride
    data_path = manifest_file.parent / table.data_file
    rows: list[list[object]] = []
    with data_path.open("rb") as stream:
        stream.seek(table.row_offsets[checkpoint])
        for _ in range(rows_to_skip):
            if not stream.readline():
                return []
        for _ in range(page_size):
            line = stream.readline()
            if not line:
                break
            row = json.loads(line)
            if not isinstance(row, list):
                raise FMPError("Riga non valida nella cache JSON.")
            rows.append(row)
    return rows


def sort_index_path_for(
    manifest_path: str | Path, table: TableInfo, column_index: int
) -> Path:
    manifest_file = Path(manifest_path)
    app_data = Path(
        os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
    )
    folder = app_data / "FileMakerViewer" / "sort_indexes"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{manifest_file.stem}_t{table.index}_c{column_index + 1}.sqlite"


def sort_index_is_fresh(
    index_path: str | Path,
    manifest_path: str | Path,
    table: TableInfo,
    column_index: int,
) -> bool:
    index_file = Path(index_path)
    try:
        manifest_file = Path(manifest_path)
        data_path = manifest_file.parent / table.data_file
        with sqlite3.connect(
            f"file:{index_file.as_posix()}?mode=ro", uri=True
        ) as connection:
            metadata = dict(
                connection.execute("SELECT key, value FROM _fmp_sort_metadata").fetchall()
            )
        return (
            metadata.get("format") == "1"
            and metadata.get("manifest") == manifest_file.name
            and metadata.get("table_index") == str(table.index)
            and metadata.get("column_index") == str(column_index)
            and metadata.get("record_count") == str(table.record_count)
            and metadata.get("data_size") == str(data_path.stat().st_size)
        )
    except (OSError, sqlite3.Error):
        return False


def build_sort_index(
    manifest_path: str | Path,
    table: TableInfo,
    column_index: int,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    """Create a persistent disk-backed sort index for one table column."""
    manifest_file = Path(manifest_path).resolve()
    data_path = manifest_file.parent / table.data_file
    index_path = sort_index_path_for(manifest_file, table, column_index)
    if sort_index_is_fresh(index_path, manifest_file, table, column_index):
        return index_path
    if column_index < -1 or column_index >= len(table.fields):
        raise FMPError("La colonna selezionata non è valida.")

    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=index_path.name + ".", suffix=".partial", dir=index_path.parent
    )
    os.close(file_descriptor)
    temporary_path = Path(temporary_name)
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(temporary_path)
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute(
            "CREATE TABLE _fmp_sort_rows ("
            "record_number INTEGER PRIMARY KEY, data_offset INTEGER NOT NULL, "
            "is_empty INTEGER NOT NULL, text_key TEXT NOT NULL, numeric_key REAL)"
        )
        if progress is not None:
            progress("Lettura valori per ordinamento", 0, table.record_count)

        batch: list[tuple[int, int, int, str, float | None]] = []
        completed = 0
        non_empty = 0
        numeric_count = 0
        numeric_possible = True
        with data_path.open("rb") as source:
            while True:
                _check_cancel(cancel)
                data_offset = source.tell()
                line = source.readline()
                if not line:
                    break
                row = json.loads(line)
                if not isinstance(row, list) or not row:
                    raise FMPError("Riga non valida nella cache JSON.")
                record_number = int(row[0])
                value = (
                    record_number
                    if column_index == -1
                    else row[column_index + 1]
                    if column_index + 1 < len(row)
                    else None
                )
                text_value = "" if value is None else str(value)
                empty = value is None or not text_value.strip()
                numeric_value: float | None = None
                if not empty:
                    non_empty += 1
                    try:
                        numeric_value = float(text_value)
                        numeric_count += 1
                    except (TypeError, ValueError, OverflowError):
                        numeric_possible = False
                batch.append(
                    (
                        record_number,
                        data_offset,
                        int(empty),
                        text_value.casefold(),
                        numeric_value,
                    )
                )
                completed += 1
                if len(batch) >= 4096:
                    connection.executemany(
                        "INSERT INTO _fmp_sort_rows VALUES (?, ?, ?, ?, ?)", batch
                    )
                    batch.clear()
                if progress is not None and (
                    completed == 1 or completed % 8192 == 0
                ):
                    progress(
                        "Lettura valori per ordinamento", completed, table.record_count
                    )
        if batch:
            connection.executemany(
                "INSERT INTO _fmp_sort_rows VALUES (?, ?, ?, ?, ?)", batch
            )
        if completed != table.record_count:
            raise FMPError(
                "La cache contiene un numero di record diverso da quello atteso; "
                "ordinamento annullato."
            )
        connection.commit()
        _check_cancel(cancel)

        sort_kind = (
            "numeric"
            if numeric_possible and numeric_count == non_empty
            else "text"
        )
        if progress is not None:
            progress("Creazione indice", 0, 1)
        sort_key = "numeric_key" if sort_kind == "numeric" else "text_key"
        connection.execute(
            f"CREATE INDEX _fmp_sort_order ON _fmp_sort_rows "
            f"(is_empty, {sort_key}, record_number)"
        )
        connection.execute(
            "CREATE TABLE _fmp_sort_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        data_size = data_path.stat().st_size
        metadata = {
            "format": "1",
            "manifest": manifest_file.name,
            "table_index": str(table.index),
            "column_index": str(column_index),
            "record_count": str(table.record_count),
            "data_size": str(data_size),
            "sort_kind": sort_kind,
        }
        connection.executemany(
            "INSERT INTO _fmp_sort_metadata(key, value) VALUES (?, ?)",
            list(metadata.items()),
        )
        connection.commit()
        connection.close()
        connection = None
        _check_cancel(cancel)
        os.replace(temporary_path, index_path)
        if progress is not None:
            progress("Ordinamento pronto", 1, 1)
        return index_path
    except Exception as exc:
        if connection is not None:
            connection.close()
        temporary_path.unlink(missing_ok=True)
        if isinstance(exc, sqlite3.Error) and cancel is not None and cancel.is_set():
            raise FMPError("Ordinamento annullato.") from exc
        raise


def read_sorted_json_page(
    manifest_path: str | Path,
    table: TableInfo,
    index_path: str | Path,
    offset: int,
    page_size: int,
    descending: bool = False,
) -> list[list[object]]:
    manifest_file = Path(manifest_path)
    index_file = Path(index_path)
    try:
        with sqlite3.connect(
            f"file:{index_file.as_posix()}?mode=ro", uri=True
        ) as connection:
            metadata = dict(
                connection.execute(
                    "SELECT key, value FROM _fmp_sort_metadata"
                ).fetchall()
            )
            sort_key = (
                "numeric_key"
                if metadata.get("sort_kind") == "numeric"
                else "text_key"
            )
            direction = "DESC" if descending else "ASC"
            query = (
                "SELECT data_offset FROM _fmp_sort_rows "
                f"ORDER BY is_empty ASC, {sort_key} {direction}, record_number ASC "
                "LIMIT ? OFFSET ?"
            )
            offsets = [
                row[0] for row in connection.execute(query, (page_size, offset))
            ]
    except sqlite3.Error as exc:
        raise FMPError(f"Indice di ordinamento non leggibile: {exc}") from exc

    data_path = manifest_file.parent / table.data_file
    rows: list[list[object]] = []
    with data_path.open("rb") as source:
        for data_offset in offsets:
            source.seek(data_offset)
            row = json.loads(source.readline())
            if not isinstance(row, list):
                raise FMPError("Riga non valida nella cache JSON.")
            rows.append(row)
    return rows


def search_json_table(
    manifest_path: str | Path,
    table: TableInfo,
    query: str,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> tuple[Path, int]:
    """Search every field in a table and write matching JSONL offsets to a temp index."""
    needle = query.strip().casefold()
    if len(needle) < 3:
        raise FMPError("Inserisci almeno 3 caratteri per avviare la ricerca.")

    manifest_file = Path(manifest_path)
    data_path = manifest_file.parent / table.data_file
    app_data = Path(
        os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")
    )
    result_dir = app_data / "FileMakerViewer" / "search_results"
    result_dir.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f"{manifest_file.stem}_t{table.index}_", suffix=".partial", dir=result_dir
    )
    temporary_path = Path(temporary_name)
    result_path = temporary_path.with_suffix(".idx")
    matches = 0
    scanned = 0
    try:
        with os.fdopen(file_descriptor, "wb") as offsets_file, data_path.open("rb") as data:
            if progress is not None:
                progress("Ricerca nei record", 0, table.record_count)
            while True:
                _check_cancel(cancel)
                data_offset = data.tell()
                line = data.readline()
                if not line:
                    break
                row = json.loads(line)
                if not isinstance(row, list):
                    raise FMPError("Riga non valida nella cache JSON.")
                if any(
                    needle in str(value).casefold()
                    for value in row
                    if value is not None
                ):
                    offsets_file.write(struct.pack("<Q", data_offset))
                    matches += 1
                scanned += 1
                if progress is not None and (
                    scanned == 1 or scanned % 8192 == 0
                ):
                    progress("Ricerca nei record", scanned, table.record_count)
            if scanned != table.record_count:
                raise FMPError(
                    "La cache contiene un numero di record diverso da quello atteso."
                )
            offsets_file.flush()
            os.fsync(offsets_file.fileno())
        _check_cancel(cancel)
        os.replace(temporary_path, result_path)
        if progress is not None:
            progress("Ricerca completata", table.record_count, table.record_count)
        return result_path, matches
    except Exception:
        temporary_path.unlink(missing_ok=True)
        if result_path.exists():
            result_path.unlink(missing_ok=True)
        raise


def read_search_page(
    manifest_path: str | Path,
    table: TableInfo,
    result_path: str | Path,
    offset: int,
    page_size: int,
) -> list[list[object]]:
    result_file = Path(result_path)
    with result_file.open("rb") as offsets_file:
        offsets_file.seek(offset * 8)
        packed_offsets = offsets_file.read(page_size * 8)
    if len(packed_offsets) % 8:
        raise FMPError("Indice dei risultati di ricerca incompleto.")
    data_offsets = [
        struct.unpack_from("<Q", packed_offsets, position)[0]
        for position in range(0, len(packed_offsets), 8)
    ]

    data_path = Path(manifest_path).parent / table.data_file
    rows: list[list[object]] = []
    with data_path.open("rb") as data:
        for data_offset in data_offsets:
            data.seek(data_offset)
            row = json.loads(data.readline())
            if not isinstance(row, list):
                raise FMPError("Riga non valida nella cache JSON.")
            rows.append(row)
    return rows


def load_legacy_catalogue(database: str | Path) -> list[TableInfo]:
    database_path = Path(database)
    uri = f"file:{database_path.as_posix()}?mode=ro"
    with sqlite3.connect(uri, uri=True) as connection:
        table_rows = connection.execute(
            "SELECT table_index, fmp_name, export_name, record_count, "
            "record_order_column FROM _fmp_tables ORDER BY table_index"
        ).fetchall()
        fields_by_table: dict[int, list[FieldInfo]] = {}
        for row in connection.execute(
            "SELECT table_index, field_index, fmp_field_name, export_field_name "
            "FROM _fmp_schema ORDER BY table_index, field_index"
        ):
            fields_by_table.setdefault(row[0], []).append(
                FieldInfo(row[1], row[2], row[3])
            )
    return [
        TableInfo(
            index=row[0],
            name=row[1],
            export_name=row[2],
            record_count=row[3],
            order_field=row[4],
            fields=fields_by_table.get(row[0], []),
        )
        for row in table_rows
    ]


def convert_legacy_sqlite_cache(
    source: str | Path,
    database: str | Path,
    destination: str | Path,
    progress: ProgressCallback | None = None,
    cancel: threading.Event | None = None,
) -> tuple[Path, list[TableInfo]]:
    """Convert an earlier local SQLite extraction to JSON without rereading FMP12."""
    source_path = Path(source).resolve()
    database_path = Path(database).resolve()
    destination_path = Path(destination).resolve()
    source_stat = source_path.stat()
    tables = load_legacy_catalogue(database_path)
    for table in tables:
        table.record_count = 0

    database_uri = f"file:{database_path.as_posix()}?mode=ro"
    with sqlite3.connect(database_uri, uri=True) as connection:
        total_rows = sum(
            int(row[0])
            for row in connection.execute("SELECT record_count FROM _fmp_tables")
        )
        metadata = dict(
            connection.execute("SELECT key, value FROM _fmp_metadata").fetchall()
        )

    _prepare_json_tables(tables)
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=destination_path.stem + ".partial-", dir=destination_path.parent
    ) as temporary_name:
        temporary_directory = Path(temporary_name)
        writers = {
            table.index: _JsonlWriter(
                temporary_directory / f"table_{table.index}.jsonl", table, {}
            )
            for table in tables
        }
        completed = 0
        try:
            with sqlite3.connect(database_uri, uri=True) as connection:
                for table in tables:
                    _check_cancel(cancel)
                    fields_sql = ", ".join(
                        '"' + field_info.export_name.replace('"', '""') + '"'
                        for field_info in table.fields
                    )
                    if fields_sql:
                        fields_sql = ", " + fields_sql
                    order_sql = '"' + table.order_field.replace('"', '""') + '"'
                    table_sql = '"' + table.export_name.replace('"', '""') + '"'
                    query = (
                        f"SELECT {order_sql}{fields_sql} FROM {table_sql} "
                        f"ORDER BY {order_sql}"
                    )
                    for row in connection.execute(query):
                        _check_cancel(cancel)
                        writers[table.index].write_record(row[0], list(row[1:]))
                        completed += 1
                        if progress is not None and (
                            completed == 1 or completed % 2048 == 0
                        ):
                            progress("Conversione cache locale", completed, total_rows)
            for writer in writers.values():
                writer.close()
            current_stat = source_path.stat()
            if (
                current_stat.st_size != source_stat.st_size
                or current_stat.st_mtime_ns != source_stat.st_mtime_ns
            ):
                raise FMPError(
                    "Il file sorgente è cambiato durante la conversione della cache."
                )
            result = _commit_json_cache(
                source_path,
                destination_path,
                temporary_directory,
                tables,
                writers,
                source_stat.st_size,
                source_stat.st_mtime_ns,
                int(metadata.get("format_version", "0")),
                metadata.get("format", "FMP12"),
            )
        except Exception:
            for writer in writers.values():
                if not writer.file.closed:
                    writer.file.close()
            raise

    if progress is not None:
        progress("Completato", 1, 1)
    return result, tables


def _check_cancel(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise FMPError("Estrazione annullata.")
