"""Local fund-order screenshot OCR and conservative field extraction.

OCR is deliberately an input assistant only.  It never writes an account
event; the caller must display the fields and require explicit confirmation.
"""
from __future__ import annotations

import base64
import hashlib
import io
import os
import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

try:
    from PIL import Image, ImageOps
except Exception:  # pragma: no cover - optional runtime dependency
    Image = None
    ImageOps = None

try:
    import pytesseract
except Exception:  # pragma: no cover - optional runtime dependency
    pytesseract = None


MAX_IMAGE_BYTES = 12 * 1024 * 1024
_NUMBER = r"([0-9][0-9,]*(?:\.[0-9]+)?)"


def _normalise_number(value: str) -> str:
    return value.replace(",", "").replace("，", "").strip()


def _find_number(text: str, labels: tuple[str, ...]) -> str | None:
    label = "|".join(re.escape(x) for x in labels)
    match = re.search(rf"(?:{label})\s*[:：]?\s*[¥￥]?\s*{_NUMBER}", text, re.I)
    return _normalise_number(match.group(1)) if match else None


def _find_date(text: str) -> str | None:
    match = re.search(
        r"(20\d{2})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*(?:日)?",
        text,
    )
    if not match:
        return None
    year, month, day = match.groups()
    result = f"{year}-{int(month):02d}-{int(day):02d}"
    tail = text[match.end():]
    clock = re.match(r"\s*(?:T|日)?\s*(\d{1,2})[:：](\d{2})(?:[:：](\d{2}))?", tail)
    if clock:
        hour, minute, second = clock.groups()
        result += f"T{int(hour):02d}:{int(minute):02d}:{int(second or 0):02d}"
    return result


def _find_reference(text: str) -> str | None:
    match = re.search(r"(?:交易流水号|流水号|订单号|平台订单号)\s*[:：]?\s*([A-Za-z0-9_-]{6,80})", text, re.I)
    if match:
        return match.group(1)
    # Screenshots sometimes lose the label; only accept a long numeric token.
    candidates = re.findall(r"\b\d{12,24}\b", text)
    return candidates[0] if candidates else None


def parse_text(text: str, funds: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Extract likely order facts from OCR text with per-field confidence."""
    text = text or ""
    catalog = funds or []
    codes = {str(item.get("code") or item.get("fund_code")): item for item in catalog}
    code_match = next((code for code in codes if re.search(rf"(?<!\d){re.escape(code)}(?!\d)", text)), None)
    fields = {
        "fund_code": code_match,
        "cash_debited": _find_number(text, ("买入金额", "申购金额", "平台实际扣款", "确认金额")),
        "confirmed_units": _find_number(text, ("确认份额", "确认数量", "持有份额")),
        "confirmed_nav": _find_number(text, ("确认净值", "净值")),
        "reported_fee": _find_number(text, ("手续费", "申购费", "费用")),
        "occurred_at": _find_date(text),
        "platform_reference": _find_reference(text),
    }
    confidence: dict[str, str] = {}
    for key, value in fields.items():
        confidence[key] = "HIGH" if value else "MISSING"
    warnings: list[str] = []
    if not fields["fund_code"]:
        warnings.append("未识别到已确认基金代码，请手工选择。")
    if not fields["cash_debited"]:
        warnings.append("未识别到平台实际扣款金额，请手工填写。")
    if not fields["platform_reference"]:
        warnings.append("未识别到平台流水号，重复订单校验将依赖人工确认。")
    return {
        "status": "PARSED" if fields["cash_debited"] or fields["fund_code"] else "REVIEW_REQUIRED",
        "fields": fields,
        "confidence": confidence,
        "warnings": warnings,
        "raw_text": text,
        "fund": codes.get(fields["fund_code"]) if fields["fund_code"] else None,
    }


def _tesseract_path() -> str | None:
    configured = os.environ.get("S2_TESSERACT_CMD", "").strip()
    candidates = [
        configured,
        r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
    ]
    return next((item for item in candidates if item and Path(item).is_file()), None)


def _tessdata_path(command: str) -> Path:
    configured = os.environ.get("S2_TESSDATA_DIR", "").strip()
    candidates = [
        Path(configured) if configured else None,
        Path(__file__).resolve().parents[1] / "runtime" / "tessdata",
        Path(command).parent / "tessdata",
    ]
    return next((item for item in candidates if item and (item / "chi_sim.traineddata").is_file()), Path(command).parent / "tessdata")


def _decode_tesseract(raw: bytes) -> str:
    """Decode Windows Tesseract output across UTF-8/GB18030 builds."""
    for encoding in ("utf-8", "gb18030", "cp936"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_image(image_data: str, funds: list[dict[str, Any]] | None = None, filename: str = "") -> dict[str, Any]:
    """Decode a data URL/base64 image and run local Tesseract when available."""
    if not image_data:
        raise ValueError("缺少订单截图。")
    payload = image_data.split(",", 1)[1] if image_data.startswith("data:") and "," in image_data else image_data
    try:
        raw = base64.b64decode(payload, validate=True)
    except Exception as exc:
        raise ValueError("订单截图不是有效的图片编码。") from exc
    if len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("订单截图不能超过 12MB。")
    digest = hashlib.sha256(raw).hexdigest().upper()
    if Image is None or pytesseract is None:
        return {"status": "OCR_UNAVAILABLE", "image_sha256": digest, "filename": filename, "fields": {},
                "confidence": {}, "warnings": ["当前运行环境未安装本地 OCR 引擎，请使用手工录入。"]}
    try:
        image = Image.open(io.BytesIO(raw))
        image.verify()
        image = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:
        raise ValueError("无法读取订单截图，请上传 PNG/JPG 图片。") from exc
    command = _tesseract_path()
    if not command:
        return {"status": "OCR_UNAVAILABLE", "image_sha256": digest, "filename": filename, "fields": {},
                "confidence": {}, "warnings": ["未找到本地 Tesseract OCR 引擎，请使用手工录入。"]}
    pytesseract.pytesseract.tesseract_cmd = command
    # Use Chinese when the optional trained data is present; otherwise English
    # still extracts numeric values and fund codes safely.
    tessdata = _tessdata_path(command)
    lang = "chi_sim+eng" if (tessdata / "chi_sim.traineddata").is_file() else "eng"
    processed = ImageOps.autocontrast(ImageOps.grayscale(image)) if ImageOps else image
    try:
        # Calling the executable directly lets us decode Windows-localized
        # stdout reliably; pytesseract assumes UTF-8 unconditionally.
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            temp_image = Path(handle.name)
        try:
            processed.save(temp_image)
            command_line = [command, str(temp_image), "stdout", "--tessdata-dir", str(tessdata), "-l", lang, "--psm", "6"]
            completed = subprocess.run(command_line, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45, check=False)
            if completed.returncode != 0:
                raise RuntimeError(_decode_tesseract(completed.stderr).strip() or "Tesseract exited with an error")
            text = _decode_tesseract(completed.stdout)
        finally:
            temp_image.unlink(missing_ok=True)
    except Exception as exc:
        return {"status": "OCR_UNAVAILABLE", "image_sha256": digest, "filename": filename, "fields": {},
                "confidence": {}, "warnings": [f"本地 OCR 运行失败：{exc}"]}
    result = parse_text(text, funds)
    result.update({"image_sha256": digest, "filename": filename, "ocr_engine": "tesseract", "ocr_language": lang})
    if lang == "eng":
        result["warnings"].append("本机未找到中文 OCR 语言包，中文字段可能需要人工核对。")
    return result
