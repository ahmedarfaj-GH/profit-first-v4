"""
Excel intake parser — matches the real-world "قالب تجميع البيانات المطلوبة"
template already in use (serial-numbered rows: الرقم التسلسلي / مجموعة
البيانات / اسم البيان المطلوب / وصف البيان / القيمة / ...). The parser is
"dynamic" in the sense that it locates the header row and the relevant
columns by their Arabic labels rather than hardcoded coordinates, so it
tolerates extra columns (some real files add تاريخ الاستحقاق / الأولوية /
الهدف الكامل / المغطى حاليًا columns before المرجع الداخلي) and minor
row-count drift — but it still relies on column A holding the stable
serial number (1..16) for every row, since that's the one thing that has
stayed identical across every real file inspected so far.
"""
import datetime as dt
import re
import zipfile
from io import BytesIO

import openpyxl
from openpyxl.styles import Alignment, Font

HEADER_MARKER = "الرقم التسلسلي"
VALUE_COLUMN_MARKER = "القيمة"
LABEL_COLUMN_MARKER = "اسم البيان"

# serial number -> our engine's field name. Serial 1 (institution name),
# 4 (period-close status), 5 (currency), 13 (refunds), 15/16 (sign-off)
# are read from the file for context but aren't inputs to the engine.
SERIAL_FIELD_MAP = {
    2: "period_start",
    3: "period_end",
    6: "opening_cash_balance",
    7: "total_collections",
    8: "total_operating_expenses_paid",
    9: "suppliers_due",
    10: "payroll_due",
    11: "vat_due",
    12: "other_short_term_due",
    14: "operational_reserve_target",
}
REQUIRED_SERIALS = set(SERIAL_FIELD_MAP)
MONEY_FIELDS = {
    "opening_cash_balance", "total_collections", "total_operating_expenses_paid",
    "suppliers_due", "payroll_due", "vat_due", "other_short_term_due", "operational_reserve_target",
}

TEMPLATE_ROWS = [
    (1, "بيانات التشغيل", "الاسم القانوني للمؤسسة", "نص"),
    (2, "بيانات التشغيل", "بداية الفترة المحاسبية", "تاريخ"),
    (3, "بيانات التشغيل", "نهاية الفترة المحاسبية", "تاريخ"),
    (4, "بيانات التشغيل", "حالة إغلاق الفترة", "اختيار من قائمة"),
    (5, "بيانات التشغيل", "العملة المستخدمة", "اختيار من قائمة"),
    (6, "المجاميع المالية الأساسية", "الرصيد النقدي الافتتاحي المتاح", "مبلغ مالي"),
    (7, "المجاميع المالية الأساسية", "إجمالي التحصيلات النقدية الفعلية خلال الفترة", "مبلغ مالي"),
    (8, "المجاميع المالية الأساسية", "إجمالي المصروفات التشغيلية المدفوعة خلال الفترة", "مبلغ مالي"),
    (9, "المجاميع المالية الأساسية", "إجمالي التزامات الموردين غير المسددة", "مبلغ مالي"),
    (10, "المجاميع المالية الأساسية", "إجمالي الرواتب والالتزامات البشرية غير المسددة", "مبلغ مالي"),
    (11, "المجاميع المالية الأساسية", "صافي ضريبة القيمة المضافة المستحقة", "مبلغ مالي"),
    (12, "المجاميع المالية الأساسية", "إجمالي الالتزامات القصيرة الأجل الأخرى غير المسددة", "مبلغ مالي"),
    (13, "المجاميع المالية الأساسية", "إجمالي المبالغ المستردة أو المرتجعات النقدية المدفوعة", "مبلغ مالي"),
    (14, "المجاميع المالية الأساسية", "قيمة الاحتياطي التشغيلي المعتمدة", "مبلغ مالي"),
]
HEADER_ROW_LABELS = ["الرقم التسلسلي", "مجموعة البيانات", "اسم البيان المطلوب", "وصف البيان", "القيمة", "نوع القيمة"]


class TemplateParseError(ValueError):
    pass


