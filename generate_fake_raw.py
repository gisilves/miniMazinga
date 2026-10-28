"""FTA SCD Microstrip Raw Data - Channel Stripper.

Reads a binary raw data file with the same word structure as the live
UDP stream (EVENT_START / QUADDER_START / QUADDER_END markers) and writes
a new binary file that keeps only ONE quadder per event (by default, the
first, index 0). That quadder's channel (ADC) data words are replaced by
a single placeholder word, 0x0b00b1e2, while its first HEADER_WORDS words
(not part of the channel payload) are preserved as-is. Every other
quadder block - markers and data alike - is dropped entirely.

The event length word (LENGTH_WORD_INDEX words after EVENT_START, in bytes)
is corrected: each event is buffered, and its length is reduced by
4 bytes for every word removed.
"""

import argparse
import struct
import numpy as np
import random

EVENT_START = 0xfa4af1ca
QUADDER_START = 0xbaba1a9a
QUADDER_END = 0x0bedface

# Value written to the first word after QUADDER_START (quadder length)
QUADDER_LENGTH = 0xB

# Number of raw words at the start of a quadder block that are NOT channel data
HEADER_WORDS = 10

# Position of the event length word (in bytes), counted from EVENT_START
# (EVENT_START itself is index 0).
LENGTH_WORD_INDEX = 5

CHUNK_WORDS = 1 << 16  # words per read chunk, tune as needed


def iter_words(path, chunk_words=CHUNK_WORDS):
    """Yield 32-bit little-endian words from a binary file, chunk by chunk."""
    chunk_bytes = chunk_words * 4
    with open(path, "rb") as f:
        leftover = b""
        while True:
            data = f.read(chunk_bytes)
            if not data:
                break
            data = leftover + data
            n = len(data) // 4
            usable = n * 4
            leftover = data[usable:]

            arr = np.frombuffer(data[:usable], dtype="<u4")
            for w in arr:
                yield int(w)

        if leftover:
            # Trailing bytes that don't form a full word are discarded.
            pass


def strip_channels(in_path, out_path, header_words=HEADER_WORDS, keep_quadder=0):
    """Rewrite in_path to out_path, keeping only one quadder block per event.

    For the kept quadder (keep_quadder, 0-based), the header_words leading
    words are preserved and the remaining channel words are collapsed into
    a single PLACEHOLDER word. Every other quadder block - markers and data
    alike - is dropped entirely from the output. The event length word is
    reduced by 4 bytes per removed word.
    """
    words_in = 0
    words_out = 0
    events = 0
    quadders_kept = 0
    quadders_dropped = 0

    in_event = False
    in_quadder = False
    is_kept_quadder = False
    quadder_read = -1          # quadder index within current event (0-based)
    quadder_word_count = 0     # words seen since QUADDER_START
    placeholder_written = False

    event_buf = []             # output words of the current event
    event_words_in = 0         # input words of the current event

    with open(in_path, "rb") as _:
        pass  # fail early if the input is missing

    with open(out_path, "wb") as out:

        def write_words(words):
            nonlocal words_out
            if words:
                out.write(struct.pack(f"<{len(words)}I", *words))
                words_out += len(words)

        def flush_event():
            nonlocal event_buf, event_words_in
            if not event_buf:
                return
            removed_words = event_words_in - len(event_buf)
            if len(event_buf) > LENGTH_WORD_INDEX:
                event_buf[LENGTH_WORD_INDEX] -= 4 * removed_words
            write_words(event_buf)
            event_buf = []
            event_words_in = 0

        def emit(word):
            if in_event:
                event_buf.append(word)
            else:
                write_words([word])

        for w in iter_words(in_path):
            words_in += 1

            if w == EVENT_START:
                flush_event()
                in_event = True
                in_quadder = False
                quadder_read = -1
                events += 1
                event_words_in = 1
                emit(w)
                continue

            if not in_event:
                emit(w)
                continue

            event_words_in += 1

            if w == QUADDER_START:
                quadder_read += 1
                quadder_word_count = 0
                placeholder_written = False
                in_quadder = True
                is_kept_quadder = (quadder_read == keep_quadder)
                if is_kept_quadder:
                    quadders_kept += 1
                    emit(w)
                else:
                    quadders_dropped += 1
                continue

            if w == QUADDER_END and in_quadder:
                in_quadder = False
                if is_kept_quadder:
                    emit(w)
                continue

            if in_quadder:
                if not is_kept_quadder:
                    # Not the quadder we're keeping: drop every word in it.
                    quadder_word_count += 1
                    continue

                if quadder_word_count == 0:
                    emit(QUADDER_LENGTH)
                elif quadder_word_count < header_words:
                    emit(w)
                elif not placeholder_written:
                    PLACEHOLDER = 0xffffff00 | random.getrandbits(8)
                    print(hex(PLACEHOLDER))
                    emit(PLACEHOLDER)
                    placeholder_written = True
                # else: further channel words for the kept quadder are dropped

                quadder_word_count += 1
                continue

            # Words inside an event but outside any quadder block
            emit(w)

        flush_event()

    print(f"Read {words_in} words ({events} events).")
    print(f"Kept quadder {keep_quadder}: {quadders_kept} blocks kept, {quadders_dropped} blocks dropped.")
    print(f"Wrote {words_out} words to {out_path}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Keep only one SCD quadder per event, with its channel words collapsed to a placeholder."
    )
    parser.add_argument("input", type=str, help="Path to the input binary raw data file")
    parser.add_argument("output", type=str, help="Path to write the resulting binary file")
    parser.add_argument(
        "--quadder", type=int, default=0,
        help="Quadder index (0-based) to keep; all others are dropped entirely (default 0)"
    )
    parser.add_argument(
        "--header-words", type=int, default=HEADER_WORDS,
        help=f"Number of leading words in the kept quadder's block to preserve (default {HEADER_WORDS})"
    )
    args = parser.parse_args()

    strip_channels(args.input, args.output, header_words=args.header_words, keep_quadder=args.quadder)