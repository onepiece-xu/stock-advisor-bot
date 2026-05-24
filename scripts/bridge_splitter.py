#!/usr/bin/env python3
"""Split bridge output into chunks small enough for Feishu delivery.

Feishu Markdown can truncate messages with | characters (table parsing). 
This splitter breaks output at 【 section boundaries, keeping each chunk
under FEISHU_MAX bytes. Chunks are separated by --- for multi-message delivery.
"""

import re
import sys

FEISHU_MAX = 4000  # bytes — safe under Feishu's ~20KB limit, less spammy


def split_at_sections(text: str, max_bytes: int = FEISHU_MAX) -> list[str]:
    """Split text at section boundaries (【...】), keeping chunks under max_bytes."""
    # Split on newline before 【
    parts = re.split(r'(\n(?=【))', text)
    chunks = []
    current = ''
    for part in parts:
        candidate = current + part
        if len(candidate.encode('utf-8')) > max_bytes and current:
            chunks.append(current.strip())
            current = part
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return chunks


def main():
    text = sys.stdin.read().strip()
    if not text:
        sys.exit(0)
    
    # Strip trailing --- separator (may come from validator)
    text = re.sub(r'\n---\s*$', '', text)
    
    # If it fits, output as-is
    if len(text.encode('utf-8')) <= FEISHU_MAX:
        print(text)
        sys.exit(0)
    
    chunks = split_at_sections(text)
    
    # If only one chunk emerged (no section boundaries), force-split at 700 bytes
    if len(chunks) == 1:
        b = text.encode('utf-8')
        # Find last newline before 700 bytes
        cutoff = b.rfind(b'\n', 0, FEISHU_MAX - 100)
        if cutoff > 0:
            chunks = [
                b[:cutoff].decode('utf-8').strip(),
                b[cutoff:].decode('utf-8').strip(),
            ]
        else:
            # Can't split gracefully, just truncate with note
            chunks = [b[:FEISHU_MAX - 50].decode('utf-8') + '\n...']

    for chunk in chunks:
        print(chunk)
        print('---')


if __name__ == '__main__':
    main()