def build_template_workbook(entity_name: str = "") -> BytesIO:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "قائمة البيانات المطلوبة"
    ws.sheet_view.rightToLeft = True

    ws.merge_cells("A1:F1")
    ws["A1"] = f"قالب تجميع الحد الأدنى من البيانات اللازمة للحساب — {entity_name}".strip(" —")
    ws["A1"].font = Font(bold=True, size=13)

    for col, label in enumerate(HEADER_ROW_LABELS, start=1):
        cell = ws.cell(row=3, column=col, value=label)
        cell.font = Font(bold=True)

    for row_offset, (serial, group, name, value_type) in enumerate(TEMPLATE_ROWS):
        row = 4 + row_offset
        ws.cell(row=row, column=1, value=serial)
        ws.cell(row=row, column=2, value=group)
        ws.cell(row=row, column=3, value=name)
        ws.cell(row=row, column=5, value=0 if value_type == "مبلغ مالي" else "")
        ws.cell(row=row, column=6, value=value_type)

    ws.column_dimensions["A"].width = 8
    ws.column_dimensions["B"].width = 24
    ws.column_dimensions["C"].width = 40
    ws.column_dimensions["E"].width = 16
    for row in ws.iter_rows():
        for cell in row:
            cell.alignment = Alignment(horizontal="right")

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def _find_header_row(ws):
    for row in ws.iter_rows(min_row=1, max_row=min(ws.max_row, 60)):
        for cell in row:
            if isinstance(cell.value, str) and HEADER_MARKER in cell.value:
                return cell.row, row
    raise TemplateParseError(
        f'تعذّر إيجاد صف العناوين — لازم يكون فيه خلية نصها "{HEADER_MARKER}" في أحد الأعمدة.'
    )


def _find_column(header_row, marker: str, required: bool = True):
    for cell in header_row:
        if isinstance(cell.value, str) and marker in cell.value:
            return cell.column
    if required:
        raise TemplateParseError(f'تعذّر إيجاد عمود يحتوي "{marker}" في صف العناوين.')
    return None


_DATE_PATTERNS = ["%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"]


def _normalize_date(raw) -> str:
    if isinstance(raw, (dt.datetime, dt.date)):
        return raw.strftime("%Y-%m-%d")
    text = str(raw).strip()
    for pattern in _DATE_PATTERNS:
        try:
            return dt.datetime.strptime(text, pattern).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise TemplateParseError(f"تعذّر فهم التاريخ: {raw!r} — الصيغ المدعومة: DD/MM/YYYY أو YYYY-MM-DD.")


def _normalize_money(raw, serial: int, label: str):
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return 0.0
    if isinstance(raw, str):
        cleaned = re.sub(r"[^\d.\-]", "", raw)
        raw = cleaned or "0"
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise TemplateParseError(f'القيمة في البند رقم {serial} ("{label}") ليست رقمًا صالحًا: {raw!r}')
    if value < 0:
        raise TemplateParseError(f'القيمة في البند رقم {serial} ("{label}") لا يمكن أن تكون سالبة: {value}')
    return value


MAX_UNCOMPRESSED_BYTES = 20 * 1024 * 1024  # zip-bomb guard: a small upload can inflate enormously


def parse_intake_workbook(file_bytes: bytes, expected_entity_id: str | None = None) -> dict:
    try:
        with zipfile.ZipFile(BytesIO(file_bytes)) as archive:
            if sum(info.file_size for info in archive.infolist()) > MAX_UNCOMPRESSED_BYTES:
                raise TemplateParseError("الملف يحتوي بيانات مضغوطة أكبر من الحد المسموح.")
    except zipfile.BadZipFile as e:
        raise TemplateParseError("الملف ليس Excel (xlsx) صالحًا.") from e

    try:
        wb = openpyxl.load_workbook(BytesIO(file_bytes), data_only=True)
    except Exception as e:  # noqa: BLE001 - surface as a clear user-facing error
        raise TemplateParseError(f"تعذّرت قراءة الملف كـExcel صالح: {e}") from e

    ws = wb.active
    header_row_idx, header_row = _find_header_row(ws)
    serial_col = _find_column(header_row, HEADER_MARKER)
    value_col = _find_column(header_row, VALUE_COLUMN_MARKER)
    label_col = _find_column(header_row, LABEL_COLUMN_MARKER, required=False)

    by_serial: dict[int, dict] = {}
    for row in ws.iter_rows(min_row=header_row_idx + 1, max_row=header_row_idx + 40):
        serial_raw = row[serial_col - 1].value
        if not isinstance(serial_raw, (int, float)):
            continue
        serial = int(serial_raw)
        value = row[value_col - 1].value
        label = row[label_col - 1].value if label_col else str(serial)
        by_serial[serial] = {"value": value, "label": label}

    missing = REQUIRED_SERIALS - by_serial.keys()
    if missing:
        raise TemplateParseError(
            f"الملف لا يطابق بنية القالب — البنود التالية (بالرقم التسلسلي) ناقصة: {sorted(missing)}. "
            "حمّل القالب الرسمي وعبّئ نفس البنود."
        )

    period_start = _normalize_date(by_serial[2]["value"])
    period_end = _normalize_date(by_serial[3]["value"])

    values = {}
    for serial, field in SERIAL_FIELD_MAP.items():
        if field in ("period_start", "period_end"):
            continue
        entry = by_serial[serial]
        values[field] = _normalize_money(entry["value"], serial, entry["label"])
    values["royalty_due"] = 0.0  # not present in this template family; no upstream royalty for this entity type

    return {"period_start": period_start, "period_end": period_end, "inputs": values}
