"""文档正文归一化：PDF 常抽出部首形（⻛/⽉/⽂）与 C0 控制符。"""
from __future__ import annotations

import re
import unicodedata

# Kangxi Radicals U+2F00..U+2FDF → 对应正字（214 个）
_KANGXI_TARGET = (
    "一丨丶丿乙亅二亠人儿入八冂冖冫几凵刀力勹匕匚匸十卜卩厂厶又口囗土士夂夊夕大女子宀寸小尢尸屮山巛工己巾干幺广廴廾弋弓彐彡彳心戈戶戊戉戉爿片牙牛犬它玄玉瓜瓦甘生用疋白皮皿目矛矢石示禸禾穴立竹米糸缶网羊羽老而耒耳聿肉臣自至臼舌舛舟艮色虍虫血行衣襾見角言谷豆豕豸貝赤走足身車辛辰辵邑酉采里金長門阜隶隹雨青非面革韋韭音頁風飛食首香馬骨高髟鬥鬯鬲鬼魚鳥鹵鹿麥麻黃黍黑黹黽鼎鼓鼠鼻齊齒龍"
)
_KANGXI_MAP: dict[str, str] = {
    chr(0x2F00 + i): ch for i, ch in enumerate(_KANGXI_TARGET)
}
# CJK Radicals Supplement（PDF 里偶发）
_SUPPLEMENT_MAP: dict[str, str] = {
    "\u2ec5": "见",
    "\u2ec6": "角",
    "\u2ed3": "龙",
    "\u2eda": "叶",
    "\u2edb": "风",
}


def normalize_doc_text(text: str) -> str:
    body = unicodedata.normalize("NFKC", str(text or ""))
    body = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", body)
    out: list[str] = []
    for ch in body:
        if ch in _SUPPLEMENT_MAP:
            out.append(_SUPPLEMENT_MAP[ch])
            continue
        o = ord(ch)
        if 0x2F00 <= o <= 0x2FDF:
            out.append(_KANGXI_MAP.get(ch, ch))
            continue
        out.append(ch)
    return "".join(out).strip()
