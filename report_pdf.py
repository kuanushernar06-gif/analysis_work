"""Тексеру аяқталған материал үшін compile_report() қайтарған құрылымды
есепті (JSON объект) PDF файлға түрлендіреді — қазақ тілінің толық
Cyrillic жиынын қолдайтын JUZ40 брендтік қаріпін қолданып."""

import io
import os

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, ListFlowable, ListItem,
)

_FONTS_DIR = os.path.join(os.path.dirname(__file__), "static", "fonts")
_FONTS_REGISTERED = False


def _ensure_fonts():
    global _FONTS_REGISTERED
    if _FONTS_REGISTERED:
        return
    pdfmetrics.registerFont(TTFont("JUZ40", os.path.join(_FONTS_DIR, "juz40-text-regular.ttf")))
    pdfmetrics.registerFont(TTFont("JUZ40-Bold", os.path.join(_FONTS_DIR, "juz40-text-bold.ttf")))
    _FONTS_REGISTERED = True


def _styles():
    _ensure_fonts()
    return {
        "title": ParagraphStyle("title", fontName="JUZ40-Bold", fontSize=15, leading=19, spaceAfter=4),
        "subtitle": ParagraphStyle("subtitle", fontName="JUZ40-Bold", fontSize=12, leading=16, spaceAfter=14,
                                    textColor=colors.HexColor("#59524a")),
        "h2": ParagraphStyle("h2", fontName="JUZ40-Bold", fontSize=12.5, leading=16,
                              spaceBefore=16, spaceAfter=8, textColor=colors.HexColor("#ae2a00")),
        "h3": ParagraphStyle("h3", fontName="JUZ40-Bold", fontSize=11, leading=14.5,
                              spaceBefore=12, spaceAfter=6),
        "body": ParagraphStyle("body", fontName="JUZ40", fontSize=9.5, leading=13.5, spaceAfter=6),
        "label": ParagraphStyle("label", fontName="JUZ40-Bold", fontSize=9.5, leading=13.5, spaceAfter=2),
        "cell": ParagraphStyle("cell", fontName="JUZ40", fontSize=9, leading=12),
        "cell_head": ParagraphStyle("cell_head", fontName="JUZ40-Bold", fontSize=9, leading=12,
                                     textColor=colors.white),
        "bullet": ParagraphStyle("bullet", fontName="JUZ40", fontSize=9.5, leading=13.5),
    }


def _p(text, style):
    return Paragraph((text or "").replace("\n", "<br/>"), style)


def _bullet_list(items, style):
    if not items:
        return _p("—", style)
    return ListFlowable(
        [ListItem(_p(it, style), leftIndent=6) for it in items],
        bulletType="bullet", start="•", leftIndent=14, spaceBefore=2, spaceAfter=6,
    )


def render_report_pdf(report, meta):
    """report — compile_report() қайтарған dict. meta — {"title", "subtitle"}
    (беттің үстіндегі тақырып пен қосалқы тақырып мәтіні)."""
    styles = _styles()
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=18 * mm,
        title=meta.get("title", "Тексеру есебі"),
    )
    flow = []

    flow.append(_p(meta.get("title", ""), styles["title"]))
    if meta.get("subtitle"):
        flow.append(_p(meta["subtitle"], styles["subtitle"]))

    flow.append(_p("1. Тексеру туралы жалпы ақпарат", styles["h2"]))
    if report.get("checked_document_summary"):
        flow.append(_p(f"<b>Тексерілген құжат:</b> {report['checked_document_summary']}", styles["body"]))
    sources = report.get("sources") or []
    if sources:
        flow.append(_p("<b>Салыстырылған дереккөздер:</b>", styles["body"]))
        flow.append(_bullet_list(
            [f"{s.get('title','')} — {s.get('detail','')}" for s in sources], styles["bullet"]
        ))
    if report.get("methodology"):
        flow.append(_p(f"<b>Әдіснама:</b> {report['methodology']}", styles["body"]))

    flow.append(_p("2. Жалпы қорытынды", styles["h2"]))
    if report.get("overall_conclusion"):
        flow.append(_p(report["overall_conclusion"], styles["body"]))
    cats = report.get("category_summary") or []
    if cats:
        rows = [[
            _p("№", styles["cell_head"]), _p("Санат", styles["cell_head"]),
            _p("Табылған саны", styles["cell_head"]), _p("Ауырлық дәрежесі", styles["cell_head"]),
        ]]
        for i, c in enumerate(cats, 1):
            rows.append([
                _p(str(i), styles["cell"]), _p(c.get("category", ""), styles["cell"]),
                _p(str(c.get("count", 0)), styles["cell"]), _p(c.get("severity", "—"), styles["cell"]),
            ])
        table = Table(rows, colWidths=[10 * mm, 90 * mm, 30 * mm, 35 * mm], repeatRows=1)
        table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#ae2a00")),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#e8e2d8")),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f1efea")]),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ]))
        flow.append(table)
        flow.append(Spacer(1, 4))

    groups = report.get("issue_groups") or []
    if groups:
        flow.append(_p("3. Анықталған қателердің толық тізімі", styles["h2"]))
        for gi, g in enumerate(groups, 1):
            flow.append(_p(f"3.{gi} {g.get('group_title','')}", styles["h3"]))
            for item in g.get("items") or []:
                flow.append(_p(item.get("label", ""), styles["label"]))
                if item.get("text_ref"):
                    flow.append(_p(f"<b>Мәтін/сілтеме:</b> {item['text_ref']}", styles["body"]))
                if item.get("problem"):
                    flow.append(_p(f"<b>Мәселе:</b> {item['problem']}", styles["body"]))
                if item.get("suggestion"):
                    flow.append(_p(f"<b>Ұсыныс:</b> {item['suggestion']}", styles["body"]))
                flow.append(Spacer(1, 4))

    positives = report.get("positive_notes") or []
    flow.append(_p("4. Оң нәтижелер", styles["h2"]))
    flow.append(_bullet_list(positives, styles["bullet"]))

    recs = report.get("final_recommendations") or []
    flow.append(_p("5. Қорытынды ұсыныстар", styles["h2"]))
    flow.append(_bullet_list(recs, styles["bullet"]))

    doc.build(flow)
    return buf.getvalue()
