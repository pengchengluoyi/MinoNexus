"""把外部用例文本解析成草稿行。Excel 前端会先收成 {table: 二维数组}。"""
from __future__ import annotations

import csv
import io
import json
import re
from typing import Any

_CASE_HEADERS = {
    "case_id": ("用例编号", "编号", "id", "case_id", "caseid"),
    "name": ("名称", "标题", "用例名", "name", "title"),
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


def _header_map(row: list[str]) -> dict[str, int]:
    mapping: dict[str, int] = {}
    for idx, cell in enumerate(row):
        key = _norm_header(cell)
        for field, aliases in _CASE_HEADERS.items():
            if key in {_norm_header(a) for a in aliases} and field not in mapping:
                mapping[field] = idx
    return mapping


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
    if idx is None or idx >= len(row):
        return ""
    return _restore_numbered_breaks(_normalize_cell_text(row[idx]))


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


def _md_table(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if cells and re.match(r"^:?-{3,}:?$", cells[0] or ""):
            continue
        if all(re.match(r"^:?-+:?$", c or "") for c in cells):
            continue
        rows.append(cells)
    return rows


def _from_case_rows(rows: list[list[str]]) -> list[dict]:
    if not rows:
        return []
    mapping = _header_map(rows[0])
    if mapping:
        body = rows[1:]
    else:
        mapping = {"name": 0, "steps": 1, "expected": 2}
        body = rows
    out = []
    for i, row in enumerate(body):
        name = _cell(row, mapping.get("name"))
        steps = _cell(row, mapping.get("steps"))
        expected = _cell(row, mapping.get("expected"))
        cid = _cell(row, mapping.get("case_id"))
        if not (name or steps or cid):
            continue
        pre = _cell(row, mapping.get("precondition"))
        point_raw = _cell(row, mapping.get("point_ids"))
        points = [p.strip() for p in re.split(r"[,，;；\s]+", point_raw) if p.strip()] if point_raw else []
        out.append({
            "case_id": cid or f"imp-{i + 1}",
            "name": name or cid or f"导入用例 {i + 1}",
            "module": _cell(row, mapping.get("module")),
            "precondition": pre,
            "steps": steps,
            "expected": expected,
            "precondition_raw": pre,
            "steps_raw": steps,
            "expected_raw": expected,
            "platform": _cell(row, mapping.get("platform")) or "双端",
            "aspect": _cell(row, mapping.get("aspect")) or "正向",
            "point_ids": points,
        })
    return out


def _from_case_blocks(text: str) -> list[dict]:
    chunks = re.split(r"\n(?=#{1,3}\s+|\d+[.)、]\s+)", text.strip())
    out = []
    for i, chunk in enumerate(chunks):
        lines = [ln.rstrip() for ln in chunk.strip().splitlines() if ln.strip()]
        if not lines:
            continue
        title = re.sub(r"^#{1,3}\s+", "", lines[0])
        title = re.sub(r"^\d+[.)、]\s+", "", title).strip()
        fields: dict[str, Any] = {"name": title}
        key = ""
        buf: list[str] = []

        def flush() -> None:
            if key:
                fields[key] = "\n".join(buf).strip()

        for line in lines[1:]:
            m = re.match(
                r"^(编号|名称|模块|前置|前置条件|步骤|测试步骤|预期|期望|预期效果|期望结果|端|情况)[:：]\s*(.*)$",
                line,
            )
            if m:
                flush()
                label = m.group(1)
                key = {
                    "编号": "case_id",
                    "名称": "name",
                    "模块": "module",
                    "前置": "precondition",
                    "前置条件": "precondition",
                    "步骤": "steps",
                    "测试步骤": "steps",
                    "预期": "expected",
                    "期望": "expected",
                    "预期效果": "expected",
                    "期望结果": "expected",
                    "端": "platform",
                    "情况": "aspect",
                }[label]
                buf = [m.group(2)] if m.group(2) else []
            else:
                buf.append(line)
        flush()
        if fields.get("name") or fields.get("steps"):
            fields.setdefault("case_id", f"imp-{i + 1}")
            out.append(fields)
    return out


def _polish_case(row: dict) -> dict:
    out = dict(row)
    for key in ("precondition", "steps", "expected"):
        if key in out:
            out[key] = _restore_numbered_breaks(_normalize_cell_text(out.get(key)))
    return out


def parse_cases(text: str, filename: str = "") -> list[dict]:
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("用例内容是空的")
    name = str(filename or "").lower()
    if raw.startswith("{") or raw.startswith("["):
        data = json.loads(raw)
        if isinstance(data, dict) and isinstance(data.get("table"), list):
            grid = [
                [_normalize_cell_text(c) for c in row]
                for row in data["table"]
                if isinstance(row, list)
            ]
            return [_polish_case(x) for x in _from_case_rows(grid)]
        if isinstance(data, dict):
            rows = data.get("cases") or data.get("draft_cases") or []
        else:
            rows = data
        if not isinstance(rows, list):
            raise ValueError("JSON 用例需要数组")
        return [_polish_case(x) for x in rows if isinstance(x, dict)]
    if "|" in raw and re.search(r"^\|.+\|", raw, re.M):
        rows = _from_case_rows(_md_table(raw))
        if rows:
            return [_polish_case(x) for x in rows]
    if name.endswith((".csv", ".tsv")) or "\t" in raw or raw.count(",") >= 2:
        rows = _from_case_rows(_split_csv(raw))
        if rows:
            return [_polish_case(x) for x in rows]
    blocks = _from_case_blocks(raw)
    if blocks:
        return [_polish_case(x) for x in blocks]
    raise ValueError(
        "无法识别用例格式。可用 Excel（请用「选择文件」上传 .xlsx）/ CSV / Markdown 表 / JSON，"
        "或「标题 + 步骤：/预期：」分段"
    )
