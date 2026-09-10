"""把外部用例表格解析成草稿行。表头/列映射由用户指定，服务端不猜测。"""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

_COLUMN_FIELDS = (
    "case_id",
    "name",
    "module",
    "platform",
    "precondition",
    "steps",
    "expected",
    "aspect",
    "point_ids",
)

_CASE_HEADERS = {
    "case_id": ("用例编号", "编号", "id", "case_id", "caseid"),
    "name": ("用例名称", "用例名", "名称", "标题", "name", "title"),
    "module": ("模块", "路径", "module", "path"),
    "precondition": ("前置", "前置条件", "precondition", "pre"),
    "steps": ("步骤", "测试步骤", "操作步骤", "steps"),
    "expected": ("预期", "期望", "预期结果", "预期效果", "期望结果", "expected"),
    "platform": ("端", "平台", "platform"),
    "aspect": ("情况", "类型", "aspect", "kind"),
    "point_ids": ("测试点", "point_ids", "points"),
}

_XML_CTRL = re.compile(r"_x([0-9A-Fa-f]{4})_", re.I)
_NUM_ITEM = re.compile(r"\d+[.、．)）]\s+")


def _norm_header(cell: str) -> str:
    return re.sub(r"\s+", "", str(cell or "").strip().lower())


def _normalize_cell_text(val: Any) -> str:
    s = str(val if val is not None else "")
    s = _XML_CTRL.sub(lambda m: chr(int(m.group(1), 16)), s)
    s = s.replace("\r\n", "\n").replace("\r", "\n")
    s = s.replace("\u2028", "\n").replace("\u2029", "\n")
    return s.strip()


def _restore_numbered_breaks(text: str) -> str:
    s = _normalize_cell_text(text)
    if not s or "\n" in s:
        return s
    out = re.sub(r"\s*(?=\d+[.、．)）]\s+)", "\n", s).strip()
    lines = [ln.strip() for ln in out.split("\n") if ln.strip()]
    if len(lines) <= 1:
        return s
    numbered = sum(1 for ln in lines if _NUM_ITEM.match(ln))
    if numbered < 2:
        return s
    return "\n".join(lines)


def _cell(row: list, idx: int | None) -> str:
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    return _restore_numbered_breaks(_normalize_cell_text(row[idx]))


def normalize_table(raw_table: Any) -> list[list[str]]:
    if not isinstance(raw_table, list):
        return []
    out: list[list[str]] = []
    for row in raw_table:
        if not isinstance(row, list):
            continue
        cells = [_normalize_cell_text(c) for c in row]
        if any(cells):
            out.append(cells)
    return out


def header_labels(table: list[list[str]], header_row: int) -> list[str]:
    if header_row < 0 or header_row >= len(table):
        return []
    labels = []
    for i, cell in enumerate(table[header_row]):
        label = str(cell or "").strip() or f"列{i + 1}"
        labels.append(label)
    return labels


def suggest_column_map(labels: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx, label in enumerate(labels):
        key = _norm_header(label)
        for field, aliases in _CASE_HEADERS.items():
            if field in mapping:
                continue
            if key in {_norm_header(a) for a in aliases}:
                mapping[field] = idx
    return mapping


def parse_table_rows(
    table: list[list[str]],
    *,
    header_row: int,
    skip_rows: list[int] | None = None,
    column_map: dict[str, int],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按用户指定的表头行、跳过行、列映射解析。合并单元格保持为空，不填充。"""
    grid = normalize_table(table)
    if not grid:
        raise ValueError("表格是空的")
    if header_row < 0 or header_row >= len(grid):
        raise ValueError("列名行超出表格范围")
    skip = {int(x) for x in (skip_rows or [])}
    skip.add(header_row)
    labels = header_labels(grid, header_row)
    cmap: dict[str, int] = {}
    for field, idx in (column_map or {}).items():
        if field not in _COLUMN_FIELDS:
            continue
        try:
            col = int(idx)
        except (TypeError, ValueError):
            continue
        if 0 <= col < len(labels):
            cmap[field] = col
    if "name" not in cmap and "steps" not in cmap and "case_id" not in cmap:
        raise ValueError("请至少映射「编号」「名称」或「步骤」中的一列")
    rows: list[dict[str, Any]] = []
    for i, row in enumerate(grid):
        if i in skip:
            continue
        name = _cell(row, cmap.get("name"))
        steps = _cell(row, cmap.get("steps"))
        expected = _cell(row, cmap.get("expected"))
        cid = _cell(row, cmap.get("case_id"))
        if not (name or steps or cid):
            continue
        pre = _cell(row, cmap.get("precondition"))
        point_raw = _cell(row, cmap.get("point_ids"))
        points = [p.strip() for p in re.split(r"[,，;；\s]+", point_raw) if p.strip()] if point_raw else []
        rows.append({
            "row_index": i,
            "case_id": cid,
            "name": name or cid,
            "module": _cell(row, cmap.get("module")),
            "precondition": pre,
            "steps": steps,
            "expected": expected,
            "precondition_raw": pre,
            "steps_raw": steps,
            "expected_raw": expected,
            "platform": _cell(row, cmap.get("platform")) or "双端",
            "aspect": _cell(row, cmap.get("aspect")) or "正向",
            "point_ids": points,
            "source": "import",
            "origin": "import",
            "locked": True,
        })
    meta = {
        "header_row": header_row,
        "skip_rows": sorted(skip),
        "column_map": cmap,
        "header_labels": labels,
        "ignored_columns": [
            labels[j]
            for j in range(len(labels))
            if j not in set(cmap.values())
        ],
    }
    return rows, meta


def _csv_dialect(text: str):
    first = ""
    for line in text.splitlines():
        if line.strip():
            first = line
            break
    tabs = first.count("\t")
    commas = first.count(",")
    if tabs >= 1 and tabs >= commas:
        return csv.excel_tab
    try:
        return csv.Sniffer().sniff(text[:800], delimiters=",\t;|")
    except Exception:
        return csv.excel_tab if tabs else csv.excel


def _split_csv(text: str) -> list[list[str]]:
    reader = csv.reader(io.StringIO(text), _csv_dialect(text))
    rows = []
    for row in reader:
        cells = [_normalize_cell_text(c) for c in row]
        if any(cells):
            rows.append(cells)
    return rows


def load_table_from_text(text: str, filename: str = "") -> list[list[str]]:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("内容是空的")
    if raw.startswith("{") or raw.startswith("["):
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("table"), list):
            return normalize_table(data["table"])
        raise ValueError("JSON 需要 {table: 二维数组}")
    name = str(filename or "").lower()
    if name.endswith((".csv", ".tsv")) or "\t" in raw or raw.count(",") >= 2:
        return normalize_table(_split_csv(raw))
    raise ValueError("无法识别格式。请上传 Excel（前端收成 table JSON）或 CSV")
