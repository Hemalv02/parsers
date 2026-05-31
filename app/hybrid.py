#!/usr/bin/env python3
"""Hybrid DOCX → Markdown converter: pandoc + vendored OMML→LaTeX.

Architecture:
  1. Walk source DOCX OOXML. For each <m:oMath> and <m:oMathPara>:
       - Convert to LaTeX via the vendored oMath2Latex (see vendor/omml/).
       - Replace the element with a plain-text alphanumeric SENTINEL
         (e.g. "XHYBRIDINLINE000042X"). Sentinels survive pandoc's escape
         logic because they contain no special characters.
  2. Run pandoc -t gfm --wrap=none --track-changes=all on the modified DOCX.
  3. Post-process pandoc's output: find each sentinel and replace with the
     corresponding LaTeX wrapped in GFM math syntax:
       inline → $`<latex>`$
       block  → ```math\n<latex>\n```

Effect: pandoc handles everything except equations; the vendored omml
module (originally docling's, originally dwml's) handles every equation,
including the block ones pandoc would have dropped. No model loading,
no ML deps — just 733 lines of XML-to-LaTeX state machine.
"""

from __future__ import annotations

import io
import logging
import re
import subprocess
import sys
import zipfile
from pathlib import Path

import lxml.etree as ET

# Vendored from docling (MIT) — see vendor/omml/LICENSE.
# Avoids pulling docling's PyTorch + IBM model deps (~5 GB) at runtime.
from .vendor.omml.omml import oMath2Latex

log = logging.getLogger("parser.docx")

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
}
W_NS = NS["w"]
M_NS = NS["m"]

MATH_PARTS = ("word/document.xml", "word/footnotes.xml", "word/endnotes.xml")

# Hardened parser for the untrusted DOCX XML parts. lxml's default parser has
# `resolve_entities=True`, which exposes two classic attacks on a hostile
# .docx: billion-laughs entity expansion (OOM) and local-file disclosure via
# `<!ENTITY x SYSTEM "file:///etc/passwd">`. We turn entity resolution off and
# block network/DTD loading. OOXML uses no custom entities, so well-formed
# documents are unaffected (predefined XML entities like &amp; still
# round-trip through ET.tostring). See DECISIONS.md §13.
_XXE_SAFE_PARSER = ET.XMLParser(
    resolve_entities=False,
    no_network=True,
    load_dtd=False,
    huge_tree=False,
)

# Sentinels: pure alphanumeric, leading/trailing X to avoid swallowing
# adjacent digits when pandoc word-wraps. 6 digits → up to 1M equations.
SENTINEL_INLINE_PREFIX = "XHYBRIDINLINE"
SENTINEL_BLOCK_PREFIX = "XHYBRIDBLOCK"
SENTINEL_SUFFIX = "X"


def _safe_latex(omath_elem) -> str:
    try:
        return (oMath2Latex(omath_elem).latex or "").strip()
    except Exception:
        # Equation conversion failed; the sentinel is dropped and the
        # postprocess `missing_inline`/`missing_block` stats count it.
        log.debug("OMML->LaTeX conversion failed for one equation", exc_info=True)
        return ""


def _make_text_run(text: str) -> ET._Element:
    r = ET.SubElement(ET.Element(f"{{{W_NS}}}root"), f"{{{W_NS}}}r")
    t = ET.SubElement(r, f"{{{W_NS}}}t")
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    t.text = text
    return r


