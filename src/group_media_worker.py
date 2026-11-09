"""Bounded, one-shot document extraction; stdin/stdout only, no file execution."""
import io
import json
import sys
import zipfile
from pathlib import PurePosixPath

MAX_BYTES = 24 * 1024 * 1024
MAX_TEXT = 24000
kind = sys.argv[1] if len(sys.argv) == 2 else ''
raw = sys.stdin.buffer.read(MAX_BYTES + 1)
if not raw or len(raw) > MAX_BYTES:
    raise SystemExit(2)
chunks = []
def add(value):
    if value:
        chunks.append(str(value))
    return sum(map(len, chunks)) >= MAX_TEXT

def validate_zip():
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        entries = z.infolist()
        if len(entries) > 5000 or sum(x.file_size for x in entries) > 80*1024*1024:
            raise ValueError('expanded document too large')
        for x in entries:
            p = PurePosixPath(x.filename)
            if p.is_absolute() or '..' in p.parts or x.flag_bits & 1:
                raise ValueError('unsupported archive')
        return [x.filename for x in entries]

if kind in ('txt','md','csv','json','log'):
    for enc in ('utf-8-sig','gb18030'):
        try:
            add(raw.decode(enc)); break
        except UnicodeDecodeError:
            pass
    else: raise ValueError('unknown text encoding')
elif kind == 'pdf':
    from pypdf import PdfReader
    pdf = PdfReader(io.BytesIO(raw), strict=False)
    if pdf.is_encrypted: raise ValueError('encrypted pdf')
    for i, page in enumerate(pdf.pages):
        if i >= 60: break
        if add(f'[第{i+1}页]\n{page.extract_text() or ""}\n'): break
elif kind == 'docx':
    validate_zip()
    from docx import Document
    doc = Document(io.BytesIO(raw))
    for para in doc.paragraphs:
        if add(para.text+'\n'): break
    if sum(map(len,chunks)) < MAX_TEXT:
        for table in doc.tables:
            for row in table.rows[:500]:
                if add(' | '.join(cell.text for cell in row.cells[:30])+'\n'): break
elif kind == 'xlsx':
    validate_zip()
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(raw), read_only=True, data_only=True, keep_links=False)
    try:
        for sheet in wb.worksheets[:10]:
            add(f'[工作表 {sheet.title}]\n')
            for row in sheet.iter_rows(max_row=1000, max_col=30, values_only=True):
                if add(' | '.join('' if x is None else str(x)[:500] for x in row)+'\n'): break
            if sum(map(len,chunks)) >= MAX_TEXT: break
    finally: wb.close()
elif kind == 'pptx':
    names=validate_zip()
    import re
    import xml.etree.ElementTree as ET
    slides=sorted((x for x in names if re.fullmatch(r'ppt/slides/slide\d+\.xml',x)),
                  key=lambda x:int(re.search(r'(\d+)\.xml$',x).group(1)))
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        for i,name in enumerate(slides[:60], 1):
            node=ET.fromstring(z.read(name))
            text=' '.join(x.text or '' for x in node.iter() if x.tag.endswith('}t'))
            if add(f'[第{i}页]\n{text}\n'): break
else:
    raise ValueError('unsupported file')
text=''.join(chunks)
print(json.dumps({'text':text[:MAX_TEXT], 'truncated':len(text)>MAX_TEXT}, ensure_ascii=False))
