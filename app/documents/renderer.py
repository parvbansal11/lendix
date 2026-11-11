"""Render fixed templates and data tables without document text generation."""

from __future__ import annotations

import csv
import json
import re
import secrets
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from .templates import ALIASES, TEMPLATES, WATERMARK

MIME_TYPES = {
    "pdf": "application/pdf", "csv": "text/csv", "json": "application/json",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "png": "image/png", "md": "text/markdown", "chat": "text/markdown",
}
QUEUE_COLUMNS = ("loan_id", "name", "requested_amount", "risk_band", "recommendation", "status")
SCHEDULE_COLUMNS = ("installment", "due_date", "emi", "principal", "interest", "balance")


def _lookup(source: dict, key: str) -> Any:
    value: Any = source
    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def _field(loan: dict, data: dict, key: str) -> Any:
    for source, name in ((data, key), (data, key.split(".")[-1]), (loan, key)):
        value = _lookup(source, name)
        if value is not None:
            return value
    aliases = {
        "approved_amount": ("loan_amount",),
        "grievance_id": ("reference",), "receipt_id": ("payment_reference", "reference"),
        "payment_amount": ("emi", "amount"), "payment_date": ("paid_at", "date"),
        "consent_purposes": ("consented_to",),
        "consent_by": ("consented_by",), "consent_at": ("consented_at",),
        "approved_by": ("approved_by_role", "officer"), "payout_account_name": ("payout_name", "account_holder", "payout_account_holder"),
        "payout_amount": ("approved_amount",), "payout_at": ("disbursed_at",),
        "outstanding_balance": ("outstanding",), "restructure_status": ("status",),
        "utr": ("simulated_utr",), "decision": ("status",),
        "account_verified": ("payout_account_verified",),
    }
    for alternative in aliases.get(key, ()):
        for source in (data, loan):
            value = _lookup(source, alternative)
            if value is not None:
                return value
    if key == "risk.source_status":
        risk = data.get("risk", loan.get("risk", {}))
        if "missing_sources" in risk:
            return "Missing: " + ", ".join(risk["missing_sources"]) if risk["missing_sources"] else "All required sources available"
    if key in {"credit.total_repayment", "credit.total_charges"}:
        principal = _field(loan, data, "approved_amount")
        if key == "credit.total_repayment":
            interest = _field(loan, data, "credit.total_interest")
            if isinstance(principal, (int, float)) and isinstance(interest, (int, float)):
                return round(principal + interest, 2)
        else:
            total = _field(loan, data, "credit.total_cost")
            if isinstance(principal, (int, float)) and isinstance(total, (int, float)):
                return round(total - principal, 2)
    if key == "kyc_status" and loan.get("kyc_dob") and "identity" in loan.get("documents", []):
        return "Synthetic identity record supplied; comparisons are recorded in the risk report."
    if key == "missing_documents" and "documents" in loan:
        return [name for name in ("identity", "bank_statement") if name not in loan["documents"]]
    if key == "account_last_four":
        account = data.get("payout_account") or loan.get("payout_account") or loan.get("bank_account")
        if isinstance(account, dict):
            account = account.get("number") or account.get("account_number")
        return str(account)[-4:] if account else None
    defaults = {"foir_cap": 0.5, "resolution_window": "30 days", "grievance_contact": "grievance@lendix.example"}
    return defaults.get(key)


def _text(value: Any) -> str:
    if value is None:
        return "Not recorded"
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, dict):
        return "; ".join(f"{str(k).replace('_', ' ')}: {_text(v)}" for k, v in value.items()) or "None"
    if isinstance(value, (list, tuple)):
        return "; ".join(_text(v) for v in value) or "None"
    return str(value)


def _display(key: str, value: Any) -> str:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if key.endswith("foir") or key == "foir_cap":
            return f"{value * 100:.2f}%" if abs(value) <= 1 else f"{value:.2f}%"
        if key.endswith(("annual_rate", "apr")):
            return f"{value * 100:.2f}%" if abs(value) <= 1 else f"{value:.2f}%"
        if any(word in key for word in ("amount", "income", "emi", "fees", "repayment", "cost", "charges", "balance")):
            return f"INR {value:,.2f}"
    return _text(value)


