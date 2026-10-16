"""Bounded, offline document extraction shared by Telegram and Web."""
from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import subprocess
import sys
import zipfile
from xml.etree import ElementTree as ET

MAX_DOCUMENT = 20 * 1024 * 1024
MAX_TEXT = 120000


def _extract(path, image_dir=None):
    suffix = path.suffix.lower()
    if not 0 < path.stat().st_size <= MAX_DOCUMENT:
        raise ValueError('Размер документа должен быть от 1 байта до 20 МБ.')
    if suffix in {'.txt', '.md'}:
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError('Текстовый файл: максимум 2 МБ.')
        text = path.read_text(encoding='utf-8-sig')
        if '\x00' in text:
            raise ValueError('Требуется текст UTF-8.')
    elif suffix == '.docx':
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            if (len(infos) > 1000 or sum(i.file_size for i in infos) > 30 * 1024 * 1024
                    or any(i.flag_bits & 1 or i.file_size > 10 * 1024 * 1024 for i in infos)):
                raise ValueError('Архив документа превышает безопасные ограничения.')
            raw = archive.read('word/document.xml')
        if b'<!DOCTYPE' in raw.upper() or b'<!ENTITY' in raw.upper():
            raise ValueError('Недопустимая структура XML.')
        root = ET.fromstring(raw)
        ns = {'w': 'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        lines = []
        for child in root.find('w:body', ns):
            if child.tag.endswith('}tbl'):
                for row in child.findall('w:tr', ns):
                    lines.append('\t'.join(' '.join(t.text or '' for t in cell.findall('.//w:t', ns)) for cell in row.findall('w:tc', ns)))
            else:
                lines.append(''.join(t.text or '' for t in child.findall('.//w:t', ns)))
        text = '\n'.join(lines)
    elif suffix == '.pdf':
        from pypdf import PdfReader
        with path.open('rb') as stream:
            if stream.read(5) != b'%PDF-':
                raise ValueError('Файл не является PDF.')
        reader = PdfReader(path, strict=True)
        if reader.is_encrypted or len(reader.pages) > 100:
            raise ValueError('PDF: максимум 100 страниц, без пароля.')
        parts = []
        total = 0
        has_text = False
        needs_images = False
        for n, page in enumerate(reader.pages, 1):
            value = (page.extract_text(extraction_mode='layout') or '') if page.get('/Contents') is not None else ''
            has_text = has_text or bool(value.strip())
            needs_images = needs_images or not value.strip()
            total += len(value)
            if total > MAX_TEXT:
                raise ValueError('Документ превышает 120000 символов.')
            parts.append(f'[Страница {n}]\n{value}')
        if needs_images and image_dir is not None:
            if len(reader.pages) > 10:
                raise ValueError('PDF со сканами: максимум 10 страниц. Разделите PDF на части до 10 страниц и отправьте отдельно.')
            import pypdfium2 as pdfium
            import math
            pages = []
            with pdfium.PdfDocument(path) as pdf:
                for n in range(len(pdf)):
                    with closing(pdf[n]) as page:
                        width, height = page.get_size()
                        if not all(math.isfinite(v) and v > 0 for v in (width, height)):
                            raise ValueError('Недопустимый размер страницы PDF.')
                        scale = min(2, 2400 / max(width, height))
                        with closing(page.render(scale=scale)) as bitmap:
                            dest = image_dir / f'page-{n+1:03}.png'
                            with bitmap.to_pil() as image:
                                image.save(dest)
                        pages.append(str(dest))
            return {'text': '\n\n'.join(parts), 'pages': pages}
        if not has_text:
            raise ValueError('PDF без текстового слоя: пришлите страницы как изображения для чтения Салли/Риком.')
        text = '\n\n'.join(parts)
    else:
        raise ValueError('Поддерживаются TXT, Markdown, PDF и DOCX.')
    if not text.strip() or len(text) > MAX_TEXT:
        raise ValueError('Документ пуст или превышает 120000 символов.')
    return text


def extract_document(path: Path) -> str:
    return document_material(path)['text']


def document_material(path: Path, image_dir: Path | None = None) -> dict:
    # Untrusted parsers run outside the bot, with a deadline and Linux resource limits.
    args = [sys.executable, '-m', 'agentbridge.documents', str(path.resolve())]
    if image_dir is not None:
        image_dir.mkdir(parents=True, exist_ok=True)
        args.append(str(image_dir.resolve()))
    try:
        result = subprocess.run(args,
                                capture_output=True, timeout=25, encoding='utf-8')
    except subprocess.TimeoutExpired:
        raise ValueError('Документ превысил 25 секунд обработки. Разделите его на части.') from None
    if result.returncode:
        raise ValueError('Документ повреждён или превысил ограничения обработки.')
    payload = json.loads(result.stdout)
    if 'error' in payload:
        raise ValueError(payload['error'])
    return dict(text=payload['text'], pages=payload.get('pages', []))


if __name__ == '__main__':
    if sys.platform == 'linux':
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024,) * 2)
        resource.setrlimit(resource.RLIMIT_CPU, (20, 20))
    try:
        result = _extract(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else None)
        payload = result if isinstance(result, dict) else {'text': result}
    except ValueError as exc:
        payload = {'error': str(exc)}
    except Exception:
        payload = {'error': 'Не удалось прочитать документ. Проверьте формат и содержимое.'}
    print(json.dumps(payload, ensure_ascii=True))
