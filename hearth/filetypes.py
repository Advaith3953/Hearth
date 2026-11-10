"""What kind of file is this? One place for the categories used by listings, summaries and the `kind` filter."""
from __future__ import annotations

import os

TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".log",
    ".xml", ".html", ".htm", ".py", ".js", ".ts", ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".sh",
    ".bat", ".sql", ".tex", ".org", ".css",
}
CODE_EXTS = {".py", ".js", ".ts", ".jsx", ".tsx", ".java", ".c", ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".rb", ".php",
             ".swift", ".kt", ".sh", ".bat", ".cmd", ".ps1", ".sql", ".html", ".htm", ".css", ".scss", ".lua", ".r", ".m"}

# `kind` values the tools accept, and the extensions behind each
KINDS: dict[str, set[str]] = {
    "excel": {".xlsx", ".xlsm", ".xls", ".csv", ".tsv", ".ods"},
    "word": {".docx", ".doc", ".odt", ".rtf"},
    "pdf": {".pdf"},
    "presentation": {".pptx", ".ppt", ".odp", ".key"},
    "text": (TEXT_EXTS - CODE_EXTS) | {""},
    "code": CODE_EXTS,
    "image": {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".tif", ".tiff", ".heic", ".ico", ".raw"},
    "audio": {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a", ".wma", ".opus"},
    "video": {".mp4", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".flv", ".m4v"},
    "archive": {".zip", ".7z", ".rar", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".iso"},
    "program": {".exe", ".msi", ".dll", ".apk", ".app", ".jar", ".com"},
}

# how the summary groups them for people
LABELS = {"excel": "Spreadsheets", "word": "Word documents", "pdf": "PDFs", "presentation": "Presentations",
          "text": "Text & data", "code": "Code", "image": "Images", "audio": "Audio", "video": "Video",
          "archive": "Archives", "program": "Programs", "other": "Other"}

_ALIASES = {
    "excel": "excel", "xlsx": "excel", "xls": "excel", "spreadsheet": "excel", "spreadsheets": "excel", "sheet": "excel",
    "sheets": "excel", "csv": "excel", "workbook": "excel", "workbooks": "excel",
    "word": "word", "docx": "word", "doc": "word", "document": "word", "documents": "word",
    "pdf": "pdf", "pdfs": "pdf", "presentation": "presentation", "presentations": "presentation", "powerpoint": "presentation",
    "pptx": "presentation", "slides": "presentation", "ppt": "presentation",
    "text": "text", "txt": "text", "notes": "text", "code": "code", "source": "code", "script": "code", "scripts": "code",
    "image": "image", "images": "image", "photo": "image", "photos": "image", "picture": "image", "pictures": "image",
    "jpg": "image", "png": "image", "audio": "audio", "music": "audio", "song": "audio", "songs": "audio", "mp3": "audio",
    "video": "video", "videos": "video", "movie": "video", "movies": "video", "mp4": "video",
    "archive": "archive", "archives": "archive", "zip": "archive", "compressed": "archive",
    "program": "program", "programs": "program", "app": "program", "apps": "program", "exe": "program",
    "any": "", "all": "", "": "", "file": "", "files": "",
}


def ext(path: str) -> str:
    return os.path.splitext(path)[1].lower()


def normalize_kind(raw: str) -> str | None:
    """Understand what a small model writes for the file kind ('Excel files', '.xlsx', 'photos'); None = not understood."""
    k = raw.lower().strip().lstrip(".")
    if k in _ALIASES:
        return _ALIASES[k]
    for word in k.replace(",", " ").split():
        if word.strip(".") in _ALIASES:
            return _ALIASES[word.strip(".")]
    return None


def kind_of(path: str) -> str:
    """The most specific kind of a file ('other' when nothing fits). Order matters: csv is a spreadsheet, .py is code."""
    e = ext(path)
    for k in ("excel", "word", "pdf", "presentation", "image", "audio", "video", "archive", "program", "code", "text"):
        if e in KINDS[k]:
            return k
    return "other"