def _spreadsheet_safe(value: Any) -> Any:
    # Keep formulas in uploaded or user-provided text inert when opened in a workbook.
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    if isinstance(value, (dict, list)):
        return _spreadsheet_safe(_text(value))
    return value


def _risk_findings(loan: dict, data: dict) -> tuple[dict, list]:
    risk = data.get("risk", loan.get("risk")) or {}
    flags = data.get("flags", risk.get("flags", risk.get("reason_codes", risk.get("codes", []))))
    explanations = risk.get("explanations", [])
    if explanations and all(isinstance(flag, str) for flag in flags):
        flags = [{"code": code, "explanation": explanations[i] if i < len(explanations) else ""} for i, code in enumerate(flags)]
    return risk, flags


class DocumentService:
    def __init__(self, output_dir: str | Path):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def render(self, kind: str, loan: dict, data: dict | None = None, format: str = "pdf") -> dict:
        """Create an artifact; the caller records its ID and manages download authorization."""
        data = dict(data or {})
        kind = re.sub(r"[\s-]+", "_", kind.strip().lower())
        kind = ALIASES.get(kind, kind)
        if kind not in TEMPLATES and kind != "risk_chart":
            raise ValueError("Unknown document kind")
        format = format.lower().lstrip(".")
        if format not in MIME_TYPES:
            raise ValueError("Unsupported document format")
        if kind in {"portfolio_dashboard", "risk_chart"} and format == "pdf":
            format = "png"
        allowed = {
            "csv": {"review_queue", "repayment_schedule"}, "xlsx": {"review_queue"},
            "png": {"risk_report", "risk_chart", "portfolio_dashboard"},
            "json": {"audit_log", "consent_record"},
        }
        if format in allowed and kind not in allowed[format]:
            raise ValueError(f"{kind} does not support {format}")
        if kind == "audit_log" and format != "json":
            raise ValueError("Audit logs must be exported as JSON")
        if kind == "risk_chart" and format != "png":
            raise ValueError("Risk charts must be exported as PNG")
        created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        loan_id = str(loan.get("id") or loan.get("loan_id") or data.get("loan_id") or "PORTFOLIO")
        document_id = f"D-{secrets.token_hex(5).upper()}"
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "_", loan_id)[:64] or "PORTFOLIO"
        suffix = "md" if format == "chat" else format
        filename = f"{safe_id}_{kind}_{document_id}.{suffix}"
        path = self.output_dir / filename
        metadata = {"document_id": document_id, "loan_id": loan_id, "kind": kind,
                    "created_at": created_at, "date": created_at[:10], "watermark": WATERMARK}
        if format == "pdf":
            self._pdf(path, kind, loan, data, metadata)
        elif format in {"csv", "xlsx"}:
            self._table_file(path, kind, loan, data, metadata, format)
        elif format == "json":
            payload = {"metadata": metadata}
            if kind == "audit_log":
                payload["events"] = data.get("events", loan.get("events", []))
            else:
                payload["consent"] = {key: _field(loan, data, key) for _, key in TEMPLATES[kind].fields}
            path.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        elif format == "png":
            self._chart(path, kind, loan, data, metadata)
        else:
            lines = [f"# {TEMPLATES[kind].title}", "", TEMPLATES[kind].introduction, ""]
            lines.extend(f"- {label}: {_display(key, _field(loan, data, key))}" for label, key in TEMPLATES[kind].fields)
            for label, value in self._narrative(loan, data):
                lines.extend(["", f"{label}: {value}"])
            lines.extend(["", WATERMARK, f"Loan: {loan_id} | Date: {metadata['date']} | Document: {document_id}"])
            path.write_text("\n".join(lines), encoding="utf-8")
        return {"path": str(path.resolve()), "filename": filename, "document_id": document_id,
                "loan_id": loan_id, "kind": kind, "mime_type": MIME_TYPES[format], "created_at": created_at}

    @staticmethod
    def _narrative(loan: dict, data: dict) -> list[tuple[str, str]]:
        result = []
        summary = data.get("summary")
        reasons = data.get("reasons")
        if summary:
            result.append(("Summary", _text(summary)))
        if reasons:
            for reason in reasons if isinstance(reasons, list) else [reasons]:
                result.append(("Reason", _text(reason)))
        return result

    @staticmethod
    def _rows(kind: str, loan: dict, data: dict) -> tuple[tuple[str, ...], list[list[Any]]]:
        if kind == "review_queue":
            source = data.get("rows", data.get("loans", []))
            rows = []
            for entry in source:
                risk = entry.get("risk") or {}
                credit = entry.get("credit") or {}
                rows.append([entry.get("loan_id", entry.get("id", "")), entry.get("name", ""),
                             entry.get("requested_amount"), entry.get("risk_band", risk.get("band", "unverified")),
                             entry.get("recommendation", credit.get("recommendation", "Not reviewed")),
                             entry.get("status", "Not recorded")])
            return QUEUE_COLUMNS, rows
        source = data.get("schedule", data.get("rows", loan.get("schedule", [])))
        rows = [[r.get("installment", r.get("month", r.get("number"))), r.get("due_date", r.get("date")),
                 r.get("emi", r.get("payment", r.get("amount"))), r.get("principal"), r.get("interest"),
                 r.get("balance", r.get("closing_balance", r.get("remaining_balance")))] for r in source]
        return SCHEDULE_COLUMNS, rows

    def _pdf(self, path: Path, kind: str, loan: dict, data: dict, metadata: dict):
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle

        template = TEMPLATES[kind]
        styles = getSampleStyleSheet()
        styles.add(ParagraphStyle(name="ReportTitle", fontName="Helvetica-Bold", fontSize=23, leading=28, textColor=colors.HexColor("#173b46"), spaceAfter=14))
        styles.add(ParagraphStyle(name="Body", fontName="Helvetica", fontSize=10, leading=15, spaceAfter=8, textColor=colors.HexColor("#263c45")))
        styles.add(ParagraphStyle(name="Cell", parent=styles["Body"], fontSize=9, leading=13, spaceAfter=0))
        styles.add(ParagraphStyle(name="Label", parent=styles["Cell"], fontName="Helvetica-Bold"))
        styles.add(ParagraphStyle(name="Small", parent=styles["Body"], fontSize=8, leading=11))
        para = lambda value, style="Body": Paragraph(escape(_text(value)).replace("\n", "<br/>"), styles[style])
        story = [para("LENDIX FINANCE", "Label"), Spacer(1, 5 * mm), para(template.title, "ReportTitle"),
                 para(f"Loan {metadata['loan_id']}  /  {metadata['date']}", "Small"),
                 para(f"Applicant: {loan.get('name', 'Portfolio / office record')}", "Label"),
                 Spacer(1, 3 * mm), para(template.introduction)]
        field_rows = [[para(label, "Label"), para(_display(key, _field(loan, data, key)), "Cell")]
                      for label, key in template.fields]
        if field_rows:
            table = Table(field_rows, colWidths=[66 * mm, 106 * mm], hAlign="LEFT")
            table.setStyle(TableStyle([("VALIGN", (0, 0), (-1, -1), "TOP"),
                                      ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eef4f4")),
                                      ("LINEBELOW", (0, 0), (-1, -1), .4, colors.HexColor("#dbe6e7")),
                                      ("LEFTPADDING", (0, 0), (-1, -1), 10), ("RIGHTPADDING", (0, 0), (-1, -1), 10),
                                      ("TOPPADDING", (0, 0), (-1, -1), 8), ("BOTTOMPADDING", (0, 0), (-1, -1), 8)]))
            story.extend([Spacer(1, 3 * mm), table, Spacer(1, 5 * mm)])
        if kind == "risk_report":
            _, flags = _risk_findings(loan, data)
            story.append(para("Rule findings", "Heading2"))
            if not flags:
                story.append(para("No flags recorded. Verify the source status before relying on this report."))
            for flag in flags:
                if isinstance(flag, dict):
                    code = flag.get("code", flag.get("reason_code", "CHECK"))
                    explanation = flag.get("explanation", flag.get("reason", flag.get("message", "")))
                    story.extend([para(code, "Label"), para(explanation)])
                else:
                    story.append(para(str(flag), "Label"))
        if kind in {"repayment_schedule", "review_queue"}:
            columns, rows = self._rows(kind, loan, data)
            if rows:
                headers = ["No.", "Due date", "EMI", "Principal", "Interest", "Balance"] if kind == "repayment_schedule" else [c.replace("_", " ").title() for c in columns]
                formatted = [[para(c, "Label") for c in headers]]
                for row in rows:
                    formatted.append([para(f"{value:,.2f}" if isinstance(value, (int, float)) and i >= 2 else _text(value), "Cell") for i, value in enumerate(row)])
                widths = [18, 30, 31, 31, 31, 31] if kind == "repayment_schedule" else [24, 40, 28, 22, 32, 26]
                table = Table(formatted, colWidths=[w * mm for w in widths], repeatRows=1, hAlign="LEFT")
                table.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e0eded")),
                                          ("VALIGN", (0, 0), (-1, -1), "TOP"),
                                          ("LINEBELOW", (0, 0), (-1, -1), .4, colors.HexColor("#dbe6e7")),
                                          ("TOPPADDING", (0, 0), (-1, -1), 7), ("BOTTOMPADDING", (0, 0), (-1, -1), 7)]))
                story.extend([table, Spacer(1, 4 * mm)])
            else:
                story.append(para("No rows recorded."))
        for label, value in self._narrative(loan, data):
            story.extend([para(label, "Label"), para(value)])
        if kind == "key_facts_statement":
            story.append(para("Cooling-off exit: the borrower may exit during the configured cooling-off period without penalty. Acceptance of this KFS must be recorded before a simulated payout."))
        if kind == "rejection_letter" and not data.get("reasons"):
            story.append(para("Main reasons have not been recorded. The officer must supply the reasons before sharing this specimen with a borrower."))
        def footer(canvas, doc):
            canvas.saveState()
            canvas.setStrokeColor(colors.HexColor("#cbdcde"))
            canvas.line(19 * mm, 23 * mm, 191 * mm, 23 * mm)
            canvas.setFillColor(colors.HexColor("#526870"))
            canvas.setFont("Helvetica-Bold", 7)
            canvas.drawCentredString(A4[0] / 2, 18 * mm, WATERMARK)
            canvas.setFont("Helvetica", 7)
            canvas.drawString(19 * mm, 13 * mm, f"{metadata['loan_id']}  |  {metadata['date']}  |  {metadata['document_id']}")
            canvas.drawRightString(191 * mm, 13 * mm, f"Page {doc.page}")
            canvas.restoreState()
        doc = SimpleDocTemplate(str(path), pagesize=A4, rightMargin=19 * mm, leftMargin=19 * mm,
                                topMargin=17 * mm, bottomMargin=30 * mm, title=template.title, author="Lendix Finance")
        doc.build(story, onFirstPage=footer, onLaterPages=footer)

    def _table_file(self, path: Path, kind: str, loan: dict, data: dict, metadata: dict, format: str):
        columns, rows = self._rows(kind, loan, data)
        safe_rows = [[_spreadsheet_safe(v) for v in row] for row in rows]
        if format == "csv":
            with path.open("w", newline="", encoding="utf-8-sig") as stream:
                writer = csv.writer(stream)
                writer.writerow(columns)
                writer.writerows(safe_rows)
                writer.writerow([])
                for key in ("watermark", "loan_id", "date", "document_id"):
                    writer.writerow([f"# {key}", _spreadsheet_safe(metadata[key])])
            return
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Review queue"
        sheet.append(["Lendix Finance - Daily review queue"])
        sheet.merge_cells("A1:F1")
        sheet["A1"].font = Font(name="Calibri", size=18, bold=True, color="173B46")
        sheet.row_dimensions[1].height = 32
        sheet.append([f"As of {metadata['date']} - amounts in INR"])
        sheet.merge_cells("A2:F2")
        sheet.append([c.replace("_", " ").title() for c in columns])
        for cell in sheet[3]:
            cell.fill = PatternFill("solid", fgColor="173B46")
            cell.font = Font(bold=True, color="FFFFFF")
            cell.alignment = Alignment(wrap_text=True, vertical="center")
        sheet.row_dimensions[3].height = 30
        for row in safe_rows:
            sheet.append(row)
        for cells in sheet.iter_rows(min_row=4, max_row=3 + len(safe_rows), min_col=1, max_col=6):
            for cell in cells:
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                cell.font = Font(name="Calibri", size=11, color="263C45")
            cells[2].number_format = '"INR "#,##0.00'
        sheet.freeze_panes = "A4"
        sheet.auto_filter.ref = f"A3:F{max(3, 3 + len(rows))}"
        for index, width in enumerate((18, 30, 24, 18, 30, 25), start=1):
            sheet.column_dimensions[get_column_letter(index)].width = width
        sheet.append([])
        for key in ("watermark", "loan_id", "date", "document_id"):
            sheet.append([key.replace("_", " ").title(), _spreadsheet_safe(metadata[key])])
            row_index = sheet.max_row
            sheet.merge_cells(start_row=row_index, start_column=2, end_row=row_index, end_column=6)
            sheet.cell(row_index, 2).alignment = Alignment(wrap_text=True)
            sheet.cell(row_index, 1).font = Font(bold=True, color="526870")
        sheet.oddFooter.center.text = WATERMARK
        sheet.oddFooter.left.text = f"{metadata['loan_id']} {metadata['date']} {metadata['document_id']}"
        sheet.print_options.horizontalCentered = True
        sheet.sheet_properties.pageSetUpPr.fitToPage = True
        sheet.page_setup.orientation = "landscape"
        sheet.page_setup.paperSize = sheet.PAPERSIZE_A4
        sheet.page_setup.fitToWidth = 1
        sheet.page_setup.fitToHeight = 0
        sheet.print_title_rows = "1:3"
        workbook.save(path)

    @staticmethod
    def _chart(path: Path, kind: str, loan: dict, data: dict, metadata: dict):
        import matplotlib
        matplotlib.use("Agg")
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        from matplotlib.ticker import MaxNLocator
        figure = Figure(figsize=(12, 6.8), dpi=150, facecolor="#f8fbfb")
        FigureCanvasAgg(figure)
        figure.text(.06, .94, "LENDIX FINANCE", fontsize=11, weight="bold", color="#4c6b70")
        if kind == "portfolio_dashboard":
            loans = data.get("loans", data.get("rows", []))
            bands = Counter((item.get("risk") or {}).get("band", item.get("risk_band", "unverified")) for item in loans)
            axes = figure.subplots(1, 3)
            figure.suptitle("Portfolio dashboard", x=.06, y=.90, ha="left", fontsize=22, weight="bold", color="#173b46")
            categories = ["low", "medium", "high", "unverified"]
            axes[0].bar(categories, [bands[k] for k in categories], color=["#2b8b7b", "#d69a40", "#b85762", "#93a9ae"])
            axes[0].set_title("Applications by risk band", loc="left", fontsize=11, pad=15)
            decisions = {"Approved": 0, "Declined": 0, "Other": 0}
            for item in loans:
                status = str(item.get("status", "pending")).lower()
                if status == "declined":
                    decisions["Declined"] += 1
                elif item.get("human_approved") or status in {"approved", "disbursed", "closed", "active", "restructure_requested"}:
                    decisions["Approved"] += 1
                else:
                    decisions["Other"] += 1
            axes[1].bar(decisions.keys(), decisions.values(), color=["#2b8b7b", "#b85762", "#93a9ae"])
            axes[1].set_title("Officer decision and lifecycle", loc="left", fontsize=11, pad=15)
            def paid_amount(item):
                if "total_paid" in item or "amount_paid" in item:
                    return float(item.get("total_paid", item.get("amount_paid", 0)) or 0)
                if item.get("schedule") and item.get("paid_installments"):
                    return sum(float(row.get("emi", 0)) for row in item["schedule"][:item["paid_installments"]])
                return sum(float(row.get("emi", row.get("amount", 0))) for row in item.get("payments", []))
            collected = sum(paid_amount(item) for item in loans)
            outstanding = sum(float(item.get("outstanding_balance", item.get("outstanding", 0)) or 0) for item in loans)
            axes[2].bar(["Collected", "Outstanding"], [collected, outstanding], color=["#2b8b7b", "#497b9b"])
            axes[2].set_title("Recorded collections (INR)", loc="left", fontsize=11, pad=15)
            axes[2].ticklabel_format(axis="y", style="plain")
            axes[0].set_ylim(0, max(1, max(bands.values(), default=0)) * 1.15)
            axes[1].set_ylim(0, max(1, max(decisions.values())) * 1.15)
            axes[2].set_ylim(0, max(1, collected, outstanding) * 1.15)
            if collected == 0 and outstanding == 0:
                axes[2].text(.5, .5, "No collection amounts recorded", ha="center", va="center", transform=axes[2].transAxes, fontsize=9, wrap=True, color="#526870")
            figure.text(.06, .13, f"{len(loans)} supplied applications. Missing collection amounts are not included.", fontsize=9, color="#526870")
        else:
            ax = figure.subplots()
            axes = [ax]
            risk, flags = _risk_findings(loan, data)
            band = data.get("band", risk.get("band", "unverified"))
            codes = [str(flag.get("code", flag.get("reason_code", "CHECK"))) if isinstance(flag, dict) else str(flag) for flag in flags]
            counts = Counter(codes)
            figure.suptitle(f"Risk findings - {metadata['loan_id']}", x=.06, y=.90, ha="left", fontsize=22, weight="bold", color="#173b46")
            if counts:
                labels = [code.replace("_", " ").title() for code in counts]
                ax.barh(labels, counts.values(), color="#b85762" if band == "high" else "#d69a40")
                ax.set_xlabel("Recorded rule flags")
                ax.set_xlim(0, max(counts.values()) + .5)
                ax.xaxis.set_major_locator(MaxNLocator(integer=True))
                ax.invert_yaxis()
            else:
                ax.text(.5, .5, "No rule flags recorded", ha="center", va="center", transform=ax.transAxes, color="#526870", fontsize=16)
                ax.set_xticks([])
                ax.set_yticks([])
            figure.text(.06, .13, f"Risk band: {band}. Source gaps remain unverified. This is not a default prediction.", fontsize=9, color="#526870")
        for ax in axes:
            ax.set_facecolor("#f8fbfb")
            ax.spines[["top", "right"]].set_visible(False)
            ax.spines[["left", "bottom"]].set_color("#c6d7d9")
            ax.tick_params(labelsize=8, colors="#3f5c63")
            ax.yaxis.set_major_locator(MaxNLocator(integer=True))
            ax.set_axisbelow(True)
            ax.grid(axis="y", color="#e5eded", linewidth=.7)
        figure.subplots_adjust(left=.31 if kind != "portfolio_dashboard" else .07, right=.96, top=.76, bottom=.23, wspace=.35)
        figure.text(.5, .065, WATERMARK, ha="center", fontsize=8, color="#526870")
        figure.text(.5, .035, f"Loan: {metadata['loan_id']}  |  {metadata['date']}  |  {metadata['document_id']}", ha="center", fontsize=8, color="#526870")
        figure.savefig(path, metadata={"Description": WATERMARK, "Document ID": metadata["document_id"], "Loan ID": metadata["loan_id"], "Date": metadata["date"]})
        figure.clear()