def _replace_math_with_sentinels(
    xml_bytes: bytes,
    inline_counter: list[int],
    block_counter: list[int],
    inline_latex: list[str],
    block_latex: list[str],
) -> bytes:
    """Replace OMML in this part with text sentinels; record LaTeX into lists."""
    try:
        root = ET.fromstring(xml_bytes, _XXE_SAFE_PARSER)
    except ET.XMLSyntaxError:
        return xml_bytes

    # Process oMathPara first. Each oMathPara wraps 1+ oMath children;
    # treat each oMath inside as a separate block-math entry.
    for para in list(root.iter(f"{{{M_NS}}}oMathPara")):
        parent = para.getparent()
        if parent is None:
            continue
        for child in para.findall(f"{{{M_NS}}}oMath"):
            block_latex.append(_safe_latex(child))
            sentinel = f"{SENTINEL_BLOCK_PREFIX}{block_counter[0]:06d}{SENTINEL_SUFFIX}"
            block_counter[0] += 1
            # Insert one sentinel run alongside (we'll only emit one paragraph
            # text for the whole oMathPara — see next step).
        # Replace the entire oMathPara with a single run containing all
        # sentinels separated by space; surrounding <w:p> stays intact.
        # If oMathPara was the sole content of its <w:p>, the run keeps
        # the paragraph as a valid block-level element.
        sentinels = []
        for offset, _ in enumerate(para.findall(f"{{{M_NS}}}oMath")):
            n = block_counter[0] - len(para.findall(f"{{{M_NS}}}oMath")) + offset
            sentinels.append(f"{SENTINEL_BLOCK_PREFIX}{n:06d}{SENTINEL_SUFFIX}")
        new_run = _make_text_run(" ".join(sentinels))
        idx = list(parent).index(para)
        parent.remove(para)
        parent.insert(idx, new_run)

    # Standalone oMath (not inside an oMathPara).
    for elem in list(root.iter(f"{{{M_NS}}}oMath")):
        parent = elem.getparent()
        if parent is None:
            continue
        # If still inside an oMathPara somehow, skip — but oMathPara was
        # already removed above, so this is defensive.
        ancestor = parent
        while ancestor is not None:
            if ET.QName(ancestor.tag).localname == "oMathPara":
                ancestor = None
                break
            ancestor = ancestor.getparent()
        # Each remaining oMath is inline.
        inline_latex.append(_safe_latex(elem))
        sentinel = f"{SENTINEL_INLINE_PREFIX}{inline_counter[0]:06d}{SENTINEL_SUFFIX}"
        inline_counter[0] += 1
        new_run = _make_text_run(sentinel)
        idx = list(parent).index(elem)
        parent.remove(elem)
        parent.insert(idx, new_run)

    return ET.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def preprocess(content: bytes) -> tuple[bytes, list[str], list[str]]:
    inline_counter = [0]
    block_counter = [0]
    inline_latex: list[str] = []
    block_latex: list[str] = []

    out_buf = io.BytesIO()
    with (
        zipfile.ZipFile(io.BytesIO(content)) as zin,
        zipfile.ZipFile(out_buf, "w", zipfile.ZIP_DEFLATED) as zout,
    ):
        for it in zin.infolist():
            data = zin.read(it.filename)
            if it.filename in MATH_PARTS:
                data = _replace_math_with_sentinels(
                    data, inline_counter, block_counter, inline_latex, block_latex
                )
            zout.writestr(it, data)
    return out_buf.getvalue(), inline_latex, block_latex


def run_pandoc(content: bytes, timeout: int = 120) -> str:
    proc = subprocess.run(
        [
            "pandoc",
            "--sandbox",
            "-f",
            "docx",
            "-t",
            "gfm",
            "--wrap=none",
            "--track-changes=all",
        ],
        input=content,
        capture_output=True,
        timeout=timeout,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pandoc exit={proc.returncode}: {proc.stderr.decode('utf-8', errors='replace')[:500]}"
        )
    return proc.stdout.decode("utf-8", errors="replace")


_RE_INLINE_SENTINEL = re.compile(SENTINEL_INLINE_PREFIX + r"(\d{6})" + SENTINEL_SUFFIX)
_RE_BLOCK_SENTINEL = re.compile(SENTINEL_BLOCK_PREFIX + r"(\d{6})" + SENTINEL_SUFFIX)


def postprocess(
    markdown: str,
    inline_latex: list[str],
    block_latex: list[str],
) -> tuple[str, dict]:
    """Replace sentinels with GFM math syntax containing docling's LaTeX."""

    stats = {
        "source_inline": len(inline_latex),
        "source_block": len(block_latex),
        "found_inline": 0,
        "found_block": 0,
        "missing_inline": 0,
        "missing_block": 0,
    }

    def inline_sub(match: re.Match) -> str:
        n = int(match.group(1))
        stats["found_inline"] += 1
        if n >= len(inline_latex) or not inline_latex[n]:
            return ""  # equation conversion failed; drop the sentinel
        return f"$`{inline_latex[n]}`$"

    markdown = _RE_INLINE_SENTINEL.sub(inline_sub, markdown)

    def block_sub(match: re.Match) -> str:
        n = int(match.group(1))
        stats["found_block"] += 1
        if n >= len(block_latex) or not block_latex[n]:
            return ""
        # Each block sentinel becomes a fenced math block.
        return f"\n\n```math\n{block_latex[n]}\n```\n\n"

    markdown = _RE_BLOCK_SENTINEL.sub(block_sub, markdown)

    stats["missing_inline"] = stats["source_inline"] - stats["found_inline"]
    stats["missing_block"] = stats["source_block"] - stats["found_block"]
    return markdown, stats


def convert(path: Path) -> tuple[str, dict]:
    content = path.read_bytes()
    modified, inline_latex, block_latex = preprocess(content)
    md = run_pandoc(modified)
    md, stats = postprocess(md, inline_latex, block_latex)
    return md, stats


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <input.docx>", file=sys.stderr)
        return 2
    md, stats = convert(Path(sys.argv[1]))
    print(md)
    print(
        f"\n# hybrid stats: "
        f"src_inline={stats['source_inline']} src_block={stats['source_block']} "
        f"found_inline={stats['found_inline']} found_block={stats['found_block']} "
        f"missing_inline={stats['missing_inline']} missing_block={stats['missing_block']}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
