"""最小合法 PDF 生成器（纯手写字节，零依赖）：压测上传场景的文档源。

pypdf 能解析、extract_text 能读出文本行——入库流水线全链真跑到 ready。
只支持 ASCII（Helvetica 标准编码没有中文）；中文语料的压测索引走
seed_loadtest.py 直建，不经过这里。
"""


def _escape(line: str) -> str:
    return line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def make_pdf(lines: list[str]) -> bytes:
    """单页 PDF：给定文本行从页首逐行排下（12pt Helvetica）。"""
    text_ops = "\n".join(f"({_escape(line)}) Tj T*" for line in lines)
    stream = f"BT /F1 12 Tf 72 760 Td 16 TL\n{text_ops}\nET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        (
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>"
        ),
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets: list[int] = []
    for i, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n%s\nendobj\n" % (i, body)
    xref_at = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for off in offsets:
        out += b"%010d 00000 n \n" % off
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref_at,
    )
    return bytes(out)
